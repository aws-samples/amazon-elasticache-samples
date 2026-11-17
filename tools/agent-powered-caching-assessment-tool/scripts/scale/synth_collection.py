"""Build a synthetic collector artifact of any size, with no database.

The API and the analysis stage only read a collection (`collect/output.json`). This makes
one of the same shape as the real collector writes, so a very large workload can be pushed
through `POST /assessments/from-collection` without a database, a host or any AWS call.

The numbers are made up but the shape is not: field names and types follow what the
collector wrote for the SF14 marketplace run. Output is deterministic for a given seed.

Usage:
    python scripts/scale/synth_collection.py --tier L2 --out /tmp/l2.json
    python scripts/scale/synth_collection.py --tables 500 --queries 1000 --out /tmp/x.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

# name -> (tables, queries, schemas). L3 sits at the collector's own query cap.
TIERS: dict[str, tuple[int, int, int]] = {
    "L0": (28, 94, 4),
    "L1": (28, 94, 4),  # same schema as L0; heavier traffic is in the numbers, see --traffic
    "L2": (500, 1000, 12),
    "L3": (2000, 5000, 20),
}

QUERY_TYPE_MIX = (("SELECT", 69), ("INSERT", 10), ("UPDATE", 11), ("DELETE", 2), ("OTHER", 8))
MAX_TABLES_PER_QUERY = 5
COLUMN_TYPES = (
    ("integer", "int4", 4), ("bigint", "int8", 8), ("smallint", "int2", 2),
    ("text", "text", -1), ("timestamp with time zone", "timestamptz", 8),
    ("numeric", "numeric", -1), ("boolean", "bool", 1),
)
MB = 1024 * 1024


def _check_positive(name: str, value: int) -> None:
    """Raise ValueError unless `value` is a positive int.

    Args:
        name: The option name, for the message.
        value: The number to check.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a whole number of at least 1, got {value!r}")


def _make_tables(rng: random.Random, n_tables: int, n_schemas: int) -> list[dict]:
    """Make the table list.

    Args:
        rng: The seeded random source.
        n_tables: How many tables.
        n_schemas: How many schemas to spread them over.

    Returns:
        Table dicts in the shape of `TableInfo`, each with columns, a primary key,
        column_stats and column_widths.
    """
    tables = []
    for i in range(n_tables):
        schema = f"s{i % n_schemas:02d}"
        name = f"t{i:05d}"
        rows = int(math.exp(rng.uniform(math.log(50), math.log(2e8))))
        n_cols = rng.randint(3, 40) if rng.random() < 0.1 else rng.randint(3, 12)
        columns, stats, widths = [{
            "column_name": "id", "ordinal_position": 1, "data_type": "bigint",
            "udt_name": "int8", "max_length": None, "is_nullable": "NO",
            "column_default": None}], {"id": {"n_distinct": -1.0, "avg_width": 8}}, {
            "id": {"typlen": 8, "typmod": -1, "type_name": "int8", "not_null": True}}
        row_bytes = 8
        for c in range(2, n_cols + 1):
            dtype, udt, width = rng.choice(COLUMN_TYPES)
            cname = f"c{c:02d}"
            avg = width if width > 0 else rng.choice((12, 24, 48, 120))
            row_bytes += avg
            columns.append({
                "column_name": cname, "ordinal_position": c, "data_type": dtype,
                "udt_name": udt, "max_length": None,
                "is_nullable": rng.choice(("YES", "NO")), "column_default": None})
            stats[cname] = {"n_distinct": rng.choice((-1.0, -0.5, 2.0, 40.0, 5000.0)),
                            "avg_width": avg}
            widths[cname] = {"typlen": width, "typmod": -1, "type_name": udt,
                             "not_null": rng.random() < 0.5}
        data_mb = round(rows * (row_bytes + 24) / MB, 3)
        index_mb = round(data_mb * rng.uniform(0.1, 0.6), 3)
        tables.append({
            "table_id": f"{schema}.{name}", "table_name": name, "schema_name": schema,
            "row_count": rows, "size_mb": round(data_mb + index_mb, 3),
            "data_size_mb": data_mb, "index_size_mb": index_mb,
            "full_table_scans": rng.randint(0, 10**5),
            "index_scans": rng.randint(0, 10**7),
            "columns": columns, "primary_key": ["id"], "reltuples": float(rows),
            "column_stats": stats, "column_widths": widths})
    return tables


def _pick_type(rng: random.Random) -> str:
    """Pick a statement type by the mix of a typical OLTP workload.

    Args:
        rng: The seeded random source.

    Returns:
        One of SELECT, INSERT, UPDATE, DELETE, OTHER.
    """
    names, weights = zip(*QUERY_TYPE_MIX, strict=True)
    return rng.choices(names, weights)[0]


