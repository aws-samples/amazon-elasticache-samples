"""Exclusions (Disqualifiers v2, contract 2.6) and table read context.

The 2.4 Efficiency factor tests were replaced by tests/unit/test_cache_efficiency.py
when contract 2.5 rebuilt the pillar. Each remaining test pins one defect:

  context      table rows taken from joined reports.
  exclusions   write-only traffic never write-dominated; idle tables carrying
               three contradictory reasons; penalties stacked on hard exclusions
               with the last reason hiding the rest; "well served" depending on
               a host check that disagreed with the CPU signal; degraded mode
               excluding every table as idle.
"""

from src.tools.analysis.caching_scoring import (
    DEFAULT_THRESHOLDS,
    CacheTableProfile,
    WorkloadContext,
    build_cache_profiles,
    fmt_mb,
    score_cache_candidacy,
)
from src.tools.analysis.query_cache_analysis import (
    QueryCacheProfile,
    build_solo_read_map,
    classify_query,
    score_query_candidacy,
)

T = DEFAULT_THRESHOLDS


def _busy(**kw) -> CacheTableProfile:
    base = dict(table_id="t", size_mb=5.0, total_calls_per_second=40.0,
                read_calls_per_second=40.0, read_ratio_weighted=1.0,
                read_ratio_known=True, weighted_exec_time_ms=20.0,
                buffer_hit_ratio_pct=100.0, rows_per_read=1.0, keyable=True)
    base.update(kw)
    return CacheTableProfile(**base)


# ---------------------------------------------------------------------------
# Efficiency context (the v2 pillar itself is tested in test_cache_efficiency.py)
# ---------------------------------------------------------------------------


def test_small_sizes_do_not_read_as_zero_mb():
    assert fmt_mb(0.02) == "0.02"
    assert fmt_mb(3.52) == "3.5"
    assert fmt_mb(250.4) == "250"


def test_solo_read_map_ignores_joined_reports():
    pks = {"retail.order": ["order_id"], "retail.order_item": ["order_item_id"]}
    known = set(pks)
    queries = [
        {"query_text": "SELECT * FROM retail.\"order\" WHERE order_id = $1",
         "calls_per_second": 30.0, "rows_returned_avg": 1.0},
        {"query_text": "SELECT o.order_id, count(*) FROM retail.\"order\" o JOIN "
                       "retail.order_item oi ON oi.order_id = o.order_id GROUP BY 1",
         "calls_per_second": 10.0, "rows_returned_avg": 5000.0},
    ]
    solo = build_solo_read_map(queries, pks, known, {})
    assert solo["retail.order"] == {"rows_per_read": 1.0, "keyable": True, "reads": 1}
    assert "retail.order_item" not in solo


def test_solo_read_map_marks_range_only_tables_unkeyable():
    known = {"retail.order"}
    queries = [{"query_text": "SELECT * FROM retail.\"order\" WHERE order_date > $1",
                "calls_per_second": 5.0, "rows_returned_avg": 40.0}]
    solo = build_solo_read_map(queries, {"retail.order": ["order_id"]}, known, {})
    assert solo["retail.order"]["keyable"] is False


def test_table_profile_takes_rows_from_its_own_reads():
    co = {
        "database_schema": {"tables": [
            {"table_id": "retail.order", "size_mb": 3.5, "primary_key": ["order_id"]},
            {"table_id": "retail.payment", "size_mb": 2.3, "primary_key": ["payment_id"]},
        ]},
        "queries": {"query_patterns": [
            {"query_text": "SELECT * FROM retail.\"order\" WHERE order_id = $1",
             "query_type": "SELECT", "calls_per_second": 30.0, "rows_returned_avg": 1.0,
             "tables_accessed": ["retail.order"]},
            {"query_text": "SELECT o.order_id, sum(p.amount) FROM retail.\"order\" o JOIN "
                           "retail.payment p ON p.order_id = o.order_id GROUP BY 1",
             "query_type": "SELECT", "calls_per_second": 10.0, "rows_returned_avg": 900.0,
             "tables_accessed": ["retail.order", "retail.payment"]},
        ]},
    }
    profiles = build_cache_profiles(co, T)
    assert profiles["retail.order"].rows_per_read == 1.0
    assert profiles["retail.order"].max_rows_returned == 900.0   # context only
    assert profiles["retail.payment"].rows_per_read is None      # read only in joins


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


ROOMY = WorkloadContext(
    host_metrics={"cpu": {"utilization_pct": 40.0, "vcpu_count": 4},
                  "memory": {"used_pct": 30.0}, "swap": {"used_pct": 0.0},
                  "snapshot_alignment": {"representative": True}},
    statement_concurrency=1.0)


def test_idle_table_is_not_excluded_for_traffic():
    """Disqualifiers v2: traffic too low was dropped."""
    b = score_cache_candidacy(CacheTableProfile(table_id="t", size_mb=0.04,
                                                buffer_hit_ratio_pct=100.0), None, T)
    assert not any(r.startswith("Traffic too low") for r in b.exclusion_reasons)


