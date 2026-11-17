"""`apcat`: drive a caching assessment from a coding agent or a terminal.

Every command prints one JSON document on stdout. A failure prints
{"error": "<code>", "detail": ...} and exits 1, with the API's own error code when the
API refused (database_not_checked, invalid_target, queue_full, ...).

    apcat guide                      the playbook a coding agent follows
    apcat start | status | stop      the local API and web console (127.0.0.1 only)
    apcat config [set|unset K V]     SSH key folder and known_hosts for the API apcat starts
    apcat hosts | keys               hosts to suggest (code hosts flagged), keys by name
    apcat probe --host H             step-by-step SSH check of a host, its PostgreSQL clusters and,
                                     when the SSH user can sudo as postgres, its databases
    apcat trust --host H --fingerprint F   trust a host key the user confirmed
    apcat discover [--host H]        databases that could be assessed
    apcat databases                  saved databases
    apcat save ...                   save a connection (never a password)
    apcat create-role ...            create the read-only role on the host (sudo, asks first)
    apcat edit DB ...                change a saved connection (then check again)
    apcat forget DB                  delete a saved database (its assessments stay)
    apcat check DB                   readiness: SSH, sign-in, pg_stat_statements
    apcat run DB --window N          start an assessment
    apcat wait JOB                   wait for it, up to --timeout seconds
    apcat job JOB | jobs             status of one, or the list
    apcat cancel | rerun | delete JOB   stop, run again on the same collection, delete
    apcat whatif JOB --set k=v ...   size a finished assessment again with other settings
    apcat results JOB [...]          summary, overview, queries, tables, one item
    apcat links [--database DB] [--job JOB]   where to see it in the browser
    apcat handoff JOB                pass it to the AWS ElastiCache skill (.elasticache/requirements.json)
    apcat agent install|uninstall|status      the skill for Claude Code, Codex and Kiro
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from importlib import resources
from typing import Any

from src.cli import agents, handoff, servers, settings
from src.cli.client import ApiClient, CliError
from src.version import __version__

TERMINAL = {"complete", "failed", "cancelled"}
DEFAULT_WAIT_SECONDS = 100  # under the 2 minute default command limit (Claude Code's); agents
# that can raise their tool timeout pass --timeout up to 540
POLL_SECONDS = 10
BULKY_OVERVIEW_KEYS = ("glossary", "scoring")
BRIEF_ITEMS = 10


def guide_text() -> str:
    """The agent playbook, the same text the installed skill carries."""
    return resources.files("src.cli").joinpath("skill/SKILL.md").read_text()


# --- commands -------------------------------------------------------------------------


def cmd_guide(_a: argparse.Namespace, _c: ApiClient | None) -> str:
    return guide_text()


def cmd_start(a: argparse.Namespace, _c: ApiClient | None) -> Any:
    out = servers.start(with_ui=not a.no_ui, restart=a.restart)
    out["capabilities"] = _caps_brief(ApiClient.from_env()) if out["api"]["running"] else None
    return out


def cmd_status(_a: argparse.Namespace, c: ApiClient) -> Any:
    out = servers.status(c)
    out["capabilities"] = _caps_brief(c) if out["api"]["running"] else None
    return out


def cmd_stop(_a: argparse.Namespace, _c: ApiClient | None) -> Any:
    return servers.stop()


def _caps_brief(c: ApiClient) -> dict[str, Any] | None:
    try:
        caps = c.get("/capabilities")
    except CliError:
        return None
    lim = caps.get("limits", {})
    return {
        "window_minutes_min": lim.get("window_minutes_min"),
        "window_minutes_max": lim.get("window_minutes_max"),
        "window_start_max_hours": lim.get("window_start_max_hours"),
        "default_region": (caps.get("region") or {}).get("default"),
        "default_model": (caps.get("model") or {}).get("default"),
    }


def cmd_config(a: argparse.Namespace, _c: ApiClient | None) -> Any:
    if a.action == "set":
        if not a.value:
            raise CliError("invalid_request", "apcat config set NAME VALUE")
        out = settings.set_value(a.name, a.value)
    elif a.action == "unset":
        out = settings.unset(a.name)
    else:
        out = settings.show()
    out["note"] = "The API uses these when apcat starts it: run `apcat start --restart` after a change."
    return out


def _target(a: argparse.Namespace) -> dict[str, Any]:
    body: dict[str, Any] = {"ssh_target": a.host, "ssh_port": a.ssh_port}
    if a.identity_file:
        body["identity_file"] = a.identity_file
    return body


def cmd_hosts(a: argparse.Namespace, c: ApiClient) -> Any:
    out = c.get("/hosts/suggestions")
    if not a.all:
        hidden = [i for i in out.get("items", []) if i.get("code_host")]
        out["items"] = [i for i in out.get("items", []) if not i.get("code_host")]
        out["code_hosts_hidden"] = len(hidden)
    return out


def cmd_keys(_a: argparse.Namespace, c: ApiClient) -> Any:
    return c.get("/ssh-keys")


def cmd_probe(a: argparse.Namespace, c: ApiClient) -> Any:
    out = c.post("/hosts/probe", _target(a), timeout=120)
    stages = out.get("stages", [])
    failed = next((st for st in stages if st.get("status") == "fail"), None)
    out["first_failure"] = failed
    key = out.get("host_key") or {}
    if key.get("state") == "unknown" and key.get("fingerprint"):
        out["next"] = (f"Show the user the {key.get('key_type')} fingerprint {key['fingerprint']} and "
                       "ask them to confirm it is the host's. Only then run "
                       f"`apcat trust --host {a.host} --ssh-port {a.ssh_port} --fingerprint "
                       f"{key['fingerprint']}` and probe again.")
    elif key.get("state") == "changed":
        out["next"] = ("Stop: the host key differs from the trusted one. The user must check "
                       "with the host's owner; apcat never replaces a trusted key.")
    elif not failed:
        out["next"] = _probe_next(out, a)
    return out


def _probe_next(out: dict[str, Any], a: argparse.Namespace) -> str | None:
    """What to do after a probe that passed: pick a database and the database user."""
    access = out.get("postgres_access")
    names = [d.get("database") for d in out.get("databases") or [] if d.get("database")]
    key = f" --identity-file {a.identity_file}" if a.identity_file else ""
    if access == "sudo" and names:
        return ("Show the user these databases (name, size, pg_stat_statements) and ask which to "
                "assess. Then offer the database user, default first: (1) apcat creates the "
                "read-only user apcat_reader, (2) the same with a name they choose, (3) an existing "
                "role with its password file or secret. For 1 or 2, after they say yes: "
                f"`apcat create-role --ssh-target {a.host} --ssh-port {a.ssh_port}{key} "
                "--database <db> --port <port> [--role-name <name>]`, then save with the role "
                "and password_file it returns.")
    if access == "no_sudo":
        return ("The SSH user cannot run commands as postgres, so the databases were not listed "
                "and apcat cannot create a user. Ask the user for the database name and an "
                "existing read-only role with its password file on the host or a secret, then "
                "`apcat save`.")
    return None


def cmd_trust(a: argparse.Namespace, c: ApiClient) -> Any:
    body = _target(a)
    body["fingerprint"] = a.fingerprint
    return c.post("/hosts/trust", body, timeout=60)


def cmd_discover(a: argparse.Namespace, c: ApiClient) -> Any:
    body: dict[str, Any] = {}
    if a.host:
        body["host"] = {"ssh_target": a.host, "ssh_port": a.ssh_port}
        if a.identity_file:
            body["host"]["identity_file"] = a.identity_file
        if a.host_only:
            body["sources"] = []
    return c.post("/databases/discover", body, timeout=120)


def cmd_databases(_a: argparse.Namespace, c: ApiClient) -> Any:
    return c.get("/databases")


def cmd_database(a: argparse.Namespace, c: ApiClient) -> Any:
    return c.get(f"/databases/{a.database_id}")


def cmd_save(a: argparse.Namespace, c: ApiClient) -> Any:
    if bool(a.password_file) == bool(a.password_secret_arn):
        raise CliError("invalid_request",
                       "Give exactly one of --password-file (a path on the database host) "
                       "or --password-secret-arn. Never the password itself.")
    conn: dict[str, Any] = {
        "ssh_target": a.ssh_target, "ssh_port": a.ssh_port, "db_user": a.db_user,
        "cluster_endpoint": a.endpoint, "port": a.port, "database_name": a.database,
    }
    for k in ("identity_file", "password_file", "password_secret_arn"):
        if getattr(a, k):
            conn[k] = getattr(a, k)
    if a.schemas:
        conn["schemas"] = [s.strip() for s in a.schemas.split(",") if s.strip()]
    body: dict[str, Any] = {"connection": conn}
    if a.label:
        body["label"] = a.label
    if a.discovered_from:
        body["discovered_from"] = a.discovered_from
    try:
        return c.post("/databases", body)
    except CliError as e:
        raise _saved_id(e) from None


def _saved_id(e: CliError) -> CliError:
    """database_exists names the saved id in its text; give it as a field too."""
    if e.code == "database_exists":
        m = re.search(r"db-[0-9a-f]{12}", str(e.detail))
        if m:
            e.extra["database_id"] = m.group(0)
    return e


def cmd_edit(a: argparse.Namespace, c: ApiClient) -> Any:
    current = c.get(f"/databases/{a.database_id}")
    conn = dict(current["connection"])
    if a.password_file and a.password_secret_arn:
        raise CliError("invalid_request", "Give one of --password-file or --password-secret-arn.")
    changes = {"ssh_target": a.ssh_target, "ssh_port": a.ssh_port, "identity_file": a.identity_file,
               "db_user": a.db_user, "cluster_endpoint": a.endpoint, "port": a.port,
               "database_name": a.database}
    conn.update({k: v for k, v in changes.items() if v is not None})
    if a.password_file:
        conn["password_file"], conn["password_secret_arn"] = a.password_file, None
    if a.password_secret_arn:
        conn["password_secret_arn"], conn["password_file"] = a.password_secret_arn, None
    if a.schemas is not None:
        conn["schemas"] = [x.strip() for x in a.schemas.split(",") if x.strip()] or None
    body: dict[str, Any] = {"connection": conn}
    if a.label:
        body["label"] = a.label
    try:
        out = c.call("PATCH", f"/databases/{a.database_id}", body)
    except CliError as e:
        raise _saved_id(e) from None
    if out.get("readiness", {}).get("status") == "unchecked":
        out["next"] = f"apcat check {a.database_id}"
    return out


def cmd_forget(a: argparse.Namespace, c: ApiClient) -> Any:
    c.call("DELETE", f"/databases/{a.database_id}")
    return {"deleted": a.database_id, "note": "Its assessments and results are kept."}


def cmd_create_role(a: argparse.Namespace, c: ApiClient) -> Any:
    body: dict[str, Any] = {"ssh_target": a.ssh_target, "ssh_port": a.ssh_port,
                            "database_name": a.database, "port": a.port,
                            "role_name": a.role_name}
    if a.identity_file:
        body["identity_file"] = a.identity_file
    if a.password_file:
        body["password_file"] = a.password_file
    out = c.post("/databases/create-role", body, timeout=120)
    if out.get("ok") and out.get("password_file"):
        key = f" --identity-file {a.identity_file}" if a.identity_file else ""
        out["next"] = (f"`apcat save --ssh-target {a.ssh_target} --ssh-port {a.ssh_port}{key} "
                       f"--db-user {out.get('role_name', a.role_name)} --password-file "
                       f"{out['password_file']} --database {a.database} --port {a.port}`, "
                       "then `apcat check`.")
    elif out.get("outcome") in ("exists_no_file", "no_sudo", "role_privileged"):
        out["next"] = ("Tell the user why, and offer a role with another name (--role-name) or an "
                       "existing role with its password file or secret.")
    return out


def cmd_check(a: argparse.Namespace, c: ApiClient) -> Any:
    return c.post(f"/databases/{a.database_id}/check", timeout=120)


def cmd_run(a: argparse.Namespace, c: ApiClient) -> Any:
    body: dict[str, Any] = {"window_minutes": a.window, "use_llm": a.llm,
                            "price": not a.no_price, "allow_degraded": a.allow_degraded}
    for k in ("start_at", "label", "region"):
        if getattr(a, k):
            body[k] = getattr(a, k)
    if a.model:
        body["model_id"] = a.model
    out = c.post(f"/databases/{a.database_id}/assessments", body)
    out["links"] = links(c, database_id=a.database_id, job_id=out.get("job_id"))
    out["expected_minutes"] = a.window
    return out


def _status_brief(s: dict[str, Any]) -> dict[str, Any]:
    keep = ("job_id", "status", "current_phase", "collection_window", "error", "note",
            "warnings", "duration_seconds", "available", "created_at", "completed_at")
    return {k: s.get(k) for k in keep if s.get(k) is not None}


def cmd_job(a: argparse.Namespace, c: ApiClient) -> Any:
    return _status_brief(c.get(f"/assessments/{a.job_id}/status"))


def cmd_jobs(a: argparse.Namespace, c: ApiClient) -> Any:
    return c.get("/assessments", query={"limit": a.limit, "database_id": a.database_id,
                                        "status": a.status})


def cmd_cancel(a: argparse.Namespace, c: ApiClient) -> Any:
    return c.post(f"/assessments/{a.job_id}/cancel")


def cmd_rerun(a: argparse.Namespace, c: ApiClient) -> Any:
    body: dict[str, Any] = {}
    if a.label:
        body["label"] = a.label
    if a.llm:
        body["use_llm"] = True
    if a.model:
        body["model_id"] = a.model
    if a.no_price:
        body["price"] = False
    if a.price:
        body["price"] = True
    if a.region:
        body["region"] = a.region
    out = c.post(f"/assessments/{a.job_id}/rerun", body or None)
    out["links"] = links(c, database_id=_job_database(c, out.get("job_id", "")), job_id=out.get("job_id"))
    return out


def cmd_delete(a: argparse.Namespace, c: ApiClient) -> Any:
    c.call("DELETE", f"/assessments/{a.job_id}")
    return {"deleted": a.job_id}


def _param(text: str) -> tuple[str, float]:
    name, _, value = text.partition("=")
    try:
        return name.strip(), float(value)
    except ValueError:
        raise CliError("invalid_request", f"--set takes name=number, got {text!r}") from None


def cmd_whatif(a: argparse.Namespace, c: ApiClient) -> Any:
    body: dict[str, Any] = {}
    if a.region:
        body["region"] = a.region
    if a.no_price:
        body["price"] = False
    if a.set:
        body["parameters"] = dict(_param(x) for x in a.set)
    return c.post(f"/assessments/{a.job_id}/sizing", body or None, timeout=120)


def cmd_wait(a: argparse.Namespace, c: ApiClient) -> Any:
    end = time.monotonic() + a.timeout
    while True:
        s = c.get(f"/assessments/{a.job_id}/status")
        if s.get("status") in TERMINAL or time.monotonic() >= end:
            out = _status_brief(s)
            out["done"] = s.get("status") in TERMINAL
            if not out["done"]:
                out["next"] = f"apcat wait {a.job_id}"
            return out
        time.sleep(min(POLL_SECONDS, max(1.0, end - time.monotonic())))


def cmd_results(a: argparse.Namespace, c: ApiClient) -> Any:
    j = a.job_id
    if a.query:
        return c.get(f"/assessments/{j}/report-data/queries/{a.query}")
    if a.table:
        return c.get(f"/assessments/{j}/report-data/tables/{a.table}")
    page = {"limit": a.limit, "offset": a.offset, "verdict": a.verdict,
            "recommended": True if a.recommended else None}
    if a.queries:
        return c.get(f"/assessments/{j}/report-data/queries", query=page)
    if a.tables:
        return c.get(f"/assessments/{j}/report-data/tables", query=page)
    summary = c.get(f"/assessments/{j}/summary")
    overview = c.get(f"/assessments/{j}/report-data/overview")
    lk = links(c, database_id=_job_database(c, j), job_id=j)
    if a.overview or a.full:
        if not a.full:  # reference text, the same on every report: kept out of the agent's context
            for k in BULKY_OVERVIEW_KEYS:
                overview.pop(k, None)
        return {"summary": summary, "overview": overview, "links": lk}
    return brief(c, j, summary, overview, lk)


def brief(c: ApiClient, j: str, summary: dict[str, Any], overview: dict[str, Any],
          lk: dict[str, Any]) -> dict[str, Any]:
    """The default `apcat results`: what an agent needs to summarize, in a few KB."""
    rec = {"recommended": True, "limit": BRIEF_ITEMS}
    queries = c.get(f"/assessments/{j}/report-data/queries", query=rec)
    tables = c.get(f"/assessments/{j}/report-data/tables", query=rec)
    sizing = summary.get("sizing") or {}
    return {
        "summary": {k: v for k, v in summary.items() if v is not None and k != "sizing"},
        "headline": overview.get("headline"),
        "at_a_glance": [{k: g.get(k) for k in ("label", "display", "note")} for g in overview.get("at_a_glance") or []],
        "keep_in_mind": overview.get("keep_in_mind"),
        "counts": overview.get("counts"),
        "sizing": sizing or None,
        "recommended_queries": {"total": queries.get("total"), "items": [
            {k: q.get(k) for k in ("queryid", "cache_type", "verdict", "sql_short", "suggested_cache_key",
                                   "db_time_share_pct")} for q in queries.get("items", [])]},
        "recommended_tables": {"total": tables.get("total"), "items": [
            {k: t.get(k) for k in ("table_id", "verdict", "caching_strategy", "size_mb")}
            for t in tables.get("items", [])]},
        "more": (f"apcat results {j} --queries|--tables [--recommended], --query ID, --table "
                 "SCHEMA.TABLE for detail; --overview for the whole overview"),
        "links": lk,
    }


def _job_database(c: ApiClient, job_id: str) -> str | None:
    """The saved database a job belongs to (the status route does not carry it)."""
    for item in c.get("/assessments", query={"q": job_id, "limit": 20}).get("items", []):
        if item.get("job_id") == job_id:
            return item.get("database_id")
    return None


def links(c: ApiClient, *, database_id: str | None = None, job_id: str | None = None) -> dict[str, Any]:
    """Browser links: the console when it runs, and the report the API serves."""
    ui = servers.ui_url()
    ui_ok = servers.ui_running()
    out: dict[str, Any] = {"console_running": ui_ok, "console": ui + "/#/"}
    if database_id:
        out["database"] = f"{ui}/#/databases/{database_id}"
    if job_id:
        out["assessment"] = (f"{ui}/#/databases/{database_id}/assessments/{job_id}"
                             if database_id else f"{ui}/#/assessments/{job_id}")
        out["report_html"] = f"{c.base}/api/v1/assessments/{job_id}/report"
        out["report_file"] = str(servers.artifact_root() / job_id / "report" / "report.html")
    if not ui_ok:
        out["note"] = "The web console is not running: `apcat start` starts it. report_html works without it."
    return out


def cmd_links(a: argparse.Namespace, c: ApiClient) -> Any:
    db = a.database_id
    if a.job_id and not db:
        db = _job_database(c, a.job_id)
    return links(c, database_id=db, job_id=a.job_id)


def cmd_handoff(a: argparse.Namespace, c: ApiClient) -> Any:
    lk = links(c, database_id=_job_database(c, a.job_id), job_id=a.job_id)
    lk = {k: v for k, v in lk.items() if k in ("database", "assessment", "report_html")}
    return handoff.write(c, a.job_id, project=a.project, include_sql=a.include_sql,
                         dry_run=a.dry_run, links=lk)


def cmd_agent(a: argparse.Namespace, _c: ApiClient | None) -> Any:
    names = agents.parse_agents(a.agents)
    project = a.project
    if a.action == "install":
        return agents.install(names, project=project, force=a.force)
    if a.action == "uninstall":
        return agents.uninstall(names, project=project)
    return agents.status(names, project=project)


# --- parser ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apcat",
        description="Would a cache (Amazon ElastiCache for Valkey) in front of this "
                    "PostgreSQL database help, and for which queries and tables? "
                    "Every command prints JSON.",
        epilog="Coding agents: run `apcat guide` first.")
    p.add_argument("--version", action="version", version=f"apcat {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def add(name: str, fn: Any, help_: str, *, api: bool = True) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn, needs_api=api)
        return sp

    add("guide", cmd_guide, "Print the playbook coding agents follow", api=False)
    sp = add("start", cmd_start, "Start the local API and web console (127.0.0.1 only)", api=False)
    sp.add_argument("--no-ui", action="store_true", help="Start the API only")
    sp.add_argument("--restart", action="store_true",
                    help="Restart the API apcat started, to pick up changed settings")
    sp = add("config", cmd_config, "Show or change the settings apcat starts the API with", api=False)
    sp.add_argument("action", nargs="?", choices=["show", "set", "unset"], default="show")
    sp.add_argument("name", nargs="?", choices=list(settings.SETTINGS))
    sp.add_argument("value", nargs="?")

    sp = add("hosts", cmd_hosts, "Hosts to suggest: saved databases, recent probes, ssh_config")
    sp.add_argument("--all", action="store_true", help="Include git and code hosts")
    add("keys", cmd_keys, "SSH keys in the key folder, by name and fingerprint")
    for name, fn, help_ in (
            ("probe", cmd_probe, "Check SSH to a host step by step, list its PostgreSQL clusters "
             "and, with sudo as postgres, its databases"),
            ("trust", cmd_trust, "Trust a host key after the user confirmed its fingerprint "
                                 "(adds it to known_hosts; never replaces a changed key)")):
        sp = add(name, fn, help_)
        sp.add_argument("--host", required=True, help="user@host, a host, or an ssh_config name")
        sp.add_argument("--ssh-port", type=int, default=22)
        sp.add_argument("--identity-file", help="Key file name in the key folder (apcat keys)")
        if name == "trust":
            sp.add_argument("--fingerprint", required=True, help="SHA256:... as the user confirmed it")
    add("status", cmd_status, "Whether the API and web console are running")
    add("stop", cmd_stop, "Stop the servers apcat start started", api=False)

    sp = add("discover", cmd_discover, "Find PostgreSQL databases that could be assessed "
             "(reads no password, connects to no database)")
    sp.add_argument("--host", help="Also look on this host over SSH (user@host or ssh_config name)")
    sp.add_argument("--ssh-port", type=int, default=22)
    sp.add_argument("--identity-file", help="Key file name in SSH_KEY_DIR")
    sp.add_argument("--host-only", action="store_true", help="With --host, skip the local sources")

    add("databases", cmd_databases, "List saved databases")
    sp = add("database", cmd_database, "Show one saved database")
    sp.add_argument("database_id")

    sp = add("save", cmd_save, "Save a database connection. Takes where the password is "
             "kept, never the password.")
    sp.add_argument("--ssh-target", required=True, help="The database host: user@host or an ssh_config name")
    sp.add_argument("--ssh-port", type=int, default=22)
    sp.add_argument("--identity-file", help="Key file name in SSH_KEY_DIR")
    sp.add_argument("--db-user", required=True, help="The read-only PostgreSQL role")
    sp.add_argument("--password-file", help="Absolute path ON the database host of a file holding the password")
    sp.add_argument("--password-secret-arn", help="Secrets Manager secret ARN holding the password")
    sp.add_argument("--endpoint", default="127.0.0.1", help="PostgreSQL's address as seen from the SSH host")
    sp.add_argument("--port", type=int, default=5432)
    sp.add_argument("--database", required=True, help="Database name")
    sp.add_argument("--schemas", help="Comma-separated schemas (default: every user schema)")
    sp.add_argument("--label")
    sp.add_argument("--discovered-from", choices=["local_server", "ssh_config", "pg_service",
                                                  "earlier_assessment", "host_probe"])

    sp = add("edit", cmd_edit, "Change a saved database's connection or label (then check it again)")
    sp.add_argument("database_id")
    sp.add_argument("--ssh-target")
    sp.add_argument("--ssh-port", type=int)
    sp.add_argument("--identity-file")
    sp.add_argument("--db-user")
    sp.add_argument("--password-file")
    sp.add_argument("--password-secret-arn")
    sp.add_argument("--endpoint")
    sp.add_argument("--port", type=int)
    sp.add_argument("--database")
    sp.add_argument("--schemas", help="Comma-separated; empty string for every user schema")
    sp.add_argument("--label")
    sp = add("forget", cmd_forget, "Delete a saved database (its assessments are kept)")
    sp.add_argument("database_id")

    sp = add("create-role", cmd_create_role, "Create (or reuse) the read-only role on the "
             "database host, over SSH with sudo -n as postgres. Changes the database: ask first.")
    sp.add_argument("--ssh-target", required=True)
    sp.add_argument("--ssh-port", type=int, default=22)
    sp.add_argument("--identity-file")
    sp.add_argument("--database", required=True)
    sp.add_argument("--port", type=int, default=5432)
    sp.add_argument("--role-name", default="apcat_reader")
    sp.add_argument("--password-file", help="Path on the host for the new password file "
                    "(default $HOME/.<role>_pw)")

    sp = add("check", cmd_check, "Check a saved database: SSH, sign-in, database host, pg_stat_statements")
    sp.add_argument("database_id")

    sp = add("run", cmd_run, "Start an assessment of a checked database")
    sp.add_argument("database_id")
    sp.add_argument("--window", type=int, required=True, help="Minutes to measure the workload")
    sp.add_argument("--start-at", help="ISO 8601 time with a zone, up to 24 hours ahead")
    sp.add_argument("--label")
    sp.add_argument("--llm", action="store_true", help="Add the AI review (Amazon Bedrock)")
    sp.add_argument("--model", help="Bedrock model or inference profile id for --llm")
    sp.add_argument("--no-price", action="store_true", help="Size without calling the AWS Pricing API")
    sp.add_argument("--region", help="Region to price in")
    sp.add_argument("--allow-degraded", action="store_true",
                    help="Run although pg_stat_statements is unavailable (confidence capped at 60)")

    sp = add("job", cmd_job, "Status of one assessment")
    sp.add_argument("job_id")
    sp = add("jobs", cmd_jobs, "Assessments, newest first")
    sp.add_argument("--database-id")
    sp.add_argument("--status")
    sp.add_argument("--limit", type=int, default=10)
    sp = add("cancel", cmd_cancel, "Stop a queued or running assessment")
    sp.add_argument("job_id")
    sp = add("rerun", cmd_rerun, "Analyze an assessment's collection again as a new job "
             "(for example with the AI review or pricing)")
    sp.add_argument("job_id")
    sp.add_argument("--label")
    sp.add_argument("--llm", action="store_true")
    sp.add_argument("--model")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--price", action="store_true")
    g.add_argument("--no-price", action="store_true")
    sp.add_argument("--region")
    sp = add("delete", cmd_delete, "Delete an assessment and its results")
    sp.add_argument("job_id")
    sp = add("whatif", cmd_whatif, "Size a finished assessment again with other settings; "
             "changes and saves nothing")
    sp.add_argument("job_id")
    sp.add_argument("--set", action="append", metavar="NAME=VALUE",
                    help="A sizing parameter, e.g. peak_factor=2 (repeatable)")
    sp.add_argument("--region")
    sp.add_argument("--no-price", action="store_true")
    sp = add("wait", cmd_wait, "Wait for an assessment to finish, up to --timeout seconds")
    sp.add_argument("job_id")
    sp.add_argument("--timeout", type=int, default=DEFAULT_WAIT_SECONDS)

    sp = add("results", cmd_results, "Results of a complete assessment (default: a brief summary "
             "with the top recommended items)")
    sp.add_argument("job_id")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--queries", action="store_true", help="The query list")
    g.add_argument("--tables", action="store_true", help="The table list")
    g.add_argument("--query", metavar="QUERYID", help="One query")
    g.add_argument("--table", metavar="SCHEMA.TABLE", help="One table")
    sp.add_argument("--recommended", action="store_true", help="Only recommended items")
    sp.add_argument("--verdict", help="STRONG_CANDIDATE, MODERATE_CANDIDATE, WEAK_CANDIDATE or NOT_RECOMMENDED")
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--offset", type=int, default=0)
    sp.add_argument("--overview", action="store_true", help="The summary and the whole overview")
    sp.add_argument("--full", action="store_true", help="--overview with the glossary and scoring explainer")

    sp = add("links", cmd_links, "Browser links for a database or an assessment")
    sp.add_argument("--database-id")
    sp.add_argument("--job-id")

    sp = add("handoff", cmd_handoff, "Write .elasticache/requirements.json from a complete "
             "assessment, for the AWS ElastiCache skill's setup and data-modeling steps")
    sp.add_argument("job_id")
    sp.add_argument("--project", help="Project folder (default: the current folder)")
    sp.add_argument("--include-sql", action="store_true",
                    help="Add each query's text (can hold literals; the file is in your project)")
    sp.add_argument("--dry-run", action="store_true", help="Print the document without writing it")

    sp = add("agent", cmd_agent, "Install the assessment skill into Claude Code, Codex and Kiro", api=False)
    sp.add_argument("action", choices=["install", "uninstall", "status"])
    sp.add_argument("--agents", default="claude,codex,kiro", help="Comma-separated: claude, codex, kiro")
    sp.add_argument("--project", help="Install into this project folder instead of your home folder")
    sp.add_argument("--force", action="store_true", help="Replace a file the tool did not write")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = ApiClient.from_env() if args.needs_api else None
        result = args.fn(args, client)
    except CliError as e:
        print(json.dumps(e.as_json(), indent=2))
        return 1
    except KeyboardInterrupt:
        return 130
    if isinstance(result, str):
        sys.stdout.write(result if result.endswith("\n") else result + "\n")
    else:
        print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
