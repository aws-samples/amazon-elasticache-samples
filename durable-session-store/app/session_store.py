# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Durable, server-side session store backed by Amazon ElastiCache (Valkey/Redis OSS).

Durability model
----------------
Session state is held in an ElastiCache replication group configured for:
  * Multi-AZ with automatic failover (a replica is promoted if the primary fails)
  * Daily automatic snapshots retained in S3 (point-in-time recovery)
  * At-rest encryption (KMS) and in-transit encryption (TLS)

This module is the client-side half: it stores each session as a single Redis
hash/string value with a TTL, so sessions survive application restarts and node
failover while expiring automatically.
"""
from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from typing import Any

import redis
import redis.cluster

from .config import RedisConfig

# Session IDs are 256 bits of CSPRNG entropy, URL-safe encoded.
_SESSION_ID_BYTES = 32
_KEY_PREFIX = "session:"


def build_redis_client(
    config: RedisConfig, *, decode_responses: bool = True
) -> redis.Redis | redis.cluster.RedisCluster:
    """Create a redis-py client honoring TLS, AUTH, and cluster-mode settings.

    Using ``ssl=True`` enables in-transit encryption, which is mandatory for
    ElastiCache in production.

    When ``config.cluster`` is set, a cluster-aware :class:`redis.cluster.RedisCluster`
    is returned (it connects to the configuration endpoint and follows the
    ``MOVED``/``ASK`` redirects that an ElastiCache *cluster mode enabled* cluster
    issues). Otherwise a single-node :class:`redis.Redis` is returned, which is
    what local dev and cluster-mode-disabled setups use.

    ``decode_responses`` defaults to ``True`` so :class:`SessionStore` gets text
    back (it serializes with JSON itself). Flask-Session, however, stores binary
    payloads and must be given a client built with ``decode_responses=False``;
    otherwise redis-py tries to UTF-8 decode binary data and raises
    ``UnicodeDecodeError``.
    """
    common: dict[str, Any] = {
        "host": config.host,
        "port": config.port,
        "socket_timeout": config.socket_timeout,
        "socket_connect_timeout": config.socket_connect_timeout,
        "decode_responses": decode_responses,
    }
    if config.auth_token:
        # Valkey/Redis AUTH token (sourced from Secrets Manager at runtime).
        common["password"] = config.auth_token
    if config.ssl:
        common["ssl"] = True
        common["ssl_cert_reqs"] = config.ssl_cert_reqs

    if config.cluster:
        # RedisCluster discovers shards from the configuration endpoint and
        # routes each key to the owning shard. It does not accept the
        # single-node 'retry_on_timeout' kwarg, so keep the kwargs minimal.
        return redis.cluster.RedisCluster(**common)

    return redis.Redis(
        retry_on_timeout=True,
        health_check_interval=30,
        **common,
    )


def new_session_id() -> str:
    """Return a cryptographically-strong, unguessable session identifier."""
    return secrets.token_urlsafe(_SESSION_ID_BYTES)


def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


class SessionStore:
    """A thin, durable session repository over an ElastiCache endpoint."""

    def __init__(
        self,
        client: redis.Redis | redis.cluster.RedisCluster,
        ttl_seconds: int = 3600,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._client = client
        self._ttl = ttl_seconds

    @property
    def client(self) -> redis.Redis | redis.cluster.RedisCluster:
        """The underlying redis-py client (shared with the cart repository)."""
        return self._client

    def ping(self) -> bool:
        """Return True if the backing store is reachable (used by health checks)."""
        return bool(self._client.ping())

    def create(self, data: Mapping[str, Any]) -> str:
        """Persist a new session and return its freshly minted id."""
        session_id = new_session_id()
        self.save(session_id, data)
        return session_id

    def save(self, session_id: str, data: Mapping[str, Any]) -> None:
        """Serialize and store ``data`` under ``session_id`` with a refreshed TTL."""
        if not session_id:
            raise ValueError("session_id must be non-empty")
        payload = json.dumps(dict(data), separators=(",", ":"))
        # SET with expiry is atomic and resets the sliding-expiry window.
        self._client.set(_key(session_id), payload, ex=self._ttl)

    def load(self, session_id: str) -> dict[str, Any] | None:
        """Return the stored session dict, or None if missing/expired."""
        if not session_id:
            return None
        raw = self._client.get(_key(session_id))
        if raw is None:
            return None
        return json.loads(raw)

    def touch(self, session_id: str) -> bool:
        """Extend the TTL of an existing session (sliding expiration)."""
        return bool(self._client.expire(_key(session_id), self._ttl))

    def delete(self, session_id: str) -> None:
        """Remove a session (used on logout)."""
        if session_id:
            self._client.delete(_key(session_id))
