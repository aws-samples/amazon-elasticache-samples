"""Cache bundles (contract 2.7). Spec: .kiro/specs/cache-bundles."""
from types import SimpleNamespace as NS

from src.tools.analysis.cache_bundles import (
    bundles_by_table,
    check_bundle_table,
    detect_bundles,
    interval_text,
    qualifying_bundle_tables,
    row_change_seconds,
)
from src.tools.analysis.caching_scoring import DEFAULT_THRESHOLDS as T


def _tp(writes, rows, locked=False):
    return NS(write_calls_per_second=writes, row_count=rows, is_lock_contended=locked)


def _tb(hard=False, kind=None):
    return NS(hard_excluded=hard, exclusion_kind=kind)


def _qp(qid, share, ms, reads, kind="not_cacheable", writes=None, system=False):
    return NS(queryid=qid, query_text=f"q{qid}", db_time_share_pct=share, db_time_ms_per_sec=ms,
              shape=NS(tables_read=reads, tables_written=writes or [], is_system_query=system,
                       unresolved_relations=[]),
              breakdown=NS(exclusion_kind=kind, exclusion_reason="Time-dependent"))


# sf14 5x figures.
PROF = {"catalog.promotion": _tp(0.0, 421137), "catalog.price": _tp(17.727, 1_400_000),
        "catalog.product": _tp(0.0, 1_400_010), "sales.cart": _tp(58.5, 700_000)}
BRK = {k: _tb() for k in PROF}
BRK["sales.cart"] = _tb(True, "well_served")
LIVE = _qp("live", 23.62, 18681.3, ["catalog.price", "catalog.product", "catalog.promotion"])


def test_row_change_interval():
    assert round(row_change_seconds(17.727, 1_400_000) / 3600, 2) == 21.94
    assert row_change_seconds(0, 10) is None
    assert row_change_seconds(5, 0) == 0.0
    assert interval_text(200) == "3 minutes" and interval_text(2) == "2 seconds"


def test_live_promotions_is_a_qualifying_bundle():
    (b,) = detect_bundles([LIVE], PROF, BRK, T)
    assert b["qualifies"] and b["tables"] == ["catalog.price", "catalog.product", "catalog.promotion"]
    assert qualifying_bundle_tables([b]) == set(b["tables"])
    m = bundles_by_table([b])["catalog.price"][0]
    assert m["other_tables"] == ["catalog.product", "catalog.promotion"] and m["qualifies"]


def test_failing_table_blocks_the_bundle():
    (b,) = detect_bundles([_qp("c", 10, 50, ["catalog.product", "sales.cart"])], PROF, BRK, T)
    assert not b["qualifies"]
    assert qualifying_bundle_tables([b]) == set()
    assert bundles_by_table([b])["sales.cart"][0]["this_table_reason"] == "hard excluded (well_served)"


def test_hot_rows_fail():
    c = check_bundle_table("t", _tp(5.0, 1000), _tb(), T)
    assert not c["qualifies"] and "3 minutes" in c["reason"]


def test_only_not_cacheable_reads_with_enough_db_time():
    for q in (_qp("s", 4.9, 10, ["catalog.product"]),
              _qp("k", 30, 10, ["catalog.product"], kind=None),
              _qp("w", 30, 10, ["catalog.price"], writes=["catalog.price"]),
              _qp("y", 30, 10, ["catalog.price"], system=True)):
        assert detect_bundles([q], PROF, BRK, T) == []


def test_validation_requires_bundle_reasoning():
    from src.agents.analysis.cache_candidacy_agent_v2 import _StructuredResult, _validate_and_build
    workload = {"overall_recommendation": "RECOMMENDED", "key_finding": "k",
                "executive_summary": "s", "primary_bottleneck": "b", "workload_archetype": "a"}
    res = _StructuredResult.model_validate({"workload": workload, "queries": [], "tables": [
        {"table_id": "catalog.price", "verdict": "MODERATE_CANDIDATE", "reasoning": "r",
         "bundle_reasoning": "replaces the live promotions report"},
        {"table_id": "catalog.product", "verdict": "MODERATE_CANDIDATE", "reasoning": "r"},
    ]})
    out = _validate_and_build(res, {"catalog.price": "WEAK_CANDIDATE",
                                    "catalog.product": "WEAK_CANDIDATE"},
                              "m", 1.0, ["get_table_detail"],
                              bundle_ids={"catalog.price", "catalog.product"})
    assert [t.table_id for t in out.tables] == ["catalog.price"]