def _query_text(qtype: str, tids: list[str], n: int) -> str:
    """Write a plausible, unique statement over the given tables.

    Args:
        qtype: The statement type.
        tids: The `schema.table` ids it touches (at least one).
        n: A running number, so no two statements are identical.

    Returns:
        SQL text with $n placeholders, as pg_stat_statements stores it.
    """
    first = tids[0]
    if qtype == "INSERT":
        return f"INSERT INTO {first} (c02, c03) VALUES ($1, $2) /* q{n} */"  # nosec B608 - synthetic query text for a fake collection, never executed
    if qtype == "UPDATE":
        return f"UPDATE {first} SET c02 = $1 WHERE id = $2 /* q{n} */"  # nosec B608 - synthetic query text for a fake collection, never executed
    if qtype == "DELETE":
        return f"DELETE FROM {first} WHERE id = $1 /* q{n} */"  # nosec B608 - synthetic query text for a fake collection, never executed
    if qtype == "OTHER":
        return f"VACUUM {first} /* q{n} */"
    joins = "".join(f" JOIN {t} j{k} ON j{k}.id = a.id" for k, t in enumerate(tids[1:]))
    return f"SELECT a.id, a.c02 FROM {first} a{joins} WHERE a.id = $1 /* q{n} */"  # nosec B608 - synthetic query text for a fake collection, never executed


def _make_queries(rng: random.Random, tables: list[dict], n_queries: int,
                  traffic: float) -> list[dict]:
    """Make the query patterns.

    Table use is skewed: a few tables take most queries, as in a real database. Time is
    heavy-tailed, so a few statements carry most of the database time.

    Args:
        rng: The seeded random source.
        tables: The tables to reference.
        n_queries: How many patterns.
        traffic: Multiplier on calls per second (1.0 is the SF14 1x rate).

    Returns:
        Query dicts in the shape of `QueryPattern`.
    """
    ids = [t["table_id"] for t in tables]
    weights = [1.0 / (i + 1) ** 0.8 for i in range(len(ids))]
    queries = []
    for n in range(n_queries):
        qtype = _pick_type(rng)
        k = min(len(ids), rng.choices((1, 2, 3, 4, 5), (60, 25, 8, 4, 3))[0])
        picked: list[str] = []
        while len(picked) < k:
            t = rng.choices(ids, weights)[0]
            if t not in picked:
                picked.append(t)
        cps = round(math.exp(rng.uniform(math.log(0.01), math.log(300))) * traffic, 6)
        avg_ms = round(math.exp(rng.uniform(math.log(0.05), math.log(800))), 4)
        calls = max(1, int(cps * 1500))
        total = round(avg_ms * calls, 3)
        rows_avg = round(math.exp(rng.uniform(0, math.log(500))), 1)
        queries.append({
            "query_id": str(rng.getrandbits(62)), "query_text": _query_text(qtype, picked, n),
            "query_type": qtype, "calls_per_second": cps,
            "frequency_per_hour": round(cps * 3600, 3),
            "tables_accessed": list(picked), "tables_accessed_raw": list(picked),
            "execution_time_ms_avg": avg_ms, "total_time_ms": total,
            "rows_returned_avg": rows_avg, "cache_hit_ratio_pct": round(rng.uniform(80, 100), 2),
            "shared_blks_hit": rng.randint(0, 10**8), "shared_blks_read": rng.randint(0, 10**5),
            "io_read_time_ms": round(rng.uniform(0, 500), 3), "temp_blocks_written": 0,
            "execution_count": calls, "stats_window_seconds": 1500.0,
            "execution_time_ms_min": round(avg_ms * 0.2, 4),
            "execution_time_ms_max": round(avg_ms * 6, 4),
            "execution_time_ms_p50": avg_ms, "execution_time_ms_p95": round(avg_ms * 3, 4),
            "execution_time_ms_p99": round(avg_ms * 4, 4),
            "rows_returned_p95": round(rows_avg * 3, 1),
            "rows_affected_avg": 0 if qtype == "SELECT" else 1,
            "rows_examined_avg": round(rows_avg * rng.uniform(1, 50), 1),
            "scan_efficiency_pct": round(rng.uniform(0, 100), 1),
            "filter_columns": ["id"], "sort_columns": [],
            "has_joins": k > 1, "join_count": k - 1, "has_aggregations": False,
            "has_subqueries": False, "has_text_search": False, "text_search_type": None,
            "has_time_range_filter": False, "io_write_time_ms": 0.0, "temp_blocks_read": 0})
    return queries


def _make_churn(rng: random.Random, tables: list[dict]) -> list[dict]:
    """Make the per-table churn rows (one per table, like pg_stat_user_tables).

    Args:
        rng: The seeded random source.
        tables: The tables.

    Returns:
        Rows with schema_name, table_name and the DML counters.
    """
    rows = []
    for t in tables:
        live = t["row_count"]
        rows.append({
            "schema_name": t["schema_name"], "table_name": t["table_name"],
            "n_live_tup": live, "n_dead_tup": int(live * rng.uniform(0, 0.3)),
            "n_tup_ins": rng.randint(0, 10**6), "n_tup_upd": rng.randint(0, 10**6),
            "n_tup_del": rng.randint(0, 10**5), "n_tup_hot_upd": rng.randint(0, 10**5),
            "seq_scan": t["full_table_scans"], "seq_tup_read": rng.randint(0, 10**8),
            "idx_scan": t["index_scans"], "idx_tup_fetch": rng.randint(0, 10**8),
            "last_vacuum": None, "last_autovacuum": None,
            "last_analyze": "2026-10-10 22:40:27+00", "last_autoanalyze": None})
    return rows


