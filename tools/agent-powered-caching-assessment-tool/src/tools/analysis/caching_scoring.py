"""
Cache-candidacy scoring — the four dimensions.

REPLACES the generic migration-fit scoring (pattern_match / complexity /
performance / cost) inherited from the upstream tool. That model was built to
rank a workload across six competing migration targets; this one answers a
single question: should a cache sit in front of this table, and what is the
evidence?

THE FOUR DIMENSIONS
-------------------
1. Access Suitability  — is this the right SHAPE of workload?
                         (read dominance, reads per second)
2. Pressure Evidence   — is the source actually STRUGGLING?
                         (buffer misses, disk reads, share of DB time,
                          temp spill, host pressure)
3. Cache Efficiency    — will it cache CLEANLY and cheaply?
                         (result size, determinism, value size)
4. Disqualifiers       — reasons NOT to cache. Applied as penalties and hard
                         exclusions, never as a positive contribution.

DEFECTS FIXED RELATIVE TO UPSTREAM scoring.py
----------------------------------------------
(a) read_ratio is frequency-weighted, not pattern-counted. Upstream counted
    distinct query shapes, so one 5000-calls/sec SELECT plus one hourly UPDATE
    scored as a 50/50 workload. Three of four upstream dimensions tested that
    ratio, so read-heavy tables were systematically understated.
(b) No dependence on db_load_contribution_percent. That field comes from RDS
    Performance Insights and is ALWAYS absent on self-managed PostgreSQL, which
    silently capped the upstream performance score at 75/100 for exactly the
    deployment type this tool targets. Relative load is derived locally instead.
(c) Buffer-pressure signals are actually read. cache_hit_ratio_pct,
    temp_blocks_written, and io_read_time_ms were collected upstream but no
    scoring function consumed them.
(d) Join/FK/column-count complexity is NOT a negative. An expensive, frequently
    run join whose inputs rarely change is among the BEST cache candidates —
    caching avoids re-paying the join cost. Upstream penalized it at 30% weight.

THRESHOLDS
----------
Every threshold below is a v1 DEFAULT, chosen to be defensible but NOT yet
calibrated against real workloads. They live in CacheScoringThresholds so they
can be tuned in one place. See docs/THRESHOLDS.md for the rationale and the
calibration plan.

STATUS: TEMPLATE — dimension structure and signal wiring are complete; the
threshold VALUES need calibration (project plan Day 7 checkpoint).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from src.tools.database.host_sampler import effective_host_view

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunable thresholds — single source of truth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheScoringThresholds:
    """v1 defaults. Not yet calibrated — see docs/THRESHOLDS.md."""

    # --- Access Suitability ---
    # Two parts, read dominance and frequency, 100 points between them. Access skew
    # was removed in 0.4.0: it counted query shapes, not key values, so it never
    # measured a hot key, and a query's was always 0. Its 20 points were shared out in
    # proportion (x 100 / 80).
    read_ratio_excellent: float = 0.90   # >=90% of calls are reads
    read_ratio_good: float = 0.75
    read_ratio_marginal: float = 0.50
    read_ratio_points_excellent: int = 56
    read_ratio_points_good: int = 44
    read_ratio_points_marginal: int = 22
    # Reads per second: a table's write statements are not traffic a cache serves.
    calls_per_sec_high: float = 10.0
    calls_per_sec_moderate: float = 1.0
    calls_per_sec_low: float = 0.1
    calls_points_high: int = 44
    calls_points_moderate: int = 31
    calls_points_low: int = 15

    # --- Pressure Evidence ---
    buffer_hit_ratio_poor: float = 90.0  # below this, PG is missing its own cache
    buffer_hit_ratio_weak: float = 95.0
    buffer_hit_ratio_healthy: float = 99.0
    io_time_share_high: float = 0.40     # blk_read_time / total_exec_time
    io_time_share_moderate: float = 0.15
    # shared_buffers as a fraction of database size. Above this the pool is
    # treated as generously provisioned, so a poor hit ratio means the working
    # set genuinely does not fit. Below it, raising shared_buffers is a cheaper
    # fix than a cache tier, so hit-ratio credit is halved and a concern raised.
    buffer_pool_adequate_ratio: float = 0.25
    # bgwriter percentages are meaningless at tiny volumes: an idle database can
    # report 35% backend writes off a few hundred buffers. A rate, so the gate means
    # the same over a 15-minute window and over two days of counters.
    bgwriter_min_buffers_per_hour: float = 10_000.0
    # Sequential scans are only meaningful evidence when scanning is actually
    # expensive. Scanning a 45-row lookup table thousands of times costs
    # nothing and must not be credited as pressure.
    seq_scan_min_table_mb: float = 50.0
    seq_scan_min_per_hour: float = 100.0
    exec_time_ms_slow: float = 50.0
    exec_time_ms_moderate: float = 10.0
    exec_time_ms_marginal: float = 2.0
    # Load share (Pressure, max 30): the item's share of total database time,
    # in steps. Gives in-memory, CPU-bound work a ceiling matching the buffer
    # hit ratio's 30. Always on, for queries and tables (spec pressure-load-share).
    load_share_low: float = 0.05
    load_share_mid: float = 0.10
    load_share_high: float = 0.20
    load_share_points_low: int = 10
    load_share_points_mid: int = 20
    load_share_points_high: int = 30
    # Bundles (spec cache-bundles): a Not cacheable read query with at least
    # this share of DB time whose tables can all be object cached, each row
    # changing no more often than this, can be computed in the application
    # from cached rows. Facts for the model; no score effect.
    bundle_min_db_share: float = 0.05
    bundle_min_row_change_hours: float = 1.0
    # Model verdicts for the tables of one qualifying bundle may differ by at
    # most this many bands; more gets one repair turn, then a warning.
    bundle_max_band_spread: int = 1
    host_cpu_pressure_pct: float = 70.0
    host_mem_pressure_pct: float = 85.0
    host_swap_active_pct: float = 1.0    # any sustained swap is significant
    bgwriter_backend_pct_high: float = 30.0

    # --- Cache Efficiency (v2, see .kiro/specs/cache-efficiency-v2) ---
    # Reuse: estimated hit rate earning full marks (ElastiCache CacheHitRate
    # guidance treats below ~0.8 as many reads finding nothing cached).
    efficiency_hit_rate_full: float = 0.8
    efficiency_weight_reuse: float = 0.75
    efficiency_weight_size: float = 0.25
    # Result size: time saved per hit, log scale between these.
    time_saved_zero_ms: float = 1.0
    time_saved_full_ms: float = 100.0
    # Bytes factor: full marks up to the soft limit (Azure Cache for Redis
    # guidance: keep values under 100 KB), log scale to 0 at the hard limit
    # (ElastiCache does not migrate slots holding items over 256 MB).
    value_soft_limit_kb: float = 100.0
    value_hard_limit_mb: float = 256.0

    # --- Disqualifiers (v2, see .kiro/specs/disqualifiers-v2) ---
    # The one hard exclusion: already well served and not busy. All five must
    # hold: hit ratio above, mean time below, no spill, host headroom (the
    # host_* pressure thresholds above), and DB time under the busy line.
    well_served_hit_ratio: float = 99.0       # strictly greater than
    well_served_exec_ms: float = 2.0          # strictly less than
    # Busy line: DB time as a share of host CPU capacity (vCPU x 1000 ms/s).
    # At or above it, a fast item is not excluded but penalised and reviewed.
    busy_share_of_capacity: float = 0.05
    busy_penalty: int = 10
    # Heavy writes: log penalty in writes/reads, 0 at the start, the two anchor
    # points below, capped. Never a hard exclusion.
    write_penalty_start: float = 0.30
    write_penalty_at_half: float = 20.0       # at 50% writes
    write_penalty_at_70: float = 40.0         # at 70% writes
    write_penalty_cap: float = 55.0
    # Tables in a blocking chain, resolved by relation OID.
    lock_penalty: int = 15
    # Context only: noted, never penalised.
    dead_tuple_pct_churn: float = 20.0

    # --- Confidence weights (must sum to 1.0) ---
    weight_access: float = 0.35
    weight_pressure: float = 0.40   # highest: this is the "why now" evidence
    weight_efficiency: float = 0.25


DEFAULT_THRESHOLDS = CacheScoringThresholds()


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@dataclass
class CacheTableProfile:
    """Per-table aggregate built from collector output.

    Extends the upstream TableProfile concept with the pressure and churn
    signals the caching decision actually needs.
    """

    table_id: str
    row_count: int = 0
    # None for a query: its cache footprint depends on how many distinct
    # parameter values are used, which is not collected.
    size_mb: float | None = 0.0

    # --- Access (all frequency-weighted) ---
    # Every statement mentioning the table, writes included. Context only: the
    # Access score reads read_calls_per_second.
    total_calls_per_second: float = 0.0
    read_calls_per_second: float = 0.0
    write_calls_per_second: float = 0.0
    read_ratio_weighted: float = 0.0     # THE fix for upstream defect (a)
    # True once reads and writes were actually measured. A read ratio of 0.0
    # otherwise means "no data", not "all writes".
    read_ratio_known: bool = False
    query_count: int = 0

    # --- Pressure ---
    # For a table, from its read statements only (0.4.0): a cache serves reads, so a
    # fast INSERT must not make the table look cheap, and a slow one must not make it
    # look expensive. Writes count in read dominance and the heavy-writes penalty.
    weighted_exec_time_ms: float = 0.0
    buffer_hit_ratio_pct: float | None = None
    io_read_time_share: float | None = None
    temp_blocks_written: int = 0
    seq_scans: int = 0
    index_scans: int = 0
    # seq_scans per hour of the period the table counters cover, None when that
    # period is unknown. The full-scan floor reads this.
    seq_scans_per_hour: float | None = None

    # --- Efficiency ---
    # Largest per-query row average among every query mentioning the table,
    # joins included. Reported for context only; it does not score.
    max_rows_returned: float = 0.0
    # Rows per read of the thing that would be cached. For a table, the
    # call-weighted average over single-table reads of that table; for a query,
    # its own average. None when nothing measured reads it directly.
    rows_per_read: float | None = None
    # Can a cache key be formed from the reads? True for equality filters or no
    # filter, False when every direct read filters by a range, None unmeasured.
    keyable: bool | None = None

    # --- Churn / disqualifiers ---
    dead_tuple_pct: float = 0.0
    is_lock_contended: bool = False
    # Database time per second: read calls/s x mean ms of those reads. For a table,
    # every read that touches it, joins included (the same population as its hit
    # ratio and mean time). Drives load share and the busy line of the well-served
    # exclusion.
    db_ms_per_sec: float = 0.0

    # --- Provenance ---
    query_stats_available: bool = True   # False in DEGRADED mode
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Workload-level context
# ---------------------------------------------------------------------------

# pg_settings reports a size as a count plus a unit, where the unit is often
# "8kB" (the block size) rather than bytes. 16384 with unit "8kB" is 128 MB.
_PG_UNIT_TO_MB: dict[str, float] = {
    "8kB": 8 / 1024,
    "kB": 1 / 1024,
    "MB": 1.0,
    "GB": 1024.0,
    "B": 1 / (1024 * 1024),
}


def parse_pg_size_setting(entry: dict | None) -> float | None:
    """Convert a pg_settings size entry to megabytes.

    Args:
        entry: {"setting": int|str, "unit": str|None} from pg_settings.

    Returns:
        Size in MB, or None when the entry is missing or is not a size (for
        example max_connections, which has no unit).
    """
    if not entry:
        return None
    setting, unit = entry.get("setting"), entry.get("unit")
    if setting is None or unit is None:
        return None
    try:
        value = float(setting)
    except (TypeError, ValueError):
        return None
    factor = _PG_UNIT_TO_MB.get(str(unit))
    return round(value * factor, 3) if factor is not None else None


def assess_buffer_pool(
    config: dict,
    database_size_gb: float | None,
    thresholds: CacheScoringThresholds = None,  # type: ignore[assignment]
) -> dict:
    """Judge whether shared_buffers is adequately provisioned.

    This is the context required to interpret a buffer hit ratio correctly. The
    same 90% means opposite things at different pool sizes: against a 128 MB
    pool it says the pool is too small, against a 32 GB pool it says the data
    genuinely does not fit and an external cache is the real answer.

    Args:
        config: The "configuration" block from collected metrics.
        database_size_gb: Database size, the comparison baseline.
        thresholds: Supplies buffer_pool_adequate_ratio.

    Returns:
        {"shared_buffers_mb", "database_size_mb", "pool_to_db_ratio",
         "adequate": bool | None, "summary": str}. adequate is None when it
        cannot be determined, which is treated as "no opinion" rather than
        as either good or bad.
    """
    t = thresholds or DEFAULT_THRESHOLDS
    pool_mb = parse_pg_size_setting((config or {}).get("shared_buffers"))
    db_mb = round(database_size_gb * 1024, 3) if database_size_gb else None

    out: dict = {
        "shared_buffers_mb": pool_mb,
        "database_size_mb": db_mb,
        "pool_to_db_ratio": None,
        "adequate": None,
        "summary": "shared_buffers or database size unavailable",
    }
    if pool_mb is None or not db_mb:
        return out

    ratio = round(pool_mb / db_mb, 4)
    out["pool_to_db_ratio"] = ratio
    out["adequate"] = ratio >= t.buffer_pool_adequate_ratio

    if out["adequate"]:
        out["summary"] = (
            f"shared_buffers is {pool_mb:,.0f} MB, {ratio:.1f}x the "
            f"{db_mb:,.0f} MB database, so it is generously provisioned"
        )
    else:
        # Use 2 significant figures so very small ratios do not render as "0%".
        pct = f"{ratio * 100:.2g}%"
        out["summary"] = (
            f"shared_buffers is only {pool_mb:,.0f} MB, {pct} of the "
            f"{db_mb:,.0f} MB database, so it may simply be undersized"
        )
    return out


def compute_lock_wait_share(wait_events: list[dict]) -> float | None:
    """Fraction of waiting sessions blocked on locks rather than I/O.

    Distinguishes I/O-bound slowness, which a cache fixes, from lock-bound
    slowness, which it does not.

    Args:
        wait_events: Rows from pg_stat_activity with wait_event_type and
            session_count.

    Returns:
        Share between 0 and 1, or None when no session was waiting on anything.
        None means "no conclusion available", which is deliberately different
        from a share of zero.
    """
    if not wait_events:
        return None

    waiting = 0
    lock_waiting = 0
    for row in wait_events:
        wtype = str(row.get("wait_event_type") or "None")
        if wtype in ("None", "", "null"):
            continue
        count = int(row.get("session_count") or 0)
        waiting += count
        if wtype == "Lock":
            lock_waiting += count

    return round(lock_waiting / waiting, 4) if waiting else None


@dataclass
class WorkloadContext:
    """Signals that describe the deployment rather than any single table.

    Buffer pool size, checkpoint behavior, host saturation, and lock contention
    are all properties of the database and its host. They are computed once and
    applied to every table's score.
    """

    host_metrics: dict = field(default_factory=dict)
    global_stats: dict = field(default_factory=dict)
    buffer_pool: dict = field(default_factory=dict)
    bgwriter: dict = field(default_factory=dict)
    lock_wait_share: float | None = None
    # Average statements executing during the stats window: total database time
    # per second / 1000, by Little's law. Covers the same period as the query
    # statistics, so it is a CPU pressure signal that a late host snapshot is not.
    statement_concurrency: float | None = None
    # The period the statistics describe (statistics_window()): mode "snapshot" for a
    # collection window, "since_reset" otherwise, with its length in seconds.
    stats_window: dict = field(default_factory=dict)
    # Seconds the pg_stat_user_tables counters cover, so a scan count becomes a rate.
    table_counter_seconds: float | None = None


def statistics_window(collector_output: dict) -> dict:
    """The period a collection's statistics describe.

    Args:
        collector_output: Raw collector output.

    Returns:
        metadata.collection_window when present; for an older collection, a
        since_reset record with the query statistics' window length (or None).
    """
    meta = (collector_output.get("metadata") or {}).get("collection_window")
    if meta:
        return dict(meta)
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    secs = next((q.get("stats_window_seconds") for q in queries
                 if q.get("stats_window_seconds")), None)
    return {"mode": "since_reset", "seconds": secs}


def table_counter_seconds(collector_output: dict) -> float | None:
    """Seconds pg_stat_user_tables counters cover: the window, the database statistics
    reset, or (older collections) the query statistics window."""
    metrics = collector_output.get("metrics") or {}
    for v in (metrics.get("table_churn_window_seconds"),
              (metrics.get("global_stats") or {}).get("stats_window_seconds"),
              statistics_window(collector_output).get("seconds")):
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def build_workload_context(
    collector_output: dict,
    thresholds: CacheScoringThresholds = DEFAULT_THRESHOLDS,
) -> WorkloadContext:
    """Assemble workload-level context from a collection artifact.

    Args:
        collector_output: Raw collector output.
        thresholds: Scoring thresholds.

    Returns:
        Populated WorkloadContext. Missing sections become empty rather than
        raising, so a partial collection still scores.
    """
    metrics = collector_output.get("metrics") or {}
    source = (collector_output.get("metadata") or {}).get("source_database") or {}
    bgwriter = dict(metrics.get("bgwriter") or {})
    if bgwriter and not bgwriter.get("window_seconds"):
        # Older collections: the query statistics window is the best stand-in.
        bgwriter["window_seconds"] = statistics_window(collector_output).get("seconds")

    return WorkloadContext(
        host_metrics=collector_output.get("host_metrics") or {},
        global_stats=metrics.get("global_stats") or {},
        buffer_pool=assess_buffer_pool(
            metrics.get("configuration") or {},
            source.get("database_size_gb"),
            thresholds,
        ),
        bgwriter=bgwriter,
        lock_wait_share=compute_lock_wait_share(metrics.get("wait_events") or []),
        statement_concurrency=_statement_concurrency(collector_output),
        stats_window=statistics_window(collector_output),
        table_counter_seconds=table_counter_seconds(collector_output),
    )


def _statement_concurrency(collector_output: dict) -> float | None:
    """Average statements executing during the stats window.

    Args:
        collector_output: Raw collector output.

    Returns:
        Over a collection window, every statement's database time in the window
        divided by its length, / 1000. Otherwise sum(calls/s x avg ms) / 1000 over the
        collected patterns. None without query statistics.
    """
    window = statistics_window(collector_output)
    st = window.get("statements") or {}
    if (window.get("mode") == "snapshot" and (window.get("basis") or {}).get("statements")
            == "window" and st.get("db_time_ms") is not None and window.get("seconds")):
        return round(float(st["db_time_ms"]) / float(window["seconds"]) / 1000.0, 3)
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    if not queries:
        return None
    total_ms = sum(
        float(q.get("calls_per_second") or 0.0) * float(q.get("execution_time_ms_avg") or 0.0)
        for q in queries
    )
    return round(total_ms / 1000.0, 3)


@dataclass
class CacheScoreBreakdown:
    """The four dimensions plus derived verdict."""

    access_suitability: int = 0
    pressure_evidence: int = 0
    # None when Efficiency could not be scored. Confidence then rescales over
    # Access and Pressure rather than counting it as 0.
    cache_efficiency: int | None = None
    # Full Efficiency detail (cache_efficiency.EfficiencyResult), or None.
    efficiency: object | None = None
    disqualifier_penalty: int = 0        # positive number, SUBTRACTED
    hard_excluded: bool = False
    # Why it cannot be recommended: "well_served" (the one disqualifier hard
    # exclusion) or "not_cacheable" (a parser gate). None when not excluded.
    exclusion_kind: str | None = None
    # Each soft penalty: {"points": int, "reason": str}. Sums to
    # disqualifier_penalty.
    penalties: list[dict] = field(default_factory=list)
    busy_but_fast: bool = False
    db_share_of_capacity: float | None = None
    # The five well-served criteria and whether each held, for the record.
    well_served: dict | None = None
    # The primary exclusion. exclusion_reasons keeps every one that applied, in
    # precedence order, so none is hidden.
    exclusion_reason: str | None = None
    exclusion_reasons: list[str] = field(default_factory=list)
    confidence: int = 0
    evidence: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Profile construction
# ---------------------------------------------------------------------------


def build_cache_profiles(
    collector_output: dict,
    thresholds: CacheScoringThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, CacheTableProfile]:
    """Build a CacheTableProfile for every table in the collector output.

    Handles DEGRADED mode: when pg_stat_statements was unavailable, query
    patterns are empty and only table/global/host signals are populated. The
    profile records that so scoring can lower confidence rather than silently
    reporting a low score as if it were evidence-backed.
    """
    schema = collector_output.get("database_schema") or {}
    tables = schema.get("tables") or []
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    churn_rows = (collector_output.get("metrics") or {}).get("table_churn") or []
    lock_rows = (collector_output.get("metrics") or {}).get("lock_contention") or []

    query_stats_available = bool(queries)

    # Index churn + lock data by table name for lookup.
    churn_by_table = {
        r.get("table_name"): r for r in churn_rows if r.get("table_name")
    }
    locked_tables = _tables_from_lock_rows(lock_rows)

    # Group queries by table.
    by_table: dict[str, list[dict]] = {}
    for q in queries:
        for tid in q.get("tables_accessed") or []:
            by_table.setdefault(tid, []).append(q)

    # Write rates from parsed write TARGETS. Keyword classification charged a
    # write to every table the statement mentions, so INSERT INTO order_item
    # ... SELECT FROM product scored 24 writes/s against product, which is never
    # written. Shared with the per-query dimension so both agree on writes.
    # Imported here, not at module level, to avoid a circular import.
    parsed_writes: dict[str, float] | None = None
    solo_reads: dict[str, dict] = {}
    if queries:
        from src.tools.analysis.query_cache_analysis import (
            build_schema_maps,
            build_solo_read_map,
            build_view_map,
            build_write_rate_map,
        )
        pks, known = build_schema_maps(collector_output)
        parsed_writes = build_write_rate_map(queries, pks, known)
        # Efficiency for object caching is judged on the reads that would be
        # served from the cache: single-table reads of this table. Joined
        # reports return rows of several tables and are not this table's unit.
        solo_reads = build_solo_read_map(
            queries, pks, known, build_view_map(collector_output, known))

    profiles: dict[str, CacheTableProfile] = {}
    for t in tables:
        tid = t.get("table_id") or t.get("table_name")
        if not tid:
            continue
        short_name = str(tid).rsplit(".", 1)[-1]

        p = CacheTableProfile(
            table_id=str(tid),
            row_count=int(t.get("row_count") or 0),
            size_mb=float(t.get("size_mb") or 0.0),
            query_stats_available=query_stats_available,
        )

        _apply_query_aggregates(p, by_table.get(str(tid), []), thresholds)
        if parsed_writes is not None:
            _apply_parsed_writes(p, parsed_writes.get(str(tid).lower(), 0.0))
        solo = solo_reads.get(str(tid).lower())
        if solo:
            p.rows_per_read = solo["rows_per_read"]
            p.keyable = solo["keyable"]
        _apply_churn(p, churn_by_table.get(short_name), table_counter_seconds(collector_output))
        p.is_lock_contended = str(tid).lower() in locked_tables
        p.db_ms_per_sec = round(p.read_calls_per_second * p.weighted_exec_time_ms, 3)

        if not query_stats_available:
            p.notes.append(
                "DEGRADED: pg_stat_statements unavailable, so query-level signals are "
                "missing; scored from table, global, and host data only"
            )

        profiles[str(tid)] = p

    return profiles


def _apply_query_aggregates(
    p: CacheTableProfile,
    queries: list[dict],
    thresholds: CacheScoringThresholds,
) -> None:
    """Aggregate query patterns into the profile, weighting by call volume.

    Call counts cover every statement (reads for read dominance, writes for context
    until _apply_parsed_writes replaces them). Mean time, disk share, hit ratio and
    spill come from read statements only: those are what a cache would serve, and a
    write's cost is not saved by caching the table.
    """
    if not queries:
        return

    read_types = {"SELECT"}
    write_types = {"INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE", "UPSERT"}

    read_cps = 0.0
    write_cps = 0.0
    total_cps = 0.0
    weighted_exec = 0.0
    io_time_total = 0.0
    cumulative_exec_total = 0.0
    hit_num = 0.0
    hit_den = 0.0
    temp_written = 0
    max_rows = 0.0

    for q in queries:
        cps = float(q.get("calls_per_second") or 0.0)
        # Fall back to hourly frequency when calls_per_second is absent.
        if cps == 0.0 and q.get("frequency_per_hour"):
            cps = float(q["frequency_per_hour"]) / 3600.0
        total_cps += cps

        qtype = str(q.get("query_type") or "").upper()
        if qtype in write_types:
            write_cps += cps
        if qtype not in read_types:
            continue
        read_cps += cps

        exec_ms = float(q.get("execution_time_ms_avg") or 0.0)
        weighted_exec += cps * exec_ms

        # io_read_time_ms and total_time_ms cover the same period, so they are
        # comparable. execution_time_ms_avg is per-call and MUST NOT be used as the
        # denominator here: doing so produced ratios above 100% (a real defect
        # caught by the negative fixture). When total_time_ms is absent the share is
        # left undefined rather than invented from mismatched units.
        io_time_total += float(q.get("io_read_time_ms") or 0.0)
        cumulative_exec_total += float(q.get("total_time_ms") or 0.0)

        # Weight each query's hit ratio by its call volume.
        hit = q.get("cache_hit_ratio_pct")
        if hit is not None:
            hit_num += float(hit) * max(cps, 1e-9)
            hit_den += max(cps, 1e-9)

        temp_written += int(q.get("temp_blocks_written") or 0)

        rows = float(q.get("rows_returned_avg") or 0.0)
        max_rows = max(max_rows, rows)

    p.query_count = len(queries)
    p.total_calls_per_second = round(total_cps, 4)
    p.read_calls_per_second = round(read_cps, 4)
    p.write_calls_per_second = round(write_cps, 4)

    classified = read_cps + write_cps
    p.read_ratio_weighted = round(read_cps / classified, 4) if classified > 0 else 0.0
    p.read_ratio_known = classified > 0

    p.weighted_exec_time_ms = round(weighted_exec / read_cps, 3) if read_cps > 0 else 0.0
    p.buffer_hit_ratio_pct = round(hit_num / hit_den, 2) if hit_den > 0 else None

    if cumulative_exec_total > 0:
        # Clamp to 1.0: I/O time can slightly exceed execution time when reads
        # are attributed across overlapping statements.
        p.io_read_time_share = round(min(io_time_total / cumulative_exec_total, 1.0), 4)
    else:
        p.io_read_time_share = None
        if io_time_total > 0:
            p.notes.append(
                "io_read_time_share unavailable: total_time_ms missing from query "
                "statistics, so disk-time share could not be computed"
            )
    p.temp_blocks_written = temp_written
    p.max_rows_returned = max_rows


def _apply_parsed_writes(p: CacheTableProfile, writes_per_s: float) -> None:
    """Replace mention-based writes with parsed write targets.

    read_ratio_weighted becomes reads / (reads + measured writes), which is what
    the Access score and the write-dominated exclusion read. Measured writes are
    used as-is, for tables exactly as for queries.

    Args:
        p: Profile, already carrying read_calls_per_second.
        writes_per_s: Write statements per second targeting this table.
    """
    p.write_calls_per_second = round(writes_per_s, 4)
    reads = p.read_calls_per_second
    denom = reads + writes_per_s
    p.read_ratio_weighted = round(reads / denom, 4) if denom > 0 else 0.0
    p.read_ratio_known = denom > 0


def _apply_churn(p: CacheTableProfile, churn: dict | None,
                 counter_seconds: float | None = None) -> None:
    """Attach dead-tuple churn and scan counters from pg_stat_user_tables.

    Args:
        p: Profile.
        churn: The table's pg_stat_user_tables row.
        counter_seconds: The period its counters cover; makes the scan count a rate.
    """
    if not churn:
        return
    live = int(churn.get("n_live_tup") or 0)
    dead = int(churn.get("n_dead_tup") or 0)
    p.dead_tuple_pct = round(dead / max(live + dead, 1) * 100, 2)
    p.seq_scans = int(churn.get("seq_scan") or 0)
    p.index_scans = int(churn.get("idx_scan") or 0)
    if counter_seconds:
        p.seq_scans_per_hour = round(p.seq_scans / (counter_seconds / 3600.0), 2)

    # When query stats are unavailable, derive a coarse read ratio from
    # table-level counters so DEGRADED mode still produces something.
    if not p.query_stats_available:
        reads = int(churn.get("seq_tup_read") or 0) + int(churn.get("idx_tup_fetch") or 0)
        writes = (
            int(churn.get("n_tup_ins") or 0)
            + int(churn.get("n_tup_upd") or 0)
            + int(churn.get("n_tup_del") or 0)
        )
        if reads + writes > 0:
            p.read_ratio_weighted = round(reads / (reads + writes), 4)
            p.read_ratio_known = True
            p.notes.append("read_ratio derived from table counters (DEGRADED mode)")


def _tables_from_lock_rows(lock_rows: list[dict]) -> set[str]:
    """Tables in a blocking chain, resolved by relation OID (spec DQ-6).

    Uses relation_name from collect_lock_contention, which pg_class resolved
    from pg_locks.relation. Query text is not parsed: a name match in text is
    not evidence that a lock is held on that table. Collections made before the
    relation was resolved yield no tables, so carry no lock penalty.

    Args:
        lock_rows: Rows from metrics.lock_contention.

    Returns:
        Lowercased schema.table names.
    """
    return {str(r["relation_name"]).lower() for r in lock_rows or []
            if r.get("relation_name")}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def fmt_mb(mb: float) -> str:
    """Format a size in MB: two decimals below 1, one below 10, so it never reads as 0.

    Args:
        mb: Size in megabytes.

    Returns:
        Display string without the unit.
    """
    if mb < 1:
        return f"{mb:.2f}"
    return f"{mb:.1f}" if mb < 10 else f"{mb:,.0f}"


def score_cache_candidacy(
    profile: CacheTableProfile,
    context: WorkloadContext | None = None,
    thresholds: CacheScoringThresholds = DEFAULT_THRESHOLDS,
    efficiency=None,
) -> CacheScoreBreakdown:
    """Score one table or query across the four dimensions.

    Args:
        profile: Per-table aggregate (or a query expressed in that shape).
        context: Workload-level signals (buffer pool, host, bgwriter, locks).
            These are properties of the deployment, not of one table, so they
            are computed once and applied to every table.
        thresholds: Scoring thresholds.
        efficiency: cache_efficiency.EfficiencyResult for this item, computed
            from its read queries. None means Efficiency is not scored.

    Returns:
        Populated CacheScoreBreakdown.
    """
    ctx = context or WorkloadContext()
    b = CacheScoreBreakdown()

    b.access_suitability = _score_access(profile, thresholds, b)
    b.pressure_evidence = _score_pressure(profile, ctx, thresholds, b)
    b.efficiency = efficiency
    b.cache_efficiency = _apply_efficiency(efficiency, b)
    b.disqualifier_penalty, b.hard_excluded, b.exclusion_reasons = _score_disqualifiers(
        profile, ctx, thresholds, b
    )
    b.exclusion_reason = b.exclusion_reasons[0] if b.exclusion_reasons else None

    b.confidence = compute_cache_confidence(b, profile, thresholds)
    return b


def _score_access(
    p: CacheTableProfile, t: CacheScoringThresholds, b: CacheScoreBreakdown
) -> int:
    """Dimension 1 — is this the right shape of workload? (0-100)

    Read dominance (max 56) and reads per second (max 44). For a table, both count
    reads; writes enter only as the denominator of read dominance.
    """
    score = 0

    # Read dominance, frequency-weighted (max 56)
    if p.read_ratio_weighted >= t.read_ratio_excellent:
        score += t.read_ratio_points_excellent
        b.evidence.append(
            f"Strongly read-dominated: {p.read_ratio_weighted:.0%} of calls are reads"
        )
    elif p.read_ratio_weighted >= t.read_ratio_good:
        score += t.read_ratio_points_good
        b.evidence.append(f"Read-dominated: {p.read_ratio_weighted:.0%} of calls are reads")
    elif p.read_ratio_weighted >= t.read_ratio_marginal:
        score += t.read_ratio_points_marginal
    elif not p.read_ratio_known:
        b.concerns.append("The mix of reads and writes was not measured")
    else:
        b.concerns.append(
            f"Write-heavy: only {p.read_ratio_weighted:.0%} of calls are reads"
        )

    # Reads per second (max 44)
    cps = p.read_calls_per_second
    if cps >= t.calls_per_sec_high:
        score += t.calls_points_high
        b.evidence.append(f"High traffic: {cps:.1f} reads/sec")
    elif cps >= t.calls_per_sec_moderate:
        score += t.calls_points_moderate
        b.evidence.append(f"Moderate traffic: {cps:.1f} reads/sec")
    elif cps >= t.calls_per_sec_low:
        score += t.calls_points_low
    else:
        b.concerns.append(f"Low traffic: {cps:.3f} reads/sec")

    return max(0, min(score, 100))


def _score_pressure(
    p: CacheTableProfile,
    ctx: WorkloadContext,
    t: CacheScoringThresholds,
    b: CacheScoreBreakdown,
) -> int:
    """Dimension 2 — is the source actually struggling? (0-100)

    This is the "why now" dimension and carries the highest confidence weight.
    A workload can be perfectly shaped for caching and still not need one.
    """
    score = 0

    # Buffer cache misses (max 30) — the single most direct signal, now scored
    # in the context of how large shared_buffers actually is.
    score += _score_hit_ratio(p, ctx, t, b)

    # Disk I/O share of execution time (max 20)
    share = p.io_read_time_share
    if share is not None:
        if share >= t.io_time_share_high:
            score += 20
            b.evidence.append(f"Disk-bound: {share:.0%} of execution time is disk reads")
        elif share >= t.io_time_share_moderate:
            score += 12

    # Expensive to compute (max 20) — high value in caching the result
    ms = p.weighted_exec_time_ms
    if ms >= t.exec_time_ms_slow:
        score += 20
        b.evidence.append(f"Expensive queries: {ms:.1f}ms weighted average")
    elif ms >= t.exec_time_ms_moderate:
        score += 14
    elif ms >= t.exec_time_ms_marginal:
        score += 6

    # Share of all database work (max 30). A query served from memory can be
    # most of the database's load; without this, Pressure saw only disk.
    score += _score_load_share(p, ctx, t, b)

    # Memory spill (max 10)
    if p.temp_blocks_written > 0:
        score += 10
        b.evidence.append(
            f"Spilling to disk: {p.temp_blocks_written} temp blocks written"
        )

    # Repeated full scans (max 10) — only when scanning is actually expensive.
    # A small lookup table scanned thousands of times costs almost nothing and
    # is not evidence of pressure. A rate, so the floor means the same over any
    # period; unknown when the period the counters cover is unknown.
    total_scans = p.seq_scans + p.index_scans
    if (
        total_scans > 0
        and p.seq_scans / total_scans > 0.5
        and p.seq_scans_per_hour is not None
        and p.seq_scans_per_hour > t.seq_scan_min_per_hour
        and p.size_mb is not None
        and p.size_mb >= t.seq_scan_min_table_mb
    ):
        score += 10
        b.evidence.append(
            f"Frequent full scans of a {p.size_mb:.0f} MB table: "
            f"{p.seq_scans_per_hour:,.0f} sequential scans an hour"
        )

    # Buffer pool write pressure (max 10) — workload-level
    score += _score_bgwriter(ctx, t, b)

    # Host capacity pressure (max 10) — workload-level, not per-table
    score += _score_host_pressure(ctx.host_metrics, t, b,
                                  concurrency=ctx.statement_concurrency)

    return max(0, min(score, 100))


def load_share(item_ms_per_sec: float | None, concurrency: float | None) -> float | None:
    """An item's share of total database time.

    Total DB time is statement_concurrency x 1000 ms/s, the same calls x mean
    ms basis as each item's db_ms_per_sec. A table's DB time includes joins, so
    tables in one join share its time and a table share is capped at 1.0.

    Args:
        item_ms_per_sec: The item's DB time, ms per second.
        concurrency: WorkloadContext.statement_concurrency.

    Returns:
        Share 0 to 1, or None when either input is missing or the total is 0.
    """
    try:
        if item_ms_per_sec is None or not concurrency or float(concurrency) <= 0:
            return None
        return min(float(item_ms_per_sec) / (float(concurrency) * 1000.0), 1.0)
    except (TypeError, ValueError):
        logger.warning("load_share: unusable input item=%r concurrency=%r",
                       item_ms_per_sec, concurrency)
        return None


def load_share_points(share: float | None,
                      t: CacheScoringThresholds = DEFAULT_THRESHOLDS) -> int:
    """Pressure points for a load share, in steps (30 / 20 / 10 at 20 / 10 / 5%).

    Args:
        share: From load_share().
        t: Thresholds.

    Returns:
        Points, 0 for None or below the lowest step.
    """
    if share is None:
        return 0
    if share >= t.load_share_high:
        return t.load_share_points_high
    if share >= t.load_share_mid:
        return t.load_share_points_mid
    if share >= t.load_share_low:
        return t.load_share_points_low
    return 0


def _score_load_share(
    p: CacheTableProfile,
    ctx: WorkloadContext,
    t: CacheScoringThresholds,
    b: CacheScoreBreakdown,
) -> int:
    """Score the item's share of all database work (max 30).

    Args:
        p: Profile; db_ms_per_sec is read (join-inclusive for tables).
        ctx: Workload context; statement_concurrency gives total DB time.
        t: Thresholds.
        b: Breakdown; evidence is appended.

    Returns:
        Points, 0 when the share is unknown or under the lowest step.
    """
    share = load_share(p.db_ms_per_sec, ctx.statement_concurrency)
    pts = load_share_points(share, t)
    if pts:
        b.evidence.append(
            f"Large share of database work: {share:.0%} of all database time"
            # Table ids are schema.table; query profiles carry the queryid.
            + (" (joins included)" if "." in str(p.table_id) else ""))
    return pts


def _score_hit_ratio(
    p: CacheTableProfile,
    ctx: WorkloadContext,
    t: CacheScoringThresholds,
    b: CacheScoreBreakdown,
) -> int:
    """Score the buffer hit ratio, adjusted for buffer pool adequacy (max 30).

    A low hit ratio against an undersized shared_buffers points at PostgreSQL
    configuration, not at a missing cache tier. Raising shared_buffers is much
    cheaper than operating a cache, so in that case credit is halved and the
    cheaper remedy is surfaced as a concern.

    A low hit ratio against a generously sized pool is the strong case: the
    working set genuinely does not fit in memory.
    """
    hit = p.buffer_hit_ratio_pct
    if hit is None:
        hit = ctx.global_stats.get("cache_hit_ratio_pct")
    if hit is None:
        return 0

    if hit < t.buffer_hit_ratio_poor:
        base = 30
        severity = "Poor"
    elif hit < t.buffer_hit_ratio_weak:
        base = 20
        severity = "Weak"
    elif hit < t.buffer_hit_ratio_healthy:
        base = 8
        severity = "Below target"
    else:
        b.concerns.append(
            f"Buffer hit ratio already healthy ({hit:.1f}%): "
            "PostgreSQL is serving this from memory"
        )
        return 0

    adequate = ctx.buffer_pool.get("adequate")
    summary = ctx.buffer_pool.get("summary", "")

    if adequate is False:
        # The cheaper fix exists. Halve the credit and say so plainly.
        b.concerns.append(
            f"{severity} buffer hit ratio ({hit:.1f}%), but {summary}. "
            "Raising shared_buffers is cheaper than adding a cache tier and "
            "should be evaluated first."
        )
        return base // 2

    if adequate is True:
        b.evidence.append(
            f"{severity} buffer hit ratio ({hit:.1f}%) despite {summary}, so "
            "the working set genuinely does not fit in memory"
        )
    else:
        # Pool size unknown. Score the ratio on its own, without claiming context.
        b.evidence.append(
            f"{severity} buffer hit ratio ({hit:.1f}%): PostgreSQL is going to disk"
        )
    return base


def _score_bgwriter(
    ctx: WorkloadContext, t: CacheScoringThresholds, b: CacheScoreBreakdown
) -> int:
    """Score buffer-pool write pressure from pg_stat_bgwriter (max 10).

    PostgreSQL normally flushes dirty buffers during checkpoints or via the
    background writer. When ordinary backends have to flush their own buffers,
    the pool is under pressure and queries pay the write cost inline.

    Guarded by a volume floor, as a rate per hour: on an idle database the
    percentage is statistically meaningless. The live test database reported 34.5%
    off only 4,481 buffers, which is noise, not pressure. Without the period the
    counters cover, the rate is unknown and nothing is credited.
    """
    bg = ctx.bgwriter
    if not bg:
        return 0
    pct = bg.get("buffers_backend_pct")
    if pct is None:
        return 0

    total = (
        int(bg.get("buffers_backend") or 0)
        + int(bg.get("buffers_checkpoint") or 0)
        + int(bg.get("buffers_clean") or 0)
    )
    try:
        secs = float(bg.get("window_seconds") or 0)
    except (TypeError, ValueError):
        secs = 0.0
    if secs <= 0:
        return 0
    per_hour = total / (secs / 3600.0)
    if per_hour < t.bgwriter_min_buffers_per_hour:
        return 0

    if float(pct) >= t.bgwriter_backend_pct_high:
        b.evidence.append(
            f"Buffer pool under write pressure: {float(pct):.0f}% of buffer writes "
            f"were performed by backends rather than checkpoints ({per_hour:,.0f} "
            "buffers an hour)"
        )
        return 10
    return 0


def _score_host_pressure(
    host: dict, t: CacheScoringThresholds, b: CacheScoreBreakdown,
    concurrency: float | None = None,
) -> int:
    """Host saturation contribution to Pressure Evidence (max 10).

    CPU pressure is credited from either source: host CPU from the effective
    view, or statement concurrency per vCPU, which is derived from the query
    statistics and so covers the same window. Credited once, not twice.

    Returns 0 when host metrics are unavailable rather than penalizing —
    absence of data is not evidence of health.
    """
    if not host:
        return 0

    pts = 0
    view = effective_host_view(host)
    cpu = view.get("cpu_pct")
    mem = view.get("memory_used_pct")
    swap = view.get("swap_used_pct")
    vcpu = view.get("vcpu_count")

    per_vcpu = (concurrency / vcpu) if (concurrency is not None and vcpu) else None
    cpu_by_concurrency = (
        per_vcpu is not None and per_vcpu * 100 >= t.host_cpu_pressure_pct
    )

    if view.get("source") == "unknown" and not cpu_by_concurrency:
        # Say so explicitly. Silence here would read as "no pressure found".
        b.concerns.append(
            f"Host CPU during the statistics window is unknown: {view.get('note')}"
        )

    if cpu is not None and cpu >= t.host_cpu_pressure_pct:
        pts += 4
        label = "p95 over the sampled window" if view.get("source") == "window" else "snapshot"
        b.evidence.append(f"Host CPU under pressure ({cpu:.0f}%, {label})")
    elif cpu_by_concurrency:
        pts += 4
        b.evidence.append(
            f"CPU under pressure: {concurrency:.1f} statements executing on average "
            f"against {vcpu} vCPU during the statistics window"
        )
    if mem is not None and mem >= t.host_mem_pressure_pct:
        pts += 3
        b.evidence.append(f"Host memory under pressure ({mem:.0f}% used)")
    if swap is not None and swap >= t.host_swap_active_pct:
        pts += 3
        b.evidence.append(f"Host is swapping ({swap:.1f}% of swap in use)")

    if pts == 0 and cpu is not None:
        b.concerns.append(
            "Host has resource headroom: consider tuning shared_buffers or "
            "indexing before adding a cache tier"
        )
    return min(pts, 10)


def _fmt_bytes(n: float) -> str:
    """Human-readable byte count."""
    for unit, size in (("MB", 1024 * 1024), ("KB", 1024)):
        if n >= size:
            return f"{n / size:,.1f} {unit}"
    return f"{n:,.0f} B"


def _apply_efficiency(e, b: CacheScoreBreakdown) -> int | None:
    """Dimension 3: how efficiently would a cache serve these reads?

    The score itself is computed by cache_efficiency from the item's read
    queries (estimated hit rate 75%, result size 25%). This records it on the
    breakdown and adds plain-language evidence and concerns.

    Args:
        e: cache_efficiency.EfficiencyResult, or None when not computed.
        b: Breakdown; evidence and concerns are appended.

    Returns:
        Efficiency score 0 to 100, or None when it could not be scored.
    """
    if e is None:
        b.concerns.append("Cache efficiency not scored: no read query was measured")
        return None

    if e.estimated_hit_rate is not None:
        line = (f"Estimated hit rate {e.estimated_hit_rate:.0%} over the "
                f"{(e.window_seconds or 0) / 60:,.0f}-minute window")
        if e.k is not None:
            line += f" ({e.calls_in_window:,.0f} calls over {e.k:,.0f} distinct entries, K from {e.k_source})"
        elif e.k_source:
            line += f" ({e.k_source})"
        (b.evidence if e.estimated_hit_rate >= 0.8 else b.concerns).append(line)
    elif e.reuse_deferred_reason:
        b.concerns.append(f"Reuse left to the reviewer: {e.reuse_deferred_reason}")
    elif e.reuse_missing_reason:
        b.concerns.append(f"Reuse not scored: {e.reuse_missing_reason}")

    if e.result_size_score is not None and e.result_bytes is not None:
        line = (f"Each hit skips {e.time_saved_ms:.2f} ms of database work and returns "
                f"about {_fmt_bytes(e.result_bytes)}")
        (b.evidence if e.result_size_score >= 50 else b.concerns).append(line)
    elif e.size_missing_reason and e.queries_rolled_up == 0:
        b.concerns.append(f"Result size not scored: {e.size_missing_reason}")

    return e.score


def _score_disqualifiers(
    p: CacheTableProfile,
    ctx: WorkloadContext,
    t: CacheScoringThresholds,
    b: CacheScoreBreakdown,
) -> tuple[int, bool, list[str]]:
    """Dimension 4: reasons not to cache (spec disqualifiers-v2).

    One hard exclusion: already well served and not busy, when all five hold
    (hit ratio, speed, no spill, host headroom, under the busy line). Every
    other reason is a flat soft penalty, listed in b.penalties:

        busy but fast     the first four hold, but at or above the busy line
        heavy writes      log scale from 30% writes, capped
        locks             the table is in a blocking chain

    Churn is noted as context only. Traffic volume is left to Access and
    Efficiency. Workload-wide lock waits are workload context only.

    Args:
        p: Profile.
        ctx: Workload context (host, statement concurrency).
        t: Thresholds.
        b: Breakdown; penalties, well-served detail and concerns are set.

    Returns:
        (soft penalty, hard_excluded, exclusion reasons in precedence order).
    """
    # 1. Already well served and not busy: the only hard exclusion (DQ-1).
    ws = assess_well_served(p, ctx, t)
    b.well_served = ws
    b.db_share_of_capacity = ws["db_share_of_capacity"]
    if ws["excluded"]:
        reason = (
            f"Already well served: PostgreSQL answers this from memory in "
            f"{p.weighted_exec_time_ms:.2f} ms with no disk spill, the host has "
            f"headroom, and it uses {share_text(ws['db_share_of_capacity'])} of host CPU "
            "capacity, so a cache would save nothing measurable"
        )
        b.exclusion_kind = "well_served"
        b.concerns.append(reason)
        return 0, True, [reason]

    def add(points: int, reason: str) -> None:
        b.penalties.append({"points": int(points), "reason": reason})
        b.concerns.append(f"{reason} (\u2212{int(points)})")

    # 2. Busy but fast (DQ-3).
    if ws["busy"]:
        b.busy_but_fast = True
        add(t.busy_penalty,
            f"Busy but fast: answered from memory in {p.weighted_exec_time_ms:.2f} ms, "
            f"but it uses {share_text(ws['db_share_of_capacity'])} of host CPU capacity. "
            "Whether that is worth caching depends on the application")

    # 3. Heavy writes, log scale, never a hard exclusion (DQ-4). A read ratio is
    # only evidence once reads and writes were measured.
    if p.read_ratio_known:
        w = 1.0 - p.read_ratio_weighted
        pts = write_penalty(w, t)
        if pts:
            add(pts, f"Heavy writes: {w:.0%} of the traffic is writes, so cached "
                     "copies are thrown away often")

    # 4. Lock contention, only for tables in a blocking chain (DQ-6).
    if p.is_lock_contended:
        add(t.lock_penalty,
            "In a lock blocking chain: a cache will not fix lock contention")

    # 5. Churn: context only (DQ-7).
    if p.dead_tuple_pct >= t.dead_tuple_pct_churn:
        b.concerns.append(
            f"Context, no penalty: {p.dead_tuple_pct:.0f}% of row versions are dead. "
            "This usually means vacuum is behind, not that the data changes often")

    return sum(x["points"] for x in b.penalties), False, []


def write_penalty(w: float | None, t: CacheScoringThresholds = DEFAULT_THRESHOLDS) -> int:
    """Heavy-write penalty, logarithmic in writes per read (spec DQ-4).

    Points = a + s x log10(w / (1 - w)), where the anchors fix a (the value at
    50% writes, one write per read) and s (so 70% gives the second anchor).
    Held at 0 below the start and capped.

    Args:
        w: Write share, 0 to 1. None means unmeasured.
        t: Thresholds.

    Returns:
        Penalty points, 0 to the cap.
    """
    if w is None or w < t.write_penalty_start:
        return 0
    if w >= 1.0:
        return int(t.write_penalty_cap)
    slope = (t.write_penalty_at_70 - t.write_penalty_at_half) / math.log10(0.7 / 0.3)
    pts = t.write_penalty_at_half + slope * math.log10(w / (1.0 - w))
    return int(round(max(0.0, min(pts, t.write_penalty_cap))))


def host_headroom(ctx: WorkloadContext, t: CacheScoringThresholds = DEFAULT_THRESHOLDS
                  ) -> tuple[bool, str]:
    """Whether the host has resource headroom (spec DQ-2).

    All of: CPU, memory and swap under the Pressure thresholds, and statements
    executing no more than the vCPU count. Missing CPU, memory or vCPU count
    means headroom is not established. Missing swap is read as none in use.

    Args:
        ctx: Workload context.
        t: Thresholds.

    Returns:
        (has_headroom, plain-language reason).
    """
    view = effective_host_view(ctx.host_metrics)
    cpu, mem = view.get("cpu_pct"), view.get("memory_used_pct")
    swap, vcpu = view.get("swap_used_pct") or 0.0, view.get("vcpu_count")
    if cpu is None or mem is None or not vcpu:
        return False, "host CPU, memory or vCPU count unknown"
    if cpu >= t.host_cpu_pressure_pct:
        return False, f"host CPU at {cpu:.0f}%"
    if mem >= t.host_mem_pressure_pct:
        return False, f"host memory at {mem:.0f}%"
    if swap >= t.host_swap_active_pct:
        return False, f"host swapping ({swap:.1f}%)"
    conc = ctx.statement_concurrency
    if conc is not None and conc > vcpu:
        return False, f"{conc:.1f} statements executing on {vcpu} vCPU"
    return True, f"CPU {cpu:.0f}%, memory {mem:.0f}%, {vcpu} vCPU"


def db_share_of_capacity(db_ms_per_sec: float | None, vcpu: int | None) -> float | None:
    """DB time as a share of host CPU capacity (vCPU x 1000 ms per second).

    pg_stat_statements time is wall-clock time in the database. Under
    saturation it includes time spent waiting for a CPU, so a raw share above 1
    measures queueing, not CPU used. The share is capped at 1.0, which reads as
    "at least the whole host". Only the 5% busy line is compared against it.

    Args:
        db_ms_per_sec: Database time per second of the window, ms.
        vcpu: Host vCPU count.

    Returns:
        Share 0 to 1, or None without DB time or a vCPU count.
    """
    if db_ms_per_sec is None or not vcpu:
        return None
    return round(min(db_ms_per_sec / (vcpu * 1000.0), 1.0), 5)


def share_text(share: float | None) -> str:
    """Report wording for a capacity share; the cap reads as 'at least 100%'.

    Args:
        share: From db_share_of_capacity().

    Returns:
        Percentage text, or "unknown" for None.
    """
    if share is None:
        return "unknown"
    return "at least 100%" if share >= 1.0 else f"{share:.1%}"


def assess_well_served(p: CacheTableProfile, ctx: WorkloadContext,
                       t: CacheScoringThresholds = DEFAULT_THRESHOLDS) -> dict:
    """Evaluate the five well-served criteria (spec DQ-1, DQ-3).

    Args:
        p: Profile. buffer_hit_ratio_pct, weighted_exec_time_ms,
            temp_blocks_written and db_ms_per_sec are read.
        ctx: Workload context, for headroom and vCPU count.
        t: Thresholds.

    Returns:
        Each criterion as True / False, plus db_share_of_capacity, headroom
        reason, excluded (all five), and busy (all but the busy line).
    """
    vcpu = effective_host_view(ctx.host_metrics).get("vcpu_count")
    share = db_share_of_capacity(p.db_ms_per_sec, vcpu)
    room, why = host_headroom(ctx, t)
    hit = p.buffer_hit_ratio_pct
    out = {
        "hit_ratio_above": hit is not None and hit > t.well_served_hit_ratio,
        "fast": p.weighted_exec_time_ms < t.well_served_exec_ms,
        "no_spill": p.temp_blocks_written == 0,
        "host_headroom": room,
        "not_busy": share is not None and share < t.busy_share_of_capacity,
        "db_share_of_capacity": share,
        "headroom_reason": why,
    }
    four = out["hit_ratio_above"] and out["fast"] and out["no_spill"] and room
    out["excluded"] = bool(four and out["not_busy"])
    out["busy"] = bool(four and share is not None and not out["not_busy"])
    return out


def compute_cache_confidence(
    b: CacheScoreBreakdown,
    p: CacheTableProfile,
    t: CacheScoringThresholds = DEFAULT_THRESHOLDS,
) -> int:
    """Weighted confidence, with disqualifier penalty applied.

    Pressure Evidence carries the highest weight (0.40) because it answers
    "why now" — the question that distinguishes a real recommendation from a
    theoretical one.

    DEGRADED mode caps confidence at 60: without query-level statistics we
    cannot claim high confidence regardless of how the table signals score.
    """
    if b.hard_excluded:
        return 0

    if b.cache_efficiency is None:
        # Not scored is not 0: rescale Access and Pressure to sum to 1.
        w = t.weight_access + t.weight_pressure
        raw = (b.access_suitability * t.weight_access
               + b.pressure_evidence * t.weight_pressure) / w
    else:
        raw = (
            b.access_suitability * t.weight_access
            + b.pressure_evidence * t.weight_pressure
            + b.cache_efficiency * t.weight_efficiency
        )
    net = raw - b.disqualifier_penalty
    confidence = int(round(max(0.0, min(net, 100.0))))

    if not p.query_stats_available:
        confidence = min(confidence, 60)

    return confidence


def classify_verdict(b: CacheScoreBreakdown) -> str:
    """Map a score breakdown to a human-facing verdict.

    TODO: band boundaries are v1 defaults and need calibration alongside the
    thresholds (project plan Day 7).
    """
    if b.hard_excluded:
        return "NOT_RECOMMENDED"
    if b.confidence >= 70:
        return "STRONG_CANDIDATE"
    if b.confidence >= 50:
        return "MODERATE_CANDIDATE"
    if b.confidence >= 30:
        return "WEAK_CANDIDATE"
    return "NOT_RECOMMENDED"
