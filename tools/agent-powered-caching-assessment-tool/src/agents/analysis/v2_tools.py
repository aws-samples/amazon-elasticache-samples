"""
Strands tools exposing scored assessment data to the v2 analysis agent.

Why tools rather than one large prompt:

A single JSON blob of 17 tables with 40 thresholds and 38 query patterns is both
expensive and counterproductive. It buries the signal, and it lets the model
answer from a skim. Tools force the model to go and look, and the record of which
tools it called is itself evidence about whether the judgement was grounded.
`tool_calls_made == 0` in the output means the model decided without examining
anything, which is worth knowing.

Every tool is READ-ONLY over data the deterministic layer already computed. No
tool recalculates a score, and no tool touches the database. The model cannot
change a number, only read and interpret it.

Tools are built by a factory so the data is captured in closures rather than held
in module state. That keeps concurrent assessments independent.
"""

import json
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from strands import tool

from src.tools.analysis.cache_bundles import bundles_by_table
from src.tools.analysis.caching_scoring import (
    CacheScoreBreakdown,
    CacheScoringThresholds,
    CacheTableProfile,
    WorkloadContext,
)
from src.tools.analysis.column_widths import column_width, table_columns, tables_by_id
from src.tools.database.host_sampler import effective_host_view

logger = logging.getLogger(__name__)

# Plain-language meaning for each threshold, so the model can reason about
# whether a value near a boundary actually matters rather than treating the
# number as arbitrary.
THRESHOLD_MEANINGS: dict[str, str] = {
    "read_ratio_excellent": "At or above this share of reads, invalidation is negligible against read volume. 0.90 corresponds to the 10:1 read:write rule of thumb.",
    "read_ratio_good": "Solidly read-dominated; invalidation on write stays manageable.",
    "read_ratio_marginal": "Break-even. Below this, writes are the majority of the work.",
    "read_ratio_points_excellent": "Access points at or above read_ratio_excellent.",
    "read_ratio_points_good": "Access points at or above read_ratio_good.",
    "read_ratio_points_marginal": "Access points at or above read_ratio_marginal.",
    "calls_per_sec_high": "Reads per second where per-call savings aggregate into meaningful load reduction. For a table, write statements are not counted.",
    "calls_per_sec_moderate": "Worth considering.",
    "calls_per_sec_low": "Floor for any measurable benefit.",
    "calls_points_high": "Access points at or above calls_per_sec_high.",
    "calls_points_moderate": "Access points at or above calls_per_sec_moderate.",
    "calls_points_low": "Access points at or above calls_per_sec_low.",
    "buffer_hit_ratio_poor": "Well below the ~99% a healthy OLTP PostgreSQL sustains. Clear evidence of disk traffic.",
    "buffer_hit_ratio_weak": "Below healthy but not alarming.",
    "buffer_hit_ratio_healthy": "Conventional target. At or above this, PostgreSQL is serving from memory and a cache adds little speed.",
    "buffer_pool_adequate_ratio": "shared_buffers as a fraction of database size. Below this the pool may simply be undersized, and raising it is cheaper than a cache tier.",
    "bgwriter_min_buffers_per_hour": "Minimum buffer writes an hour before the backend-write percentage means anything. A rate, so it means the same over a short collection window and over days of counters.",
    "io_time_share_high": "Share of execution time spent waiting on disk. This is the cost a cache removes directly.",
    "io_time_share_moderate": "Material but not dominant.",
    "exec_time_ms_slow": "Clearly user-perceptible.",
    "exec_time_ms_moderate": "Worth removing at volume.",
    "exec_time_ms_marginal": "Below this, a network round-trip to a cache may not be faster than PostgreSQL answering from memory. The most consequential threshold in the set, and an estimate rather than a measurement.",
    "host_cpu_pressure_pct": "Sustained CPU where queueing begins to degrade latency non-linearly.",
    "host_mem_pressure_pct": "Above this, little room remains for the OS page cache to absorb reads.",
    "host_swap_active_pct": "Deliberately near zero. Any sustained swapping on a database host is significant.",
    "seq_scan_min_table_mb": "Sequential scans only count as pressure above this table size. Scanning a small table repeatedly costs almost nothing.",
    "seq_scan_min_per_hour": "Ignore incidental scans: fewer sequential scans an hour than this earn nothing.",
    "efficiency_hit_rate_full": "Estimated hit rate earning full reuse marks. ElastiCache guidance reads a CacheHitRate below about 0.8 as many reads finding nothing cached. Linear below.",
    "efficiency_weight_reuse": "Share of Cache Efficiency from reuse (estimated hit rate).",
    "efficiency_weight_size": "Share of Cache Efficiency from result size (time saved per hit x bytes factor).",
    "time_saved_zero_ms": "PostgreSQL time at or below which a hit skips almost no work, so the time saved score is 0.",
    "time_saved_full_ms": "PostgreSQL time at or above which a hit removes a user-noticeable delay on its own, so the time saved score is 100. Log scale between.",
    "value_soft_limit_kb": "Result size up to which the bytes factor is 1. Redis guidance keeps values under 100 KB.",
    "value_hard_limit_mb": "Result size at which the bytes factor reaches 0. ElastiCache will not migrate a slot holding an item over 256 MB. Log scale between.",
    "dead_tuple_pct_churn": "Context only, never a penalty. A high dead-tuple share usually means vacuum is behind.",
    "well_served_hit_ratio": "Criterion of the one hard exclusion: hit ratio strictly above this.",
    "well_served_exec_ms": "Criterion of the one hard exclusion: mean time strictly below this.",
    "busy_share_of_capacity": "Criterion of the one hard exclusion: DB time under this share of host CPU capacity (vCPU x 1000 ms/s, capped at 1.0; wall-clock, so it includes CPU queueing on a saturated host). At or above it a fast item is busy but fast: penalised, not excluded, and judged by you against the application's industry.",
    "busy_penalty": "Points for busy but fast.",
    "bundle_min_db_share": "A Not cacheable read query with at least this share of DB time is checked as a bundle.",
    "bundle_min_row_change_hours": "A bundled table qualifies only if each row changes no more often than this (row_count / writes per second).",
    "write_penalty_start": "Heavy-write penalty starts at this write share. Log scale in writes per read, never a hard exclusion.",
    "write_penalty_at_half": "Heavy-write penalty at 50% writes (one write per read).",
    "write_penalty_at_70": "Heavy-write penalty at 70% writes.",
    "write_penalty_cap": "Heavy-write penalty ceiling.",
    "lock_penalty": "Points for a table found in a lock blocking chain, resolved by relation OID.",
    "weight_access": "Weight of Access Suitability in confidence. Necessary but not sufficient on its own.",
    "weight_pressure": "Weight of Pressure Evidence. Highest, because it answers why now.",
    "weight_efficiency": "Weight of Cache Efficiency: how efficiently a cache would serve the reads. When Efficiency is not scored, Access and Pressure are rescaled instead of counting it as 0.",
}


