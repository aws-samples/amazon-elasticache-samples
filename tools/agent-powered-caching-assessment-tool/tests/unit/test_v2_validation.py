"""
Validation of model output before it becomes the verdict of record.

A query verdict must name a real query, use a valid verdict string, and give
reasoning. Contract 2.3 has no TTL or staleness fields: if a model or an older
artifact supplies them, they are ignored rather than published.
"""

from src.agents.analysis.cache_candidacy_agent_v2 import (
    _StructuredResult,
    _validate_and_build,
)
from src.contracts.cache_analysis_output_v2 import (
    LLMQueryVerdict,
    LLMTableVerdict,
    LLMWorkloadAssessment,
)


def _validate(queries: list[LLMQueryVerdict]):
    result = _StructuredResult(
        workload=LLMWorkloadAssessment(overall_recommendation="NOT_RECOMMENDED",
                                       executive_summary="s"),
        queries=queries,
        tables=[LLMTableVerdict(table_id="retail.t", verdict="NOT_RECOMMENDED",
                                reasoning="because")],
    )
    return _validate_and_build(result, {"retail.t": "NOT_RECOMMENDED"}, "m", 1.0, [],
                               known_query_ids={"q1"})


def test_valid_query_verdict_is_kept():
    out = _validate([LLMQueryVerdict(queryid="q1", verdict="NOT_RECOMMENDED",
                                     reasoning="write-dominated")])
    assert [q.queryid for q in out.queries] == ["q1"]


def test_unknown_query_id_is_dropped():
    out = _validate([LLMQueryVerdict(queryid="nope", verdict="NOT_RECOMMENDED",
                                     reasoning="because")])
    assert out.queries == []


def test_query_verdict_without_reasoning_is_dropped():
    out = _validate([LLMQueryVerdict(queryid="q1", verdict="STRONG_CANDIDATE",
                                     reasoning="  ")])
    assert out.queries == []


def test_invalid_verdict_string_is_dropped():
    out = _validate([LLMQueryVerdict(queryid="q1", verdict="MAYBE", reasoning="x")])
    assert out.queries == []


def test_ttl_and_staleness_from_older_output_are_ignored():
    v = LLMQueryVerdict.model_validate({
        "queryid": "q1", "verdict": "MODERATE_CANDIDATE", "reasoning": "because",
        "suggested_ttl_seconds": 60, "staleness_assumption": "up to 60 s old",
    })
    dumped = v.model_dump()
    assert "suggested_ttl_seconds" not in dumped
    assert "staleness_assumption" not in dumped


def test_bedrock_client_config_has_long_read_timeout_and_retries():
    """sf14 5x: extraction ran past the 120 s Strands default and fell back."""
    from src.agents.analysis.cache_candidacy_agent_v2 import bedrock_client_config
    c = bedrock_client_config()
    assert c.read_timeout >= 600
    assert c.retries == {"max_attempts": 3, "mode": "standard"}


def test_bad_read_timeout_env_falls_back(monkeypatch):
    from src.agents.analysis import cache_candidacy_agent_v2 as m
    monkeypatch.setenv("PGCA_BEDROCK_READ_TIMEOUT_SEC", "abc")
    assert m._read_timeout_from_env() == m.DEFAULT_BEDROCK_READ_TIMEOUT_SEC
    monkeypatch.setenv("PGCA_BEDROCK_READ_TIMEOUT_SEC", "900")
    assert m._read_timeout_from_env() == 900
