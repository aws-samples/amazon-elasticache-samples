"""
Scoring smoke tests, run through the analyzer without the AI model.

These are the two checks that matter most: the positive fixture must produce a
candidate and the negative fixture must not. If either flips, thresholds have
drifted into nonsense.

They also lock in the two defects fixed relative to the upstream scoring module:
frequency-weighted read ratio, and no dependence on Performance Insights.
"""

import copy
import json
from pathlib import Path

import pytest

from src.agents.analysis.cache_candidacy_agent_v2 import analyze_cache_candidacy_v2
from src.tools.analysis.caching_scoring import build_cache_profiles

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def analyze_cache_candidacy(collection: dict):
    """The analyzer without the AI model, so the bands are what is tested."""
    return analyze_cache_candidacy_v2(collection, use_llm=False)


@pytest.fixture
def strong() -> dict:
    return _load("strong_candidate_collection.json")


@pytest.fixture
def none_fixture() -> dict:
    return _load("no_candidate_collection.json")


# ---------------------------------------------------------------------------
# Profile construction
# ---------------------------------------------------------------------------


def test_read_ratio_is_frequency_weighted_not_pattern_counted(strong):
    """The upstream defect: product_catalog has 2 SELECT patterns and 1 UPDATE
    pattern. Counting patterns gives 2/3 = 0.67. Weighting by call volume gives
    225/225.4 = ~0.998, which is the truth — the UPDATE runs 0.4 calls/sec
    against 225 calls/sec of reads.
    """
    profiles = build_cache_profiles(strong)
    p = profiles["public.product_catalog"]
    assert p.read_ratio_weighted > 0.99, (
        f"expected frequency-weighted ratio ~0.998, got {p.read_ratio_weighted}. "
        "A value near 0.67 means pattern-counting regressed."
    )


def test_write_heavy_table_ratio_reflects_call_volume(none_fixture):
    """transactions is genuinely write-dominated: 335 write cps vs 60 read cps."""
    profiles = build_cache_profiles(none_fixture)
    p = profiles["public.transactions"]
    assert p.read_ratio_weighted < 0.25


def test_degraded_mode_detected_when_query_stats_missing(strong):
    """Stripping query patterns must flip the profile into degraded provenance."""
    degraded = {**strong, "queries": {"query_patterns": []}}
    profiles = build_cache_profiles(degraded)
    p = profiles["public.product_catalog"]
    assert p.query_stats_available is False
    assert any("DEGRADED" in n for n in p.notes)


# ---------------------------------------------------------------------------
# Analysis agent
# ---------------------------------------------------------------------------


def test_strong_fixture_yields_a_candidate(strong):
    output, trace, _ = analyze_cache_candidacy(strong)
    assert trace["assessment_mode"] == "full"
    verdicts = {r.table_id: r.verdict for r in output.table_recommendations}
    assert verdicts["public.product_catalog"] in (
        "STRONG_CANDIDATE",
        "MODERATE_CANDIDATE",
    ), f"product_catalog should be a candidate, got {verdicts}"


def test_negative_fixture_yields_no_candidates(none_fixture):
    output, _trace, _ = analyze_cache_candidacy(none_fixture)
    candidates = [
        r for r in output.table_recommendations
        if r.verdict in ("STRONG_CANDIDATE", "MODERATE_CANDIDATE")
    ]
    assert not candidates, (
        "negative fixture produced candidates: "
        f"{[(r.table_id, r.verdict, r.confidence_score) for r in candidates]}"
    )


def test_write_dominated_table_is_penalised_not_excluded(none_fixture):
    """Disqualifiers v2: heavy writes are a log penalty, never a hard exclusion.

    The table's reads are made slower than the 2 ms well-served line, so the only
    thing that could exclude it is its writes. Its own metrics count reads only, so
    with fast reads it would be well served whatever its writes.
    """
    coll = copy.deepcopy(none_fixture)
    for q in coll["queries"]["query_patterns"]:
        if (q.get("query_type") == "SELECT"
                and "public.transactions" in (q.get("tables_accessed") or [])):
            q["execution_time_ms_avg"] = 5.0
    output, _trace, _ = analyze_cache_candidacy(coll)
    txn = next(
        r for r in output.table_recommendations if r.table_id == "public.transactions"
    )
    assert not txn.hard_excluded
    assert any(p["reason"].startswith("Heavy writes") for p in txn.penalties)
    assert txn.verdict == "NOT_RECOMMENDED"


def test_a_table_with_fast_reads_is_well_served_whatever_its_writes(none_fixture):
    """Reads only (0.4.0): a write-heavy table whose reads are fast and from memory is
    already well served. Its writes do not change what a cache would save on reads."""
    output, _trace, _ = analyze_cache_candidacy(none_fixture)
    txn = next(
        r for r in output.table_recommendations if r.table_id == "public.transactions"
    )
    assert txn.hard_excluded and txn.exclusion_kind == "well_served"


def test_low_traffic_table_is_scored_not_excluded(none_fixture):
    """Traffic too low was dropped: Access and Efficiency score it down instead."""
    output, _trace, _ = analyze_cache_candidacy(none_fixture)
    audit = next(
        r for r in output.table_recommendations if r.table_id == "public.audit_archive"
    )
    assert not audit.hard_excluded
    assert audit.verdict not in ("STRONG_CANDIDATE", "MODERATE_CANDIDATE")


def test_healthy_buffer_ratio_is_recorded_as_a_concern(none_fixture):
    """config_flags is read-heavy and frequent, but PG already serves it from
    memory at 99.9%. Pressure Evidence must stay low and say why.
    """
    output, _trace, _ = analyze_cache_candidacy(none_fixture)
    flags = next(
        r for r in output.table_recommendations if r.table_id == "public.config_flags"
    )
    assert flags.pressure_evidence_score < 40
    assert any("healthy" in c.lower() for c in flags.concerns)


def test_degraded_mode_caps_confidence(strong):
    degraded = {
        **strong,
        "queries": {"query_patterns": []},
        "pg_stat_statements_probe": {
            "available": False,
            "reason": "not in shared_preload_libraries — requires restart",
        },
    }
    output, trace, _ = analyze_cache_candidacy(degraded)
    assert trace["assessment_mode"] == "degraded"
    assert output.degraded_reason is not None
    assert all(r.confidence_score <= 60 for r in output.table_recommendations)


def test_thresholds_are_recorded_for_reproducibility(strong):
    output, _trace, _ = analyze_cache_candidacy(strong)
    assert output.thresholds_used
    assert "read_ratio_excellent" in output.thresholds_used
