"""
The collection window: statistics measured between two snapshots.

WHY THIS EXISTS
---------------
pg_stat_statements, pg_stat_database, pg_stat_user_tables and pg_stat_bgwriter count
from their last reset. On a server that was reset two days ago, calls / seconds is a
two-day average: a busy hour and a quiet night count the same. The collector now reads
the counters at the start and the end of a window the user chooses and scores the
difference, so every rate, mean time, hit ratio and spill describes that window only.

Cache Efficiency keeps the counts since the reset: an estimated hit rate needs many
calls, and the result size of a query does not depend on the window. Each query keeps
them under `since_reset`.

WHAT CAN GO WRONG, AND WHAT HAPPENS
-----------------------------------
- The statistics were reset during the window. The two snapshots do not share a
  baseline, so the window is not used for that view and the collection says so.
- pg_stat_statements evicted entries (pg_stat_statements_info.dealloc went up). A
  query evicted and seen again restarts from zero, so its count in the window is a
  lower bound. Flagged per query when the second count is below the first.
- A query ran too few times in the window to give a stable mean. It keeps its rates
  since the reset, projected over the window so totals stay comparable, and is marked
  `rate_basis = since_reset`.
- min and max execution times cannot be subtracted. They stay as since the reset.

Every function here is pure: snapshots in, numbers out.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from src.tools.database.postgres_tools import (
    STATEMENT_COUNTERS,
    _estimate_percentiles,
    _estimate_rows_p95,
    bgwriter_summary,
    statement_counters_from_rows,
)

logger = logging.getLogger(__name__)

# Below this many calls in the window a query's mean is too noisy to stand on, so its
# rates since the reset are used instead (spec CW-7).
WINDOW_MIN_CALLS = 5

# pg_stat_database counters that are cumulative. The rest of the row is a level.
_GLOBAL_COUNTERS = ("buffer_pool_hits", "buffer_pool_reads_from_disk", "tmp_disk_tables",
                    "total_transactions", "temp_bytes")
# pg_stat_user_tables counters. n_live_tup, n_dead_tup and the last_* times are levels.
_TABLE_COUNTERS = ("seq_scan", "seq_tup_read", "idx_scan", "idx_tup_fetch", "n_tup_ins",
                   "n_tup_upd", "n_tup_del", "n_tup_hot_upd")
_BGWRITER_SKIP = ("window_seconds", "stats_reset_epoch", "buffers_backend_pct",
                  "requested_checkpoint_pct")


def _num(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _rounded(c: dict | None) -> dict | None:
    """Counters as stored in a collection: whole numbers, times to 0.01 ms."""
    if c is None:
        return None
    out = {}
    for k in STATEMENT_COUNTERS:
        v = float(c.get(k) or 0.0)
        out[k] = round(v, 2) if k.endswith("_ms") else int(round(v))
    return out


def rate_fields(c: dict, seconds: float) -> dict:
    """Pattern fields from cumulative counters over a period.

    Same formulas as collect_query_patterns, so a window pattern reads like any other.

    Args:
        c: Counters (STATEMENT_COUNTERS) for one query.
        seconds: The period they cover.

    Returns:
        execution_count, rates, mean, deviation, estimated percentiles, totals, hit
        ratio, I/O and temp blocks.
    """
    calls = max(float(c.get("calls") or 0.0), 0.0)
    secs = max(float(seconds or 0.0), 1.0)
    n = max(calls, 1.0)
    total = float(c.get("total_time_ms") or 0.0)
    mean = total / n
    var = max(float(c.get("sumsq_ms") or 0.0) / n - mean * mean, 0.0)
    sd = math.sqrt(var)
    p50, p95, p99 = _estimate_percentiles(mean, sd)
    rows_avg = float(c.get("rows") or 0.0) / n
    hit = int(round(float(c.get("shared_blks_hit") or 0.0)))
    read = int(round(float(c.get("shared_blks_read") or 0.0)))
    blks = hit + read
    return {
        "execution_count": int(round(calls)),
        "frequency_per_hour": round(calls / (secs / 3600.0), 3),
        "calls_per_second": round(calls / secs, 6),
        "stats_window_seconds": round(secs, 1),
        "execution_time_ms_avg": mean,
        "execution_time_ms_stddev": sd,
        "execution_time_ms_p50": p50,
        "execution_time_ms_p95": p95,
        "execution_time_ms_p99": p99,
        "total_time_ms": total,
        "rows_returned_avg": rows_avg,
        "rows_returned_p95": _estimate_rows_p95(rows_avg, mean, p95),
        "rows_examined_avg": blks * 8192.0 / n,
        "scan_efficiency_pct": (
            min(round(float(c.get("rows") or 0.0) / max(blks * 8192.0, 1) * 100, 2), 100.0)
            if blks > 0 else None),
        "cache_hit_ratio_pct": round(hit / max(blks, 1) * 100, 2),
        "shared_blks_hit": hit,
        "shared_blks_read": read,
        "io_read_time_ms": float(c.get("io_read_time_ms") or 0.0),
        "io_write_time_ms": float(c.get("io_write_time_ms") or 0.0),
        "temp_blocks_read": int(round(float(c.get("temp_blks_read") or 0.0))),
        "temp_blocks_written": int(round(float(c.get("temp_blks_written") or 0.0))),
    }


def _delta(after: dict, before: dict | None) -> tuple[dict, str | None]:
    """after minus before, per counter, with what happened to the entry.

    Returns:
        (counters, flag). flag is "new" when the entry was not in the first snapshot,
        "restarted" when its count went down (evicted and seen again, or reset on its
        own), else None. In both flagged cases the second snapshot is the window's
        count, a lower bound.
    """
    if before is None:
        return dict(after), "new"
    if float(after.get("calls") or 0) < float(before.get("calls") or 0):
        return dict(after), "restarted"
    return {k: max(float(after.get(k) or 0) - float(before.get(k) or 0), 0.0)
            for k in STATEMENT_COUNTERS}, None


def statements_reset_during(info_before: dict | None, info_after: dict | None) -> bool:
    """Whether pg_stat_statements was reset between the snapshots.

    Unknown (PostgreSQL 13, or the view could not be read) is not a reset: a reset
    also shows as most entries restarting, which is reported per query.
    """
    a = (info_before or {}).get("stats_reset_epoch")
    b = (info_after or {}).get("stats_reset_epoch")
    return a is not None and b is not None and abs(float(b) - float(a)) > 0.5


def window_patterns(
    before: dict[str, dict],
    after_patterns: list[dict],
    seconds: float,
    limit: int,
    min_calls: int = WINDOW_MIN_CALLS,
) -> tuple[list[dict], dict]:
    """The query patterns of the window: top `limit` by database time in the window.

    Args:
        before: Counters per query id at the start (snapshot_statement_counters).
        after_patterns: Every pattern at the end (collect_query_patterns, no limit),
            so a query busy in the window is found even when it was quiet before.
        seconds: Window length, from the database clock.
        limit: How many patterns to keep.
        min_calls: Fewer calls than this in the window and the pattern keeps its rates
            since the reset (rate_basis since_reset).

    Returns:
        (patterns, summary). Each pattern carries the window's figures in the usual
        fields, `since_reset` with the figures since the last reset (Cache Efficiency
        reads these), and `window` with its call count, rate basis and any flag. Rows
        of one query id under several users are merged into one pattern.
    """
    after = statement_counters_from_rows(after_patterns, id_key="query_id")
    template: dict[str, dict] = {}
    for p in after_patterns:
        qid = p.get("query_id")
        if qid not in (None, "") and str(qid) not in template:
            template[str(qid)] = p

    seen: list[tuple[str, dict, str | None]] = []
    restarted = new = 0
    for qid, a in after.items():
        d, flag = _delta(a, before.get(qid))
        if d["calls"] <= 0:
            continue
        restarted += flag == "restarted"
        new += flag == "new"
        seen.append((qid, d, flag))
    seen.sort(key=lambda x: -x[1]["total_time_ms"])
    selected = seen[:max(int(limit), 0)]

    out: list[dict] = []
    fallback: list[str] = []
    for qid, d, flag in selected:
        base = template[qid]
        sr_secs = float(base.get("stats_window_seconds") or seconds)
        since = rate_fields(after[qid], sr_secs)
        if d["calls"] >= min_calls:
            fields, basis = rate_fields(d, seconds), "window"
        else:
            # Project the rates since the reset over the window, so this pattern's
            # totals add up with the others'.
            scale = seconds / max(sr_secs, 1.0)
            fields = rate_fields({k: v * scale for k, v in after[qid].items()}, seconds)
            basis = "since_reset"
            fallback.append(qid)
        pat = {**base, **fields,
               "execution_time_ms_min": base.get("execution_time_ms_min"),
               "execution_time_ms_max": base.get("execution_time_ms_max"),
               "since_reset": since,
               "window": {"calls": int(round(d["calls"])), "rate_basis": basis,
                          "flag": flag,
                          # The raw counters each snapshot read, so the difference can
                          # be checked by hand. start is None for an entry that was not
                          # there at the start.
                          "start": _rounded(before.get(qid)),
                          "end": _rounded(after[qid])}}
        out.append(pat)

    def total(snap: dict, key: str) -> float:
        return round(sum(float(c.get(key) or 0.0) for c in snap.values()), 2)

    summary = {
        "statements_seen": len(seen),
        "calls": int(round(sum(d["calls"] for _, d, _ in seen))),
        "db_time_ms": round(sum(d["total_time_ms"] for _, d, _ in seen), 2),
        # What each snapshot read in total, every entry. end - start differs from the
        # window's calls only by entries that were new or restarted.
        "start_snapshot": {"entries": len(before), "calls": total(before, "calls"),
                           "total_time_ms": total(before, "total_time_ms")},
        "end_snapshot": {"entries": len(after), "calls": total(after, "calls"),
                         "total_time_ms": total(after, "total_time_ms")},
        "kept": len(out),
        "dropped_by_limit": max(len(seen) - len(out), 0),
        "not_seen_in_window": len(after) - len(seen),
        "since_reset_rates": fallback,
        "restarted_entries": restarted,
        "new_entries": new,
        "min_calls_for_window_rates": min_calls,
    }
    return out, summary


def window_global_stats(before: dict, after: dict, seconds: float) -> tuple[dict | None, str | None]:
    """pg_stat_database over the window.

    Returns:
        (stats, problem). stats is None with the reason when the database statistics
        were reset during the window or a counter went down.
    """
    if not before or not after:
        return None, "pg_stat_database was not read at both ends of the window"
    ra, rb = _num(before.get("stats_reset_epoch")), _num(after.get("stats_reset_epoch"))
    if ra != rb:
        return None, "the database statistics were reset during the window"
    d = {}
    for k in _GLOBAL_COUNTERS:
        v = (_num(after.get(k)) or 0.0) - (_num(before.get(k)) or 0.0)
        if v < 0:
            return None, f"pg_stat_database {k} went down during the window"
        d[k] = int(round(v))
    hits, reads = d["buffer_pool_hits"], d["buffer_pool_reads_from_disk"]
    return {
        **after,
        **d,
        "cache_hit_ratio_pct": round(hits / max(hits + reads, 1) * 100, 2),
        "buffer_pool_read_requests": hits + reads,
        "tmp_tables": d["tmp_disk_tables"],
        "stats_window_seconds": round(seconds, 1),
    }, None


def window_bgwriter(before: dict, after: dict, seconds: float) -> tuple[dict | None, str | None]:
    """pg_stat_bgwriter over the window.

    Returns:
        (stats, problem), as window_global_stats.
    """
    if not before or not after:
        return None, "pg_stat_bgwriter was not read at both ends of the window"
    if _num(before.get("stats_reset_epoch")) != _num(after.get("stats_reset_epoch")):
        return None, "the background writer statistics were reset during the window"
    d: dict = {}
    for k, v in after.items():
        if k in _BGWRITER_SKIP:
            continue
        a, b = _num(v), _num(before.get(k))
        if a is None or b is None:
            continue
        if a < b:
            return None, f"pg_stat_bgwriter {k} went down during the window"
        d[k] = a - b
    d["window_seconds"] = round(seconds, 1)
    d["stats_reset_epoch"] = after.get("stats_reset_epoch")
    return bgwriter_summary(d), None


def window_table_churn(before: list[dict], after: list[dict]) -> tuple[list[dict], int]:
    """pg_stat_user_tables counters over the window. Levels stay as at the end.

    Returns:
        (rows, restarted). A table missing from the first read, or whose counter went
        down, keeps its end values and is counted in `restarted`.
    """
    first = {(r.get("schema_name"), r.get("table_name")): r for r in before or []}
    rows, restarted = [], 0
    for r in after or []:
        b = first.get((r.get("schema_name"), r.get("table_name")))
        row = dict(r)
        if b is not None:
            deltas = {}
            for k in _TABLE_COUNTERS:
                a, s = _num(r.get(k)), _num(b.get(k))
                deltas[k] = None if a is None else (a - (s or 0.0))
            if any(v is not None and v < 0 for v in deltas.values()):
                restarted += 1
            else:
                row.update({k: (int(v) if v is not None else None) for k, v in deltas.items()})
        else:
            restarted += 1
        rows.append(row)
    rows.sort(key=lambda r: -sum(int(_num(r.get(k)) or 0)
                                 for k in ("n_tup_ins", "n_tup_upd", "n_tup_del")))
    return rows, restarted


def merge_lock_samples(samples: list[list[dict]]) -> list[dict]:
    """Every blocking pair seen during the window, once, with how often it was seen."""
    seen: dict[tuple, dict] = {}
    for rows in samples:
        for r in rows or []:
            key = (r.get("blocked_pid"), r.get("blocking_pid"), r.get("locktype"),
                   r.get("relation_name"), r.get("blocked_mode"))
            if key in seen:
                seen[key]["samples_seen"] += 1
            else:
                seen[key] = {**r, "samples_seen": 1}
    return list(seen.values())


def merge_wait_samples(samples: list[list[dict]]) -> list[dict]:
    """Session counts per wait event, summed over the samples.

    A sum over evenly spaced samples is proportional to time spent waiting, so a share
    computed from it (compute_lock_wait_share) is a share of the window.
    """
    acc: dict[tuple, dict] = {}
    for rows in samples:
        for r in rows or []:
            key = (r.get("wait_event_type"), r.get("wait_event"), r.get("state"))
            a = acc.setdefault(key, {"wait_event_type": key[0], "wait_event": key[1],
                                     "state": key[2], "session_count": 0, "samples": 0})
            a["session_count"] += int(_num(r.get("session_count")) or 0)
            a["samples"] += 1
    return sorted(acc.values(), key=lambda r: -r["session_count"])
