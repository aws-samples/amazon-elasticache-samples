"""Prove a database role sees everything the collector reads, by comparing it with a superuser.

Runs the real collector once over SSH as the role under test. Every query it sends is also run
as the `postgres` superuser (`sudo -n -u postgres psql` on the database host), and the two
answers are compared. The report lists, per collector method:

  rows_missing   rows the superuser saw and the role did not
  hidden_values  values the superuser saw and the role got as NULL, empty or
                 '<insufficient privilege>' (information_schema hides some columns from a
                 role that does not own the table, without an error)
  errors         a query that failed for the role and not for the superuser

A difference between two live readings (a counter that moved between the two queries) is not a
privilege gap and is not reported: only rows that vanish and values that are blanked are.

Needs passwordless sudo to postgres for the SSH user, which is a test-host arrangement.
Read-only on the database: it runs only the collector's SELECTs.

Usage:
    python scripts/scale/privilege_diff.py --ssh-target ec2-user@13.218.48.29 \
        --identity-file ~/.apcat-live/keys/apcat_live --known-hosts ~/.apcat-live/known_hosts \
        --db-user apcat_reader --password-file /home/ec2-user/.apcat-reader-password \
        --database marketplace --out /tmp/privilege_diff.json
"""

from __future__ import annotations

import argparse
import inspect
import json
import shlex
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.tools.database.ssh_executor import SshPsqlExecutor  # noqa: E402
from src.tools.database.tabular import PSQL_FORMAT_ARGS  # noqa: E402

# Columns that name a row, used to pair the role's rows with the superuser's.
IDENTITY_COLUMNS = ("schema_name", "schemaname", "table_name", "relname", "view_name",
                    "column_name", "attname", "queryid", "name", "index_name", "position",
                    "ordinal_position", "wait_event_type", "wait_event", "blocked_pid")
HIDDEN_MARKERS = (None, "", "<insufficient privilege>")
# Method names in postgres_tools that issue queries, so a call can be named by its caller.
METHOD_PREFIXES = ("collect_", "get_", "probe_", "discover_", "count_")


class SuperuserExecutor(SshPsqlExecutor):
    """Runs SQL as the postgres superuser over the local socket on the SSH host."""

    def run_sql(self, host: str, port: int, database: str, sql: str = "",
                timeout: int = 300) -> str:
        """Run SQL through `sudo -n -u postgres psql`; host and port are ignored (socket).

        Args:
            host: Unused; the socket is used.
            port: Unused.
            database: Database name.
            sql: The statement.
            timeout: Seconds.

        Returns:
            psql's raw output, in the collector's format.
        """
        psql = " ".join(shlex.quote(a) for a in (
            "sudo", "-n", "-u", "postgres", self.psql_path, "-d", database,
            *PSQL_FORMAT_ARGS, "-c", sql))
        return self._ssh(psql, timeout, None)


def caller_method() -> str:
    """The postgres_tools method that issued the current query, or 'unknown'."""
    for frame in inspect.stack():
        if frame.filename.endswith("postgres_tools.py") and frame.function.startswith(
                METHOD_PREFIXES):
            return frame.function
    return "unknown"


def _key(row: dict, columns: tuple[str, ...]) -> tuple:
    return tuple(str(row.get(c)) for c in columns)


def compare_rows(reader: list[dict], superuser: list[dict]) -> dict[str, Any]:
    """Compare one query's answer for the role with the superuser's.

    Rows are paired on the identity columns both answers have. A query with none (a single
    row of totals) is paired by position.

    Args:
        reader: The role's rows.
        superuser: The superuser's rows.

    Returns:
        {"reader_rows", "superuser_rows", "rows_missing", "hidden_values"} where
        rows_missing counts superuser rows with no partner and hidden_values maps a column
        to how many paired rows had it blanked for the role.
    """
    columns = sorted(set().union(*[r.keys() for r in superuser]) if superuser else set())
    ids = tuple(c for c in IDENTITY_COLUMNS if c in columns)
    if ids:
        mine = {}
        for r in reader:
            mine.setdefault(_key(r, ids), []).append(r)
        pairs, missing = [], 0
        for s in superuser:
            bucket = mine.get(_key(s, ids))
            if bucket:
                pairs.append((bucket.pop(), s))
            else:
                missing += 1
    else:
        pairs = list(zip(reader, superuser, strict=False))
        missing = max(len(superuser) - len(reader), 0)
    hidden: dict[str, int] = {}
    for r, s in pairs:
        for c in columns:
            if s.get(c) not in HIDDEN_MARKERS and r.get(c) in HIDDEN_MARKERS:
                hidden[c] = hidden.get(c, 0) + 1
    return {"reader_rows": len(reader), "superuser_rows": len(superuser),
            "rows_missing": missing, "hidden_values": hidden}


