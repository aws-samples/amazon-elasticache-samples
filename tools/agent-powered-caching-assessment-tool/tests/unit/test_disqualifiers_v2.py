"""Disqualifiers v2 (contract 2.6). Spec: .kiro/specs/disqualifiers-v2."""

import pytest

from src.tools.analysis.caching_scoring import (
    DEFAULT_THRESHOLDS as T,
)
from src.tools.analysis.caching_scoring import (
    CacheTableProfile,
    WorkloadContext,
    assess_well_served,
    db_share_of_capacity,
    host_headroom,
    score_cache_candidacy,
    write_penalty,
)

HOST = {"cpu": {"utilization_pct": 47.0, "vcpu_count": 4}, "memory": {"used_pct": 32.0},
        "swap": {"used_pct": 0.0}, "snapshot_alignment": {"representative": True}}
CTX = WorkloadContext(host_metrics=HOST, statement_concurrency=1.0)


def _p(**kw) -> CacheTableProfile:
    base = dict(table_id="t", total_calls_per_second=50.0, read_calls_per_second=50.0,
                read_ratio_weighted=1.0, read_ratio_known=True, weighted_exec_time_ms=0.05,
                buffer_hit_ratio_pct=100.0, db_ms_per_sec=2.5)
    base.update(kw)
    return CacheTableProfile(**base)


@pytest.mark.parametrize("w,want", [(None, 0), (0.2, 0), (0.3, 0), (0.4, 10), (0.5, 20),
                                    (0.6, 30), (0.7, 40), (0.8, 53), (0.9, 55), (1.0, 55)])
def test_write_penalty_curve(w, want):
    assert write_penalty(w) == want


@pytest.mark.parametrize("host,conc,ok", [
    (HOST, 1.0, True),
    ({**HOST, "cpu": {"utilization_pct": 70.0, "vcpu_count": 4}}, 1.0, False),
    ({**HOST, "memory": {"used_pct": 85.0}}, 1.0, False),
    ({**HOST, "swap": {"used_pct": 2.0}}, 1.0, False),
    (HOST, 4.5, False),
    ({}, None, False),
])
def test_host_headroom(host, conc, ok):
    assert host_headroom(WorkloadContext(host_metrics=host, statement_concurrency=conc))[0] is ok


def test_share_of_capacity():
    assert db_share_of_capacity(906.5, 4) == pytest.approx(0.2266, abs=1e-4)
    assert db_share_of_capacity(10, None) is None


def test_share_of_capacity_is_capped_under_saturation():
    """sf14 5x: catalog.product had 57,742 ms/s on 4 vCPU, a raw 14.4."""
    from src.tools.analysis.caching_scoring import share_text
    assert db_share_of_capacity(57742.0, 4) == 1.0
    assert share_text(1.0) == "at least 100%"
    assert share_text(0.0625) == "6.2%"
    assert share_text(None) == "unknown"


@pytest.mark.parametrize("kw,excluded,busy", [
    ({}, True, False),
    ({"db_ms_per_sec": 200.0}, False, True),           # exactly 5% is busy
    ({"db_ms_per_sec": 199.0}, True, False),
    ({"buffer_hit_ratio_pct": 99.0}, False, False),
    ({"weighted_exec_time_ms": 2.0}, False, False),
    ({"temp_blocks_written": 1}, False, False),
    ({"buffer_hit_ratio_pct": None}, False, False),
])
def test_well_served_criteria(kw, excluded, busy):
    ws = assess_well_served(_p(**kw), CTX)
    assert (ws["excluded"], ws["busy"]) == (excluded, busy)


def test_only_well_served_is_hard_and_it_adds_no_points():
    b = score_cache_candidacy(_p(), CTX)
    assert b.hard_excluded and b.exclusion_kind == "well_served"
    assert b.disqualifier_penalty == 0 and not b.penalties


def test_penalties_stack_as_flat_points():
    b = score_cache_candidacy(_p(read_ratio_weighted=0.3, weighted_exec_time_ms=1.6,
                                 db_ms_per_sec=900.0, is_lock_contended=True), CTX)
    assert not b.hard_excluded
    assert sorted(p["points"] for p in b.penalties) == [10, 15, 40]
    assert b.disqualifier_penalty == 65


def test_heavy_writes_are_never_hard():
    b = score_cache_candidacy(_p(read_ratio_weighted=0.0, weighted_exec_time_ms=400.0), CTX)
    assert not b.hard_excluded and b.disqualifier_penalty == 55


def test_validation_requires_busy_context_reasoning():
    from src.agents.analysis.cache_candidacy_agent_v2 import _StructuredResult, _validate_and_build
    workload = {"overall_recommendation": "PARTIALLY_RECOMMENDED", "key_finding": "k",
                "executive_summary": "s", "primary_bottleneck": "b", "workload_archetype": "a"}
    v = {"verdict": "MODERATE_CANDIDATE", "reasoning": "r"}
    res = _StructuredResult.model_validate({
        "workload": workload,
        "tables": [{"table_id": "a.t1", **v, "busy_context_reasoning": "retail peaks"},
                   {"table_id": "a.t2", **v}],
        "queries": [{"queryid": "q1", **v}, {"queryid": "q2", **v,
                                              "busy_context_reasoning": "x"}],
    })
    out = _validate_and_build(res, {"a.t1": "X", "a.t2": "X"}, "m", 0.0, [],
                              known_query_ids={"q1", "q2"},
                              busy_ids={"a.t1", "a.t2", "q1", "q2"})
    assert [t.table_id for t in out.tables] == ["a.t1"]
    assert [q.queryid for q in out.queries] == ["q2"]


# ---------------------------------------------------------------------------
# Pressure load share (spec pressure-load-share)
# ---------------------------------------------------------------------------


def test_load_share_steps():
    from src.tools.analysis.caching_scoring import load_share, load_share_points
    assert load_share_points(load_share(32547.7, 79.1065)) == 30   # 5x search, 41%
    assert load_share_points(load_share(4958.3, 79.1065)) == 10    # 5x facets, 6.3%
    assert load_share_points(load_share(3441.2, 79.1065)) == 0     # 4.4%
    assert load_share_points(0.10) == 20
    assert load_share(5000, 1.0) == 1.0                            # join-inclusive cap
    assert load_share(10, None) is None and load_share_points(None) == 0


def test_load_share_adds_to_pressure_for_in_memory_work():
    """A 160 ms query served from memory that is 41% of DB time gets +30."""
    from src.tools.analysis.caching_scoring import CacheScoreBreakdown, _score_pressure
    ctx = WorkloadContext(host_metrics=HOST, statement_concurrency=79.1065)
    base = dict(buffer_hit_ratio_pct=100.0, weighted_exec_time_ms=160.0)
    big = _p(db_ms_per_sec=32547.7, **base)
    small = _p(db_ms_per_sec=10.0, **base)
    b1, b2 = CacheScoreBreakdown(), CacheScoreBreakdown()
    assert _score_pressure(big, ctx, T, b1) - _score_pressure(small, ctx, T, b2) == 30
    assert any("41% of all database time" in e for e in b1.evidence)
