# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the Flask storefront routes.

A fake SessionStore is injected so the app can be exercised without any real
ElastiCache endpoint. When a store is injected, the app falls back to Flask's
signed-cookie session, which the test client round-trips automatically, so cart
state persists across requests within a test.
"""
from __future__ import annotations

import secrets

import fakeredis
import pytest
from flask.testing import FlaskClient

from app.catalog import all_products
from app.config import AppConfig, RedisConfig
from app.main import create_app
from app.session_store import SessionStore

_FIRST = all_products()[0]
_SECOND = all_products()[1]


@pytest.fixture
def client() -> FlaskClient:
    store = SessionStore(
        fakeredis.FakeStrictRedis(decode_responses=True), ttl_seconds=3600
    )
    config = AppConfig(
        # Generated per run — no secret literal in source for scanners to flag.
        secret_key=secrets.token_urlsafe(16),
        redis=RedisConfig(host="localhost", ssl=False),
        session_cookie_secure=False,  # test client is not over HTTPS
        testing=True,
    )
    app = create_app(config=config, store=store)
    return app.test_client()


# --- Catalog pages ----------------------------------------------------------


def test_index_lists_products(client: FlaskClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert _FIRST.name.encode() in resp.data
    assert b"Add to cart" in resp.data


def test_product_detail(client: FlaskClient) -> None:
    resp = client.get(f"/product/{_FIRST.id}")
    assert resp.status_code == 200
    assert _FIRST.name.encode() in resp.data


def test_product_detail_unknown_is_404(client: FlaskClient) -> None:
    resp = client.get("/product/does-not-exist")
    assert resp.status_code == 404


def test_thumbnail_is_svg(client: FlaskClient) -> None:
    resp = client.get(f"/thumbnail/{_FIRST.id}.svg")
    assert resp.status_code == 200
    assert resp.mimetype == "image/svg+xml"
    assert b"<svg" in resp.data


def test_thumbnail_unknown_is_404(client: FlaskClient) -> None:
    assert client.get("/thumbnail/nope.svg").status_code == 404


# --- Cart flow (the durability demo) ----------------------------------------


def test_add_to_cart_then_view(client: FlaskClient) -> None:
    client.post("/cart/add", data={"product_id": _FIRST.id})
    resp = client.get("/cart")
    assert resp.status_code == 200
    assert _FIRST.name.encode() in resp.data
    assert _FIRST.price_display.encode() in resp.data


def test_cart_persists_across_requests(client: FlaskClient) -> None:
    # Each request is independent; persistence proves the cart is in Valkey.
    client.post("/cart/add", data={"product_id": _FIRST.id})
    client.post("/cart/add", data={"product_id": _SECOND.id})
    resp = client.get("/cart")
    assert _FIRST.name.encode() in resp.data
    assert _SECOND.name.encode() in resp.data


def test_add_to_cart_ignores_external_referrer(client: FlaskClient) -> None:
    """An attacker-controlled Referer must not become an open redirect."""
    resp = client.post(
        "/cart/add",
        data={"product_id": _FIRST.id},
        headers={"Referer": "https://evil.example.com/phish"},
    )
    assert resp.status_code in (301, 302)
    location = resp.headers["Location"]
    # Must not redirect off-site; falls back to a local path.
    assert "evil.example.com" not in location
    assert location.endswith("/") or location.startswith("/")


def test_add_to_cart_honors_same_site_referrer(client: FlaskClient) -> None:
    resp = client.post(
        "/cart/add",
        data={"product_id": _FIRST.id},
        headers={"Referer": "http://localhost/product/" + _FIRST.id},
    )
    assert resp.status_code in (301, 302)
    assert "/product/" in resp.headers["Location"]


def test_forged_cart_cookie_cannot_read_account_cart(client: FlaskClient) -> None:
    """Cart takeover: cart_id=user:alice must NOT expose Alice's cart."""
    # Alice logs in and fills her account cart.
    client.post("/login", data={"username": "alice"})
    client.post("/cart/add", data={"product_id": _FIRST.id, "quantity": "3"})
    client.get("/logout")

    # Attacker (fresh browser) forges the cookie to point at Alice's cart.
    attacker = client.application.test_client()
    attacker.set_cookie("cart_id", "user:alice")
    resp = attacker.get("/cart")
    assert _FIRST.name.encode() not in resp.data
    assert b"cart is empty" in resp.data


def test_forged_cart_cookie_cannot_hijack_via_login(client: FlaskClient) -> None:
    """Logging in with a forged cookie must not merge or delete Alice's cart."""
    client.post("/login", data={"username": "alice"})
    client.post("/cart/add", data={"product_id": _FIRST.id, "quantity": "3"})
    client.get("/logout")

    attacker = client.application.test_client()
    attacker.set_cookie("cart_id", "user:alice")
    attacker.post("/login", data={"username": "mallory"})
    # Mallory gets nothing from Alice...
    assert _FIRST.name.encode() not in attacker.get("/cart").data

    # ...and Alice's cart is still intact.
    client.post("/login", data={"username": "alice"})
    assert _FIRST.name.encode() in client.get("/cart").data