def merge(report: dict[str, dict], method: str, result: dict[str, Any]) -> None:
    """Add one query's comparison to the per-method report.

    Args:
        report: method -> totals, updated in place.
        method: The collector method that issued the query.
        result: compare_rows output, or {"error": ...}.
    """
    entry = report.setdefault(method, {"queries": 0, "reader_rows": 0, "superuser_rows": 0,
                                       "rows_missing": 0, "hidden_values": {}, "errors": []})
    entry["queries"] += 1
    if "error" in result:
        entry["errors"].append(result["error"])
        return
    for k in ("reader_rows", "superuser_rows", "rows_missing"):
        entry[k] += result[k]
    for c, n in result["hidden_values"].items():
        entry["hidden_values"][c] = entry["hidden_values"].get(c, 0) + n


class ComparingExecutor:
    """Gives the collector the role's answers, and compares each with the superuser's."""

    def __init__(self, reader: SshPsqlExecutor, superuser: SuperuserExecutor):
        """
        Args:
            reader: Executor for the role under test.
            superuser: Executor for the superuser.
        """
        self.reader, self.superuser = reader, superuser
        self.report: dict[str, dict] = {}

    def run_sql_json(self, host: str, port: int, database: str, sql: str = "",
                     timeout: int = 300) -> list[dict]:
        """Run the query both ways, record the comparison, return the role's rows."""
        method = caller_method()
        s_rows = self.superuser.run_sql_json(host, port, database, sql, timeout)
        try:
            r_rows = self.reader.run_sql_json(host, port, database, sql, timeout)
        except Exception as e:  # noqa: BLE001 - recorded, then raised for the collector
            merge(self.report, method, {"error": " ".join(str(e).split())[:200]})
            raise
        merge(self.report, method, compare_rows(r_rows, s_rows))
        return r_rows

    def run_sql(self, host: str, port: int, database: str, sql: str = "",
                timeout: int = 300) -> str:
        """Raw output as the role (the collector only uses run_sql_json)."""
        return self.reader.run_sql(host, port, database, sql, timeout)

    def run_command(self, command: str, timeout: int = 300) -> str:
        """Host commands run as the SSH user; they do not depend on the database role."""
        return self.reader.run_command(command, timeout)


def gaps(report: dict[str, dict]) -> list[str]:
    """One line per method that lost rows, had values hidden, or failed for the role."""
    out = []
    for method, e in sorted(report.items()):
        if e["rows_missing"]:
            out.append(f"{method}: {e['rows_missing']} of {e['superuser_rows']} rows missing")
        for c, n in sorted(e["hidden_values"].items()):
            out.append(f"{method}: column {c} hidden in {n} rows")
        for err in e["errors"]:
            out.append(f"{method}: error: {err}")
    return out


def main(argv: list[str] | None = None) -> int:
    """Run the collector through the comparing executor and print the report.

    Returns:
        0 when the role saw everything, 1 when there is a gap, 2 when the run failed.
    """
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--ssh-target", required=True)
    p.add_argument("--ssh-port", type=int, default=22)
    p.add_argument("--identity-file", type=Path)
    p.add_argument("--known-hosts", type=Path)
    p.add_argument("--db-user", required=True)
    p.add_argument("--password-file", required=True, help="Path ON the database host")
    p.add_argument("--database", required=True)
    p.add_argument("--endpoint", default="127.0.0.1")
    p.add_argument("--db-port", type=int, default=5432)
    p.add_argument("--query-limit", type=int, default=5000)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args(argv)

    from src.agents.collector.postgres_collector import collect
    from src.contracts.collector_input import CollectorInput

    common = {"ssh_target": a.ssh_target, "identity_file": a.identity_file,
              "ssh_port": a.ssh_port, "known_hosts": a.known_hosts}
    reader = SshPsqlExecutor(user=a.db_user, password_file=a.password_file, **common)
    superuser = SuperuserExecutor(user="postgres", password_file="/dev/null", **common)  # nosec B106 - a path, not a password: the superuser signs in by peer auth
    pair = ComparingExecutor(reader, superuser)
    inp = CollectorInput.model_validate({
        "job_id": "privilege-diff", "cluster_endpoint": a.endpoint, "port": a.db_port,
        "database_name": a.database, "mode": "live",
        "live_config": {"ssh_target": a.ssh_target, "ssh_port": a.ssh_port,
                        "db_user": a.db_user, "password_file": a.password_file},
        "collection_options": {"collect_host_metrics": False,
                               "query_pattern_limit": a.query_limit}})
    status = "complete"
    try:
        collect(inp, executor=pair)
    except Exception as e:  # noqa: BLE001 - the report still says what was compared
        status = f"failed: {type(e).__name__}: {' '.join(str(e).split())[:300]}"
    finally:
        reader.close()
        superuser.close()
    found = gaps(pair.report)
    result = {"role": a.db_user, "collection": status, "gaps": found, "methods": pair.report}
    a.out.write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps({"role": a.db_user, "collection": status, "gaps": found,
                      "methods_compared": len(pair.report)}, indent=2))
    if status != "complete":
        return 2
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
