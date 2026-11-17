#!/usr/bin/env python3
"""
Run a collection against PostgreSQL and write it to a file.

Collection only. Assessment is a separate step, scripts/run_assessment_v2.py,
and the report another, scripts/build_report_v2.py, so a saved collection can be
analysed again without touching the database.

Three ways to reach the database:

  --backend docker   psql inside a local container
  --backend binary   a local psql binary, against --host/--port
  --ssh TARGET       psql and host commands run ON the database host over SSH.
                     No database port is opened and nothing listens anywhere.
                     Host metrics then describe the database host. TARGET is
                     anything ssh accepts, including an ~/.ssh/config alias.

The password, in order of preference: --password-file (a file ON the database
host, so it never reaches this machine; --ssh only), --password on the command
line, the PGPASSWORD environment variable, or an interactive prompt.
--password is visible in shell history and to local `ps`; it never appears on
the remote host's command line.

Examples:
    # Remote host over SSH, password file kept on the host
    python scripts/collect_local.py --ssh my-db-host \
        --user apcat_reader --password-file /home/ec2-user/.apcat_reader_pw \
        --database retail --schema shop -o collection.json

    # Remote host over SSH, password on the command line
    python scripts/collect_local.py --ssh ec2-user@10.0.1.50 \
        --user apcat_reader --password 'secret' --database retail -o collection.json

    # Docker container (no psql needed on the host)
    python scripts/collect_local.py --container retail-order-store-pg \
        --user retail_admin --password retail_admin_pw --database retail_order_store

    # Write the collection, then assess it
    python scripts/collect_local.py ... -o collection.json
    python scripts/run_assessment_v2.py --file collection.json -o analysis.json
"""

