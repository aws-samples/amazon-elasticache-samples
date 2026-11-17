"""Cache Efficiency v2 (contract 2.5): estimated hit rate and result size.

Spec: .kiro/specs/cache-efficiency-v2. Worked examples use the pgmkt-sf14
observation window of 1,501.8 s.
"""

import pytest
import sqlglot

from src.agents.collector.postgres_collector import group_column_stats, normalize_reltuples
from src.tools.analysis.cache_efficiency import (
    TableStats,
    build_alias_map,
    build_table_stats,
    bytes_factor,
    convert_n_distinct,
    efficiency_score,
    estimate_hit_rate,
    extract_outputs,
    extract_params,
    resolve_k,
    reuse_score,
    rollup_table_efficiency,
    row_width,
    score_query_efficiency,
    time_saved_score,
)
from src.tools.analysis.caching_scoring import (
    DEFAULT_THRESHOLDS,
    CacheScoreBreakdown,
    CacheTableProfile,
    compute_cache_confidence,
    score_cache_candidacy,
)
from src.tools.analysis.query_cache_analysis import (
    build_query_profiles,
    score_query_profiles,
    table_efficiency_rollup,
)

W = 1501.8
STATS = {
    "s.customer": TableStats(
        reltuples=1_000_000, primary_key=["customer_id"],
        columns=["customer_id", "email", "tier"],
        column_types={"customer_id": "int8", "email": "text", "tier": "int2"},
        column_stats={"customer_id": {"n_distinct": -1.0, "avg_width": 8},
                      "email": {"n_distinct": -1.0, "avg_width": 24},
                      "tier": {"n_distinct": 5.0, "avg_width": 2}}),
    "s.flag": TableStats(
        reltuples=40, primary_key=["name"], columns=["name", "enabled"],
        column_stats={"name": {"n_distinct": -1.0, "avg_width": 20},
                      "enabled": {"n_distinct": 2.0, "avg_width": 1}}),
}


def _shape(sql: str):
    tree = sqlglot.parse_one(sql, dialect="postgres")
    tcols = {t: set(s.columns) for t, s in STATS.items()}
    al = build_alias_map(tree, lambda n: n if "." in n else f"s.{n}")
    return (extract_params(tree, al, tcols),
            extract_outputs(tree, al, tcols, {t: s.column_types for t, s in STATS.items()}))


# --- collector helpers -------------------------------------------------------

def test_reltuples_minus_one_means_unknown():
    assert normalize_reltuples("-1") is None
    assert normalize_reltuples(None) is None
    assert normalize_reltuples("250") == 250.0


def test_column_stats_grouped_by_table():
    rows = [{"schema_name": "s", "table_name": "t", "column_name": "c",
             "n_distinct": "-0.5", "avg_width": "8"}]
    assert group_column_stats(rows) == {"s.t": {"c": {"n_distinct": -0.5, "avg_width": 8}}}


# --- K -------------------------------------------------------------------------

def test_negative_n_distinct_is_a_fraction_of_rows():
    assert convert_n_distinct(-0.5, 1000) == 500
    assert convert_n_distinct(250, None) == 250
    assert convert_n_distinct(-0.5, None) is None
    assert convert_n_distinct(0, 100) is None


@pytest.mark.parametrize("sql,k,source", [
    ("SELECT name, enabled FROM s.flag", 1.0, "no parameters"),
    ("SELECT * FROM s.customer WHERE customer_id = $1", 1_000_000, "primary key"),
    ("SELECT * FROM s.customer WHERE tier = $1", 5, "n_distinct"),
    ("SELECT * FROM s.customer WHERE email = $1 AND tier = $2", 1_000_000, "n_distinct"),
])
def test_k_by_parameter_shape(sql, k, source):
    got = resolve_k(_shape(sql)[0], STATS)
    assert got[0] == k and got[1] == source


@pytest.mark.parametrize("sql", [
    "SELECT * FROM s.customer WHERE tier > $1",
    "SELECT * FROM s.customer WHERE tier IN ($1, $2)",
    "SELECT * FROM s.customer WHERE email LIKE $1",
    "SELECT * FROM s.customer ORDER BY tier OFFSET $1",
])
def test_range_in_like_offset_are_deferred(sql):
    k, _src, deferred, missing = resolve_k(_shape(sql)[0], STATS)
    assert k is None and deferred and missing is None


