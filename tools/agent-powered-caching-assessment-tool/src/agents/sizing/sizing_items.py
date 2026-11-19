"""
Per-item sizing for the SAC agent: query keyspaces and object keyspaces.

QUERY   entries  distinct entries the window's calls would create, from the
                 analyzer's K (statistics) or the model's estimate of distinct
                 requests (deferred reuse)
        value    the analyzer's result_bytes
OBJECT  entries  distinct key values the window's calls would create; for a
                 recommended table, its row estimate
        value    width of the cached columns x rows per key. The model chose
                 the columns; their widths come from statistics.

Object keyspaces on the same table and key are merged, never double counted.
"""

from __future__ import annotations

from src.contracts.cache_sizing_output import SizedItem
from src.tools.analysis.cache_sizing import (
    SizingParameters,
    bytes_per_entry,
    item_traffic,
    object_key_bytes,
    window_entries,
)
from src.tools.analysis.column_widths import column_width, table_columns

K_SOURCE_AGENT = "agent estimate"


def finish_item(item: SizedItem, key_bytes: float, write_rate: float,
                p: SizingParameters, missing: list[tuple]) -> None:
    """Fill traffic always, and memory only when every input is present.

    Args:
        item: Item with entries, value_bytes, reads and hit rate set.
        key_bytes: Key length in bytes.
        write_rate: Measured writes/s on the tables the item reads.
        p: Sizing parameters.
        missing: (value, reason) pairs; the first None value leaves the item
            unsized with that reason.
    """
    t = item_traffic(item.read_ops_per_sec, item.hit_rate, write_rate,
                     item.value_bytes, p)
    item.fill_ops_per_sec = round(t["fill"], 4)
    item.invalidation_ops_per_sec = round(t["invalidation"], 4)
    item.total_ops_per_sec = round(t["total"], 4)
    item.bandwidth_gbps = round(t["bandwidth_gbps"], 6)
    for value, reason in missing:
        if value is None:
            item.sized, item.not_sized_reason = False, reason
            return
    item.bytes_per_entry = round(bytes_per_entry(item.value_bytes, key_bytes, p), 1)
    item.memory_bytes = round(item.entries * item.bytes_per_entry, 1)
    item.entries = round(item.entries, 1)
    item.sized = True


def size_query_item(rec, verdict, p: SizingParameters) -> SizedItem:
    """Size one recommended QUERY-cache query.

    Args:
        rec: QueryCacheRecommendation.
        verdict: The model's LLMQueryVerdict, or None under fallback.
        p: Sizing parameters.
    """
    eff = rec.efficiency or {}
    item = SizedItem(
        item_id=rec.queryid, cache_type="QUERY", source="query",
        query_ids=[rec.queryid], key_columns=list(rec.key_columns),
        key_pattern=getattr(verdict, "suggested_cache_key", None),
        read_ops_per_sec=rec.calls_per_second,
        hit_rate=eff.get("estimated_hit_rate"),
        db_time_ms_per_sec=rec.db_time_ms_per_sec,
    )
    calls, k = eff.get("calls_in_window"), eff.get("k")
    if eff.get("k_source") == K_SOURCE_AGENT and k:
        item.entries = min(float(k), float(calls or k))
        item.entries_source = "model estimate of distinct requests in the window"
    else:
        item.entries = window_entries(calls, k)
        item.entries_source = "expected distinct entries over the window, from statistics"
    item.key_space = k
    if eff.get("result_bytes") is not None:
        item.value_bytes = float(eff["result_bytes"])
        item.value_bytes_source = "analyzer result size"
    finish_item(item, p.query_key_bytes, rec.invalidating_write_rate, p, missing=[
        (item.entries, eff.get("reuse_missing_reason") or eff.get("reuse_deferred_reason")
         or "no distinct-entry count for this query"),
        (item.value_bytes, eff.get("size_missing_reason") or "no result size for this query"),
    ])
    return item


# ---------------------------------------------------------------------------
# OBJECT keyspaces
# ---------------------------------------------------------------------------


def new_group(table_id: str, key: list[str]) -> dict:
    """Empty accumulator for one table + key keyspace."""
    return {"table_id": table_id, "key": key, "query_ids": [], "sources": set(),
            "columns": None, "columns_source": None, "rows_per_key": 1.0,
            "entries": None, "entries_source": None, "key_space": None,
            "reads": 0.0, "hits": 0.0, "hit_known": True,
            "key_pattern": None, "db_time": 0.0}


def _merge_columns(g: dict, cols: list[str] | None) -> None:
    if cols:
        g["columns"] = list(dict.fromkeys((g["columns"] or []) + list(cols)))
        g["columns_source"] = "model"


