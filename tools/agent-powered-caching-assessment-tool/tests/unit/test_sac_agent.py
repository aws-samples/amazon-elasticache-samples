"""Unit tests for the Sizing & Cost agent.

The regressions that matter most: a declined or hard-excluded item is never
sized (product rule 7), fallback is labelled, and a degraded run sizes nothing.
"""

from src.agents.sizing.handler import run_sizing
from src.agents.sizing.sac_agent import size_and_cost
from src.contracts.cache_analysis_output_v2 import CacheAnalysisOutputV2
from src.tools.report.sizing_section import render_sizing_section

META = {"agent_name": "t", "agent_version": "0", "analysis_timestamp": "2026-01-01T00:00:00Z",
        "analysis_duration_seconds": 1.0}

COLLECTION = {"database_schema": {"tables": [{
    "table_id": "s.customer", "primary_key": ["id"], "reltuples": 1000.0,
    "columns": [{"column_name": "id", "udt_name": "int8"},
                {"column_name": "email", "udt_name": "varchar"},
                {"column_name": "notes", "udt_name": "text"}],
    "column_stats": {"id": {"avg_width": 8}, "email": {"avg_width": 24},
                     "notes": {"avg_width": 400}},
}]}}

EFF_Q = {"calls_in_window": 10_000, "k": 50, "k_source": "n_distinct",
         "estimated_hit_rate": 0.995, "result_bytes": 2048}
EFF_O = {"calls_in_window": 10_000, "k": 1000, "k_source": "primary key",
         "estimated_hit_rate": 0.9}


def _q(qid, cache_type="QUERY", verdict="MODERATE_CANDIDATE", hard=False, eff=None, **kw):
    return {"queryid": qid, "query_text": "select 1", "cache_type": cache_type,
            "calls_per_second": 10.0, "db_time_ms_per_sec": 500.0,
            "invalidating_write_rate": 0.5, "verdict": verdict, "confidence_score": 60,
            "hard_excluded": hard, "efficiency": eff, **kw}


def _analysis(queries, *, mode="deterministic_fallback", llm_queries=None,
              assessment_mode="full", tables=None, llm_tables=None, tool_calls=5):
    a = {"agent_metadata": META, "assessment_mode": assessment_mode,
         "interpretation_mode": mode, "query_recommendations": queries,
         "table_recommendations": tables or [],
         "query_analysis_summary": {"write_rate_by_table": {"s.customer": 2.0}}}
    if mode == "llm":
        a["llm_interpretation"] = {
            "workload": {"overall_recommendation": "PARTIALLY_RECOMMENDED",
                         "executive_summary": "x"},
            "queries": llm_queries or [], "tables": llm_tables or [],
            "tool_calls_made": tool_calls}
    return a


def _lv(qid, verdict, **kw):
    return {"queryid": qid, "verdict": verdict, "reasoning": "because", **kw}


def test_only_recommended_queries_are_sized():
    a = _analysis([_q("good", eff=EFF_Q), _q("weak", verdict="WEAK_CANDIDATE", eff=EFF_Q),
                   _q("excluded", hard=True, eff=EFF_Q)])
    s = size_and_cost(a, COLLECTION, price=False)
    assert [i.item_id for i in s.items] == ["good"]


def test_model_decline_overrides_a_moderate_band():
    a = _analysis([_q("q1", eff=EFF_Q)], mode="llm",
                  llm_queries=[_lv("q1", "NOT_RECOMMENDED")])
    s = size_and_cost(a, COLLECTION, price=False)
    assert s.status == "NOTHING_TO_SIZE" and not s.items


def test_model_cannot_promote_a_hard_excluded_query():
    a = _analysis([_q("q1", hard=True, verdict="NOT_RECOMMENDED", eff=EFF_Q)], mode="llm",
                  llm_queries=[_lv("q1", "STRONG_CANDIDATE")])
    assert not size_and_cost(a, COLLECTION, price=False).items


def test_query_item_memory_from_entries_and_result_size():
    s = size_and_cost(_analysis([_q("q1", eff=EFF_Q)]), COLLECTION, price=False)
    i = s.query_items[0]
    assert i.sized and 49 < i.entries <= 50
    assert i.bytes_per_entry == 48 + 2048 * 1.25 + 64
    assert s.status == "SIZED" and s.node.instance_type.startswith("cache.r")