def test_limit_parameter_does_not_change_k():
    assert resolve_k(_shape("SELECT * FROM s.flag LIMIT $1")[0], STATS)[0] == 1.0


def test_missing_statistics_are_not_scored_not_zero():
    k, _s, deferred, missing = resolve_k(_shape("SELECT * FROM s.customer WHERE nope = $1")[0],
                                         STATS)
    assert k is None and deferred is None and missing


# --- reuse ---------------------------------------------------------------------

@pytest.mark.parametrize("cps,k,want", [
    (54.8, 1, 1.0), (8.2, 250, 0.980), (75.5, 10_000, 0.912),
    (75.5, 100_000, 0.402), (100, 1_000_000, 0.072),
])
def test_estimated_hit_rate_sf14_examples(cps, k, want):
    assert estimate_hit_rate(cps * W, k) == pytest.approx(want, abs=0.006)


def test_reuse_full_marks_at_point_eight_linear_below():
    assert reuse_score(0.8) == 100 and reuse_score(0.99) == 100
    assert reuse_score(0.4) == 50 and reuse_score(0.0) == 0


# --- result size ---------------------------------------------------------------

@pytest.mark.parametrize("ms,want", [(0.07, 0), (1.0, 0), (9.0, 48), (68.9, 92), (885, 100)])
def test_time_saved_log_scale_1_to_100_ms(ms, want):
    assert time_saved_score(ms) == want


@pytest.mark.parametrize("nbytes,want", [
    (50 * 1024, 1.0), (1024 ** 2, 0.70), (10 * 1024 ** 2, 0.41), (300 * 1024 ** 2, 0.0)])
def test_bytes_factor_log_scale_100kb_to_256mb(nbytes, want):
    assert bytes_factor(nbytes) == pytest.approx(want, abs=0.01)


def test_row_width_star_and_variable_outputs():
    assert row_width(_shape("SELECT * FROM s.customer")[1], STATS) == (34.0, None)
    assert row_width(_shape("SELECT count(*), customer_id FROM s.customer")[1], STATS)[0] == 16
    w, why = row_width(_shape("SELECT string_agg(email, ',') FROM s.customer")[1], STATS)
    assert w is None and why


# --- pillar --------------------------------------------------------------------

def test_weights_and_rescaling():
    assert efficiency_score(100, 40) == 85
    assert efficiency_score(None, 40) == 40
    assert efficiency_score(None, None) is None


def test_feature_flags_and_customer_by_id_differ_clearly():
    def score(sql, cps, ms, rows):
        p, o = _shape(sql)
        return score_query_efficiency(calls_in_window=cps * W, window_seconds=W, exec_ms=ms,
                                      rows_per_call=rows, params=p, outputs=o, stats=STATS)
    flags = score("SELECT name, enabled FROM s.flag", 54.8, 0.06, 40)
    cust = score("SELECT * FROM s.customer WHERE customer_id = $1", 100, 0.07, 1)
    assert flags.score - cust.score >= 50
    assert flags.status == cust.status == "scored"


def test_deferred_query_scores_on_result_size_alone():
    p, o = _shape("SELECT * FROM s.customer WHERE tier > $1")
    r = score_query_efficiency(calls_in_window=10 * W, window_seconds=W, exec_ms=50,
                               rows_per_call=20, params=p, outputs=o, stats=STATS)
    assert r.status == "deferred" and r.reuse_score is None
    assert r.score == r.result_size_score


def test_table_rollup_sums_hits_and_skips_deferred():
    p, o = _shape("SELECT * FROM s.customer WHERE tier = $1")
    a = score_query_efficiency(calls_in_window=1000, window_seconds=W, exec_ms=5,
                               rows_per_call=1, params=p, outputs=o, stats=STATS)
    p2, o2 = _shape("SELECT * FROM s.customer WHERE tier > $1")
    d = score_query_efficiency(calls_in_window=500, window_seconds=W, exec_ms=5,
                               rows_per_call=1, params=p2, outputs=o2, stats=STATS)
    roll = rollup_table_efficiency([(1000, a), (500, d)])
    assert roll.estimated_hit_rate == pytest.approx(a.estimated_hit_rate, abs=1e-4)
    assert roll.queries_deferred == 1 and roll.queries_rolled_up == 2
    assert rollup_table_efficiency([]).score is None