def build_tools(
    profiles: dict[str, CacheTableProfile],
    breakdowns: dict[str, CacheScoreBreakdown],
    deterministic_verdicts: dict[str, str],
    context: WorkloadContext,
    thresholds: CacheScoringThresholds,
    collector_output: dict,
    query_profiles: list | None = None,
    query_summary: dict | None = None,
    bundles: list[dict] | None = None,
) -> list[Callable[..., Any]]:
    """Build the read-only tool set, closing over the scored data.

    Args:
        profiles: Per-table aggregates.
        breakdowns: Per-table score breakdowns.
        deterministic_verdicts: What the threshold bands concluded per table.
        context: Workload-level signals.
        thresholds: Threshold values in effect.
        collector_output: Raw collection, for query-level drill-down.
        query_profiles: Per-query QueryCacheProfile records, ranked by database
            time. When present, the query tools are included and the model is
            expected to lead with the per-query dimension.
        query_summary: Coverage and distribution for the per-query pass.
        bundles: From cache_bundles.detect_bundles().

    Returns:
        Tool callables ready to pass to a Strands Agent.
    """
    query_profiles = query_profiles or []
    query_summary = query_summary or {}
    bundles = bundles or []
    bundle_membership = bundles_by_table(bundles)
    qp_by_id = {p.queryid: p for p in query_profiles}
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    churn_rows = (collector_output.get("metrics") or {}).get("table_churn") or []
    churn_by_name = {r.get("table_name"): r for r in churn_rows if r.get("table_name")}
    tables_idx = tables_by_id(collector_output)

    # Index queries by the tables they touch, once.
    by_table: dict[str, list[dict]] = defaultdict(list)
    for q in queries:
        for tid in q.get("tables_accessed") or []:
            by_table[tid].append(q)

    def _short(tid: str) -> str:
        return tid.rsplit(".", 1)[-1]

    def _eff(e) -> dict | None:
        """Cache Efficiency detail for a tool response, or None."""
        if e is None:
            return None
        d = e.as_dict()
        d["note"] = (
            "estimated_hit_rate is the hit rate a cache holding every entry would "
            "have reached over the window, with every key equally popular, so it "
            "tends to run low. reuse_deferred_reason set means K could not be read "
            "from statistics: estimate it yourself from what the query is for, as "
            "estimated_distinct_requests: distinct requests actually sent over the "
            "window, at most calls_in_window; reuse is scored as 1 - estimate / calls, "
            "and explain it in reuse_reasoning.")
        return d

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @tool
    def list_scored_tables() -> str:
        """List every assessed table with its four dimension scores.

        Start here. Gives the shape of the whole workload before drilling in.

        Returns:
            JSON array, highest confidence first. Each entry has the table id,
            the deterministic verdict from threshold bands, the confidence
            score, the three positive dimension scores, the disqualifier
            penalty, whether it was hard-excluded, and the exclusion reason.
        """
        rows = []
        for tid, b in breakdowns.items():
            rows.append({
                "table_id": tid,
                "deterministic_verdict": deterministic_verdicts.get(tid),
                "confidence": b.confidence,
                "access_suitability": b.access_suitability,
                "pressure_evidence": b.pressure_evidence,
                "cache_efficiency": b.cache_efficiency,
                "disqualifier_penalty": b.disqualifier_penalty,
                "busy_but_fast": b.busy_but_fast,
                "hard_excluded": b.hard_excluded,
                "exclusion_kind": b.exclusion_kind,
                "exclusion_reason": b.exclusion_reason,
                "exclusion_reasons": b.exclusion_reasons,
            })
        rows.sort(key=lambda r: (r["hard_excluded"], -r["confidence"]))
        return json.dumps(rows, indent=1, default=str)

    @tool
    def get_table_detail(table_id: str) -> str:
        """Full measured signals, evidence, and concerns for one table.

        Use this before forming a verdict on any table. The evidence and concerns
        lists are generated by the deterministic scorer and each item corresponds
        to a specific threshold being crossed.

        Args:
            table_id: Canonical identifier, for example "retail.order_item".

        Returns:
            JSON object with scores, raw measured signals, evidence, concerns,
            and provenance notes. Returns an error object if unknown.
        """
        p, b = profiles.get(table_id), breakdowns.get(table_id)
        if p is None or b is None:
            return json.dumps({
                "error": f"unknown table_id {table_id!r}",
                "known": sorted(profiles)[:25],
            })
        return json.dumps({
            "table_id": table_id,
            "deterministic_verdict": deterministic_verdicts.get(table_id),
            "scores": {
                "confidence": b.confidence,
                "access_suitability": b.access_suitability,
                "pressure_evidence": b.pressure_evidence,
                "cache_efficiency": b.cache_efficiency,
                "disqualifier_penalty": b.disqualifier_penalty,
                "penalties": b.penalties,
                "busy_but_fast": b.busy_but_fast,
                "db_share_of_capacity": b.db_share_of_capacity,
                "well_served": b.well_served,
                "hard_excluded": b.hard_excluded,
                "exclusion_kind": b.exclusion_kind,
                "exclusion_reason": b.exclusion_reason,
                "exclusion_reasons": b.exclusion_reasons,
            },
            "measured_signals": {
                "read_ratio_weighted": p.read_ratio_weighted,
                "total_calls_per_second": p.total_calls_per_second,
                "read_calls_per_second": p.read_calls_per_second,
                "write_calls_per_second": p.write_calls_per_second,
                "distinct_query_patterns": p.query_count,
                "weighted_exec_time_ms_reads": p.weighted_exec_time_ms,
                "buffer_hit_ratio_pct_reads": p.buffer_hit_ratio_pct,
                "io_read_time_share_reads": p.io_read_time_share,
                "temp_blocks_written_reads": p.temp_blocks_written,
                "sequential_scans": p.seq_scans,
                "sequential_scans_per_hour": p.seq_scans_per_hour,
                "index_scans": p.index_scans,
                "max_rows_returned_any_query": p.max_rows_returned,
                "size_mb": p.size_mb,
                "row_count": p.row_count,
                "dead_tuple_pct": p.dead_tuple_pct,
                "is_lock_contended": p.is_lock_contended,
            },
            "cache_efficiency_detail": _eff(b.efficiency),
            "bundles": bundle_membership.get(table_id, []),
            "evidence_supporting_caching": b.evidence,
            "concerns_against_caching": b.concerns,
            "query_stats_available": p.query_stats_available,
            "provenance_notes": p.notes,
        }, indent=1, default=str)

    @tool
    def get_table_queries(table_id: str) -> str:
        """The actual queries touching a table, with their measured behaviour.

        Use this to judge cache SHAPE, which the scores cannot express. A table
        can be read heavily by point lookups, by aggregates, or by full scans,
        and those imply different caching strategies and different invalidation
        exposure.

        Important: a query joining several tables is attributed to ALL of them,
        so a table may appear busy because it is a dimension in someone else's
        join rather than the subject of its own queries. Check
        tables_in_query to tell the difference.

        Args:
            table_id: Canonical identifier.

        Returns:
            JSON array of queries, busiest first, each with type, call rate,
            average execution time, rows returned, buffer hit ratio, temp blocks,
            the other tables in the same query, and truncated query text.
        """
        rows = []
        for q in by_table.get(table_id, []):
            accessed = q.get("tables_accessed") or []
            rows.append({
                "query_type": q.get("query_type"),
                "calls_per_second": q.get("calls_per_second"),
                "avg_exec_ms": q.get("execution_time_ms_avg"),
                "rows_returned_avg": q.get("rows_returned_avg"),
                "buffer_hit_ratio_pct": q.get("cache_hit_ratio_pct"),
                "temp_blocks_written": q.get("temp_blocks_written"),
                "tables_in_query": accessed,
                "is_multi_table": len(accessed) > 1,
                "query_text": (q.get("query_text") or "")[:400],
            })
        rows.sort(key=lambda r: -(r.get("calls_per_second") or 0))
        if not rows:
            return json.dumps({
                "table_id": table_id,
                "queries": [],
                "note": "No query patterns reference this table. Either it is "
                        "genuinely unused, or pg_stat_statements was unavailable.",
            })
        return json.dumps({"table_id": table_id, "queries": rows}, indent=1, default=str)

    @tool
    def get_write_breakdown(table_id: str) -> str:
        """Insert, update, and delete volumes for a table.

        Use this to reason about invalidation blast radius, which the write ratio
        alone cannot express. The three operations differ:

          UPDATE and DELETE make a specific cached row wrong.
          INSERT makes aggregates, counts, and ranges wrong, but does NOT make a
          cached point lookup of a different key wrong.

        So an append-only table with a high write ratio may still be safe to
        cache for point lookups. The deterministic scorer does not currently make
        this distinction and treats all writes as equally invalidating, which is
        pessimistic for insert-heavy tables. Say so if it applies.

        Args:
            table_id: Canonical identifier.

        Returns:
            JSON with cumulative insert/update/delete counts, live and dead tuple
            counts, vacuum timestamps, and the write call rate.
        """
        c = churn_by_name.get(_short(table_id))
        p = profiles.get(table_id)
        if c is None:
            return json.dumps({"error": f"no churn data for {table_id!r}"})
        ins = int(c.get("n_tup_ins") or 0)
        upd = int(c.get("n_tup_upd") or 0)
        dele = int(c.get("n_tup_del") or 0)
        total = ins + upd + dele
        return json.dumps({
            "table_id": table_id,
            "inserts": ins,
            "updates": upd,
            "deletes": dele,
            "insert_share_of_writes": round(ins / total, 3) if total else None,
            "modifies_existing_rows": upd + dele > 0,
            "append_only": total > 0 and upd == 0 and dele == 0,
            "live_tuples": c.get("n_live_tup"),
            "dead_tuples": c.get("n_dead_tup"),
            "dead_tuple_pct": p.dead_tuple_pct if p else None,
            "hot_updates": c.get("n_tup_hot_upd"),
            "last_autovacuum": c.get("last_autovacuum"),
            "write_calls_per_second": p.write_calls_per_second if p else None,
            "caveat": "Dead tuple count is unreliable as a churn signal: HOT "
                      "updates and prompt autovacuum can keep it near zero under "
                      "heavy update load. Trust the update and delete counts.",
        }, indent=1, default=str)

    @tool
    def get_workload_context() -> str:
        """Deployment-level signals that apply to every table.

        Read this early. It says whether reads come from memory, whether the
        host was busy during the statistics window, and how many statements
        were executing concurrently. Check host.source before relying on the
        CPU figure.

        Returns:
            JSON with buffer pool adequacy, database-wide hit ratio, host CPU,
            memory, swap, checkpoint behaviour, and lock wait share.
        """
        host = context.host_metrics or {}
        view = effective_host_view(host) if host else {}
        concurrency = None
        if query_summary.get("total_db_time_ms_per_sec") is not None:
            # Little's law: executing statements on average = rate x duration.
            concurrency = round(query_summary["total_db_time_ms_per_sec"] / 1000, 2)
        return json.dumps({
            "buffer_pool": context.buffer_pool,
            "database_wide_buffer_hit_ratio_pct":
                context.global_stats.get("cache_hit_ratio_pct"),
            "active_connections_at_collection":
                context.global_stats.get("active_connections"),
            "temp_files_spilled": context.global_stats.get("tmp_disk_tables"),
            "avg_statements_executing_during_stats_window": concurrency,
            "host": {
                "cpu_utilization_pct": view.get("cpu_pct"),
                "cpu_utilization_mean_pct": view.get("cpu_mean_pct"),
                "cpu_iowait_pct": view.get("iowait_pct"),
                "vcpu_count": view.get("vcpu_count"),
                "memory_used_pct": view.get("memory_used_pct"),
                "swap_used_pct": view.get("swap_used_pct"),
                "source": view.get("source"),
                "source_note": view.get("note"),
                "snapshot_cpu_pct_at_collection":
                    (host.get("cpu") or {}).get("utilization_pct"),
                "load_average_at_collection": host.get("load_average"),
                "page_cache_gb": (host.get("memory") or {}).get("page_cache_gb"),
                "metrics_source": host.get("executor_type"),
            },
            "bgwriter_backend_write_pct": context.bgwriter.get("buffers_backend_pct"),
            "lock_wait_share": context.lock_wait_share,
            "statistics_window": {
                "mode": context.stats_window.get("mode"),
                "seconds": context.stats_window.get("seconds"),
                "started_at": context.stats_window.get("started_at"),
                "ended_at": context.stats_window.get("ended_at"),
                "basis": context.stats_window.get("basis"),
                "calls_in_window": (context.stats_window.get("statements") or {}).get("calls"),
                "notes": context.stats_window.get("notes"),
            },
            "bundles": bundles,
            "notes": [
                "statistics_window says which period every rate, mean time, hit ratio, "
                "spill and host figure describes. 'snapshot' is the difference between "
                "two readings taken at the start and end of a window the user chose, "
                "with the host sampled during it. 'since_reset' is an average since the "
                "statistics were last reset, which can blend busy and quiet hours. Cache "
                "Efficiency always uses the counts since the reset.",
                "bundles lists every Not cacheable read query with at least "
                f"{thresholds.bundle_min_db_share:.0%} of all database time. Such a "
                "query cannot be cached as a result, but if qualifies is true every "
                "table it reads can be object cached (not hard excluded, no lock, "
                f"each row changing at most every {thresholds.bundle_min_row_change_hours:g} "
                "hour), so the application could read the cached rows and apply the "
                "filter, join and sort itself. db_time_share_pct is what caching the "
                "whole bundle saves. table_checks says why a table does not qualify.",
                "host.source tells you which period the CPU figure describes. "
                "'window' is the p95 across samples taken during the load or the "
                "collection window, the same period as the query statistics. "
                "'snapshot' is one reading at collection time that agrees with the "
                "load average. 'unknown' means the snapshot contradicted the load "
                "average, or the collection window could not sample the host, so "
                "CPU during the statistics window is NOT known. Never describe "
                "an 'unknown' host as idle or as having headroom.",
                "avg_statements_executing_during_stats_window is total database "
                "time per second divided by 1000, by Little's law. Compare it "
                "with vcpu_count: above it, statements were queueing for CPU.",
                "active_connections_at_collection is a point-in-time count taken "
                "after the load, not the concurrency during it.",
                "lock_wait_share of null means no session was waiting on "
                "anything, which is different from zero lock contention.",
                "If metrics_source names a local executor, host numbers describe "
                "that machine rather than a dedicated database host, and may "
                "include the load generator itself.",
            ],
        }, indent=1, default=str)

    @tool
    def get_thresholds() -> str:
        """Threshold values in effect, with what each one means.

        Use this when a table sits near a boundary, or when deciding whether a
        threshold is appropriate for this particular workload. The values are
        reasoned defaults and have NOT been calibrated against real workloads,
        so disagreeing with one is legitimate if the data justifies it.

        Returns:
            JSON mapping each threshold to its value and plain-language meaning.
        """
        out = {}
        for field in thresholds.__dataclass_fields__:
            out[field] = {
                "value": getattr(thresholds, field),
                "meaning": THRESHOLD_MEANINGS.get(field, "no description available"),
            }
        return json.dumps({
            "thresholds": out,
            "verdict_bands": {
                "STRONG_CANDIDATE": "confidence >= 70",
                "MODERATE_CANDIDATE": "confidence 50 to 69",
                "WEAK_CANDIDATE": "confidence 30 to 49",
                "NOT_RECOMMENDED": "confidence < 30, or hard-excluded",
            },
            "caveat": "The verdict bands are the least defensible numbers here. "
                      "They were chosen so two test fixtures landed correctly, "
                      "which is not evidence.",
        }, indent=1, default=str)

    @tool
    def compare_tables(table_ids: list[str]) -> str:
        """Compare several tables side by side on their key signals.

        Useful for ranking candidates against each other and for spotting tables
        that should be treated as a group.

        Args:
            table_ids: Canonical identifiers to compare.

        Returns:
            JSON array with one row per requested table.
        """
        rows = []
        for tid in table_ids:
            p, b = profiles.get(tid), breakdowns.get(tid)
            if p is None or b is None:
                rows.append({"table_id": tid, "error": "unknown"})
                continue
            rows.append({
                "table_id": tid,
                "confidence": b.confidence,
                "access": b.access_suitability,
                "pressure": b.pressure_evidence,
                "efficiency": b.cache_efficiency,
                "read_ratio": p.read_ratio_weighted,
                "calls_per_sec": p.total_calls_per_second,
                "exec_ms": p.weighted_exec_time_ms,
                "hit_ratio_pct": p.buffer_hit_ratio_pct,
                "size_mb": p.size_mb,
                "hard_excluded": b.hard_excluded,
            })
        return json.dumps(rows, indent=1, default=str)

    @tool
    def get_co_accessed_tables(table_id: str) -> str:
        """Tables queried together with this one in the same statement.

        Use this to judge whether caching a table in isolation is coherent.
        Tables always read together often need to be cached together, or the
        cached data will be inconsistent with what the application reads next.
        Two tables joined in every query may also be better cached as a single
        joined result than as two independent entries.

        Args:
            table_id: Canonical identifier.

        Returns:
            JSON listing co-accessed tables with how often they appear together
            and the share of this table's traffic that is multi-table.
        """
        together: dict[str, float] = defaultdict(float)
        solo = 0.0
        joined = 0.0
        for q in by_table.get(table_id, []):
            cps = q.get("calls_per_second") or 0.0
            accessed = list(q.get("tables_accessed") or [])
            if len(accessed) <= 1:
                solo += cps
                continue
            joined += cps
            for other in accessed:
                if other != table_id:
                    together[other] += cps
        total = solo + joined
        return json.dumps({
            "table_id": table_id,
            "solo_calls_per_second": round(solo, 3),
            "multi_table_calls_per_second": round(joined, 3),
            "share_of_traffic_from_joins": round(joined / total, 3) if total else None,
            "co_accessed": [
                {"table_id": t, "shared_calls_per_second": round(v, 3)}
                for t, v in sorted(together.items(), key=lambda kv: -kv[1])
            ],
            "interpretation_hint": "A high share_of_traffic_from_joins means this "
                                   "table's apparent traffic is largely inherited "
                                   "from queries about other tables. Judge whether "
                                   "it is the subject of those queries or a "
                                   "dimension lookup.",
        }, indent=1, default=str)

    # ------------------------------------------------------------------
    # Per-query tools. The primary dimension.
    # ------------------------------------------------------------------

    def _q_row(p, include_text: bool = True) -> dict:
        """Render one query profile for a tool response."""
        row = {
            "queryid": p.queryid,
            "cache_type": p.shape.cache_type,
            "access_shape": p.shape.access_shape,
            "db_time_ms_per_sec": p.db_time_ms_per_sec,
            "db_time_share_pct": p.db_time_share_pct,
            "calls_per_second": p.calls_per_second,
            "avg_exec_ms": p.avg_exec_ms,
            "rows_returned_avg": round(p.rows_returned_avg, 2),
            "invalidating_write_rate": p.invalidating_write_rate,
            "tables_read": p.shape.tables_read,
            "non_deterministic": p.shape.non_deterministic,
            "clock_uses": p.shape.clock_uses,
            "views_resolved": p.shape.views_resolved,
            "unresolved_relations": p.shape.unresolved_relations,
        }
        b = p.breakdown
        if b is not None:
            # The same four dimensions, thresholds, and bands as tables.
            row.update({
                "deterministic_verdict": p.deterministic_verdict,
                "confidence": b.confidence,
                "access_suitability": b.access_suitability,
                "pressure_evidence": b.pressure_evidence,
                "cache_efficiency": b.cache_efficiency,
                "disqualifier_penalty": b.disqualifier_penalty,
                "penalties": b.penalties,
                "busy_but_fast": b.busy_but_fast,
                "db_share_of_capacity": b.db_share_of_capacity,
                "hard_excluded": b.hard_excluded,
                "exclusion_kind": b.exclusion_kind,
                "exclusion_reason": b.exclusion_reason,
                "exclusion_reasons": b.exclusion_reasons,
                "read_dominance": p.read_dominance,
            })
        e = p.efficiency
        if e is not None:
            row.update({
                "estimated_hit_rate": e.estimated_hit_rate,
                "reuse_deferred": bool(e.reuse_deferred_reason),
                "reuse_deferred_reason": e.reuse_deferred_reason,
                "result_bytes": e.result_bytes,
            })
        if include_text:
            row["query_text"] = p.query_text[:400]
        return row

    @tool
    def list_top_queries(limit: int = 25) -> str:
        """Queries ranked by database time consumed. START HERE.

        Database time is calls_per_second x avg_exec_ms, so this ordering is the
        ordering of caching opportunity. The first row is where the workload
        actually spends its time.

        Each row carries a cache_type derived from the SQL parse tree:
          OBJECT         a row or collection keyed by a unique column
          QUERY          a result set keyed by query plus parameters
          NOT_CACHEABLE  a write, introspection traffic, or per-call-unique
          UNKNOWN        unparseable, never treat as a candidate

        Args:
            limit: How many rows to return, highest database time first.

        Returns:
            JSON with the ranked queries and workload coverage figures.
        """
        if not query_profiles:
            return json.dumps({
                "queries": [],
                "note": "No per-query data. pg_stat_statements was unavailable, "
                        "so only the table dimension can be assessed.",
            })
        return json.dumps({
            "coverage": {
                k: query_summary.get(k) for k in (
                    "patterns_collected", "analysed", "dropped_by_cap", "top_n",
                    "db_time_covered_pct", "cache_type_counts",
                    "object_candidates", "query_candidates",
                    "system_queries_excluded", "unparseable",
                    "total_db_time_ms_per_sec", "cacheable_db_time_ms_per_sec",
                    "deterministic_band_counts", "views_resolved",
                    "queries_on_resolved_views", "queries_on_unresolved_relations",
                )
            },
            "queries": [_q_row(p) for p in query_profiles[:limit]],
        }, indent=1, default=str)

    @tool
    def get_query_detail(queryid: str) -> str:
        """Everything measured and parsed about one query.

        Use this before forming a verdict on any query. It carries the full query
        text, the parse-derived shape, the four-dimension scores and band, the
        read dominance against measured writes, clock-function granularity, and
        db_time_ms_per_sec,
        the upper bound on what caching it could remove.

        Args:
            queryid: Identifier from list_top_queries.

        Returns:
            JSON detail, or an error object when the id is unknown.
        """
        p = qp_by_id.get(str(queryid))
        if p is None:
            return json.dumps({
                "error": f"unknown queryid {queryid!r}",
                "known": [x.queryid for x in query_profiles[:20]],
            })
        return json.dumps({
            **_q_row(p),
            "query_text_full": p.query_text,
            "query_type": p.query_type,
            "total_time_ms": p.total_time_ms,
            "buffer_hit_ratio_pct": p.buffer_hit_ratio_pct,
            "io_read_time_share": p.io_read_time_share,
            "temp_blocks_written": p.temp_blocks_written,
            "invalidation_ratio": p.invalidation_ratio,
            "write_sources": p.write_sources,
            "key_columns": p.shape.key_columns,
            "key_is_primary_key": p.shape.key_is_primary_key,
            "caches_collection": p.shape.caches_collection,
            "tables_written": p.shape.tables_written,
            "unresolved_relations": p.shape.unresolved_relations,
            "is_system_query": p.shape.is_system_query,
            "parse_ok": p.shape.parse_ok,
            "classification_reasons": p.shape.reasons,
            "notes": p.notes,
            "cache_efficiency_detail": _eff(p.efficiency),
            "parameters": p.shape.params,
            "evidence_supporting_caching": list(p.breakdown.evidence) if p.breakdown else [],
            "concerns_against_caching": list(p.breakdown.concerns) if p.breakdown else [],
        }, indent=1, default=str)

    @tool
    def get_cache_candidates_by_type(cache_type: str) -> str:
        """Every candidate of one cache type, ranked by database time.

        Use this to assess the two kinds of work separately, because they are
        different designs with different invalidation stories.

        OBJECT candidates cache a row or collection under a key. Their read
        dominance uses the table-wide write rate, which overstates what any single
        key experiences; scoping it per key needs column cardinality the collector
        does not gather. Reason about whether writes are concentrated on a few keys
        or spread across them.

        QUERY candidates cache a result set. Every entry shares one invalidation
        surface, and read dominance uses the measured write rate on every table
        read, views counted through their base tables. No key-count cap applies.

        Args:
            cache_type: "OBJECT" or "QUERY".

        Returns:
            JSON array of matching queries, highest database time first.
        """
        wanted = str(cache_type).upper()
        if wanted not in ("OBJECT", "QUERY"):
            return json.dumps({
                "error": f"cache_type must be OBJECT or QUERY, got {cache_type!r}"
            })
        rows = [_q_row(p) for p in query_profiles if p.shape.cache_type == wanted]
        extra = {}
        if wanted == "OBJECT":
            extra["note"] = (
                "key_is_primary_key true means one row per key and precise "
                "invalidation. caches_collection true means the key is not "
                "unique, so the entry holds several rows, its size varies, and "
                "any write for that key invalidates the whole entry."
            )
            # rows was built from this same filter, so the lengths always match; strict
            # turns a future mismatch into an error instead of rows silently left out.
            for p, row in zip(
                (p for p in query_profiles if p.shape.cache_type == "OBJECT"), rows,
                strict=True,
            ):
                row["key_columns"] = p.shape.key_columns
                row["key_is_primary_key"] = p.shape.key_is_primary_key
                row["caches_collection"] = p.shape.caches_collection
        return json.dumps({
            "cache_type": wanted,
            "count": len(rows),
            "total_db_time_ms_per_sec": round(
                sum(r["db_time_ms_per_sec"] for r in rows), 2),
            "queries": rows,
            **extra,
        }, indent=1, default=str)

    @tool
    def get_invalidation_map() -> str:
        """Write rates per table, and which queries each one invalidates.

        Use this to judge whether a cache would hold. A query reading a table
        that takes 48 writes per second invalidates a cached result far more
        often than it is read, however expensive the query is.

        Returns:
            JSON mapping each written table to its statement rate and the
            cacheable queries that read it.
        """
        rates = query_summary.get("write_rate_by_table") or {}
        out = []
        for table, rate in sorted(rates.items(), key=lambda kv: -kv[1]):
            affected = [
                {"queryid": p.queryid,
                 "db_time_ms_per_sec": p.db_time_ms_per_sec,
                 "cache_type": p.shape.cache_type}
                for p in query_profiles
                if table in p.shape.tables_read and p.shape.cache_type in
                ("OBJECT", "QUERY")
            ]
            out.append({
                "table": table,
                "writes_per_second": rate,
                "cacheable_queries_affected": len(affected),
                "db_time_at_risk_ms_per_sec": round(
                    sum(a["db_time_ms_per_sec"] for a in affected), 2),
                "queries": affected[:10],
            })
        return json.dumps({
            "write_rates": out,
            "note": "db_time_at_risk is the cacheable database time whose entries "
                    "this table's writes can invalidate. A high write rate against "
                    "high at-risk time means cached entries would rarely survive "
                    "to be read.",
        }, indent=1, default=str)

    @tool
    def get_table_columns(table_id: str) -> str:
        """Columns of one table with their byte widths, for choosing what to cache.

        Use this before recommending OBJECT caching for a query or a table. Pick
        cached_columns from what the application's queries on this table read
        (see get_table_queries and query_text): cache the columns those reads
        need, not columns no query reads. Widths come from statistics; the
        sizing that uses them is computed deterministically afterwards.

        Args:
            table_id: Canonical identifier, for example "shop.customer".

        Returns:
            JSON with each column's name, type, width in bytes and its source
            (pg_stats average, declared fixed, or declared maximum), whether it
            is in the primary key, the full row width, and the row estimate.
        """
        t = tables_idx.get(str(table_id).lower())
        if t is None:
            return json.dumps({"error": f"unknown table_id {table_id!r}",
                               "known": sorted(tables_idx)[:25]})
        pk = {str(c).lower() for c in (t.get("primary_key") or [])}
        types = {str(c.get("column_name")).lower(): c.get("udt_name")
                 for c in (t.get("columns") or []) if c.get("column_name")}
        cols, row, unknown = [], 0.0, []
        for c in table_columns(t):
            w, src = column_width(t, c)
            cols.append({"column": c, "type": types.get(c), "width_bytes": w,
                         "width_source": src, "in_primary_key": c in pk})
            if w is None:
                unknown.append(c)
            else:
                row += w
        return json.dumps({
            "table_id": table_id,
            "row_estimate": t.get("reltuples") if t.get("reltuples") is not None
            else t.get("row_count"),
            "primary_key": sorted(pk),
            "columns": cols,
            "full_row_width_bytes": round(row, 1),
            "columns_without_width": unknown,
            "note": "Name cached_columns exactly as listed here. A declared "
                    "maximum is an upper bound, not an average.",
        }, indent=1, default=str)

    base = [
        list_scored_tables,
        get_table_detail,
        get_table_queries,
        get_table_columns,
        get_write_breakdown,
        get_workload_context,
        get_thresholds,
        compare_tables,
        get_co_accessed_tables,
    ]
    if query_profiles:
        # Query tools first, because the model should lead with that dimension.
        return [
            list_top_queries,
            get_query_detail,
            get_cache_candidates_by_type,
            get_invalidation_map,
        ] + base
    return base
