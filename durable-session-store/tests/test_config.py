# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for environment-driven configuration."""
from __future__ import annotations

import pytest

from app.config import AppConfig, ConfigError, RedisConfig


def test_redis_config_requires_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_HOST", raising=False)
    with pytest.raises(ConfigError):
        RedisConfig.from_env()


def test_redis_config_defaults_to_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_HOST", "cache.example.com")
    monkeypatch.delenv("REDIS_SSL", raising=False)
    config = RedisConfig.from_env()
    assert config.ssl is True
    assert config.port == 6379


def test_app_config_requires_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("REDIS_HOST", "cache.example.com")
    with pytest.raises(ConfigError):
        AppConfig.from_env()


def test_app_config_secure_cookie_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    monkeypatch.setenv("REDIS_HOST", "cache.example.com")
    config = AppConfig.from_env()
    assert config.session_cookie_secure is True
    assert config.session_cookie_httponly is True
    assert config.session_cookie_samesite == "Lax"
