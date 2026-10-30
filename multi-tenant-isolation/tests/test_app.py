# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Route/wiring tests for the multi-tenant storefront demo.

These exercise the Flask layer with a fake TenantStore, so they run without any
AWS or Valkey. Engine-enforced isolation (the real NOPERM) is proven separately
by scripts/verify-isolation.sh against a live cluster; here we check that the
app surfaces allow/deny outcomes correctly and keeps each session scoped to its
chosen tenant.
"""
from __future__ import annotations

import pytest

from app.config import AppConfig
from app.main import create_app
from app.tenant_store import PeekResult


class FakeStore:
    """In-memory stand-in for TenantStore with per-tenant keyspaces."""

    def __init__(self) -> None:
        self.data: dict[int, list[str]] = {}

    def list_items(self, tenant: int) -> list[str]:
        return list(self.data.get(tenant, []))

    def add_item(self, tenant: int, item: str) -> None:
        self.data.setdefault(tenant, []).append(item)

    def clear_own(self, tenant: int) -> None:
        self.data[tenant] = []

    def try_flushall(self, tenant: int) -> PeekResult:
        # A single-db tenant is denied FLUSHALL by the engine.
        return PeekResult(target_db=-1, allowed=False,
                          message="NOPERM this user has no permissions to run 'flushall'")

    def peek(self, tenant: int, target_db: int) -> PeekResult:
        if target_db == tenant:
            return PeekResult(target_db, True, "Read succeeded")
        return PeekResult(target_db, False,
                          f"NOPERM No permissions to access database {target_db}")

    def ping(self, tenant: int = 1) -> bool:
        return True


@pytest.fixture
def client():
    config = AppConfig(
        # Placeholder signing key for the in-process test client; not a real secret.
        secret_key="test-key",  # nosec B106 - test fixture, not a credential
        redis_host="localhost",
        tenant_count=3,
        session_cookie_secure=False,
        testing=True,
    )
    # testing=True above already propagates to app.config via create_app.
    app = create_app(config=config, store=FakeStore())
    return app.test_client()


def _login(client, tenant: int):
    return client.post("/login", data={"tenant": str(tenant)}, follow_redirects=True)


def test_index_lists_tenants(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"tenant1" in resp.data and b"tenant3" in resp.data


def test_login_scopes_session_and_store_roundtrip(client):
    _login(client, 1)
    client.post("/store/add", data={"item": "order-1"}, follow_redirects=True)
    resp = client.get("/store")
    assert b"order-1" in resp.data


def test_items_are_isolated_per_tenant_session(client):
    _login(client, 1)
    client.post("/store/add", data={"item": "tenant1-only"}, follow_redirects=True)
    # Switch to tenant 2; it must not see tenant 1's item.
    client.get("/logout")
    _login(client, 2)
    resp = client.get("/store")
    assert b"tenant1-only" not in resp.data


def test_peek_denied_on_other_db(client):
    _login(client, 1)
    resp = client.post("/peek", data={"target_db": "2"}, follow_redirects=True)
    assert b"DENIED by the engine" in resp.data
    assert b"NOPERM" in resp.data


def test_peek_allowed_on_own_db(client):
    _login(client, 1)
    resp = client.post("/peek", data={"target_db": "1"}, follow_redirects=True)
    assert b"READ ALLOWED" in resp.data


def test_flushall_denied(client):
    _login(client, 1)
    resp = client.post("/peek/flushall", data={}, follow_redirects=True)
    assert b"FLUSHALL DENIED by the engine" in resp.data


def test_store_requires_login(client):
    resp = client.get("/store", follow_redirects=True)
    # Redirected back to the tenant picker.
    assert b"Pick a tenant storefront" in resp.data


def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json["status"] == "ok"