import argparse
import getpass
import json
import logging
import os
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.collector.postgres_collector import CollectionIncomplete, collect  # noqa: E402
from src.contracts.collector_input import (  # noqa: E402
    CollectionOptions,
    CollectorInput,
    LiveConnectionConfig,
)
from src.tools.database.local_executor import LocalPsqlExecutor  # noqa: E402
from src.tools.database.ssh_executor import SshPsqlExecutor  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Collect from PostgreSQL, locally or over SSH",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["docker", "binary"], default="docker",
                    help="Local mode only; ignored with --ssh")
    ap.add_argument("--container", help="Container name (backend=docker)")
    ap.add_argument("--ssh", metavar="TARGET",
                    help="Collect over SSH from the database host: an ~/.ssh/config "
                         "alias, host, or user@host")
    ap.add_argument("--ssh-key", metavar="FILE", help="Private key for ssh -i")
    ap.add_argument("--ssh-port", type=int, default=22, help="SSH port (default 22)")
    ap.add_argument("--host", default=None,
                    help="Database host. Default localhost, or 127.0.0.1 as seen "
                         "from the SSH host with --ssh")
    ap.add_argument("--port", type=int, default=5432)
    ap.add_argument("--user", required=True)
    ap.add_argument(
        "--password",
        help="Password on the command line. Visible in shell history and local "
             "ps; prefer PGPASSWORD or --password-file.",
    )
    ap.add_argument("--password-file", metavar="REMOTE_PATH",
                    help="--ssh only: file on the database host holding the "
                         "password. It never reaches this machine.")
    ap.add_argument("--database", required=True)
    ap.add_argument("--job-id", default="local-collect")
    ap.add_argument(
        "--schema", action="append", dest="schemas", metavar="NAME",
        help="Limit collection to this schema. Repeatable. "
             "Omit to auto-discover every non-system schema.",
    )
    ap.add_argument("--no-host-metrics", action="store_true",
                    help="Skip OS metrics")
    ap.add_argument("--host-window", metavar="FILE",
                    help="Host window summary written by run_load.py "
                         "--host-window-out. Scoring then uses the busy period "
                         "instead of a snapshot taken after the load ended.")
    ap.add_argument("--remote-host-window", metavar="REMOTE_PATH",
                    help="--ssh only: host window file on the database host. It "
                         "is copied next to --output and attached.")
    ap.add_argument("--collect-only", action="store_true",
                    help="Accepted for compatibility and ignored: this script "
                         "only collects")
    ap.add_argument("-o", "--output", help="Write collection JSON to this path")
    ap.add_argument("--window-minutes", type=int, default=None, metavar="N",
                    help="Measure the statistics over the next N minutes (5 to 240) "
                         "instead of using the counters since the last reset. Run the "
                         "load during it.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.ssh and (args.password_file or args.remote_host_window):
        print("error: --password-file and --remote-host-window need --ssh",
              file=sys.stderr)
        return 2
    if not args.ssh and args.backend == "docker" and not args.container:
        print("error: --container is required with --backend docker", file=sys.stderr)
        return 2
    host = args.host or ("127.0.0.1" if args.ssh else "localhost")

    password = None
    if not args.password_file:
        password = args.password or os.environ.get("PGPASSWORD")
        if not password:
            password = getpass.getpass(f"Password for {args.user}: ")

    if args.ssh:
        # The host key must already be in known_hosts (SSH_KNOWN_HOSTS, else ~/.ssh/known_hosts).
        try:
            executor = SshPsqlExecutor(
                ssh_target=args.ssh, user=args.user, password=password,
                password_file=args.password_file, identity_file=args.ssh_key,
                ssh_port=args.ssh_port)
        except (ValueError, FileNotFoundError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        probe = executor.probe(host, args.port, args.database)
    else:
        executor = LocalPsqlExecutor(
            user=args.user, password=password, backend=args.backend,
            container=args.container)
        probe = executor.probe()
    if not probe.get("reachable"):
        print(f"error: cannot reach database: {probe.get('error', 'unknown')}",
              file=sys.stderr)
        return 1
    print(f"Connected via {probe['backend']}.")

    inp = CollectorInput(
        job_id=args.job_id,
        cluster_endpoint=host,
        port=args.port,
        database_name=args.database,
        live_config=LiveConnectionConfig(
            # The executor above already holds the real connection. These satisfy the
            # contract and are not used.
            ssh_target=args.ssh or "local",
            db_user=args.user,
            password_file=args.password_file or "/not/used",
        ),
        collection_options=CollectionOptions(
            collect_host_metrics=not args.no_host_metrics,
            schemas=args.schemas,  # None -> auto-discover
            window_minutes=args.window_minutes,
        ),
    )

    if args.window_minutes:
        print(f"Measuring a {args.window_minutes}-minute window: the statistics are read now "
              "and again at the end, and the host is sampled every 15 seconds.")
    print(f"Collecting from {args.database} ...")
    try:
        out = collect(inp, executor=executor)
    except CollectionIncomplete as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # The SSH connection stays open for the host-window copy below, and closes at exit.

    window = args.host_window
    if args.remote_host_window:
        dest = (Path(args.output).with_name("host_window.json") if args.output
                else Path("host_window.json"))
        err = fetch_remote_host_window(executor, args.remote_host_window, dest)
        if err:
            print(f"warning: {err}", file=sys.stderr)
        else:
            window = str(dest)
    if window:
        err = attach_host_window(out, window)
        if err:
            print(f"warning: host window not attached: {err}", file=sys.stderr)

    _print_collection_summary(out)

    if args.output:
        Path(args.output).write_text(json.dumps(out, indent=2, default=str))
        print(f"\nCollection artifact written to {args.output}")

    print("\nNext: run scripts/run_assessment_v2.py --file <collection.json> to assess it.")
    return 0


def fetch_remote_host_window(executor, remote_path: str, dest: Path) -> str | None:
    """Copy a host window file from the database host over SSH.

    Args:
        executor: An SshPsqlExecutor.
        remote_path: Path of the window JSON on the database host.
        dest: Local path to write it to.

    Returns:
        None on success, or an error message.
    """
    try:
        raw = executor.run_command(f"cat {shlex.quote(remote_path)}", timeout=60)
        json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - reported, collection continues
        return f"could not read {remote_path} on the host: {exc}"
    dest.write_text(raw)
    return None


def attach_host_window(out: dict, path: str) -> str | None:
    """Merge a sampled host window into a collection, with an overlap check.

    The window only helps if it describes the same period as the query
    statistics. It covers the load; the statistics cover reset to collection.
    The share of the statistics window the samples cover is recorded so a
    reader can tell how representative the host figures are.

    Args:
        out: Collector output, modified in place.
        path: Path to the window JSON from run_load.py.

    Returns:
        None on success, or an error message.
    """
    p = Path(path)
    if not p.exists():
        return f"{path} not found"
    try:
        window = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        return f"{path} is not valid JSON: {exc}"
    if not window.get("available"):
        return f"window has no usable samples: {window.get('reason') or window.get('errors')}"

    patterns = (out.get("queries") or {}).get("query_patterns") or []
    stats_window = max(
        (float(q.get("stats_window_seconds") or 0) for q in patterns), default=0.0
    )
    if stats_window > 0:
        window["stats_window_seconds"] = round(stats_window, 1)
        window["covers_stats_window_pct"] = round(
            min(float(window.get("window_seconds") or 0) / stats_window, 1.0) * 100, 1
        )
    host = out.get("host_metrics")
    if host is None:
        out["host_metrics"] = host = {}
    host["window"] = window
    return None


def _print_collection_summary(out: dict) -> None:
    md = out.get("metadata", {}).get("source_database", {})
    probe = out.get("pg_stat_statements_probe", {})
    tables = out.get("database_schema", {}).get("tables", [])
    queries = out.get("queries", {}).get("query_patterns", [])
    metrics = out.get("metrics", {})
    host = out.get("host_metrics", {})

    print("\n" + "-" * 72)
    print("COLLECTION SUMMARY")
    print("-" * 72)
    print(f"  PostgreSQL version   : {md.get('version')}")
    print(f"  Database size        : {md.get('database_size_gb')} GB")
    discovered = out.get("schemas_discovered") or []
    if discovered:
        desc = ", ".join(f"{s['schema_name']}({s['table_count']})" for s in discovered)
        print(f"  Schemas discovered   : {desc}")
    print(f"  Schemas collected    : {', '.join(out.get('schemas_collected') or [])}")
    print(f"  Tables collected     : {len(tables)}")
    print(f"  Query patterns       : {len(queries)}")
    print(f"  pg_stat_statements   : {'AVAILABLE' if probe.get('available') else 'UNAVAILABLE'}")
    if not probe.get("available"):
        print(f"      reason           : {probe.get('reason')}")
        print("      -> assessment will run in DEGRADED mode")

    g = metrics.get("global_stats") or {}
    if g:
        print(f"  Buffer hit ratio     : {g.get('cache_hit_ratio_pct')}%")
        print(f"  Active connections   : {g.get('active_connections')}")
    bg = metrics.get("bgwriter") or {}
    if bg:
        print(f"  Backend buffer writes: {bg.get('buffers_backend_pct')}%")
    print(f"  Wait event rows      : {len(metrics.get('wait_events') or [])}")
    print(f"  Lock contention rows : {len(metrics.get('lock_contention') or [])}")
    print(f"  Table churn rows     : {len(metrics.get('table_churn') or [])}")

    if host:
        cpu = host.get("cpu") or {}
        mem = host.get("memory") or {}
        print(f"  Host CPU             : {cpu.get('utilization_pct')}% "
              f"({cpu.get('vcpu_count')} vCPU)")
        print(f"  Host memory used     : {mem.get('used_pct')}% of {mem.get('total_gb')} GB")
        print(f"  sysstat available    : {host.get('sysstat_available')}")
        executor = str(host.get("executor_type") or "")
        where = ("the database host, over SSH" if executor.startswith("Ssh")
                 else "the local machine or container, NOT a dedicated DB host")
        print(f"  Metrics source       : {executor} ({where})")
        align = host.get("snapshot_alignment") or {}
        if align.get("representative") is False:
            print("  Snapshot alignment   : NOT representative")
            print(f"      {align.get('reason', '')[:100]}")
        win = host.get("window") or {}
        if win.get("available"):
            c = win.get("cpu_utilization_pct") or {}
            print(f"  Host window          : {win.get('sample_count')} samples over "
                  f"{win.get('window_seconds')}s, CPU mean {c.get('mean')}% "
                  f"p95 {c.get('p95')}%, covers "
                  f"{win.get('covers_stats_window_pct')}% of the stats window")
        for w in host.get("collection_warnings") or []:
            print(f"      warning          : {w[:90]}")

    big = sorted(tables, key=lambda t: t.get("size_mb") or 0, reverse=True)[:5]
    if big:
        print("\n  Largest tables:")
        for t in big:
            print(f"      {t['table_name'][:34]:<36} "
                  f"{t.get('size_mb', 0):>9.2f} MB  "
                  f"{t.get('row_count', 0):>10,} rows")

    _print_timings(out)


def _print_timings(out: dict) -> None:
    """Print per-stage collection timings, slowest first.

    Args:
        out: Collector output dict.
    """
    md = out.get("metadata", {})
    stages = md.get("stage_timings_seconds") or {}
    total = md.get("collection_duration_seconds")
    if not stages:
        return

    print("\n" + "-" * 72)
    print("COLLECTION TIMING")
    print("-" * 72)
    print(f"  Total: {total}s   "
          f"(measured {md.get('stage_timings_accounted_seconds')}s, "
          f"other {md.get('stage_timings_unaccounted_seconds')}s)")
    print()
    for stage, secs in stages.items():
        pct = (secs / total * 100) if total else 0
        bar = "#" * max(1, int(pct / 2.5))
        print(f"  {stage:<30}{secs:>7.3f}s  {pct:>5.1f}%  {bar}")

    host = out.get("host_metrics") or {}
    probes = host.get("probe_timings_seconds") or {}
    if probes:
        print("\n  Host metric probes:")
        for name, secs in sorted(probes.items(), key=lambda kv: kv[1], reverse=True):
            print(f"      {name:<24}{secs:>7.3f}s")
        note = host.get("probe_timings_note")
        if note:
            print(f"      note: {note}")


if __name__ == "__main__":
    raise SystemExit(main())
