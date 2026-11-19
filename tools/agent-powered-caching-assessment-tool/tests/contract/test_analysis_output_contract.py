"""
Contract tests for the analysis output (CacheAnalysisOutputV2, contract 2.7).

These pin down the invariants that make a recommendation auditable: score
bounds, mode handling, the thresholds and context that travel with every score,
the hard-exclusion rules, the accessors that decide the verdict of record, and
that the artifact survives the JSON the orchestrator stores it as.

The analyzer runs without the AI model (use_llm=False) so the tests are
repeatable and need no AWS access.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.agents.analysis.cache_candidacy_agent_v2 import analyze_cache_candidacy_v2
from src.contracts.cache_analysis_output import (
    AgentMetadata,
    CacheAssessmentMode,
    CacheTableRecommendation,
)
from src.contracts.cache_analysis_output_v2 import CacheAnalysisOutputV2, InterpretationMode

FIXTURES = Path(__file__).parent.parent / "fixtures"

pytestmark = pytest.mark.contract


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _analyze(name: str) -> CacheAnalysisOutputV2:
    out, _trace, _diagram = analyze_cache_candidacy_v2(_fixture(name), use_llm=False)
    return out


def _meta() -> AgentMetadata:
    return AgentMetadata(
        agent_name="t", agent_version="1", analysis_timestamp=datetime.now(UTC),
        analysis_duration_seconds=0.1,
    )


# ---------------------------------------------------------------------------
# Score bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["confidence_score", "access_suitability_score",
     "pressure_evidence_score", "cache_efficiency_score"],
)
def test_scores_are_bounded_0_to_100(field):
    base = {
        "table_id": "s.t", "verdict": "WEAK_CANDIDATE", "confidence_score": 50,
        "access_suitability_score": 50, "pressure_evidence_score": 50,
        "cache_efficiency_score": 50,
    }
    for bad in (-1, 101):
        with pytest.raises(ValidationError):
            CacheTableRecommendation.model_validate({**base, field: bad})


def test_disqualifier_penalty_cannot_be_negative():
    """The penalty is subtracted, so a negative value would silently become a
    bonus.
    """
    with pytest.raises(ValidationError):
        CacheTableRecommendation.model_validate({
            "table_id": "s.t", "verdict": "WEAK_CANDIDATE", "confidence_score": 50,
            "access_suitability_score": 50, "pressure_evidence_score": 50,
            "cache_efficiency_score": 50, "disqualifier_penalty": -10,
        })


def test_efficiency_may_be_not_scored():
    """Missing data is never 0: a record may carry no Efficiency score at all."""
    r = CacheTableRecommendation.model_validate({
        "table_id": "s.t", "verdict": "WEAK_CANDIDATE", "confidence_score": 50,
        "access_suitability_score": 50, "pressure_evidence_score": 50,
        "cache_efficiency_score": None,
    })
    assert r.cache_efficiency_score is None


# ---------------------------------------------------------------------------
# Mode handling
# ---------------------------------------------------------------------------


def test_assessment_mode_defaults_to_full():
    out = CacheAnalysisOutputV2(agent_metadata=_meta())
    assert out.assessment_mode is CacheAssessmentMode.FULL
    assert out.interpretation_mode is InterpretationMode.DETERMINISTIC_FALLBACK


def test_degraded_mode_accepts_a_reason():
    out = CacheAnalysisOutputV2(
        agent_metadata=_meta(),
        assessment_mode=CacheAssessmentMode.DEGRADED,
        degraded_reason="pg_stat_statements not in shared_preload_libraries",
    )
    assert out.assessment_mode is CacheAssessmentMode.DEGRADED
    assert "shared_preload_libraries" in out.degraded_reason


def test_run_without_the_model_says_so():
    """A fallback must be labelled, so bands are never read as a model's judgement."""
    out = _analyze("strong_candidate_collection.json")
    assert out.interpretation_mode is InterpretationMode.DETERMINISTIC_FALLBACK
    assert out.llm_interpretation is None
    assert out.fallback_reason and "use_llm=False" in out.fallback_reason


