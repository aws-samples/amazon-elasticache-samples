"""Phase 2 of the large-workload test: collect through the API over real SSH, and count what arrived.

Submits `POST /assessments` (a live job, phases=collect only) to an API already running on
loopback, waits for it, downloads the collection and prints how much of it is there. Compare
the counts with what the database holds (see --expect-*) to see whether anything was lost.

The API process needs SSH_KNOWN_HOSTS (with the host's key in it) and, for a key chosen by
name, SSH_KEY_DIR. Nothing secret is passed here: the password stays in a file on the host.

Usage:
    python scripts/scale/live_check.py --port 8765 \
        --ssh-target ec2-user@10.0.1.50 --identity-file apcat_ed25519 \
        --db-user apcat_ro --password-file /home/ec2-user/.apcat-db-password \
        --database marketplace --expect-tables 528 --expect-stats-rows 6177 --out /tmp/live.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HOST = "127.0.0.1"
BASE = "/api/v1"
POLL_SECONDS = 5
DONE = {"complete", "failed", "cancelled", "interrupted"}


def summarize(collection: dict) -> dict:
    """Count what a collection holds.

    Args:
        collection: The `collect/output.json` content.

    Returns:
        Counts of tables, tables with columns, column statistics and widths, and query
        patterns, plus the stage record the collector wrote. Works on a partial collection.
    """
    tables = (collection.get("database_schema") or {}).get("tables") or []
    queries = collection.get("queries") or {}
    patterns = queries.get("query_patterns") or []
    meta = collection.get("metadata") or {}
    stages = meta.get("stages") or {}
    return {
        "tables": len(tables),
        "tables_with_columns": sum(1 for t in tables if t.get("columns")),
        "columns": sum(len(t.get("columns") or []) for t in tables),
        "tables_with_column_stats": sum(1 for t in tables if t.get("column_stats")),
        "column_stats_rows": sum(len(t.get("column_stats") or {}) for t in tables),
        "tables_with_column_widths": sum(1 for t in tables if t.get("column_widths")),
        "tables_with_primary_key": sum(1 for t in tables if t.get("primary_key")),
        "query_patterns": len(patterns),
        "total_queries_analyzed": queries.get("total_queries_analyzed"),
        "query_patterns_available_after_filter": queries.get("available_after_filter"),
        "query_patterns_dropped_by_limit": queries.get("dropped_by_limit"),
        "views": len((collection.get("database_schema") or {}).get("views") or []),
        "table_churn_rows": len((collection.get("metrics") or {}).get("table_churn") or []),
        "probe_available": (collection.get("pg_stat_statements_probe") or {}).get("available"),
        "executor_type": (collection.get("host_metrics") or {}).get("executor_type"),
        "host_metrics_method": (collection.get("host_metrics") or {}).get("collection_method"),
        "duration_seconds": meta.get("collection_duration_seconds"),
        "failed_stages": sorted(s for s, v in stages.items() if v.get("status") != "ok"),
        "warnings": meta.get("warnings") or [],
        "slowest_stages": dict(list((meta.get("stage_timings_seconds") or {}).items())[:6]),
    }


def verdict(found: dict, expect_tables: int | None, expect_stats_rows: int | None) -> list[str]:
    """Compare counts with what the database holds.

    Args:
        found: What `summarize` returned.
        expect_tables: Tables the database has in the collected schemas, or None to skip.
        expect_stats_rows: Rows of pg_stats for those tables, or None to skip.

    Returns:
        One line per mismatch. An empty list means everything expected arrived.
    """
    problems = []
    if expect_tables is not None:
        for key in ("tables", "tables_with_columns", "tables_with_primary_key"):
            if found[key] != expect_tables:
                problems.append(f"{key}: found {found[key]}, expected {expect_tables}")
    if expect_stats_rows is not None and found["column_stats_rows"] != expect_stats_rows:
        problems.append(f"column_stats_rows: found {found['column_stats_rows']}, "
                        f"expected {expect_stats_rows}")
    return problems


def call(port: int, method: str, path: str, body: Any = None, timeout: int = 900) -> dict:
    """One HTTP call to the loopback API.

    Args:
        port: API port.
        method: GET or POST.
        path: Path including `/api/v1`.
        body: JSON-able body or None.
        timeout: Seconds to wait.

    Returns:
        {"status", "json"} for any HTTP answer, or {"error"} when none came.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://{HOST}:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - http://127.0.0.1 only, built above
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return {"error": f"{method} {path}: {e}"}
    try:
        return {"status": status, "json": json.loads(raw) if raw else None}
    except ValueError:
        return {"status": status, "json": None}


