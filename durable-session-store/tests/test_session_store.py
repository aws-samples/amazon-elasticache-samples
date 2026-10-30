# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the durable session store."""
from __future__ import annotations

import json
import secrets

import fakeredis
import pytest

from app.config import RedisConfig
from app.session_store import (
    SessionStore,
    build_redis_client,
    new_session_id,
)


def test_new_session_id_is_unique_and_strong() -> None:
    ids = {new_session_id() for _ in range(1000)}
    assert len(ids) == 1000  # no collisions
    # token_urlsafe(32) yields ~43 chars of URL-safe entropy.
    assert all(len(i) >= 43 for i in ids)


def test_create_and_load_roundtrip(store: SessionStore) -> None:
    session_id = store.create({"username": "ada", "role": "admin"})
    loaded = store.load(session_id)
    assert loaded == {"username": "ada", "role": "admin"}


def test_load_missing_returns_none(store: SessionStore) -> None:
    assert store.load("does-not-exist") is None


def test_load_empty_id_returns_none(store: SessionStore) -> None:
    assert store.load("") is None


def test_save_overwrites_existing(store: SessionStore) -> None:
    sid = store.create({"count": 1})
    store.save(sid, {"count": 2})
    assert store.load(sid) == {"count": 2}


def test_delete_removes_session(store: SessionStore) -> None:
    sid = store.create({"username": "grace"})
    store.delete(sid)
    assert store.load(sid) is None


def test_delete_empty_id_is_noop(store: SessionStore) -> None:
    # Should not raise.
    store.delete("")


def test_save_sets_ttl(
    store: SessionStore, fake_redis: fakeredis.FakeStrictRedis
) -> None:
    sid = store.create({"username": "linus"})
    ttl = fake_redis.ttl(f"session:{sid}")
    assert 0 < ttl <= 3600


def test_touch_extends_ttl(
    store: SessionStore, fake_redis: fakeredis.FakeStrictRedis
) -> None:
    sid = store.create({"username": "margaret"})
    key = f"session:{sid}"
    fake_redis.expire(key, 10)
    assert store.touch(sid) is True
    assert fake_redis.ttl(key) > 10


def test_touch_missing_returns_false(store: SessionStore) -> None:
    assert store.touch("nope") is False


def test_ping(store: SessionStore) -> None:
    assert store.ping() is True


def test_payload_is_compact_json(
    store: SessionStore, fake_redis: fakeredis.FakeStrictRedis
) -> None:
    sid = store.create({"a": 1, "b": 2})
    raw = fake_redis.get(f"session:{sid}")
    # Stored as compact JSON (no spaces).
    assert raw == json.dumps({"a": 1, "b": 2}, separators=(",", ":"))


def test_zero_ttl_rejected(fake_redis: fakeredis.FakeStrictRedis) -> None:
    with pytest.raises(ValueError):
        SessionStore(fake_redis, ttl_seconds=0)


def test_save_empty_session_id_rejected(store: SessionStore) -> None:
    with pytest.raises(ValueError):
        store.save("", {"x": 1})


# --- build_redis_client configuration tests ---------------------------------


def test_build_client_enables_tls_and_auth() -> None:
    token = secrets.token_urlsafe(16)  # generated, no literal secret in source
    config = RedisConfig(
        host="clustercfg.example.cache.amazonaws.com",
        port=6379,
        ssl=True,
        auth_token=token,
    )
    client = build_redis_client(config)
    opts = client.get_connection_kwargs()
    assert opts.get("password") == token
    # redis-py records SSL by using an SSLConnection class.
    assert client.connection_pool.connection_class.__name__ == "SSLConnection"


def test_build_client_without_tls_for_local_dev() -> None:
    config = RedisConfig(host="localhost", ssl=False, auth_token=None)
    client = build_redis_client(config)
    opts = client.get_connection_kwargs()
    assert "password" not in opts or opts.get("password") is None


def test_build_client_single_node_by_default() -> None:
    import redis

    client = build_redis_client(RedisConfig(host="localhost", ssl=False))
    assert isinstance(client, redis.Redis)
    assert not isinstance(client, redis.cluster.RedisCluster)


def test_build_client_cluster_mode_uses_cluster_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cluster mode must construct RedisCluster with TLS + AUTH kwargs.

    RedisCluster connects eagerly on construction, so we patch it with a stub to
    test the selection/kwargs without needing a live cluster.
    """
    import redis

    captured: dict[str, object] = {}

    class StubCluster:
        def __init__(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)

    monkeypatch.setattr(redis.cluster, "RedisCluster", StubCluster)

    token = secrets.token_urlsafe(16)  # generated, no literal secret in source
    config = RedisConfig(
        host="clustercfg.example.cache.amazonaws.com",
        ssl=True,
        auth_token=token,
        cluster=True,
    )
    client = build_redis_client(config, decode_responses=False)
    assert isinstance(client, StubCluster)
    assert captured["host"] == "clustercfg.example.cache.amazonaws.com"
    assert captured["ssl"] is True
    assert captured["ssl_cert_reqs"] == "required"
    assert captured["password"] == token
    assert captured["decode_responses"] is False
    # The single-node-only kwargs must NOT be passed to the cluster client.
    assert "retry_on_timeout" not in captured
    assert "health_check_interval" not in captured


def test_cluster_mode_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_HOST", "clustercfg.example.cache.amazonaws.com")
    monkeypatch.setenv("REDIS_CLUSTER_MODE", "true")
    config = RedisConfig.from_env()
    assert config.cluster is True