def _add_reads(g: dict, reads: float, hit: float | None) -> None:
    g["reads"] += reads
    if hit is None:
        g["hit_known"] = False
    else:
        g["hits"] += reads * hit


def _max_entries(g: dict, entries: float | None, source: str, key_space) -> None:
    if entries is not None and (g["entries"] is None or entries > g["entries"]):
        g["entries"], g["entries_source"] = entries, source
    if key_space is not None:
        g["key_space"] = max(g["key_space"] or 0, float(key_space))


def add_object_query(groups: dict, rec, verdict) -> None:
    """Fold one recommended OBJECT query into its table + key group."""
    if not rec.tables_read:
        return
    table = str(rec.tables_read[0]).lower()
    key = sorted(c.lower() for c in rec.key_columns)
    g = groups.setdefault((table, tuple(key)), new_group(table, key))
    g["query_ids"].append(rec.queryid)
    g["sources"].add("query")
    g["db_time"] += rec.db_time_ms_per_sec or 0.0
    g["key_pattern"] = g["key_pattern"] or getattr(verdict, "suggested_cache_key", None)
    _merge_columns(g, getattr(verdict, "cached_columns", None))
    g["rows_per_key"] = max(g["rows_per_key"], float(rec.rows_returned_avg or 0.0))
    eff = rec.efficiency or {}
    _max_entries(g, window_entries(eff.get("calls_in_window"), eff.get("k")),
                 "expected distinct keys over the window, from statistics", eff.get("k"))
    _add_reads(g, rec.calls_per_second, eff.get("estimated_hit_rate"))


def add_object_table(groups: dict, rec, verdict, table: dict | None) -> None:
    """Fold one recommended table into its primary-key group."""
    tid = str(rec.table_id).lower()
    key = sorted(str(c).lower() for c in ((table or {}).get("primary_key") or []))
    g = groups.setdefault((tid, tuple(key)), new_group(tid, key))
    g["sources"].add("table")
    _merge_columns(g, getattr(verdict, "cached_columns", None))
    rows = (table or {}).get("reltuples")
    if rows is None:
        rows = (table or {}).get("row_count")
    rows = float(rows) if rows else None
    _max_entries(g, rows, "table row estimate: every key cached", rows)
    eff = rec.efficiency or {}
    _add_reads(g, float(rec.read_calls_per_second or 0.0), eff.get("estimated_hit_rate"))


def finish_object_group(g: dict, table: dict | None, write_rate: float,
                        p: SizingParameters) -> SizedItem:
    """Turn a merged object keyspace into a sized item."""
    if g["columns"] is None and table is not None:
        g["columns"], g["columns_source"] = table_columns(table), "whole row"
    cols = g["columns"] or []
    item = SizedItem(
        item_id=f"{g['table_id']}:{','.join(g['key']) or 'no primary key'}",
        cache_type="OBJECT", source="+".join(sorted(g["sources"])),
        query_ids=g["query_ids"], table_id=g["table_id"], key_columns=g["key"],
        key_pattern=g["key_pattern"], cached_columns=cols,
        cached_columns_source=g["columns_source"],
        entries=g["entries"], entries_source=g["entries_source"],
        key_space=g["key_space"], read_ops_per_sec=round(g["reads"], 4),
        hit_rate=(round(g["hits"] / g["reads"], 4)
                  if g["hit_known"] and g["reads"] > 0 else None),
        db_time_ms_per_sec=round(g["db_time"], 2) if g["query_ids"] else None,
    )
    reason, row = None, 0.0
    if table is None:
        reason = f"{g['table_id']} is not in the collection"
    elif not g["key"]:
        reason = (f"{g['table_id']} has no primary key, so there is no key to cache "
                  "objects under")
    elif not cols:
        reason = f"{g['table_id']} has no known columns"
    else:
        unknown = [c for c in cols if column_width(table, c)[0] is None]
        if unknown:
            reason = f"no width statistics for {', '.join(unknown)}"
        else:
            row = sum(column_width(table, c)[0] for c in cols)
    if reason is None:
        item.value_bytes = round(row * g["rows_per_key"], 1)
        item.value_bytes_source = (f"{len(cols)} column(s), {row:.0f} bytes per row x "
                                   f"{g['rows_per_key']:.1f} rows per key")
    key_widths = [column_width(table, c)[0] or 8.0 for c in g["key"]] if table else []
    finish_item(item, object_key_bytes(key_widths, p), write_rate, p, missing=[
        (item.entries, "no distinct-key count for this keyspace"),
        (item.value_bytes, reason),
    ])
    return item