def test_thresholds_are_recorded_for_reproducibility():
    """A score is meaningless without the thresholds that produced it, since
    those are uncalibrated defaults expected to change.
    """
    out = _analyze("strong_candidate_collection.json")
    assert out.thresholds_used
    for key in ("read_ratio_excellent", "buffer_hit_ratio_poor", "weight_pressure",
                "load_share_high", "bundle_max_band_spread"):
        assert key in out.thresholds_used


def test_workload_context_is_recorded():
    """Deployment-level context must travel with the scores so a reader can see
    what the numbers were interpreted against.
    """
    out = _analyze("strong_candidate_collection.json")
    assert "buffer_pool" in out.workload_context
    assert "lock_wait_share" in out.workload_context


# ---------------------------------------------------------------------------
# Hard exclusion invariants
# ---------------------------------------------------------------------------


def test_hard_excluded_tables_score_zero_confidence():
    """An exclusion must override the positive dimensions entirely. A table with
    a perfect access shape that is already well served must not surface as a
    candidate on score alone.
    """
    out = _analyze("no_candidate_collection.json")
    excluded = [r for r in out.table_recommendations if r.hard_excluded]
    assert excluded, "negative fixture should exclude tables"
    for r in excluded:
        assert r.confidence_score == 0
        assert r.exclusion_reason
        assert r.exclusion_kind == "well_served"
        assert r.verdict == "NOT_RECOMMENDED"


def test_excluded_table_can_still_have_a_high_access_score():
    """config_flags has a perfect read shape and is still correctly declined:
    PostgreSQL answers it from memory with the host idle.
    """
    out = _analyze("no_candidate_collection.json")
    flags = next(
        (r for r in out.table_recommendations if r.table_id.endswith("config_flags")), None
    )
    assert flags is not None
    assert flags.hard_excluded
    assert flags.access_suitability_score >= 90
    assert flags.confidence_score == 0


def test_accessors_never_recommend_an_excluded_table():
    out = _analyze("no_candidate_collection.json")
    excluded = {r.table_id for r in out.table_recommendations if r.hard_excluded}
    assert excluded
    assert excluded.isdisjoint(out.candidate_table_ids())
    for table_id in excluded:
        assert out.verdict_for(table_id) == "NOT_RECOMMENDED"


def test_accessors_read_the_bands_when_no_model_ran():
    out = _analyze("strong_candidate_collection.json")
    for r in out.table_recommendations:
        assert out.verdict_for(r.table_id) == r.verdict
    assert out.candidate_table_ids()


# ---------------------------------------------------------------------------
# Versions and bundles
# ---------------------------------------------------------------------------


def test_contract_version_is_semver_like():
    out = CacheAnalysisOutputV2(agent_metadata=_meta())
    assert out.contract_version == "2.7"
    with pytest.raises(ValidationError):
        CacheAnalysisOutputV2(agent_metadata=_meta(), contract_version="one")


def test_bundles_default_to_empty_and_ride_on_the_artifact():
    assert CacheAnalysisOutputV2(agent_metadata=_meta()).bundles == []
    out = _analyze("strong_candidate_collection.json")
    assert isinstance(out.bundles, list)
    assert all(isinstance(r.bundles, list) for r in out.table_recommendations)


def test_agent_metadata_reports_the_release():
    from src.version import __version__
    out = _analyze("strong_candidate_collection.json")
    assert out.agent_metadata.agent_version == __version__


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_analysis_output_survives_json_roundtrip():
    """The orchestrator persists artifacts as JSON, so the model must survive it."""
    out = _analyze("strong_candidate_collection.json")
    again = CacheAnalysisOutputV2.model_validate(json.loads(out.model_dump_json()))
    assert len(again.table_recommendations) == len(out.table_recommendations)
    assert len(again.query_recommendations) == len(out.query_recommendations)
    assert again.assessment_mode is out.assessment_mode
    assert again.candidate_table_ids() == out.candidate_table_ids()
