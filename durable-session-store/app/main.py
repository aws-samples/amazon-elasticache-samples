# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Flask web application demonstrating a durable ElastiCache-backed session store.

This expands the aws-samples ``session-store`` tutorial into a production-shaped
example: server-side sessions in ElastiCache (Valkey/Redis OSS), TLS + AUTH,
secure cookies, and a health endpoint for load balancer / ECS checks.
"""
from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import cart as cart_ops
from .catalog import all_products, get_product
from .config import AppConfig
from .failover import (
    FailoverError,
    describe_failover_status,
    trigger_test_failover,
)
from .session_store import SessionStore, build_redis_client
from .thumbnails import render_thumbnail_svg

# Guest cart ids are opaque and unguessable. They live in their own long-lived
# cookie (not the session) so a guest cart outlives the short login session.
_CART_ID_BYTES = 32
_CART_ID_COOKIE = "cart_id"
# secrets.token_urlsafe(32) always yields exactly 43 URL-safe base64 chars.
_GUEST_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def _valid_guest_token(raw: str | None) -> str | None:
    """Return ``raw`` only if it has the exact shape of a server-minted token.

    Rejects anything else (e.g. ``user:alice``, path tricks, oversized values),
    so a client-controlled cookie can't choose an arbitrary cart key.
    """
    if raw and _GUEST_TOKEN_RE.fullmatch(raw):
        return raw
    return None


def _guest_key(token: str) -> str:
    """Guest carts get their own namespace, disjoint from ``user:`` carts."""
    return f"guest:{token}"


def create_app(
    config: AppConfig | None = None, store: SessionStore | None = None
) -> Flask:
    """Application factory.

    Accepting an optional ``store`` makes the app trivially testable: tests inject
    a fake/in-memory Redis without touching real infrastructure.
    """
    config = config or AppConfig.from_env()
    app = Flask(__name__)

    # --- Secure session cookie configuration ---------------------------------
    app.secret_key = config.secret_key
    app.config.update(
        SESSION_COOKIE_SECURE=config.session_cookie_secure,
        SESSION_COOKIE_HTTPONLY=config.session_cookie_httponly,
        SESSION_COOKIE_SAMESITE=config.session_cookie_samesite,
        PERMANENT_SESSION_LIFETIME=timedelta(
            seconds=config.session_lifetime_seconds
        ),
        TESTING=config.testing,
    )

    client = None
    if store is None:
        client = build_redis_client(config.redis)
        store = SessionStore(client, ttl_seconds=config.session_lifetime_seconds)
    app.extensions["session_store"] = store

    # The cart repository reuses the session store's text-mode client (it does
    # its own hash I/O under cart:{id} keys, separate from the login session).
    cart_repo = cart_ops.CartRepository(
        store.client, ttl_seconds=config.cart_ttl_seconds
    )
    app.extensions["cart_repo"] = cart_repo

    # --- Session handling ----------------------------------------------------
    # The DURABLE data (the shopping cart) lives in the cluster under cart:{id}
    # hashes via CartRepository, independent of the Flask session. The Flask
    # session itself only carries two tiny, non-durable values: the logged-in
    # username and the guest cart id. Those ride in Flask's signed cookie.
    #
    # Why not Flask-Session's redis backend: its 0.8 redis store rejects a
    # cluster client (``isinstance(client, redis.Redis)`` is False for
    # ``RedisCluster``) and silently falls back to localhost. Since the cart is
    # already durable in the cluster on its own key, the login/cart-id cookie is
    # all the session needs here.
    #
    # ``client is None`` only in tests (an in-memory store is injected); there we
    # also use the signed-cookie session, which the test client round-trips.

    def _cart_id() -> str:
        """Return the active cart id, minting a guest cart id if needed.

        A logged-in user's cart is keyed by their account (``user:{username}``)
        so it is reachable from any device.

        A guest's cart id lives in its OWN long-lived cookie (14 days, matching
        the cart TTL), NOT in the Flask session. This is deliberate: the login
        session is short-lived (1h), but the guest cart must survive session
        expiry so a returning visitor still resolves to the same ``cart:{id}``
        hash. A newly minted id is stashed on ``g`` and written to the response
        cookie by ``_persist_cart_cookie`` (an after_request hook).

        Security: the cookie is client-controlled, so it is never used as a
        key directly. Only a value matching the exact token shape the server
        mints is accepted, and guest carts live in their own ``guest:``
        namespace. A forged cookie such as ``cart_id=user:alice`` is ignored
        (a fresh guest cart is minted) and can never resolve to an account cart.
        """
        if "username" in session:
            return f"user:{session['username']}"
        # Already resolved on this request?
        if getattr(g, "cart_key", None):
            return g.cart_key
        token = _valid_guest_token(request.cookies.get(_CART_ID_COOKIE))
        if token is None:
            token = secrets.token_urlsafe(_CART_ID_BYTES)
            g.cart_id_is_new = True
        g.cart_token = token
        g.cart_key = _guest_key(token)
        return g.cart_key

    @app.after_request
    def _persist_cart_cookie(response: Response) -> Response:
        """Write the guest cart-id cookie (14-day) when a new one was minted."""
        if getattr(g, "cart_id_is_new", False) and getattr(g, "cart_token", None):
            response.set_cookie(
                _CART_ID_COOKIE,
                g.cart_token,
                max_age=config.cart_ttl_seconds,
                httponly=True,
                secure=config.session_cookie_secure,
                samesite=config.session_cookie_samesite,
            )
        return response

    @app.context_processor
    def inject_nav() -> dict[str, Any]:
        """Expose the username, cart item count, and demo flag to templates."""
        try:
            count = cart_repo.count(_cart_id())
        except Exception:  # noqa: BLE001 - never let the badge break a page
            count = 0
        return {
            "username": session.get("username"),
            "cart_count": count,
            "failover_demo_enabled": config.enable_failover_demo,
        }

    # --- Storefront ----------------------------------------------------------
    @app.route("/")
    def index() -> str:
        return render_template("catalog.html", products=all_products())

    @app.route("/product/<product_id>")
    def product_detail(product_id: str) -> Any:
        product = get_product(product_id)
        if product is None:
            return render_template("catalog.html", products=all_products()), 404
        return render_template("product.html", product=product)

    @app.route("/thumbnail/<product_id>.svg")
    @app.route("/thumbnail/<product_id>")
    def thumbnail(product_id: str) -> Any:
        """Serve a generated SVG thumbnail for a product."""
        product = get_product(product_id)
        if product is None:
            return "not found", 404
        svg = render_thumbnail_svg(product)
        resp = Response(svg, mimetype="image/svg+xml")
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp

    # --- Cart (dedicated cart:{id} hash in the durable store) ----------------
    @app.route("/cart")
    def view_cart() -> str:
        return render_template("cart.html", cart=cart_repo.view(_cart_id()))

    @app.route("/cart/add", methods=["POST"])
    def add_to_cart() -> Any:
        product_id = request.form.get("product_id", "")
        quantity = request.form.get("quantity", "1")
        product = get_product(product_id)
        if cart_repo.add_item(_cart_id(), product_id, _as_int(quantity, default=1)):
            flash(f"Added {product.name} to your cart.")
        return redirect(_safe_back())

    @app.route("/cart/update", methods=["POST"])
    def update_cart() -> Any:
        product_id = request.form.get("product_id", "")
        quantity = request.form.get("quantity", "0")
        cart_repo.set_quantity(_cart_id(), product_id, _as_int(quantity, default=0))
        return redirect(url_for("view_cart"))

    @app.route("/cart/remove", methods=["POST"])
    def remove_from_cart() -> Any:
        cart_repo.remove_item(_cart_id(), request.form.get("product_id", ""))
        return redirect(url_for("view_cart"))

    @app.route("/cart/clear", methods=["POST"])
    def clear_cart() -> Any:
        if cart_repo.clear(_cart_id()):
            flash("Cart cleared.")
        return redirect(url_for("view_cart"))

    @app.route("/cart/save", methods=["POST"])
    def save_for_later() -> Any:
        cart_repo.save_for_later(_cart_id(), request.form.get("product_id", ""))
        return redirect(url_for("view_cart"))

    @app.route("/cart/move", methods=["POST"])
    def move_to_cart() -> Any:
        cart_repo.move_to_cart(_cart_id(), request.form.get("product_id", ""))
        return redirect(url_for("view_cart"))

    @app.route("/cart/coupon", methods=["POST"])
    def apply_coupon() -> Any:
        code = request.form.get("code", "").strip()
        if code and cart_repo.apply_coupon(_cart_id(), code):
            flash(f"Applied coupon {code}.")
        return redirect(url_for("view_cart"))

    # --- Failover demo (opt-in, disruptive, POST-only) -----------------------
    # Lets a demo audience watch their cart survive a primary failover. Off
    # unless ENABLE_FAILOVER_DEMO is set. Uses the app's own IAM credentials,
    # which must grant elasticache:TestFailover.
    @app.route("/demo/failover", methods=["POST"])
    def demo_failover() -> Any:
        if not config.enable_failover_demo:
            # Inert when the demo is off: never calls AWS.
            return render_template("failover.html", enabled=False), 403
        snapshot = cart_repo.view(_cart_id())
        try:
            result = trigger_test_failover(
                replication_group_id=config.replication_group_id or "",
                node_group_id=config.failover_node_group_id,
                region=config.aws_region,
            )
        except FailoverError as exc:
            return (
                render_template(
                    "failover.html",
                    enabled=True,
                    error=str(exc),
                    cart=snapshot,
                ),
                502,
            )
        return render_template(
            "failover.html",
            enabled=True,
            result=result,
            cart=snapshot,
        )

    @app.route("/demo/failover/status")
    def demo_failover_status() -> Any:
        """Live cluster status for the UI to poll during a failover (read-only).

        Returns the shard status, whether a failover is in progress, the member
        node roles, and the user's current cart count — so the page can show the
        primary moving while the cart count stays constant.
        """
        if not config.enable_failover_demo:
            return jsonify({"enabled": False}), 403
        try:
            st = describe_failover_status(
                replication_group_id=config.replication_group_id or "",
                node_group_id=config.failover_node_group_id,
                region=config.aws_region,
            )
        except FailoverError as exc:
            return jsonify({"error": str(exc)}), 502
        try:
            cart_count = cart_repo.count(_cart_id())
        except Exception:  # noqa: BLE001
            cart_count = None
        return jsonify(
            {
                "enabled": True,
                "status": st.status,
                "in_progress": st.in_progress,
                "members": list(st.members),
                "cart_count": cart_count,
            }
        )

    # --- Auth ----------------------------------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            if not username:
                return render_template("login.html"), 400
            # Merge any guest cart into the account cart (account-wins), then
            # forget the guest id so the account cart becomes the active one.
            # Merge any guest cart (from its long-lived cookie) into the account
            # cart (account-wins), then clear the guest cookie so the account
            # cart becomes the active one.
            # Validate the client-controlled cookie the same way as _cart_id():
            # only a server-minted token, and only from the guest: namespace,
            # so a forged cookie can never merge (and delete) another account.
            guest_token = _valid_guest_token(request.cookies.get(_CART_ID_COOKIE))
            session.permanent = True
            session["username"] = username
            resp = redirect(url_for("index"))
            if guest_token:
                cart_repo.merge(_guest_key(guest_token), f"user:{username}")
            resp.delete_cookie(_CART_ID_COOKIE)
            flash(f"Welcome, {username}!")
            return resp
        return render_template("login.html")

    @app.route("/logout")
    def logout() -> Any:
        session.pop("username", None)
        return redirect(url_for("index"))

    @app.route("/healthz")
    def healthz() -> Any:
        """Liveness/readiness probe: verifies the ElastiCache backend is reachable."""
        try:
            healthy = app.extensions["session_store"].ping()
        except Exception:  # noqa: BLE001 - report any backend failure as unhealthy
            healthy = False
        status = 200 if healthy else 503
        return jsonify({"status": "ok" if healthy else "unavailable"}), status

    return app


def _safe_back() -> str:
    """Return a safe redirect target after a cart action.

    Only honor ``request.referrer`` when it points at this same host; otherwise
    fall back to the catalog. This prevents an open-redirect where an attacker
    crafts a referrer to bounce the user to an external site.
    """
    referrer = request.referrer
    if referrer:
        ref = urlparse(referrer)
        host = urlparse(request.host_url)
        # Same scheme+host (and only a path we generated) is safe to return to.
        if ref.netloc == host.netloc:
            return referrer
    return url_for("index")


def _as_int(value: str, *, default: int) -> int:
    """Best-effort parse of a form value to int, falling back to ``default``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# WSGI entrypoint for gunicorn/uwsgi: ``gunicorn app.main:app``
#
# When the required configuration is absent (e.g. importing the module in tooling
# or tests without env set), we skip building the app so the import doesn't crash.
# But when SECRET_KEY/REDIS_HOST ARE present (as in the container), we build the
# app and let any failure propagate with its real traceback instead of silently
# yielding ``app = None`` (which surfaces later as gunicorn's unhelpful
# "Failed to find application object: 'app'").
def _build_default_app() -> Flask | None:
    import os

    if not os.environ.get("SECRET_KEY") or not os.environ.get("REDIS_HOST"):
        return None
    return create_app()


app = _build_default_app()
