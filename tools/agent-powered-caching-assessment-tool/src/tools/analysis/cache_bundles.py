"""Cache bundles: Not cacheable queries the application could compute from cache.

A query that cannot be cached as a result (exclusion_kind not_cacheable, for
example because it filters on the current time) can still be answered without
the database if every table it reads is object cached: the application reads
the cached rows and applies the filter, join and sort itself. Such a query is
a bundle, and its database time is what caching the whole bundle saves.

Detection is deterministic so the model is handed the bundle as a fact rather
than asked to find it (prompt-only guidance was applied in one run and ignored
in the next). Bundles add no score points. Spec: .kiro/specs/cache-bundles.
"""
from __future__ import annotations

import logging

from src.tools.analysis.caching_scoring import DEFAULT_THRESHOLDS, CacheScoringThresholds

logger = logging.getLogger(__name__)

SECONDS_PER_HOUR = 3600.0


def row_change_seconds(write_cps: float | None, row_count: float | None) -> float | None:
    """Seconds between changes to one row: row_count / writes per second.

    Args:
        write_cps: Write statements per second on the table.
        row_count: Rows in the table.

    Returns:
        Seconds, None when there were no writes, 0.0 for writes on an empty or
        unknown-size table or unusable input (changes too often to tell).
    """
    try:
        w = float(write_cps or 0.0)
        if w <= 0:
            return None
        rows = float(row_count or 0.0)
        return rows / w if rows > 0 else 0.0
    except (TypeError, ValueError):
        logger.warning("row_change_seconds: unusable input write_cps=%r row_count=%r",
                       write_cps, row_count)
        return 0.0


def interval_text(secs: float) -> str:
    """Readable interval: seconds under a minute, minutes under an hour, else hours."""
    if secs < 60:
        return f"{secs:.0f} seconds"
    if secs < SECONDS_PER_HOUR:
        return f"{secs / 60:.0f} minutes"
    return f"{secs / SECONDS_PER_HOUR:.1f} hours"


def check_bundle_table(table_id: str, profile, breakdown,
                       t: CacheScoringThresholds = DEFAULT_THRESHOLDS) -> dict:
    """Can this table's rows be object cached as part of a bundle?

    Args:
        table_id: schema.table as the query names it.
        profile: CacheTableProfile, or None when the table was not collected.
        breakdown: CacheScoreBreakdown, or None.
        t: Thresholds.

    Returns:
        {"table_id", "qualifies", "reason", "row_change_interval_hours"}.
        reason is None when the table qualifies.
    """
    out = {"table_id": table_id, "qualifies": False, "reason": None,
           "row_change_interval_hours": None}
    if profile is None or breakdown is None:
        out["reason"] = "no collected statistics for this table"
        return out
    secs = row_change_seconds(profile.write_calls_per_second, profile.row_count)
    out["row_change_interval_hours"] = (None if secs is None
                                        else round(secs / SECONDS_PER_HOUR, 2))
    if breakdown.hard_excluded:
        out["reason"] = f"hard excluded ({breakdown.exclusion_kind})"
    elif profile.is_lock_contended:
        out["reason"] = "in a lock blocking chain"
    elif secs is not None and secs < t.bundle_min_row_change_hours * SECONDS_PER_HOUR:
        out["reason"] = (f"each row changes about every {interval_text(secs)}, more "
                         f"often than once every {t.bundle_min_row_change_hours:g} hour")
    else:
        out["qualifies"] = True
    return out


def detect_bundles(query_profiles: list, profiles: dict, breakdowns: dict,
                   t: CacheScoringThresholds = DEFAULT_THRESHOLDS) -> list[dict]:
    """Find Not cacheable read queries whose tables can all be object cached.

    Args:
        query_profiles: Scored QueryCacheProfile records (breakdown set).
        profiles: table_id -> CacheTableProfile.
        breakdowns: table_id -> CacheScoreBreakdown.
        t: Thresholds.

    Returns:
        One dict per Not cacheable read query with at least bundle_min_db_share
        of DB time, qualifying or not, sorted by DB time descending:
        {"queryid", "query_text", "db_time_ms_per_sec", "db_time_share_pct",
         "not_cacheable_reason", "tables", "qualifies", "table_checks"}.
    """
    by_lower = {str(k).lower(): k for k in profiles}
    out: list[dict] = []
    for q in query_profiles or []:
        b, sh = q.breakdown, q.shape
        if b is None or b.exclusion_kind != "not_cacheable":
            continue
        # Writes and session statements are gated too, but are not reads.
        if sh.tables_written or sh.is_system_query or not sh.tables_read:
            continue
        share = (q.db_time_share_pct or 0.0) / 100.0
        if share < t.bundle_min_db_share:
            continue
        checks = []
        for name in sorted(set(sh.tables_read) | set(sh.unresolved_relations)):
            key = by_lower.get(str(name).lower())
            checks.append(check_bundle_table(
                str(key or name), profiles.get(key) if key else None,
                breakdowns.get(key) if key else None, t))
        out.append({
            "queryid": q.queryid,
            "query_text": (q.query_text or "")[:300],
            "db_time_ms_per_sec": q.db_time_ms_per_sec,
            "db_time_share_pct": q.db_time_share_pct,
            "not_cacheable_reason": b.exclusion_reason,
            "tables": [c["table_id"] for c in checks],
            "qualifies": all(c["qualifies"] for c in checks),
            "table_checks": checks,
        })
    return sorted(out, key=lambda x: -(x["db_time_ms_per_sec"] or 0.0))


