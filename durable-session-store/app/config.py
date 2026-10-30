# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Application configuration.

All secrets and connection parameters are sourced from the environment (which, in
production, is populated from AWS Secrets Manager / SSM Parameter Store). Nothing
sensitive is ever hardcoded or committed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


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
class RedisConfig:
    """Connection settings for the ElastiCache (Valkey/Redis OSS) endpoint.

    In production ``ssl`` and ``auth_token`` MUST be set so that the client uses
    in-transit encryption (TLS) and authenticated access (Valkey RBAC / AUTH).
    """

    host: str
    port: int = 6379
    ssl: bool = True
    auth_token: str | None = None
    socket_timeout: float = 5.0
    socket_connect_timeout: float = 5.0
    # ElastiCache in-transit encryption terminates on an AWS-managed CA. We keep
    # certificate verification ON by default; it can be relaxed only for local dev.
    ssl_cert_reqs: str = "required"
    # When true, connect with a cluster-aware client (redis.cluster.RedisCluster)
    # to the configuration endpoint. Required for ElastiCache cluster mode enabled.
    cluster: bool = False

    @staticmethod
    def from_env() -> RedisConfig:
        host = os.environ.get("REDIS_HOST")
        if not host:
            raise ConfigError("REDIS_HOST environment variable is required")
        return RedisConfig(
            host=host,
            port=_env_int("REDIS_PORT", 6379),
            ssl=_env_bool("REDIS_SSL", default=True),
            auth_token=os.environ.get("REDIS_AUTH_TOKEN") or None,
            socket_timeout=float(os.environ.get("REDIS_SOCKET_TIMEOUT", "5")),
            socket_connect_timeout=float(
                os.environ.get("REDIS_CONNECT_TIMEOUT", "5")
            ),
            ssl_cert_reqs=os.environ.get("REDIS_SSL_CERT_REQS", "required"),
            cluster=_env_bool("REDIS_CLUSTER_MODE", default=False),
        )


@dataclass(frozen=True)
class AppConfig:
    """Top-level Flask application configuration."""

    secret_key: str
    redis: RedisConfig
    session_lifetime_seconds: int = 3600
    session_cookie_secure: bool = True
    session_cookie_httponly: bool = True
    session_cookie_samesite: str = "Lax"
    testing: bool = False
    # Shopping-cart hashes live under their own key with a sliding TTL,
    # independent of the login session (default 14 days).
    cart_ttl_seconds: int = 1_209_600
    # --- Optional failover demo (OFF by default) ---------------------------
    # When enabled, the UI exposes an admin button that triggers an ElastiCache
    # TestFailover so you can watch a cart survive a primary failover. This is a
    # disruptive control-plane action; keep it disabled outside a demo.
    enable_failover_demo: bool = False
    replication_group_id: str | None = None
    failover_node_group_id: str = "0001"
    aws_region: str | None = None

    @staticmethod
    def from_env() -> AppConfig:
        secret_key = os.environ.get("SECRET_KEY")
        if not secret_key:
            raise ConfigError(
                "SECRET_KEY environment variable is required (store it in "
                "AWS Secrets Manager, never in source)"
            )
        enable_failover_demo = _env_bool("ENABLE_FAILOVER_DEMO", default=False)
        replication_group_id = (
            os.environ.get("REDIS_REPLICATION_GROUP_ID") or None
        )
        if enable_failover_demo and not replication_group_id:
            raise ConfigError(
                "ENABLE_FAILOVER_DEMO is true but REDIS_REPLICATION_GROUP_ID is "
                "not set; the failover demo needs the replication group id."
            )
        return AppConfig(
            secret_key=secret_key,
            redis=RedisConfig.from_env(),
            session_lifetime_seconds=_env_int("SESSION_LIFETIME_SECONDS", 3600),
            session_cookie_secure=_env_bool("SESSION_COOKIE_SECURE", default=True),
            session_cookie_httponly=_env_bool(
                "SESSION_COOKIE_HTTPONLY", default=True
            ),
            session_cookie_samesite=os.environ.get(
                "SESSION_COOKIE_SAMESITE", "Lax"
            ),
            testing=_env_bool("TESTING", default=False),
            cart_ttl_seconds=_env_int("CART_TTL_SECONDS", 1_209_600),
            enable_failover_demo=enable_failover_demo,
            replication_group_id=replication_group_id,
            failover_node_group_id=os.environ.get(
                "REDIS_NODE_GROUP_ID", "0001"
            ),
            aws_region=os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or None,
        )
