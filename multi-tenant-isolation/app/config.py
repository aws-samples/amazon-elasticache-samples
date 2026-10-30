# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Application configuration for the multi-tenant storefront demo.

Every secret and connection parameter is sourced from the environment, which in
the deployed stack is populated from AWS Secrets Manager. Nothing sensitive is
hardcoded. Each tenant authenticates to Valkey as its OWN RBAC user, pinned to
its own numbered database by the `db=` ACL rule.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


class ConfigError(RuntimeError):
    """Raised when a required configuration value is missing or invalid."""


@dataclass(frozen=True)
class AppConfig:
    """Top-level configuration for the multi-tenant demo app."""

    secret_key: str
    redis_host: str
    redis_port: int = 6379
    redis_ssl: bool = True
    redis_ssl_cert_reqs: str = "required"
    tenant_count: int = 5
    # Mapping of tenant index -> RBAC password. Resolved lazily: first from the
    # environment (TENANT1_PASSWORD, ...), otherwise fetched from Secrets Manager
    # (<secrets_prefix>/<n>) by the task role. See tenant_store for the lookup.
    tenant_passwords: dict[int, str] = field(default_factory=dict)
    # Secrets Manager name prefix for per-tenant password secrets, e.g.
    # "valkey-multi-tenant/tenant". A tenant N's secret is "<prefix>/<N>".
    tenant_secret_prefix: str = ""
    aws_region: str | None = None
    session_cookie_secure: bool = True
    session_cookie_httponly: bool = True
    session_cookie_samesite: str = "Lax"
    socket_timeout: float = 5.0
    testing: bool = False

    def tenant_user(self, n: int) -> str:
        return f"tenant{n}"

    def tenant_db(self, n: int) -> int:
        return n

    @staticmethod
    def from_env() -> AppConfig:
        secret_key = os.environ.get("SECRET_KEY")
        if not secret_key:
            raise ConfigError(
                "SECRET_KEY is required (inject it from Secrets Manager)."
            )
        host = os.environ.get("REDIS_HOST")
        if not host:
            raise ConfigError("REDIS_HOST is required.")

        tenant_count = _env_int("TENANT_COUNT", 5)
        passwords: dict[int, str] = {}
        for n in range(1, tenant_count + 1):
            pw = os.environ.get(f"TENANT{n}_PASSWORD")
            if pw:
                passwords[n] = pw

        return AppConfig(
            secret_key=secret_key,
            redis_host=host,
            redis_port=_env_int("REDIS_PORT", 6379),
            redis_ssl=_env_bool("REDIS_SSL", default=True),
            redis_ssl_cert_reqs=os.environ.get("REDIS_SSL_CERT_REQS", "required"),
            tenant_count=tenant_count,
            tenant_passwords=passwords,
            tenant_secret_prefix=os.environ.get("TENANT_SECRET_PREFIX", ""),
            aws_region=os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or None,
            session_cookie_secure=_env_bool("SESSION_COOKIE_SECURE", default=True),
            session_cookie_httponly=_env_bool(
                "SESSION_COOKIE_HTTPONLY", default=True
            ),
            session_cookie_samesite=os.environ.get("SESSION_COOKIE_SAMESITE", "Lax"),
            socket_timeout=float(os.environ.get("REDIS_SOCKET_TIMEOUT", "5")),
            testing=_env_bool("TESTING", default=False),
        )
