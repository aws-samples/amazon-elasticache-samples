#!/usr/bin/env python3
"""
Run a weighted mixed read/write load against PostgreSQL using pgbench.

Resets pg_stat_statements first so the measurement window covers only the
generated load, then reports the achieved read/write split from server-side
statistics rather than trusting the configured weights.

Usage:
    python scripts/loadgen/run_load.py --container retail-order-store-pg \
        --user retail_admin --database retail_order_store \
        --clients 16 --jobs 4 --duration 120
"""

import argparse
import json
import os
import re
import subprocess  # nosec B404 — local tooling, argv lists only, never shell=True
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from src.tools.database.host_sampler import HostSampler  # noqa: E402


class _ContainerShell:
    """Minimal executor for HostSampler: runs a shell command in the container."""

    def __init__(self, container: str):
        self.container = container

    def run_command(self, command: str, timeout: int = 30) -> str:
        """Run a command inside the container, raising on failure."""
        rc, out, err = run(["docker", "exec", self.container, "sh", "-c", command],
                           timeout=timeout)
        if rc != 0:
            raise RuntimeError(f"rc={rc}: {err.strip()[:200]}")
        return out

# Script weights. Reads total 70, writes total 30.
SCRIPTS: list[tuple[str, int, str]] = [
    ("r_simple.sql", 35, "simple read"),
    ("r_medium.sql", 25, "medium read"),
    ("r_complex.sql", 10, "complex read"),
    ("w_simple.sql", 18, "simple write"),
    ("w_medium.sql", 12, "medium write"),
]

CONTAINER_TMP = "/tmp/loadgen"


