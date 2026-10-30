# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-tenant data access against a shared ElastiCache for Valkey cluster.

Each tenant connects as its OWN RBAC user, which is pinned to its own numbered
database with the Valkey 9.1 ``db=`` ACL rule. The engine therefore enforces
isolation: a connection authenticated as ``tenantN`` can only ``SELECT`` /
operate on database ``N``. Any attempt to touch another database returns a
``NOPERM`` error straight from the server - the application cannot bypass it.

This module is deliberately small: it opens a client per (tenant -> database)
and exposes a handful of operations plus the isolation-demonstrating "peek".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import redis

from .config import AppConfig


class TenantError(RuntimeError):
    """Raised when a tenant operation fails for a non-isolation reason."""


@dataclass(frozen=True)
class PeekResult:
    """Outcome of one tenant trying to read another tenant's database."""

    target_db: int
    allowed: bool
    message: str


class TenantStore:
    """Opens per-tenant Valkey connections and runs tenant-scoped operations."""

    # Key under which each tenant keeps a small list of "storefront items".
    _ITEMS_KEY = "items"

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._pw_cache: dict[int, str] = dict(config.tenant_passwords)
        self._sm_client: Any = None

    # -- credentials ----------------------------------------------------------
    def _password(self, tenant: int) -> str:
        """Return tenant N's RBAC password.

        Prefers a value injected via the environment; otherwise fetches it from
        Secrets Manager (``<prefix>/<N>``) using the task role and caches it.
        """
        if tenant in self._pw_cache:
            return self._pw_cache[tenant]
        prefix = self._config.tenant_secret_prefix
        if not prefix:
            raise TenantError(f"No credential configured for tenant {tenant}.")
        if self._sm_client is None:
            import boto3  # imported lazily; only needed when env vars are absent

            self._sm_client = boto3.client(
                "secretsmanager", region_name=self._config.aws_region
            )
        try:
            resp = self._sm_client.get_secret_value(SecretId=f"{prefix}/{tenant}")
        except Exception as exc:
            raise TenantError(
                f"Could not read credential for tenant {tenant}: {exc}"
            ) from exc
        pw = resp.get("SecretString") or ""
        if not pw:
            raise TenantError(f"Empty credential for tenant {tenant}.")
        self._pw_cache[tenant] = pw
        return pw

    # -- connection -----------------------------------------------------------
    def _client(self, tenant: int) -> redis.Redis:
        """Build a client authenticated as ``tenantN`` and selected to db N.

        A fresh connection starts in database 0; we pass ``db=N`` so redis-py
        issues ``SELECT N`` on connect. Because the RBAC user is restricted to
        database N, this succeeds for its own db and fails with NOPERM for any
        other - which is exactly the behaviour the "peek" relies on.
        """
        password = self._password(tenant)
        kwargs: dict[str, Any] = {
            "host": self._config.redis_host,
            "port": self._config.redis_port,
            "username": self._config.tenant_user(tenant),
            "password": password,
            "db": self._config.tenant_db(tenant),
            "socket_timeout": self._config.socket_timeout,
            "socket_connect_timeout": self._config.socket_timeout,
            "decode_responses": True,
        }
        if self._config.redis_ssl:
            kwargs["ssl"] = True
            kwargs["ssl_cert_reqs"] = self._config.redis_ssl_cert_reqs
        return redis.Redis(**kwargs)

    # -- tenant-scoped operations (always against the tenant's own db) --------
    def list_items(self, tenant: int) -> list[str]:
        client = self._client(tenant)
        return client.lrange(self._ITEMS_KEY, 0, -1)

    def add_item(self, tenant: int, item: str) -> None:
        item = (item or "").strip()
        if not item:
            return
        client = self._client(tenant)
        client.rpush(self._ITEMS_KEY, item)

    def clear_own(self, tenant: int) -> None:
        """FLUSHDB: clears only this tenant's database (allowed)."""
        self._client(tenant).flushdb()

    def try_flushall(self, tenant: int) -> PeekResult:
        """Attempt FLUSHALL, which requires alldbs and must be denied."""
        try:
            self._client(tenant).flushall()
            return PeekResult(
                target_db=-1,
                allowed=True,
                message="FLUSHALL succeeded (unexpected - tenant has alldbs).",
            )
        except redis.exceptions.ResponseError as exc:
            return PeekResult(target_db=-1, allowed=False, message=str(exc))

    # -- the isolation demonstration -----------------------------------------
    def peek(self, tenant: int, target_db: int) -> PeekResult:
        """Try to read a key from ``target_db`` as ``tenantN``.

        Returns a PeekResult describing whether the engine allowed it. For any
        database other than the tenant's own, Valkey returns a NOPERM error,
        proving the application cannot reach another tenant's data even when it
        explicitly tries.
        """
        password = self._password(tenant)
        kwargs: dict[str, Any] = {
            "host": self._config.redis_host,
            "port": self._config.redis_port,
            "username": self._config.tenant_user(tenant),
            "password": password,
            # Start in db 0 and SELECT explicitly below, so the SELECT itself is
            # what gets allowed or denied.
            "db": 0,
            "socket_timeout": self._config.socket_timeout,
            "socket_connect_timeout": self._config.socket_timeout,
            "decode_responses": True,
        }
        if self._config.redis_ssl:
            kwargs["ssl"] = True
            kwargs["ssl_cert_reqs"] = self._config.redis_ssl_cert_reqs

        try:
            client = redis.Redis(**kwargs)
            # Explicit SELECT to the target db; NOPERM is raised here for a db
            # the tenant is not allowed on. (db=0 in kwargs may also be denied,
            # so connect lazily and let the first command drive it.)
            client.execute_command("SELECT", target_db)
            value = client.lrange(self._ITEMS_KEY, 0, -1)
            return PeekResult(
                target_db=target_db,
                allowed=True,
                message=f"Read succeeded: {value!r}",
            )
        except redis.exceptions.ResponseError as exc:
            # NOPERM (and any other ACL error) lands here - isolation enforced.
            return PeekResult(target_db=target_db, allowed=False, message=str(exc))

    def ping(self, tenant: int = 1) -> bool:
        """Health probe: authenticate as a tenant and PING its database."""
        try:
            return bool(self._client(tenant).ping())
        except Exception:  # noqa: BLE001 - any failure means unhealthy
            return False