def main(argv: list[str] | None = None) -> int:
    """Submit the live job, wait, and report.

    Args:
        argv: Arguments. Defaults to `sys.argv[1:]`.

    Returns:
        0 when the job completed and every expectation held, 1 when counts differ or the job
        did not complete, 2 for a bad argument or an unreachable API.
    """
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--ssh-target", required=True, help="The database host, user@host")
    p.add_argument("--ssh-port", type=int, default=22)
    p.add_argument("--identity-file", help="Name of a key in the API's SSH_KEY_DIR")
    p.add_argument("--db-user", required=True)
    p.add_argument("--password-file", required=True, help="Path ON the database host")
    p.add_argument("--database", required=True)
    p.add_argument("--endpoint", default="127.0.0.1", help="DB host as seen from the SSH host")
    p.add_argument("--db-port", type=int, default=5432)
    p.add_argument("--query-limit", type=int, default=1000)
    p.add_argument("--min-calls", type=int, default=10)
    p.add_argument("--expect-tables", type=int)
    p.add_argument("--expect-stats-rows", type=int)
    p.add_argument("--max-wait", type=int, default=3600)
    p.add_argument("--out", required=True, type=Path)
    a = p.parse_args(argv)

    live = {"ssh_target": a.ssh_target, "ssh_port": a.ssh_port, "db_user": a.db_user,
            "password_file": a.password_file}
    if a.identity_file:
        live["identity_file"] = a.identity_file
    body = {
        "job_id": f"live-ssh-{int(time.time())}", "cluster_endpoint": a.endpoint,
        "port": a.db_port, "database_name": a.database, "mode": "live",
        "live_config": live,
        "collection_options": {"query_pattern_limit": a.query_limit,
                               "min_query_calls": a.min_calls},
    }
    sub = call(a.port, "POST", f"{BASE}/assessments?phases=collect&use_llm=false&price=false",
               body)
    if "error" in sub or sub.get("status") != 200:
        print(f"submit failed: {sub}", file=sys.stderr)
        return 2
    job = sub["json"]["job_id"]
    t0 = time.time()
    state = "queued"
    while time.time() - t0 < a.max_wait:
        st = call(a.port, "GET", f"{BASE}/assessments/{job}/status")
        state = (st.get("json") or {}).get("status", "unknown")
        if state in DONE:
            break
        time.sleep(POLL_SECONDS)
    result: dict[str, Any] = {"job_id": job, "state": state,
                              "wall_seconds": round(time.time() - t0, 1)}
    if state != "complete":
        result["status_body"] = (call(a.port, "GET", f"{BASE}/assessments/{job}/status")
                                 .get("json"))
        a.out.write_text(json.dumps(result, indent=2, default=str))
        print(json.dumps(result, indent=2, default=str))
        return 1
    coll = call(a.port, "GET", f"{BASE}/assessments/{job}/collection")
    if coll.get("status") != 200:
        print(f"could not read the collection: {coll}", file=sys.stderr)
        return 1
    result["found"] = summarize(coll["json"])
    result["mismatches"] = verdict(result["found"], a.expect_tables, a.expect_stats_rows)
    a.out.write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result, indent=2, default=str))
    return 1 if result["mismatches"] else 0


if __name__ == "__main__":
    sys.exit(main())