def run(argv: list[str], timeout: int = 900) -> tuple[int, str, str]:
    """Run a command as an argv list.

    Args:
        argv: Command and arguments.
        timeout: Seconds before giving up.

    Returns:
        (returncode, stdout, stderr)
    """
    try:
        p = subprocess.run(  # nosec B603 - argv list, no shell  # nosemgrep: dangerous-subprocess-use-audit
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"


def psql(container: str, user: str, db: str, password: str, sql: str) -> tuple[int, str, str]:
    """Execute SQL in the container via psql."""
    return run([
        "docker", "exec", "-e", f"PGPASSWORD={password}", container,
        "psql", "-U", user, "-d", db, "-t", "-A", "-F", "|", "-c", sql,
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a mixed read/write load")
    ap.add_argument("--container", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--database", required=True)
    ap.add_argument("--clients", type=int, default=16, help="Concurrent connections")
    ap.add_argument("--jobs", type=int, default=4, help="pgbench worker threads")
    ap.add_argument("--duration", type=int, default=120, help="Seconds to run")
    ap.add_argument("--no-reset", action="store_true",
                    help="Do not reset pg_stat_statements first")
    ap.add_argument("--host-window-out", metavar="FILE",
                    help="Sample host CPU/memory during the load and write the "
                         "window summary here. Pass it to collect_local.py "
                         "--host-window so scoring uses the busy period rather "
                         "than an after-the-fact snapshot.")
    ap.add_argument("--sample-interval", type=float, default=5.0,
                    help="Seconds between host samples")
    args = ap.parse_args()

    password = os.environ.get("PGPASSWORD")
    if not password:
        print("error: set PGPASSWORD", file=sys.stderr)
        return 2

    # --- Preconditions -------------------------------------------------
    rc, out, err = psql(
        args.container, args.user, args.database, password,
        "SELECT count(*) FROM pg_extension WHERE extname='pg_stat_statements'",
    )
    if rc != 0:
        print(f"error: cannot reach database: {err.strip()[:200]}", file=sys.stderr)
        return 1
    if out.strip() != "1":
        print("error: pg_stat_statements is not installed in this database.",
              file=sys.stderr)
        print("       The load would run but produce no per-query statistics.",
              file=sys.stderr)
        return 1

    # --- Copy scripts into the container -------------------------------
    run(["docker", "exec", args.container, "mkdir", "-p", CONTAINER_TMP])
    for name, _w, _label in SCRIPTS:
        src = HERE / name
        if not src.exists():
            print(f"error: missing script {src}", file=sys.stderr)
            return 1
        rc, _o, err = run([
            "docker", "cp", str(src), f"{args.container}:{CONTAINER_TMP}/{name}"
        ])
        if rc != 0:
            print(f"error: docker cp failed for {name}: {err[:200]}", file=sys.stderr)
            return 1

    # --- Reset statistics so the window is clean ----------------------
    if not args.no_reset:
        psql(args.container, args.user, args.database, password,
             "SELECT pg_stat_statements_reset()")
        psql(args.container, args.user, args.database, password,
             "SELECT pg_stat_reset()")
        print("Reset pg_stat_statements and pg_stat_* counters.")

    # --- Build and run pgbench ----------------------------------------
    total_weight = sum(w for _n, w, _l in SCRIPTS)
    read_weight = sum(w for n, w, _l in SCRIPTS if n.startswith("r_"))
    print(f"\nWorkload: {args.clients} clients, {args.jobs} jobs, "
          f"{args.duration}s, target {read_weight}/{total_weight - read_weight} read/write")
    for name, w, label in SCRIPTS:
        print(f"  {label:<14} weight {w:>3}  ({w / total_weight:.0%})  {name}")

    argv = [
        "docker", "exec", "-e", f"PGPASSWORD={password}", args.container,
        "pgbench", "-U", args.user, "-d", args.database,
        "-c", str(args.clients), "-j", str(args.jobs), "-T", str(args.duration),
        "-n",  # no vacuum: this is not the standard pgbench schema
        "--no-vacuum",
        "-P", "15",  # progress every 15s
    ]
    for name, w, _label in SCRIPTS:
        argv += ["-f", f"{CONTAINER_TMP}/{name}@{w}"]

    sampler = None
    if args.host_window_out:
        sampler = HostSampler(_ContainerShell(args.container),
                              interval_sec=args.sample_interval)
        sampler.start()
        print(f"Sampling host every {args.sample_interval}s during the load.")

    print("\nRunning...\n")
    load_started = time.time()
    rc, out, err = run(argv, timeout=args.duration + 300)
    load_ended = time.time()

    if sampler is not None:
        window = sampler.stop()
        window["load_started_epoch"] = round(load_started, 1)
        window["load_ended_epoch"] = round(load_ended, 1)
        window["load_seconds"] = round(load_ended - load_started, 1)
        window["stats_reset_before_load"] = not args.no_reset
        Path(args.host_window_out).write_text(json.dumps(window, indent=2))
        cpu = window.get("cpu_utilization_pct") or {}
        print(f"\nHost window: {window.get('sample_count')} samples, "
              f"CPU mean {cpu.get('mean')}% p95 {cpu.get('p95')}% "
              f"max {cpu.get('max')}% -> {args.host_window_out}")
    print(out.strip() or "(no stdout)")
    if err.strip():
        # pgbench writes progress to stderr; only the tail is interesting.
        tail = "\n".join(err.strip().split("\n")[-12:])
        print(f"\n[pgbench stderr tail]\n{tail}")
    if rc != 0:
        print(f"\nwarning: pgbench exited {rc}", file=sys.stderr)

    _report_actual_mix(args, password)
    return 0 if rc == 0 else 1


def _report_actual_mix(args, password: str) -> None:
    """Report the achieved read/write split from pg_stat_statements.

    Configured weights are the intent. This is what actually executed, which is
    what the assessment will score.
    """
    sql = """
    WITH classified AS (
      SELECT calls,
             CASE WHEN query ~* '^\\s*(SELECT|WITH)' THEN 'read' ELSE 'write' END AS kind
      FROM pg_stat_statements
      WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
    )
    SELECT kind, sum(calls) FROM classified GROUP BY kind ORDER BY kind
    """
    rc, out, _err = psql(args.container, args.user, args.database, password, sql)
    if rc != 0 or not out.strip():
        return

    counts: dict[str, int] = {}
    for line in out.strip().split("\n"):
        parts = line.split("|")
        if len(parts) == 2:
            try:
                counts[parts[0].strip()] = int(parts[1])
            except ValueError:
                continue

    total = sum(counts.values())
    if not total:
        return

    reads = counts.get("read", 0)
    writes = counts.get("write", 0)
    print("\n" + "-" * 62)
    print("ACHIEVED MIX (server-side, from pg_stat_statements)")
    print("-" * 62)
    print(f"  statements executed : {total:,}")
    print(f"  reads               : {reads:,} ({reads / total:.1%})")
    print(f"  writes              : {writes:,} ({writes / total:.1%})")
    print("\n  Note: this counts STATEMENTS, not transactions. A write script")
    print("  contains several statements, so the statement-level split differs")
    print("  from the configured script weights. The assessment scores")
    print("  statement-level call volume, which is what matters for caching.")

    # Surface the heaviest statements as a sanity check.
    rc, out, _e = psql(
        args.container, args.user, args.database, password,
        "SELECT calls, round(mean_exec_time::numeric,2), "
        "round(100.0*shared_blks_hit/greatest(shared_blks_hit+shared_blks_read,1),1), "
        "left(regexp_replace(query, '\\s+', ' ', 'g'), 62) "
        "FROM pg_stat_statements "
        "WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database()) "
        "ORDER BY total_exec_time DESC LIMIT 8",
    )
    if rc == 0 and out.strip():
        print("\n  Top statements by total time:")
        print(f"    {'calls':>8} {'avg ms':>8} {'hit%':>6}  query")
        for line in out.strip().split("\n"):
            p = line.split("|")
            if len(p) >= 4:
                print(f"    {p[0]:>8} {p[1]:>8} {p[2]:>6}  {p[3][:58]}")


if __name__ == "__main__":
    raise SystemExit(main())