# --- integration ---------------------------------------------------------------

def test_confidence_rescales_when_efficiency_not_scored():
    b = CacheScoreBreakdown(access_suitability=80, pressure_evidence=40, cache_efficiency=None)
    p = CacheTableProfile(table_id="t")
    assert compute_cache_confidence(b, p, DEFAULT_THRESHOLDS) == round(
        (80 * 0.35 + 40 * 0.40) / 0.75)


def test_no_efficiency_is_not_scored_with_a_reason():
    b = score_cache_candidacy(CacheTableProfile(table_id="t", total_calls_per_second=5.0,
                                                read_calls_per_second=5.0,
                                                read_ratio_weighted=1.0, read_ratio_known=True,
                                                weighted_exec_time_ms=20.0), None)
    assert b.cache_efficiency is None
    assert any("not scored" in c for c in b.concerns)


def _collection(with_stats: bool) -> dict:
    cols = [{"column_name": "customer_id", "udt_name": "int8"},
            {"column_name": "tier", "udt_name": "int2"}]
    table = {"table_id": "s.customer", "size_mb": 10.0, "primary_key": ["customer_id"],
             "columns": cols}
    if with_stats:
        table["reltuples"] = 1000.0
        table["column_stats"] = {"customer_id": {"n_distinct": -1.0, "avg_width": 8},
                                 "tier": {"n_distinct": 5.0, "avg_width": 2}}
    q = {"query_id": "1", "query_text": "SELECT * FROM s.customer WHERE tier = $1",
         "query_type": "SELECT", "calls_per_second": 10.0, "execution_count": 15018,
         "stats_window_seconds": W, "execution_time_ms_avg": 20.0, "total_time_ms": 300360.0,
         "rows_returned_avg": 200.0, "cache_hit_ratio_pct": 90.0, "tables_accessed": ["s.customer"]}
    return {"database_schema": {"tables": [table]}, "queries": {"query_patterns": [q]}}


def test_end_to_end_query_and_table_efficiency():
    coll = _collection(with_stats=True)
    profiles, _ = build_query_profiles(coll, thresholds=DEFAULT_THRESHOLDS)
    score_query_profiles(profiles, None, DEFAULT_THRESHOLDS)
    e = profiles[0].efficiency
    assert e.k == 5 and e.k_source == "n_distinct" and e.estimated_hit_rate > 0.99
    assert e.result_bytes == 2000.0 and profiles[0].breakdown.cache_efficiency == e.score
    roll = table_efficiency_rollup(profiles, DEFAULT_THRESHOLDS)
    assert roll["s.customer"].reuse_score == 100


def test_older_collection_without_stats_still_runs():
    coll = _collection(with_stats=False)
    profiles, _ = build_query_profiles(coll)
    e = profiles[0].efficiency
    assert e.reuse_score is None and e.reuse_missing_reason
    assert e.result_size_score is None and e.score is None and e.status == "not_scored"
    assert build_table_stats(coll)["s.customer"].reltuples is None


# --- round 2: numeric aggregates, constants, NEQ, agent-estimated K -------------

STATS["s.orders"] = TableStats(
    reltuples=500_000, primary_key=["order_id"],
    columns=["order_id", "customer_id", "amount"],
    column_types={"order_id": "int8", "customer_id": "int8", "amount": "numeric"},
    column_stats={"order_id": {"n_distinct": -1.0, "avg_width": 8},
                  "customer_id": {"n_distinct": 20_000.0, "avg_width": 8},
                  "amount": {"n_distinct": -0.4, "avg_width": 7}})


def test_numeric_aggregates_use_widest_input_column():
    o = _shape("SELECT coalesce(sum(amount), $2), avg(amount), sum(customer_id - order_id) "
               "FROM s.orders WHERE customer_id = $1")[1]
    assert row_width(o, STATS) == (7 + 7 + 8, None)


@pytest.mark.parametrize("sql,params", [
    ("SELECT coalesce(sum(amount), $2) FROM s.orders WHERE customer_id = $1", ["@1"]),
    ("SELECT EXISTS (SELECT $3 FROM s.orders WHERE customer_id = $1 AND order_id = $2)",
     ["@1", "@2"]),
    ("SELECT order_id FROM s.orders WHERE customer_id = $1 ORDER BY order_id LIMIT $2", ["@1"]),
])
def test_constants_do_not_count_toward_k(sql, params):
    assert sorted(x["param"] for x in _shape(sql)[0]) == params


