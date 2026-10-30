# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared pytest fixtures.

Tests run entirely against an in-memory ``fakeredis`` server, so no real
ElastiCache endpoint, network access, or credentials are required.
"""
from __future__ import annotations

import fakeredis
import pytest

from app.session_store import SessionStore


@pytest.fixture
def fake_redis() -> fakeredis.FakeStrictRedis:
    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture
def store(fake_redis: fakeredis.FakeStrictRedis) -> SessionStore:
    return SessionStore(fake_redis, ttl_seconds=3600)