def test_write_only_traffic_is_penalised_at_the_cap_not_excluded():
    b = score_cache_candidacy(_busy(read_calls_per_second=0.0, write_calls_per_second=40.0,
                                    read_ratio_weighted=0.0), ROOMY, T)
    assert not b.hard_excluded
    assert b.disqualifier_penalty == 55


def test_unmeasured_read_ratio_is_not_all_writes():
    b = score_cache_candidacy(_busy(read_ratio_weighted=0.0, read_ratio_known=False), None, T)
    assert not b.penalties
    assert any("not measured" in c for c in b.concerns)


def test_well_served_is_the_only_hard_exclusion_and_adds_no_points():
    b = score_cache_candidacy(_busy(read_ratio_weighted=0.42, weighted_exec_time_ms=0.09,
                                    db_ms_per_sec=3.6), ROOMY, T)
    assert b.hard_excluded and b.exclusion_kind == "well_served"
    assert b.exclusion_reasons == [b.exclusion_reason]
    assert b.disqualifier_penalty == 0
    assert b.confidence == 0


def test_penalties_are_listed_and_churn_is_context_only():
    b = score_cache_candidacy(_busy(read_ratio_weighted=0.50, dead_tuple_pct=25.0), None, T)
    assert not b.hard_excluded
    assert b.disqualifier_penalty == 20      # heavy writes at 50%; churn adds nothing
    assert [p["points"] for p in b.penalties] == [20]


def test_well_served_needs_host_headroom():
    """No headroom, no hard exclusion: the item is scored."""
    busy_host = {"cpu": {"utilization_pct": 95.0, "vcpu_count": 8},
                 "memory": {"used_pct": 20.0}, "swap": {"used_pct": 0.0}}
    ctx = WorkloadContext(host_metrics=busy_host, statement_concurrency=13.5)
    b = score_cache_candidacy(_busy(weighted_exec_time_ms=0.25), ctx, T)
    assert not b.hard_excluded
    assert b.well_served["host_headroom"] is False


def test_busy_but_fast_is_penalised_and_flagged():
    b = score_cache_candidacy(_busy(weighted_exec_time_ms=1.6, db_ms_per_sec=900.0), ROOMY, T)
    assert not b.hard_excluded and b.busy_but_fast
    assert b.disqualifier_penalty == 10


def test_degraded_mode_is_not_excluded_as_idle():
    p = CacheTableProfile(table_id="t", size_mb=5.0, read_ratio_weighted=0.95,
                          read_ratio_known=True, query_stats_available=False)
    b = score_cache_candidacy(p, WorkloadContext(), T)
    assert not any(r.startswith("Traffic too low") for r in b.exclusion_reasons)


def test_query_inherits_lock_penalty_but_not_churn():
    shape = classify_query("SELECT a, b FROM retail.t WHERE a = $1", {"retail.t": ["a"]},
                           {"retail.t"})
    p = QueryCacheProfile(queryid="q", query_text="x", shape=shape, calls_per_second=40.0,
                          avg_exec_ms=20.0, rows_returned_avg=1.0,
                          buffer_hit_ratio_pct=100.0)
    tables = {"retail.t": CacheTableProfile(table_id="retail.t", dead_tuple_pct=30.0,
                                            is_lock_contended=True)}
    score_query_candidacy(p, None, T, tables)
    assert p.breakdown.disqualifier_penalty == 15
    assert any("lock" in x["reason"].lower() for x in p.breakdown.penalties)


def test_lock_rows_resolve_by_relation_name_only():
    from src.tools.analysis.caching_scoring import _tables_from_lock_rows
    rows = [{"blocked_query": "UPDATE retail.t SET a = 1", "relation_name": "Retail.Orders"},
            {"blocked_query": "UPDATE retail.x SET a = 1"}]
    assert _tables_from_lock_rows(rows) == {"retail.orders"}


def test_gated_query_records_its_reason_in_the_list():
    shape = classify_query("SELECT * FROM retail.t WHERE ts > now()", {}, {"retail.t"})
    p = QueryCacheProfile(queryid="q", query_text="x", shape=shape, calls_per_second=5.0)
    score_query_candidacy(p, None, T)
    assert p.breakdown.exclusion_reasons == [p.breakdown.exclusion_reason]
    assert p.breakdown.exclusion_kind == "not_cacheable"
    assert p.read_dominance is None


def test_no_em_dashes_in_scorer_wording():
    b = score_cache_candidacy(_busy(read_ratio_weighted=0.42, weighted_exec_time_ms=0.09,
                                    rows_per_read=5000.0, size_mb=2000.0), None, T)
    assert not any("\u2014" in s for s in b.evidence + b.concerns + b.exclusion_reasons)