def test_tsquery_config_is_a_constant_but_typed_text_is_deferred():
    keyed = _shape("SELECT order_id FROM s.orders o WHERE o.v @@ plainto_tsquery($2, "
                   "(SELECT email FROM s.customer WHERE customer_id = $1))")[0]
    assert [(x["kind"], x["table"], x["column"]) for x in keyed] == [
        ("eq", "s.customer", "customer_id")]
    typed = _shape("SELECT order_id FROM s.orders o WHERE o.v @@ plainto_tsquery($1, $2)")[0]
    assert [(x["param"], x["kind"]) for x in typed] == [("@2", "other")]


def test_not_equal_counts_as_equality():
    p = _shape("SELECT order_id FROM s.orders WHERE customer_id = $1 AND order_id <> $2")[0]
    assert sorted(x["kind"] for x in p) == ["eq", "eq"]
    assert resolve_k(p, STATS)[:2] == (500_000, "primary key")


def test_agent_estimate_scores_deferred_reuse_as_distinct_requests_sent():
    from src.tools.analysis.cache_efficiency import apply_estimated_k
    p, o = _shape("SELECT * FROM s.customer WHERE tier > $1")
    d = score_query_efficiency(calls_in_window=10 * W, window_seconds=W, exec_ms=50,
                               rows_per_call=20, params=p, outputs=o, stats=STATS)
    e = apply_estimated_k(d, 50)
    assert e.status == "agent_estimated" and e.k_source == "agent estimate"
    # The estimate counts requests actually sent, so hits = N - D.
    assert e.estimated_hit_rate == round(1 - 50 / (10 * W), 4)
    assert e.estimated_hits == 10 * W - 50
    assert e.score == efficiency_score(e.reuse_score, e.result_size_score)
    assert apply_estimated_k(d, None).status == "deferred"


def test_agent_estimate_of_all_unique_requests_gives_no_reuse():
    """A cursor or watermark sends a new request every call: no hits, not 37%."""
    from src.tools.analysis.cache_efficiency import apply_estimated_k
    p, o = _shape("SELECT * FROM s.customer WHERE tier > $1")
    d = score_query_efficiency(calls_in_window=2082, window_seconds=W, exec_ms=50,
                               rows_per_call=20, params=p, outputs=o, stats=STATS)
    assert apply_estimated_k(d, 2082).estimated_hit_rate == 0.0
    assert apply_estimated_k(d, 5000).reuse_score == 0
    assert apply_estimated_k(d, 2000).estimated_hit_rate == 0.0394


def test_observed_hit_rate_edges():
    from src.tools.analysis.cache_efficiency import observed_hit_rate, observed_hits
    assert observed_hit_rate(100, 1) == 0.99
    assert observed_hit_rate(100, 0.2) == 0.99
    assert observed_hit_rate(0, 5) == 0.0
    assert observed_hit_rate("x", 5) == 0.0
    assert observed_hits(100, 500) == 0.0


def test_validation_requires_estimate_and_reasoning_for_deferred_queries():
    from src.agents.analysis.cache_candidacy_agent_v2 import _StructuredResult, _validate_and_build
    from src.contracts.cache_analysis_output_v2 import LLMQueryVerdict

    def q(qid, **kw):
        return LLMQueryVerdict(queryid=qid, verdict="MODERATE_CANDIDATE", reasoning="r", **kw)

    workload = {"overall_recommendation": "PARTIALLY_RECOMMENDED", "key_finding": "k",
                "executive_summary": "s", "primary_bottleneck": "b", "workload_archetype": "a"}
    res = _StructuredResult.model_validate({"workload": workload, "tables": [], "queries": [
        q("a", reuse_reasoning="fixed day", estimated_distinct_requests=30).model_dump(),
        q("b", reuse_reasoning="fixed day").model_dump(),
        q("c", estimated_distinct_requests=30).model_dump(),
        q("d", reuse_reasoning="x", estimated_distinct_requests=0.5).model_dump(),
        q("e").model_dump(),
    ]})
    out = _validate_and_build(res, {}, "m", 0.0, [], known_query_ids=set("abcde"),
                              deferred_query_ids=set("abcd"))
    assert sorted(v.queryid for v in out.queries) == ["a", "e"]