def test_report_shows_bundle_factors():
    from src.contracts.cache_analysis_output_v2 import LLMTableVerdict
    from src.tools.report.html_report_v2 import _bundle_factors
    (b,) = detect_bundles([LIVE], PROF, BRK, T)
    r = NS(bundles=bundles_by_table([b])["catalog.price"])
    v = LLMTableVerdict(table_id="catalog.price", verdict="MODERATE_CANDIDATE", reasoning="r",
                        bundle_reasoning="worth it")
    f = dict(_bundle_factors(r, v))
    assert "23.6% of all database time" in f["Bundle"]
    assert "catalog.product, catalog.promotion" in f["Bundle"]
    assert f["Bundle, in context"] == "worth it"


# ---------------------------------------------------------------------------
# Verdict consistency (spec CB-8)
# ---------------------------------------------------------------------------

BUNDLE = [{"queryid": "live", "db_time_share_pct": 23.18, "qualifies": True,
           "tables": ["catalog.price", "catalog.product", "catalog.promotion"]}]


def _interp(verdicts: dict):
    from src.contracts.cache_analysis_output_v2 import (
        LLMInterpretation,
        LLMTableVerdict,
        LLMWorkloadAssessment,
    )
    return LLMInterpretation(
        workload=LLMWorkloadAssessment(overall_recommendation="RECOMMENDED", executive_summary="s"),
        tables=[LLMTableVerdict(table_id=t, verdict=v, reasoning="r", bundle_reasoning="b")
                for t, v in verdicts.items()])


V1 = {"catalog.promotion": "MODERATE_CANDIDATE", "catalog.price": "NOT_RECOMMENDED",
      "catalog.product": "MODERATE_CANDIDATE"}


class _Agent:
    """Fake agent: structured_output returns a fixed repair or raises."""

    def __init__(self, tables=None, error=None):
        self.tables, self.error, self.prompts = tables or [], error, []

    def structured_output(self, schema, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return schema(tables=self.tables)


def test_conflicts_detects_the_1x_spread_and_allows_the_5x_one():
    from src.tools.analysis.cache_bundles import bundle_conflicts
    (c,) = bundle_conflicts(BUNDLE, V1)
    assert c["spread"] == 2
    assert bundle_conflicts(BUNDLE, {"catalog.promotion": "STRONG_CANDIDATE",
                                     "catalog.price": "MODERATE_CANDIDATE",
                                     "catalog.product": "MODERATE_CANDIDATE"}) == []


def test_repair_turn_resolves_and_records():
    from src.agents.analysis.cache_candidacy_agent_v2 import _repair_bundle_consistency
    from src.contracts.cache_analysis_output_v2 import LLMTableVerdict
    agent = _Agent([LLMTableVerdict(table_id="catalog.price", verdict="WEAK_CANDIDATE",
                                    reasoning="r2", bundle_reasoning="bundle worth it")])
    out = _repair_bundle_consistency(agent, _interp(V1), BUNDLE, T)
    assert {t.table_id: t.verdict for t in out.tables}["catalog.price"] == "WEAK_CANDIDATE"
    (rec,) = out.bundle_consistency
    assert rec["resolved"] and rec["revised"] == ["catalog.price"]
    assert rec["before"]["catalog.price"] == "NOT_RECOMMENDED"
    assert "23.18% of all database time" in agent.prompts[0]


def test_failed_repair_is_recorded_as_unresolved():
    from src.agents.analysis.cache_candidacy_agent_v2 import _repair_bundle_consistency
    out = _repair_bundle_consistency(_Agent(error=TimeoutError("slow")), _interp(V1), BUNDLE, T)
    assert out.bundle_consistency[0]["resolved"] is False
    assert {t.table_id: t.verdict for t in out.tables} == V1


def test_no_conflict_means_no_repair_turn():
    from src.agents.analysis.cache_candidacy_agent_v2 import _repair_bundle_consistency
    agent = _Agent()
    v = {**V1, "catalog.price": "WEAK_CANDIDATE"}
    out = _repair_bundle_consistency(agent, _interp(v), BUNDLE, T)
    assert agent.prompts == [] and out.bundle_consistency == []


def test_report_shows_warning_and_revision():
    from src.tools.report.html_report_v2 import _bundle_factors
    rec = {"queryid": "live", "tables": BUNDLE[0]["tables"], "before": V1,
           "after": {**V1, "catalog.price": "WEAK_CANDIDATE"}, "revised": ["catalog.price"],
           "resolved": True}
    f = dict(_bundle_factors(NS(table_id="catalog.price", bundles=[]), None, [rec]))
    assert "Revised from Not recommended to Weak candidate" in f["Bundle check"]
    bad = {**rec, "after": V1, "revised": [], "resolved": False}
    f = dict(_bundle_factors(NS(table_id="catalog.product", bundles=[]), None, [bad]))
    assert "more than one band apart" in f["Bundle warning"]