def test_object_item_uses_model_chosen_columns():
    a = _analysis([_q("o1", cache_type="OBJECT", eff=EFF_O, tables_read=["s.customer"],
                      key_columns=["id"], rows_returned_avg=1.0)], mode="llm",
                  llm_queries=[_lv("o1", "MODERATE_CANDIDATE", cached_columns=["id", "email"])])
    i = size_and_cost(a, COLLECTION, price=False).object_items[0]
    assert i.cached_columns == ["id", "email"] and i.cached_columns_source == "model"
    assert i.value_bytes == 32.0
    assert i.invalidation_ops_per_sec == 2.0


def test_object_item_without_model_columns_caches_whole_row():
    a = _analysis([_q("o1", cache_type="OBJECT", eff=EFF_O, tables_read=["s.customer"],
                      key_columns=["id"], rows_returned_avg=1.0)])
    i = size_and_cost(a, COLLECTION, price=False).object_items[0]
    assert i.cached_columns_source == "whole row" and i.value_bytes == 432.0


def test_two_lookups_on_one_key_share_one_keyspace():
    qs = [_q(q, cache_type="OBJECT", eff=EFF_O, tables_read=["s.customer"],
             key_columns=["id"], rows_returned_avg=1.0) for q in ("o1", "o2")]
    s = size_and_cost(_analysis(qs), COLLECTION, price=False)
    assert len(s.object_items) == 1
    assert s.object_items[0].query_ids == ["o1", "o2"]
    assert s.object_items[0].read_ops_per_sec == 20.0


def test_missing_statistics_leave_item_unsized_not_guessed():
    s = size_and_cost(_analysis([_q("q1", eff={"reuse_missing_reason": "no stats"})]),
                      COLLECTION, price=False)
    i = s.query_items[0]
    assert not i.sized and i.memory_bytes is None and i.not_sized_reason == "no stats"
    assert s.status == "NOT_SIZED"
    assert s.totals.read_ops_per_sec == 10.0  # traffic still counted


def test_degraded_assessment_sizes_nothing():
    s = size_and_cost(_analysis([_q("q1", eff=EFF_Q)], assessment_mode="degraded"),
                      COLLECTION, price=False)
    assert s.status == "INSUFFICIENT_DATA" and not s.items


def test_fallback_and_ungrounded_model_are_labelled():
    s = size_and_cost(_analysis([_q("q1", eff=EFF_Q)]), COLLECTION, price=False)
    assert s.verdict_source == "deterministic_fallback"
    assert any("threshold bands" in w for w in s.warnings)
    a = _analysis([_q("q1", eff=EFF_Q)], mode="llm",
                  llm_queries=[_lv("q1", "STRONG_CANDIDATE")], tool_calls=0)
    assert any("no tool calls" in w for w in size_and_cost(a, COLLECTION, price=False).warnings)


def test_recommended_table_sized_from_row_estimate():
    t = {"table_id": "s.customer", "verdict": "MODERATE_CANDIDATE", "confidence_score": 55,
         "access_suitability_score": 70, "pressure_evidence_score": 40,
         "read_calls_per_second": 5.0}
    s = size_and_cost(_analysis([], tables=[t]), COLLECTION, price=False)
    i = s.object_items[0]
    assert i.source == "table" and i.entries == 1000.0 and i.key_columns == ["id"]


def test_handler_attaches_valid_sizing_and_report_renders_it():
    a = run_sizing(_analysis([_q("q1", eff=EFF_Q)]), COLLECTION, price=False)
    CacheAnalysisOutputV2.model_validate(a)
    html = render_sizing_section(a["sizing"])
    assert 'id="sizing"' in html and "Not a capacity plan" in html and "Not a quote" in html
    assert "pricing skipped by caller" in html


def test_table_without_primary_key_is_not_object_sized():
    col = {"database_schema": {"tables": [{**COLLECTION["database_schema"]["tables"][0],
                                           "primary_key": []}]}}
    t = {"table_id": "s.customer", "verdict": "MODERATE_CANDIDATE", "confidence_score": 55,
         "access_suitability_score": 70, "pressure_evidence_score": 40,
         "read_calls_per_second": 5.0}
    i = size_and_cost(_analysis([], tables=[t]), col, price=False).object_items[0]
    assert not i.sized and "no primary key" in i.not_sized_reason
