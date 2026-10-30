# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Multi-tenant SaaS storefront demo on Amazon ElastiCache for Valkey.

Each browser session acts as one tenant. The app connects to the shared Valkey
cluster AS THAT TENANT'S RBAC USER, pinned to the tenant's own numbered database
with the Valkey 9.1 ``db=`` ACL rule. Isolation is enforced by the engine:

  * a tenant can read and write only its own database,
  * trying to peek at another tenant's database returns NOPERM from the server,
  * a tenant cannot run FLUSHALL (it lacks ``alldbs``).

This is the "many small tenants on one shared cluster" pattern made clickable:
thousands of tenants on a single cluster, each isolated by the engine.
"""
from __future__ import annotations

from typing import Any

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .config import AppConfig
from .tenant_store import TenantStore


def create_app(
    config: AppConfig | None = None, store: TenantStore | None = None
) -> Flask:
    """Application factory. Injecting ``store`` makes the app testable."""
    config = config or AppConfig.from_env()
    store = store or TenantStore(config)

    app = Flask(__name__)
    app.secret_key = config.secret_key
    app.config.update(
        SESSION_COOKIE_SECURE=config.session_cookie_secure,
        SESSION_COOKIE_HTTPONLY=config.session_cookie_httponly,
        SESSION_COOKIE_SAMESITE=config.session_cookie_samesite,
        TESTING=config.testing,
    )
    app.extensions["tenant_store"] = store

    def _current_tenant() -> int | None:
        return session.get("tenant")

    def _require_tenant() -> int | None:
        t = _current_tenant()
        if t is None:
            return None
        return int(t)

    @app.context_processor
    def inject_nav() -> dict[str, Any]:
        return {
            "current_tenant": _current_tenant(),
            "tenant_count": config.tenant_count,
        }

    # -- tenant picker / login -----------------------------------------------
    @app.route("/")
    def index() -> str:
        tenants = [
            {"n": n, "user": config.tenant_user(n), "db": config.tenant_db(n)}
            for n in range(1, config.tenant_count + 1)
        ]
        return render_template("index.html", tenants=tenants)

    @app.route("/login", methods=["POST"])
    def login() -> Any:
        try:
            n = int(request.form.get("tenant", ""))
        except ValueError:
            flash("Pick a valid tenant.")
            return redirect(url_for("index"))
        if n < 1 or n > config.tenant_count:
            flash("Pick a valid tenant.")
            return redirect(url_for("index"))
        session["tenant"] = n
        flash(f"Signed in as tenant{n} (database {n}).")
        return redirect(url_for("store"))

    @app.route("/logout")
    def logout() -> Any:
        session.pop("tenant", None)
        return redirect(url_for("index"))

    # -- the tenant's own storefront -----------------------------------------
    @app.route("/store", endpoint="store")
    def store_view() -> Any:
        t = _require_tenant()
        if t is None:
            return redirect(url_for("index"))
        try:
            items = store.list_items(t)
        except Exception as exc:  # noqa: BLE001
            items = []
            flash(f"Could not read your database: {exc}")
        # Candidate databases to "peek" at: every other tenant.
        others = [n for n in range(1, config.tenant_count + 1) if n != t]
        return render_template("store.html", tenant=t, items=items, others=others)

    @app.route("/store/add", methods=["POST"])
    def add_item() -> Any:
        t = _require_tenant()
        if t is None:
            return redirect(url_for("index"))
        store.add_item(t, request.form.get("item", ""))
        return redirect(url_for("store"))

    @app.route("/store/clear", methods=["POST"])
    def clear_items() -> Any:
        t = _require_tenant()
        if t is None:
            return redirect(url_for("index"))
        store.clear_own(t)  # FLUSHDB - clears only this tenant
        flash("Cleared your own database (FLUSHDB).")
        return redirect(url_for("store"))

    # -- isolation demonstration ---------------------------------------------
    @app.route("/peek", methods=["POST"])
    def peek() -> Any:
        t = _require_tenant()
        if t is None:
            return redirect(url_for("index"))
        try:
            target = int(request.form.get("target_db", ""))
        except ValueError:
            flash("Pick a database to peek at.")
            return redirect(url_for("store"))
        result = store.peek(t, target)
        if result.allowed:
            flash(f"db {target}: READ ALLOWED - {result.message}")
        else:
            flash(f"db {target}: DENIED by the engine - {result.message}")
        return redirect(url_for("store"))

    @app.route("/peek/flushall", methods=["POST"])
    def peek_flushall() -> Any:
        t = _require_tenant()
        if t is None:
            return redirect(url_for("index"))
        result = store.try_flushall(t)
        if result.allowed:
            flash(f"FLUSHALL ALLOWED (unexpected): {result.message}")
        else:
            flash(f"FLUSHALL DENIED by the engine: {result.message}")
        return redirect(url_for("store"))

    # -- health --------------------------------------------------------------
    @app.route("/healthz")
    def healthz() -> Any:
        healthy = store.ping(1)
        return (
            jsonify({"status": "ok" if healthy else "unavailable"}),
            200 if healthy else 503,
        )

    return app


def _build_default_app() -> Flask | None:
    import os

    if not os.environ.get("SECRET_KEY") or not os.environ.get("REDIS_HOST"):
        return None
    return create_app()


# WSGI entrypoint: gunicorn app.main:app
app = _build_default_app()