def bundles_by_table(bundles: list[dict]) -> dict[str, list[dict]]:
    """Per-table membership, for table records and the table tool.

    Args:
        bundles: From detect_bundles().

    Returns:
        table_id -> list of {"queryid", "db_time_share_pct", "qualifies",
        "other_tables", "this_table_qualifies", "this_table_reason"}.
    """
    out: dict[str, list[dict]] = {}
    for bd in bundles or []:
        for c in bd["table_checks"]:
            tid = c["table_id"]
            out.setdefault(tid, []).append({
                "queryid": bd["queryid"],
                "db_time_share_pct": bd["db_time_share_pct"],
                "qualifies": bd["qualifies"],
                "other_tables": [x for x in bd["tables"] if x != tid],
                "this_table_qualifies": c["qualifies"],
                "this_table_reason": c["reason"],
            })
    return out


def qualifying_bundle_tables(bundles: list[dict]) -> set[str]:
    """Tables in at least one qualifying bundle; their model verdicts need
    bundle_reasoning."""
    return {tid for bd in bundles or [] if bd["qualifies"] for tid in bd["tables"]}


# ---------------------------------------------------------------------------
# Verdict consistency (spec CB-8)
# ---------------------------------------------------------------------------

BAND_RANK = {"STRONG_CANDIDATE": 3, "MODERATE_CANDIDATE": 2,
             "WEAK_CANDIDATE": 1, "NOT_RECOMMENDED": 0}


def bundle_conflicts(bundles: list[dict], verdicts: dict[str, str],
                     max_spread: int = DEFAULT_THRESHOLDS.bundle_max_band_spread) -> list[dict]:
    """Qualifying bundles whose table verdicts span more than max_spread bands.

    The saving needs every table cached, so verdicts far apart inside one
    bundle mean the model judged the tables, not the bundle.

    Args:
        bundles: From detect_bundles().
        verdicts: table_id -> model verdict, for tables the model judged.
        max_spread: Largest allowed band difference inside one bundle.

    Returns:
        One dict per conflicting bundle: {"queryid", "db_time_share_pct",
        "tables", "verdicts": {table_id: verdict}, "spread"}. Tables without a
        verdict are left out; fewer than two judged tables cannot conflict.
    """
    out: list[dict] = []
    for b in bundles or []:
        if not b.get("qualifies"):
            continue
        judged = {t: verdicts[t] for t in b["tables"] if verdicts.get(t) in BAND_RANK}
        if len(judged) < 2:
            continue
        ranks = [BAND_RANK[v] for v in judged.values()]
        spread = max(ranks) - min(ranks)
        if spread > max_spread:
            out.append({"queryid": b["queryid"], "db_time_share_pct": b["db_time_share_pct"],
                        "tables": list(b["tables"]), "verdicts": judged, "spread": spread})
    return out


def merge_table_verdicts(tables: list, repaired: list, allowed: set[str]) -> tuple[list, list[str]]:
    """Replace verdicts for allowed tables with repaired ones; keep the rest.

    Args:
        tables: Current LLMTableVerdict list.
        repaired: LLMTableVerdict list from the repair turn.
        allowed: Table ids the repair turn was asked about.

    Returns:
        (merged list in the original order, ids actually replaced). A repaired
        verdict for a table outside `allowed`, with a bad verdict string, or
        without reasoning or bundle_reasoning is ignored.
    """
    good = {}
    for r in repaired or []:
        if (r.table_id in allowed and r.verdict in BAND_RANK
                and (r.reasoning or "").strip() and (r.bundle_reasoning or "").strip()):
            good[r.table_id] = r
    return [good.get(t.table_id, t) for t in tables], sorted(good)