def build_collection(n_tables: int, n_queries: int, *, n_schemas: int = 4, seed: int = 1,
                     traffic: float = 1.0, job_id: str = "synthetic") -> dict:
    """Build a whole collector artifact.

    Args:
        n_tables: Number of tables (at least 1).
        n_queries: Number of query patterns (at least 1).
        n_schemas: Schemas to spread tables over (at least 1, at most `n_tables`).
        seed: Random seed. The same arguments always give the same artifact.
        traffic: Multiplier on query rates, for a heavier workload on the same schema.
        job_id: The `job_id` field.

    Returns:
        A dict that validates as `CollectorOutput`.

    Raises:
        ValueError: A count is not a whole number of at least 1, or `traffic` is not a
            positive number.
    """
    _check_positive("n_tables", n_tables)
    _check_positive("n_queries", n_queries)
    _check_positive("n_schemas", n_schemas)
    if isinstance(traffic, bool) or not isinstance(traffic, (int, float)) or traffic <= 0:
        raise ValueError(f"traffic must be a positive number, got {traffic!r}")
    n_schemas = min(n_schemas, n_tables)
    rng = random.Random(seed)
    tables = _make_tables(rng, n_tables, n_schemas)
    queries = _make_queries(rng, tables, n_queries, float(traffic))
    per_schema: dict[str, int] = {}
    for t in tables:
        per_schema[t["schema_name"]] = per_schema.get(t["schema_name"], 0) + 1
    size_gb = sum(t["size_mb"] for t in tables) / 1024
    return {
        "contract_version": "1.0", "job_id": job_id,
        "metadata": {
            "collection_timestamp": "2026-10-10T23:05:31Z", "collector_version": "synthetic",
            "collection_duration_seconds": 0.0,
            "source_database": {"engine": "postgresql", "version": "16.15",
                                "hostname": "synthetic", "database_name": "synthetic",
                                "database_size_gb": round(size_gb, 3),
                                "deployment_type": "self_managed"},
            "stage_timings_seconds": {}},
        "pg_stat_statements_probe": {"available": True, "in_preload": True,
                                     "extension_installed": True, "reason": None},
        "schemas_discovered": [{"schema_name": s, "table_count": c}
                               for s, c in per_schema.items()],
        "schemas_collected": list(per_schema),
        "database_schema": {"tables": tables, "views": []},
        "queries": {"query_patterns": queries, "total_queries_analyzed": len(queries),
                    "query_log_source": "pg_stat_statements"},
        "metrics": {
            "global_stats": {"cache_hit_ratio_pct": 99.0, "active_connections": 100,
                             "total_transactions": 10**7},
            "bgwriter": {}, "wait_events": [], "lock_contention": [],
            "table_churn": _make_churn(rng, tables),
            "configuration": {"shared_buffers": {"setting": 1048576, "unit": "8kB"},
                              "effective_cache_size": {"setting": 2883584, "unit": "8kB"},
                              "work_mem": {"setting": 16384, "unit": "kB"}}},
        "host_metrics": {
            "collection_method": "synthetic", "executor_type": "synthetic",
            "cpu": {"vcpu_count": 4, "utilization_pct": 50.0},
            "memory": {"total_gb": 30.0, "available_gb": 15.0, "used_pct": 50.0},
            "load_average": {"load_1m": 2.0, "load_5m": 2.0, "load_15m": 2.0}}}


def main(argv: list[str] | None = None) -> int:
    """Write a synthetic collection to a file.

    Args:
        argv: Command-line arguments. Defaults to `sys.argv[1:]`.

    Returns:
        0 on success, 2 on a bad argument.
    """
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tier", choices=sorted(TIERS), help="A preset size")
    p.add_argument("--tables", type=int, help="Number of tables (overrides the tier)")
    p.add_argument("--queries", type=int, help="Number of query patterns (overrides the tier)")
    p.add_argument("--schemas", type=int, help="Number of schemas (overrides the tier)")
    p.add_argument("--traffic", type=float, default=1.0, help="Rate multiplier (default 1)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--job-id", default="synthetic")
    p.add_argument("--out", required=True, type=Path)
    a = p.parse_args(argv)
    t, q, s = TIERS[a.tier] if a.tier else (None, None, None)
    t, q, s = a.tables or t, a.queries or q, a.schemas or s or 4
    if t is None or q is None:
        print("error: give --tier, or both --tables and --queries", file=sys.stderr)
        return 2
    try:
        doc = build_collection(t, q, n_schemas=s, seed=a.seed, traffic=a.traffic,
                               job_id=a.job_id)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(doc))
    print(f"{a.out}: {t} tables, {q} queries, {a.out.stat().st_size / MB:.2f} MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
