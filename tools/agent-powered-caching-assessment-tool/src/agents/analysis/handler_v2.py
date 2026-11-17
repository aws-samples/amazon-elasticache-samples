"""Handler for the analysis agent: the entry point the orchestrator and scripts use."""

import logging

from src.agents.analysis.cache_candidacy_agent_v2 import analyze_cache_candidacy_v2
from src.tools.analysis.caching_scoring import DEFAULT_THRESHOLDS, CacheScoringThresholds

logger = logging.getLogger(__name__)


def run_analysis_v2(
    collector_output: dict,
    thresholds: CacheScoringThresholds = DEFAULT_THRESHOLDS,
    model_id: str | None = None,
    region: str | None = None,
    use_llm: bool = True,
) -> tuple[dict, dict]:
    """Run the analysis.

    Args:
        collector_output: Output from the collector stage.
        thresholds: Scoring thresholds.
        model_id: Bedrock model id override.
        region: AWS region override.
        use_llm: False scores only, with the score bands as the verdicts.

    Returns:
        (analysis_output_dict, decision_trace)
    """
    output, trace, _diagram = analyze_cache_candidacy_v2(
        collector_output, thresholds, model_id=model_id, region=region, use_llm=use_llm,
    )

    if trace["interpretation_mode"] == "llm":
        logger.info(
            "v2 analysis complete via LLM: %d tables, %d tool calls, verdict %s, %d candidate(s)",
            trace["tables_analyzed"],
            trace["llm_tool_calls"],
            trace["workload_verdict"],
            trace["candidates"],
        )
    else:
        logger.warning(
            "v2 analysis fell back to deterministic bands: %s", trace["fallback_reason"]
        )

    return output.model_dump(mode="json"), trace
