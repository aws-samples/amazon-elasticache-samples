# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the opt-in failover demo (route gating + service error handling).

No real AWS calls are made: boto3 is mocked. These tests verify the safety
posture (off by default, POST-only) and that the service surfaces errors as
FailoverError rather than leaking exceptions.
"""
from __future__ import annotations

import secrets
import sys
import types

import fakeredis
import pytest
from flask.testing import FlaskClient

from app.config import AppConfig, ConfigError, RedisConfig
from app.failover import FailoverError, trigger_test_failover
from app.main import create_app
from app.session_store import SessionStore


def _client(*, enable_demo: bool, rg_id: str | None = "my-rg") -> FlaskClient:
    store = SessionStore(
        fakeredis.FakeStrictRedis(decode_responses=True), ttl_seconds=3600
    )
    config = AppConfig(
        secret_key=secrets.token_urlsafe(16),  # generated, no literal secret
        redis=RedisConfig(host="localhost", ssl=False),
        session_cookie_secure=False,
        testing=True,
        enable_failover_demo=enable_demo,
        replication_group_id=rg_id,
    )
    return create_app(config=config, store=store).test_client()


# --- Route gating -----------------------------------------------------------


def test_failover_route_is_post_only() -> None:
    client = _client(enable_demo=True)
    assert client.get("/demo/failover").status_code == 405


def test_failover_disabled_is_inert_and_forbidden() -> None:
    client = _client(enable_demo=False, rg_id=None)
    resp = client.post("/demo/failover")
    assert resp.status_code == 403
    assert b"turned off" in resp.data


def test_status_endpoint_forbidden_when_disabled() -> None:
    client = _client(enable_demo=False, rg_id=None)
    resp = client.get("/demo/failover/status")
    assert resp.status_code == 403
    assert resp.get_json() == {"enabled": False}


def test_status_endpoint_returns_live_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_mod
    from app.failover import FailoverStatus

    def fake_status(replication_group_id, node_group_id, region=None):  # noqa: ANN001
        return FailoverStatus(
            status="modifying",
            in_progress=True,
            members=(
                {"id": "rg-0001-001", "role": "replica", "az": "us-east-1a"},
                {"id": "rg-0001-002", "role": "primary", "az": "us-east-1b"},
            ),
        )

    monkeypatch.setattr(main_mod, "describe_failover_status", fake_status)
    client = _client(enable_demo=True)
    resp = client.get("/demo/failover/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "modifying"
    assert data["in_progress"] is True
    assert len(data["members"]) == 2
    assert "cart_count" in data


def test_cart_page_hides_button_when_disabled() -> None:
    client = _client(enable_demo=False, rg_id=None)
    client.post("/cart/add", data={"product_id": "sku-coffee"})
    resp = client.get("/cart")
    assert b"Fail over the primary" not in resp.data


def test_cart_page_shows_button_when_enabled() -> None:
    client = _client(enable_demo=True)
    client.post("/cart/add", data={"product_id": "sku-coffee"})
    resp = client.get("/cart")
    assert b"Fail over the primary" in resp.data


def test_failover_enabled_invokes_service(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main as main_mod
    from app.failover import FailoverResult

    calls: dict[str, str] = {}

    def fake_trigger(replication_group_id, node_group_id, region=None):  # noqa: ANN001
        calls["rg"] = replication_group_id
        calls["ng"] = node_group_id
        return FailoverResult(replication_group_id, node_group_id, "modifying")

    monkeypatch.setattr(main_mod, "trigger_test_failover", fake_trigger)
    client = _client(enable_demo=True)
    resp = client.post("/demo/failover")
    assert resp.status_code == 200
    assert b"Failover initiated" in resp.data
    assert calls["rg"] == "my-rg"


def test_failover_service_error_renders_friendly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_mod

    def boom(replication_group_id, node_group_id, region=None):  # noqa: ANN001
        raise FailoverError("access denied")

    monkeypatch.setattr(main_mod, "trigger_test_failover", boom)
    client = _client(enable_demo=True)
    resp = client.post("/demo/failover")
    assert resp.status_code == 502
    assert b"Could not start a new failover" in resp.data
    # A non-"already in progress" error shows the cart-unaffected reassurance.
    assert b"unaffected" in resp.data
    # The live progress panel renders even on error, so it can track a failover
    # that may already be running.
    assert b"Failover progress" in resp.data


# --- Config gating ----------------------------------------------------------


def test_config_requires_rg_id_when_demo_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRET_KEY", "x")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    monkeypatch.setenv("ENABLE_FAILOVER_DEMO", "true")
    monkeypatch.delenv("REDIS_REPLICATION_GROUP_ID", raising=False)
    with pytest.raises(ConfigError):
        AppConfig.from_env()


# --- Service layer ----------------------------------------------------------


def test_trigger_requires_rg_id() -> None:
    with pytest.raises(FailoverError):
        trigger_test_failover("", "0001")


def test_trigger_calls_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a fake boto3 module and assert TestFailover is called correctly."""
    recorded: dict[str, object] = {}

    class FakeClient:
        def test_failover(self, **kwargs):  # noqa: ANN003
            recorded.update(kwargs)
            return {"ReplicationGroup": {"Status": "modifying"}}

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda service, region_name=None: FakeClient()  # type: ignore[attr-defined]
    fake_botocore = types.ModuleType("botocore")
    fake_exceptions = types.ModuleType("botocore.exceptions")
    fake_exceptions.ClientError = Exception  # type: ignore[attr-defined]
    fake_exceptions.BotoCoreError = Exception  # type: ignore[attr-defined]
    fake_botocore.exceptions = fake_exceptions  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", fake_exceptions)

    result = trigger_test_failover("rg-1", "0001", region="us-east-1")
    assert result.status == "modifying"
    assert recorded == {"ReplicationGroupId": "rg-1", "NodeGroupId": "0001"}


def test_describe_failover_status_parses_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.failover import describe_failover_status

    class FakeClient:
        def describe_replication_groups(self, **kwargs):  # noqa: ANN003
            return {
                "ReplicationGroups": [
                    {
                        "Status": "modifying",
                        "NodeGroups": [
                            {
                                "NodeGroupId": "0001",
                                "NodeGroupMembers": [
                                    {
                                        "CacheClusterId": "rg-0001-001",
                                        "CurrentRole": "primary",
                                        "PreferredAvailabilityZone": "us-east-1a",
                                    },
                                    {
                                        "CacheClusterId": "rg-0001-002",
                                        "CurrentRole": "replica",
                                        "PreferredAvailabilityZone": "us-east-1b",
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda service, region_name=None: FakeClient()  # type: ignore[attr-defined]
    fake_botocore = types.ModuleType("botocore")
    fake_exceptions = types.ModuleType("botocore.exceptions")
    fake_exceptions.ClientError = Exception  # type: ignore[attr-defined]
    fake_exceptions.BotoCoreError = Exception  # type: ignore[attr-defined]
    fake_botocore.exceptions = fake_exceptions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", fake_exceptions)

    st = describe_failover_status("rg-1", "0001", region="us-east-1")
    assert st.status == "modifying"
    assert st.in_progress is True
    assert len(st.members) == 2
    assert st.members[0]["role"] == "primary"