def test_malformed_cart_cookie_gets_a_fresh_guest_cart(client: FlaskClient) -> None:
    """Any cookie not shaped like a server-minted token is replaced."""
    client.set_cookie("cart_id", "../../etc")
    resp = client.post("/cart/add", data={"product_id": _FIRST.id})
    new_cookie = client.get_cookie("cart_id")
    assert new_cookie is not None and new_cookie.value != "../../etc"
    assert "cart_id=" in resp.headers.get("Set-Cookie", "")


def test_guest_cart_id_uses_a_long_lived_cookie(client: FlaskClient) -> None:
    """The guest cart id rides in its own cookie (not the session), with a
    max-age matching the cart TTL, so the cart survives session expiry."""
    resp = client.post("/cart/add", data={"product_id": _FIRST.id})
    set_cookie = resp.headers.get("Set-Cookie", "")
    # A dedicated cart_id cookie is issued...
    assert "cart_id=" in set_cookie
    # ...and it is long-lived (max-age set, i.e. not a session cookie).
    assert "Max-Age=" in set_cookie


def test_guest_cart_survives_session_reset(client: FlaskClient) -> None:
    """Dropping the Flask session but keeping the cart_id cookie still resolves
    the same cart — the whole point of decoupling the cart from the session."""
    client.post("/cart/add", data={"product_id": _FIRST.id, "quantity": "2"})
    # Grab the persistent cart_id cookie the server issued.
    cart_cookie = client.get_cookie("cart_id")
    assert cart_cookie is not None
    # Simulate session expiry: wipe all cookies, then restore ONLY cart_id.
    client.delete_cookie("session")
    resp = client.get("/cart")
    # Cart is still there because cart_id persisted independently.
    assert _FIRST.name.encode() in resp.data


def test_add_quantity_accumulates(client: FlaskClient) -> None:
    client.post("/cart/add", data={"product_id": _FIRST.id, "quantity": "2"})
    client.post("/cart/add", data={"product_id": _FIRST.id, "quantity": "3"})
    resp = client.get("/cart")
    # 5 of one item at its price.
    expected_total = _FIRST.price_cents * 5
    assert f"${expected_total / 100:,.2f}".encode() in resp.data


def test_update_quantity(client: FlaskClient) -> None:
    client.post("/cart/add", data={"product_id": _FIRST.id})
    client.post("/cart/update", data={"product_id": _FIRST.id, "quantity": "4"})
    resp = client.get("/cart")
    assert f"({4} items)".encode() in resp.data


def test_update_to_zero_removes(client: FlaskClient) -> None:
    client.post("/cart/add", data={"product_id": _FIRST.id})
    client.post("/cart/update", data={"product_id": _FIRST.id, "quantity": "0"})
    resp = client.get("/cart")
    assert b"cart is empty" in resp.data


def test_remove_from_cart(client: FlaskClient) -> None:
    client.post("/cart/add", data={"product_id": _FIRST.id})
    client.post("/cart/remove", data={"product_id": _FIRST.id})
    resp = client.get("/cart")
    assert b"cart is empty" in resp.data


def test_clear_cart(client: FlaskClient) -> None:
    client.post("/cart/add", data={"product_id": _FIRST.id})
    client.post("/cart/add", data={"product_id": _SECOND.id})
    client.post("/cart/clear")
    resp = client.get("/cart")
    assert b"cart is empty" in resp.data


def test_add_unknown_product_is_ignored(client: FlaskClient) -> None:
    client.post("/cart/add", data={"product_id": "nope"})
    resp = client.get("/cart")
    assert b"cart is empty" in resp.data


def test_cart_badge_reflects_count(client: FlaskClient) -> None:
    client.post("/cart/add", data={"product_id": _FIRST.id, "quantity": "2"})
    resp = client.get("/")
    assert b'class="badge"' in resp.data


# --- Auth -------------------------------------------------------------------


def test_login_then_greeting(client: FlaskClient) -> None:
    resp = client.post(
        "/login", data={"username": "ada"}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert b"Hi, ada" in resp.data


def test_login_requires_username(client: FlaskClient) -> None:
    resp = client.post("/login", data={"username": "   "})
    assert resp.status_code == 400


def test_login_escapes_html(client: FlaskClient) -> None:
    resp = client.post(
        "/login",
        data={"username": "<script>alert(1)</script>"},
        follow_redirects=True,
    )
    assert b"<script>alert(1)</script>" not in resp.data


def test_logout_clears_user(client: FlaskClient) -> None:
    client.post("/login", data={"username": "ada"}, follow_redirects=True)
    client.get("/logout")
    resp = client.get("/")
    assert b"Hi, ada" not in resp.data


def test_cart_survives_login(client: FlaskClient) -> None:
    client.post("/cart/add", data={"product_id": _FIRST.id})
    client.post("/login", data={"username": "ada"}, follow_redirects=True)
    resp = client.get("/cart")
    assert _FIRST.name.encode() in resp.data


def test_healthz_ok(client: FlaskClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
