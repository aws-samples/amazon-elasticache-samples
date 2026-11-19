"""
Cache-Candidacy Analysis Agent, v2 (LLM-interpreted).

v1 remains untouched at `cache_candidacy_agent.py`. v2 is additive.

WHAT CHANGED FROM v1
--------------------
v1 mapped a confidence score to a verdict with fixed bands:

    confidence >= 70  ->  STRONG_CANDIDATE
    confidence >= 50  ->  MODERATE_CANDIDATE
    ...

Those bands are the least defensible numbers in the project. They were chosen so
two test fixtures landed correctly, which is not evidence. They also cannot
express judgement the arithmetic does not capture: that a table's traffic is
inherited from joins, that an append-only table is safe to cache for point
lookups despite its write ratio, or that the right cache entry is a join result
rather than a table.

v2 keeps all the measurement and scoring, and replaces only the interpretation.
A Strands agent on Bedrock reads the scored data through read-only tools and
decides the verdicts.

WHAT DID NOT CHANGE
-------------------
Everything measured or calculated:
  - build_cache_profiles      (aggregation, frequency-weighted ratios)
  - build_workload_context    (buffer pool adequacy, host, locks, bgwriter)
  - score_cache_candidacy     (four dimensions, hard exclusions)

The model cannot alter a score. It receives them through tools that only read.

There is one verdict of record in a v2 artifact and it is the model's. The band
is retained on each table record for v1 type compatibility, but it is an input.
Where the two differ, the difference goes into the decision trace as calibration
data for whoever maintains the thresholds; it is not published.

FALLBACK
--------
If Bedrock is unreachable, credentials are missing, Strands is not installed, or
the model returns unusable output, v2 falls back to v1's deterministic bands and
records why. An assessment always completes.
"""

import json
import logging
import os
import time
from datetime import UTC, datetime

from botocore.config import Config as BotocoreConfig

from src.agents.analysis.v2_prompts import (
    EXTRACTION_PROMPT,
    EXTRACTION_RETRY_PROMPT,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_bundle_repair_prompt,
    build_task_prompt,
)
from src.contracts.cache_analysis_output import (
    AgentMetadata,
    CacheAssessmentMode,
    CacheTableRecommendation,
)
from src.contracts.cache_analysis_output_v2 import (
    CacheAnalysisOutputV2,
    InterpretationMode,
    LLMInterpretation,
    LLMQueryVerdict,
    LLMTableVerdict,
    LLMWorkloadAssessment,
    QueryCacheRecommendation,
)
from src.contracts.models import DEFAULT_MODEL_ID, forces_tool_choice, takes_temperature
from src.tools.analysis.cache_bundles import (
    bundle_conflicts,
    bundles_by_table,
    detect_bundles,
    merge_table_verdicts,
    qualifying_bundle_tables,
)
from src.tools.analysis.cache_efficiency import apply_estimated_k
from src.tools.analysis.caching_scoring import (
    DEFAULT_THRESHOLDS,
    CacheScoringThresholds,
    build_cache_profiles,
    build_workload_context,
    classify_verdict,
    fmt_mb,
    score_cache_candidacy,
)
from src.tools.analysis.column_widths import table_columns, tables_by_id
from src.tools.analysis.query_cache_analysis import (
    DEFAULT_TOP_N,
    build_query_profiles,
    score_query_candidacy,
    score_query_profiles,
    table_db_time_incl_joins,
    table_efficiency_rollup,
)
from src.tools.database.host_sampler import effective_host_view
from src.version import __version__

logger = logging.getLogger(__name__)

AGENT_NAME = "cache-candidacy-analysis-agent-v2"
AGENT_VERSION = __version__  # the release; the contract and prompt keep their own numbers

# DEFAULT_MODEL_ID: override with PGCA_BEDROCK_MODEL_ID. This must be a model enabled in
# the target account and region; an unavailable id surfaces as a fallback, not a crash.
DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Low but non-zero. Zero makes structured output brittle on some models; high
# values produce inconsistent verdicts across runs on identical data.
DEFAULT_TEMPERATURE = 0.2
# The extraction turn emits a verdict per candidate query plus one per table,
# each with reasoning. 8192 was enough for tables alone and truncated as soon as
# the query dimension was added, which surfaced as a max_tokens stop reason and
# a fall back to bands. Raised with headroom; the prompt also stops the model
# restating parser conclusions for NOT_CACHEABLE queries.
DEFAULT_MAX_TOKENS = 32768

# Bedrock client timeouts. The extraction turn writes a verdict per candidate
# query and table; on the sf14 5x run (48 weighed queries, 28 tables) it ran past
# the Strands default 120 s read timeout and the run fell back to bands. Read
# timeout overridable through the environment; standard retry mode retries
# transient connection and read timeouts.
DEFAULT_BEDROCK_READ_TIMEOUT_SEC = 600


def _read_timeout_from_env() -> int:
    """PGCA_BEDROCK_READ_TIMEOUT_SEC as a positive int, else the default."""
    raw = os.environ.get("PGCA_BEDROCK_READ_TIMEOUT_SEC")
    if not raw:
        return DEFAULT_BEDROCK_READ_TIMEOUT_SEC
    try:
        value = int(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    logger.warning("PGCA_BEDROCK_READ_TIMEOUT_SEC=%r is not a positive integer; using %d",
                   raw, DEFAULT_BEDROCK_READ_TIMEOUT_SEC)
    return DEFAULT_BEDROCK_READ_TIMEOUT_SEC


BEDROCK_READ_TIMEOUT_SEC = _read_timeout_from_env()
BEDROCK_CONNECT_TIMEOUT_SEC = 20
BEDROCK_MAX_ATTEMPTS = 3


def bedrock_client_config() -> BotocoreConfig:
    """Botocore client config for the Bedrock runtime client.

    Returns:
        Config with a long read timeout and standard-mode retries.
    """
    return BotocoreConfig(
        read_timeout=BEDROCK_READ_TIMEOUT_SEC,
        connect_timeout=BEDROCK_CONNECT_TIMEOUT_SEC,
        retries={"max_attempts": BEDROCK_MAX_ATTEMPTS, "mode": "standard"},
    )

VALID_TABLE_VERDICTS = {
    "STRONG_CANDIDATE", "MODERATE_CANDIDATE", "WEAK_CANDIDATE", "NOT_RECOMMENDED",
}
VALID_WORKLOAD_VERDICTS = {
    "RECOMMENDED", "PARTIALLY_RECOMMENDED", "NOT_RECOMMENDED", "INSUFFICIENT_DATA",
}


def analyze_cache_candidacy_v2(
    collector_output: dict,
    thresholds: CacheScoringThresholds = DEFAULT_THRESHOLDS,
    model_id: str | None = None,
    region: str | None = None,
    use_llm: bool = True,
    top_n: int = DEFAULT_TOP_N,
) -> tuple[CacheAnalysisOutputV2, dict, None]:
    """Score deterministically, then interpret with an LLM.

    Args:
        collector_output: Raw collector output.
        thresholds: Scoring thresholds, injectable for calibration.
        model_id: Bedrock model id. Defaults to DEFAULT_MODEL_ID.
        region: AWS region. Defaults to DEFAULT_REGION.
        use_llm: Set False to run deterministic-only, which reproduces v1
            verdicts exactly. Useful for comparison and for offline runs.
        top_n: How many queries to analyse, ranked by database time.

    Returns:
        (CacheAnalysisOutputV2, decision_trace, None). The third element is
        reserved for a diagram, matching the v1 signature.
    """
    started = time.monotonic()

    # ---------- deterministic layer, identical to v1 ----------
    mode = _determine_mode(collector_output)
    degraded_reason = _degraded_reason(collector_output)
    if mode is CacheAssessmentMode.DEGRADED:
        logger.warning("DEGRADED mode: %s", degraded_reason or "query stats unavailable")

    context = build_workload_context(collector_output, thresholds)
    profiles = build_cache_profiles(collector_output, thresholds)

    # ---------- per-query layer, deterministic ----------
    # Queries are scored first: table Cache Efficiency is rolled up from the
    # read queries that read each table. Ranked by database time, so the top-N
    # cap keeps the highest-value candidates. Cache type comes from the parser.
    query_profiles, query_summary = build_query_profiles(collector_output, top_n, thresholds)
    # Scored with the SAME four dimensions, thresholds, and bands as tables.
    # Table profiles supply churn and lock signals for the tables each query reads.
    score_query_profiles(query_profiles, context, thresholds, profiles)
    query_summary["deterministic_band_counts"] = _band_counts(query_profiles)
    query_recommendations = [_to_query_recommendation(p) for p in query_profiles]
    table_efficiency = table_efficiency_rollup(query_profiles, thresholds)

    breakdowns = {}
    deterministic_verdicts = {}
    recommendations: list[CacheTableRecommendation] = []

    for table_id, profile in profiles.items():
        b = score_cache_candidacy(profile, context, thresholds,
                                  efficiency=table_efficiency.get(str(table_id).lower()))
        breakdowns[table_id] = b
        deterministic_verdicts[table_id] = classify_verdict(b)
        recommendations.append(_to_recommendation(table_id, profile, b))

    recommendations.sort(key=lambda r: (r.hard_excluded, -r.confidence_score))

    # Table DB time, including joined queries, so the report can show the same
    # metric set for tables as for queries. Overlapping by construction.
    attributed = table_db_time_incl_joins(query_profiles)
    for rec in recommendations:
        rec.db_time_ms_per_sec_incl_joins = attributed.get(rec.table_id, 0.0)

    # Bundles: Not cacheable queries the application could compute from cached
    # rows. Facts for the model and the report; no score effect (spec
    # cache-bundles).
    bundles = detect_bundles(query_profiles, profiles, breakdowns, thresholds)
    membership = bundles_by_table(bundles)
    for rec in recommendations:
        rec.bundles = membership.get(rec.table_id, [])

    # ---------- LLM interpretation layer ----------
    interpretation: LLMInterpretation | None = None
    fallback_reason: str | None = None

    interpretation_mode = InterpretationMode.DETERMINISTIC_FALLBACK

    if not use_llm:
        fallback_reason = "LLM interpretation disabled by caller (use_llm=False)"
    elif not profiles:
        fallback_reason = "No tables to interpret"
    else:
        interpretation, fallback_reason = _interpret_with_llm(
            collector_output=collector_output,
            profiles=profiles,
            breakdowns=breakdowns,
            deterministic_verdicts=deterministic_verdicts,
            context=context,
            thresholds=thresholds,
            mode=mode,
            degraded_reason=degraded_reason,
            model_id=model_id or DEFAULT_MODEL_ID,
            region=region or DEFAULT_REGION,
            query_profiles=query_profiles,
            query_summary=query_summary,
            bundles=bundles,
        )
        if interpretation is not None:
            interpretation_mode = InterpretationMode.LLM
            # Deferred reuse: score it from the model's estimate of distinct
            # requests sent, as 1 - D/N (spec R5.5). Queries only; the
            # table scores the model reasoned over stay as they were (R5.6).
            rescored = _apply_agent_k_estimates(
                interpretation, query_profiles, context, thresholds, profiles)
            if rescored:
                query_recommendations = [_to_query_recommendation(p) for p in query_profiles]
                logger.info("Scored deferred reuse from the model's estimate for %d "
                            "quer%s", rescored, "y" if rescored == 1 else "ies")

    # Threshold comparison is a calibration concern for whoever maintains the
    # thresholds. It belongs in the developer-facing trace, not in an artifact a
    # customer reads, so it is computed here and not stored on the contract.
    band_differences: list[dict] = []
    if interpretation:
        for t in interpretation.tables:
            band = deterministic_verdicts.get(t.table_id)
            if band and band != t.verdict:
                band_differences.append({
                    "table_id": t.table_id,
                    "threshold_band": band,
                    "llm_verdict": t.verdict,
                    "confidence_score": breakdowns[t.table_id].confidence,
                })
        qband = {p.queryid: p for p in query_profiles}
        for q in interpretation.queries:
            p = qband.get(q.queryid)
            if p and p.deterministic_verdict and p.deterministic_verdict != q.verdict:
                band_differences.append({
                    "queryid": q.queryid,
                    "threshold_band": p.deterministic_verdict,
                    "llm_verdict": q.verdict,
                    "confidence_score": p.breakdown.confidence if p.breakdown else None,
                })

    duration = time.monotonic() - started

    output = CacheAnalysisOutputV2(
        # Taken from the contract default so the two cannot drift apart.
        contract_version=CacheAnalysisOutputV2.model_fields["contract_version"].default,
        agent_metadata=AgentMetadata(
            agent_name=AGENT_NAME,
            agent_version=AGENT_VERSION,
            analysis_timestamp=datetime.now(UTC),
            analysis_duration_seconds=round(duration, 2),
        ),
        assessment_mode=mode,
        degraded_reason=degraded_reason,
        query_recommendations=query_recommendations,
        query_analysis_summary=query_summary,
        table_recommendations=recommendations,
        workload_context=_serialize_context(context),
        thresholds_used={f: getattr(thresholds, f) for f in thresholds.__dataclass_fields__},
        bundles=bundles,
        interpretation_mode=interpretation_mode,
        llm_interpretation=interpretation,
        fallback_reason=fallback_reason,
    )

    trace = {
        "agent": AGENT_NAME,
        "assessment_mode": mode.value,
        "interpretation_mode": interpretation_mode.value,
        "fallback_reason": fallback_reason,
        "tables_analyzed": len(profiles),
        "hard_excluded": sum(1 for r in recommendations if r.hard_excluded),
        "deterministic_strong": sum(
            1 for v in deterministic_verdicts.values() if v == "STRONG_CANDIDATE"
        ),
        "llm_tool_calls": interpretation.tool_calls_made if interpretation else 0,
        "llm_tools_used": interpretation.tools_used if interpretation else [],
        "model_id": interpretation.model_id if interpretation else None,
        "workload_verdict": (
            interpretation.workload.overall_recommendation if interpretation else None
        ),
        "candidates": len(output.candidate_table_ids()),
        # Per-query dimension.
        "queries_analysed": len(query_recommendations),
        "queries_dropped_by_cap": query_summary.get("dropped_by_cap", 0),
        "db_time_covered_pct": query_summary.get("db_time_covered_pct"),
        "query_cache_type_counts": query_summary.get("cache_type_counts", {}),
        "queries_judged": len(interpretation.queries) if interpretation else 0,
        "cached_query_ids": output.cached_query_ids(),
        "recommended_by_cache_type": output.recommended_by_cache_type(),
        "cacheable_db_time_ms_per_sec": output.total_cacheable_db_time_ms_per_sec(),
        # Calibration data: where the model's judgement parted from the bands.
        # Developer-facing only.
        "threshold_band_differences": band_differences,
    }
    return output, trace, None


def _to_query_recommendation(p) -> QueryCacheRecommendation:
    """Convert a QueryCacheProfile into its contract record.

    Args:
        p: A QueryCacheProfile from build_query_profiles.

    Returns:
        The contract record. Its `verdict` is the four-dimension band, an input
        to the model exactly as the table band is.
    """
    b = p.breakdown
    return QueryCacheRecommendation(
        queryid=p.queryid,
        query_text=p.query_text,
        query_type=p.query_type,
        cache_type=p.shape.cache_type,
        access_shape=p.shape.access_shape,
        calls_per_second=p.calls_per_second,
        avg_exec_ms=p.avg_exec_ms,
        total_time_ms=p.total_time_ms,
        rows_returned_avg=p.rows_returned_avg,
        buffer_hit_ratio_pct=p.buffer_hit_ratio_pct,
        io_read_time_share=p.io_read_time_share,
        temp_blocks_written=p.temp_blocks_written,
        db_time_ms_per_sec=p.db_time_ms_per_sec,
        db_time_share_pct=p.db_time_share_pct,
        invalidating_write_rate=p.invalidating_write_rate,
        invalidation_ratio=p.invalidation_ratio,
        tables_read=p.shape.tables_read,
        tables_written=p.shape.tables_written,
        key_columns=p.shape.key_columns,
        key_is_primary_key=p.shape.key_is_primary_key,
        caches_collection=p.shape.caches_collection,
        non_deterministic=p.shape.non_deterministic,
        clock_uses=p.shape.clock_uses,
        unresolved_relations=p.shape.unresolved_relations,
        views_resolved=p.shape.views_resolved,
        is_system_query=p.shape.is_system_query,
        parse_ok=p.shape.parse_ok,
        write_sources=p.write_sources,
        classification_reasons=p.shape.reasons,
        notes=p.notes,
        verdict=p.deterministic_verdict or "NOT_RECOMMENDED",
        confidence_score=b.confidence if b else 0,
        access_suitability_score=b.access_suitability if b else 0,
        pressure_evidence_score=b.pressure_evidence if b else 0,
        cache_efficiency_score=b.cache_efficiency if b else None,
        efficiency=p.efficiency.as_dict() if p.efficiency is not None else None,
        disqualifier_penalty=b.disqualifier_penalty if b else 0,
        **_disqualifier_fields(b),
        hard_excluded=bool(b and b.hard_excluded),
        exclusion_reason=b.exclusion_reason if b else None,
        exclusion_reasons=list(b.exclusion_reasons) if b else [],
        evidence=list(b.evidence) if b else [],
        concerns=list(b.concerns) if b else [],
        read_dominance=p.read_dominance,
    )


def _apply_agent_k_estimates(interpretation, query_profiles, context, thresholds,
                             table_profiles) -> int:
    """Rescore deferred-reuse queries from the model's estimated distinct requests.

    Args:
        interpretation: Validated LLMInterpretation.
        query_profiles: Query profiles, updated in place.
        context: WorkloadContext.
        thresholds: CacheScoringThresholds.
        table_profiles: Table profiles, for churn and lock signals.

    Returns:
        How many queries were rescored.
    """
    by_id = {p.queryid: p for p in query_profiles or []}
    n = 0
    for v in interpretation.queries or []:
        p = by_id.get(v.queryid)
        if (p is None or p.efficiency is None or not p.efficiency.reuse_deferred_reason
                or v.estimated_distinct_requests is None):
            continue
        try:
            p.efficiency = apply_estimated_k(p.efficiency, v.estimated_distinct_requests,
                                             thresholds)
            score_query_candidacy(p, context, thresholds, table_profiles)
            n += 1
        except Exception as exc:  # noqa: BLE001 - keep the deterministic record
            logger.warning("agent K estimate for %s not applied: %s", v.queryid, exc)
    return n


def _disqualifier_fields(b) -> dict:
    """Disqualifier v2 fields for a contract record, from a score breakdown.

    Args:
        b: CacheScoreBreakdown, or None.

    Returns:
        exclusion_kind, penalties, busy_but_fast, db_share_of_capacity,
        well_served.
    """
    if b is None:
        return {}
    return {
        "exclusion_kind": b.exclusion_kind,
        "penalties": list(b.penalties),
        "busy_but_fast": b.busy_but_fast,
        "db_share_of_capacity": b.db_share_of_capacity,
        "well_served": b.well_served,
    }


def _band_counts(profiles) -> dict[str, int]:
    """How many queries landed in each deterministic band."""
    out: dict[str, int] = {}
    for p in profiles:
        v = p.deterministic_verdict or "NOT_RECOMMENDED"
        out[v] = out.get(v, 0) + 1
    return out


# ---------------------------------------------------------------------------
# LLM layer
# ---------------------------------------------------------------------------


def _interpret_with_llm(
    collector_output: dict,
    profiles: dict,
    breakdowns: dict,
    deterministic_verdicts: dict,
    context,
    thresholds: CacheScoringThresholds,
    mode: CacheAssessmentMode,
    degraded_reason: str | None,
    model_id: str,
    region: str,
    query_profiles: list | None = None,
    query_summary: dict | None = None,
    bundles: list[dict] | None = None,
) -> tuple[LLMInterpretation | None, str | None]:
    """Run the Strands agent.

    Args:
        bundles: From detect_bundles(); exposed through the tools, and tables in
            a qualifying bundle need bundle_reasoning.

    Returns:
        (interpretation, None) on success, or (None, reason) on any failure.
        Never raises: a failed interpretation degrades to deterministic bands
        rather than losing the assessment.
    """
    started = time.monotonic()

    try:
        from strands import Agent
        from strands.models import BedrockModel
    except ImportError as e:
        return None, f"strands-agents not installed: {e}"

    from src.agents.analysis.v2_tools import build_tools

    try:
        tools = build_tools(
            profiles=profiles,
            breakdowns=breakdowns,
            deterministic_verdicts=deterministic_verdicts,
            context=context,
            thresholds=thresholds,
            collector_output=collector_output,
            query_profiles=query_profiles,
            query_summary=query_summary,
            bundles=bundles,
        )
    except Exception as e:
        logger.exception("Failed to build tools")
        return None, f"tool construction failed: {e}"

    try:
        model_cls = BedrockModel if forces_tool_choice(model_id) else _auto_choice_model(BedrockModel)
        bedrock = model_cls(
            region_name=region,
            model_id=model_id,
            **({"temperature": DEFAULT_TEMPERATURE} if takes_temperature(model_id) else {}),
            max_tokens=DEFAULT_MAX_TOKENS,
            boto_client_config=bedrock_client_config(),
        )
        agent = Agent(model=bedrock, tools=tools, system_prompt=SYSTEM_PROMPT)
    except Exception as e:
        logger.warning("Could not construct Bedrock agent: %s", e)
        return None, f"bedrock agent construction failed: {e}"

    prompt = build_task_prompt(
        collector_output, mode.value, degraded_reason, query_summary
    )

    # Two phases, and the split is necessary rather than stylistic.
    #
    # structured_output() forces a single tool call for the output schema. Given
    # a prompt directly, it competes with the data tools: the model reaches for
    # a data tool, no schema tool_use appears, and Strands raises
    # "No valid tool use or tool use input was found in the Bedrock response".
    #
    # So phase 1 runs the normal agentic loop, letting the model call as many
    # data tools as it needs. Phase 2 calls structured_output with NO prompt,
    # which extracts the typed result from the conversation just produced.
    try:
        agent(prompt)
    except Exception as e:
        logger.warning("LLM exploration phase failed: %s", e)
        return None, f"model exploration failed: {type(e).__name__}: {e}"

    tools_used = _extract_tools_used(agent)
    if not tools_used:
        # Not fatal, but it means the verdicts are not grounded in the data.
        # Recorded so the report can warn rather than presenting it as analysis.
        logger.warning(
            "Model made no tool calls; its judgement is not grounded in the data"
        )

    # A short user turn is required here, not optional. After phase 1 the
    # conversation ends with an assistant message, and calling structured_output
    # with no prompt attempts an assistant-message prefill, which Bedrock
    # rejects with "This model does not support assistant message prefill. The
    # conversation must end with a user message."
    #
    # The tool results from phase 1 are still in history, so the model has the
    # data. This turn only asks it to commit its conclusions to the schema.
    # The model occasionally answers the extraction turn in prose, or reaches
    # for a data tool, instead of calling the output tool; Strands then raises
    # "No valid tool use ... was found". The exploration is already in history,
    # so one retry of this cheap turn with a stricter instruction recovers it
    # without repeating the analysis (seen on ec2-retail-002).
    result = None
    last_error: Exception | None = None
    for attempt, extraction in enumerate((EXTRACTION_PROMPT, EXTRACTION_RETRY_PROMPT), 1):
        try:
            result = agent.structured_output(_StructuredResult, extraction)
            break
        except Exception as e:
            last_error = e
            logger.warning("LLM structured extraction attempt %d failed: %s", attempt, e)
    if result is None:
        return None, (f"structured extraction failed: "
                      f"{type(last_error).__name__}: {last_error}")

    known_query_ids = {p.queryid for p in (query_profiles or [])}
    deferred_query_ids = {
        p.queryid for p in (query_profiles or [])
        if p.efficiency is not None and p.efficiency.reuse_deferred_reason
    }
    try:
        interpretation = _validate_and_build(
            result, deterministic_verdicts, model_id,
            round(time.monotonic() - started, 2), tools_used,
            known_query_ids=known_query_ids,
            deferred_query_ids=deferred_query_ids,
            busy_ids=(
                {p.queryid for p in (query_profiles or [])
                 if p.breakdown is not None and p.breakdown.busy_but_fast}
                | {tid for tid, b in breakdowns.items() if b.busy_but_fast}
            ),
            bundle_ids=qualifying_bundle_tables(bundles or []),
            table_column_sets={
                tid: set(table_columns(t))
                for tid, t in tables_by_id(collector_output).items()
            },
            object_query_tables={
                p.queryid: str(p.shape.tables_read[0]).lower()
                for p in (query_profiles or [])
                if p.shape.cache_type == "OBJECT" and p.shape.tables_read
            },
        )
    except ValueError as e:
        return None, f"model output rejected: {e}"

    # Bundle verdicts must agree within a band; one repair turn, then a warning
    # (spec CB-8). Never fails the interpretation.
    interpretation = _repair_bundle_consistency(agent, interpretation, bundles or [], thresholds)
    interpretation.interpretation_duration_seconds = round(time.monotonic() - started, 2)
    return interpretation, None


def _repair_bundle_consistency(agent, interpretation: LLMInterpretation,
                               bundles: list[dict],
                               thresholds: CacheScoringThresholds) -> LLMInterpretation:
    """Give the model one turn to fix bundle verdicts that are too far apart.

    Args:
        agent: The Strands agent from the main interpretation, so the repair
            turn sees the same conversation and data.
        interpretation: Validated interpretation.
        bundles: From detect_bundles().
        thresholds: For bundle_max_band_spread.

    Returns:
        The interpretation with repaired table verdicts merged in and
        bundle_consistency recorded. Unchanged apart from the record when there
        is no conflict or the repair turn fails.
    """
    spread = thresholds.bundle_max_band_spread
    before = bundle_conflicts(bundles, {t.table_id: t.verdict for t in interpretation.tables},
                              spread)
    if not before:
        return interpretation
    allowed = {t for c in before for t in c["verdicts"]}
    replaced: list[str] = []
    try:
        repair = agent.structured_output(_BundleRepairResult,
                                         build_bundle_repair_prompt(before, spread))
        interpretation.tables, replaced = merge_table_verdicts(
            interpretation.tables, repair.tables, allowed)
    except Exception as e:
        # A failed repair must not lose the assessment; the warning records it.
        logger.warning("Bundle consistency repair turn failed: %s: %s", type(e).__name__, e)
    now = {t.table_id: t.verdict for t in interpretation.tables}
    after = bundle_conflicts(bundles, now, spread)
    still = {c["queryid"] for c in after}
    interpretation.bundle_consistency = [{
        "queryid": c["queryid"],
        "db_time_share_pct": c["db_time_share_pct"],
        "tables": c["tables"],
        "before": c["verdicts"],
        "after": {t: now.get(t) for t in c["verdicts"]},
        "revised": [t for t in replaced if t in c["verdicts"]],
        "resolved": c["queryid"] not in still,
    } for c in before]
    logger.info("Bundle consistency: %d conflict(s), %d resolved by the repair turn",
                len(before), len(before) - len(still))
    return interpretation


# The structured output schema the model must fill. Deliberately mirrors the
# contract models so there is no lossy translation step.
from pydantic import BaseModel, Field  # noqa: E402


class _BundleRepairResult(BaseModel):
    """Schema for the bundle consistency repair turn: revised tables only."""

    tables: list[LLMTableVerdict] = Field(default_factory=list)


class _StructuredResult(BaseModel):
    """Schema the model must return."""

    workload: LLMWorkloadAssessment
    queries: list[LLMQueryVerdict] = Field(default_factory=list)
    tables: list[LLMTableVerdict] = Field(default_factory=list)


def _validate_and_build(
    result: "_StructuredResult",
    deterministic_verdicts: dict,
    model_id: str,
    duration: float,
    tools_used: list[str],
    known_query_ids: set[str] | None = None,
    deferred_query_ids: set[str] | None = None,
    busy_ids: set[str] | None = None,
    bundle_ids: set[str] | None = None,
    table_column_sets: dict[str, set[str]] | None = None,
    object_query_tables: dict[str, str] | None = None,
) -> LLMInterpretation:
    """Validate model output and assemble the interpretation.

    Rejects output that would corrupt the artifact. Verdict strings are checked
    against the allowed set, and hallucinated table ids are dropped rather than
    passed through, because a verdict for a table that does not exist would
    silently appear in the report.

    Raises:
        ValueError: when output is unusable.
    """
    if result.workload.overall_recommendation not in VALID_WORKLOAD_VERDICTS:
        raise ValueError(
            f"invalid workload verdict {result.workload.overall_recommendation!r}"
        )

    known = set(deterministic_verdicts)
    kept: list[LLMTableVerdict] = []
    dropped: list[str] = []

    for t in result.tables:
        if t.table_id not in known:
            dropped.append(t.table_id)
            continue
        if t.verdict not in VALID_TABLE_VERDICTS:
            dropped.append(f"{t.table_id} (bad verdict {t.verdict!r})")
            continue
        # A verdict with no reasoning is not a judgement, it is an assertion.
        # Reject rather than publish an unexplained recommendation.
        if not (t.reasoning or "").strip():
            dropped.append(f"{t.table_id} (empty reasoning)")
            continue
        # Busy but fast: the case turns on the application, so the model must
        # say how (spec DQ-3.2).
        if t.table_id in (busy_ids or set()) and not (t.busy_context_reasoning or "").strip():
            dropped.append(f"{t.table_id} (busy but fast without busy_context_reasoning)")
            continue
        # A table in a qualifying bundle: the model must weigh the bundle
        # rather than judge the table on its own lookups alone (spec CB-4).
        if t.table_id in (bundle_ids or set()) and not (t.bundle_reasoning or "").strip():
            dropped.append(f"{t.table_id} (in a bundle without bundle_reasoning)")
            continue
        t.cached_columns = clean_cached_columns(
            t.cached_columns, (table_column_sets or {}).get(str(t.table_id).lower()),
            t.table_id)
        kept.append(t)

    if dropped:
        logger.warning("Dropped %d invalid table verdict(s): %s", len(dropped), dropped[:5])
    # Per-query verdicts, validated on the same terms: a real id, a valid
    # verdict string, and non-empty reasoning.
    known_query_ids = known_query_ids or set()
    kept_queries: list[LLMQueryVerdict] = []
    dropped_queries: list[str] = []
    for q in result.queries:
        if known_query_ids and q.queryid not in known_query_ids:
            dropped_queries.append(f"{q.queryid} (unknown id)")
            continue
        if q.verdict not in VALID_TABLE_VERDICTS:
            dropped_queries.append(f"{q.queryid} (bad verdict {q.verdict!r})")
            continue
        if not (q.reasoning or "").strip():
            dropped_queries.append(f"{q.queryid} (empty reasoning)")
            continue
        # Reuse for range, IN, LIKE, OFFSET and other parameter uses has no K
        # in the statistics; the model must estimate it and say how (R5.3, R5.4).
        if q.queryid in (busy_ids or set()) and not (q.busy_context_reasoning or "").strip():
            dropped_queries.append(f"{q.queryid} (busy but fast without busy_context_reasoning)")
            continue
        if q.queryid in (deferred_query_ids or set()):
            if not (q.reuse_reasoning or "").strip():
                dropped_queries.append(f"{q.queryid} (deferred reuse without reuse_reasoning)")
                continue
            if q.estimated_distinct_requests is None or q.estimated_distinct_requests < 1:
                dropped_queries.append(
                    f"{q.queryid} (deferred reuse without a usable "
                    "estimated_distinct_requests)")
                continue
        q_table = (object_query_tables or {}).get(q.queryid)
        q.cached_columns = (
            clean_cached_columns(q.cached_columns,
                                 (table_column_sets or {}).get(q_table or ""), q.queryid)
            if q_table else None
        )
        kept_queries.append(q)

    if dropped_queries:
        logger.warning(
            "Dropped %d invalid query verdict(s): %s",
            len(dropped_queries), dropped_queries[:5],
        )

    if not kept and not kept_queries:
        raise ValueError("no valid table or query verdicts returned")
    if not kept:
        logger.warning(
            "No valid table verdicts; deterministic bands remain authoritative "
            "for the object-caching dimension"
        )

    missing = known - {t.table_id for t in kept}
    if missing:
        logger.warning(
            "Model omitted %d table(s); deterministic verdicts remain authoritative "
            "for them: %s", len(missing), sorted(missing)[:5]
        )

    return LLMInterpretation(
        workload=result.workload,
        queries=kept_queries,
        tables=kept,
        model_id=model_id,
        prompt_version=PROMPT_VERSION,
        tool_calls_made=len(tools_used),
        tools_used=sorted(set(tools_used)),
        interpretation_duration_seconds=duration,
    )


def clean_cached_columns(columns: list[str] | None, known: set[str] | None,
                         owner: str) -> list[str] | None:
    """Keep only real, distinct column names from a model's cached_columns.

    A misnamed column is dropped with a warning rather than rejecting the
    verdict, because the verdict itself can still be sound; sizing then falls
    back to the columns the query reads.

    Args:
        columns: The model's list, possibly None.
        known: Lowercased column names of the table, or None when unknown.
        owner: Query id or table id, for the log.

    Returns:
        Cleaned list in the model's order, or None when nothing usable remains.
    """
    if not columns:
        return None
    out: list[str] = []
    bad: list[str] = []
    for c in columns:
        name = str(c or "").strip().strip('"').lower()
        if "." in name:
            name = name.rsplit(".", 1)[-1]
        if not name or name in out:
            continue
        if known is not None and name not in known:
            bad.append(name)
            continue
        out.append(name)
    if bad:
        logger.warning("%s: dropped cached_columns not in the table: %s", owner, bad)
    return out or None


def _auto_choice_model(base: type) -> type:
    """A Strands Bedrock model that never forces a tool choice.

    Strands' structured_output forces the output tool with toolChoice "any", which some
    models reject. With "auto" the model is asked, not forced; the extraction prompt tells
    it to call the output tool and a stricter retry follows, and Strands still raises when
    no output tool call comes back, which the caller treats as a fallback.
    """

    class AutoChoiceBedrockModel(base):  # type: ignore[misc, valid-type]
        def stream(self, messages, tool_specs=None, system_prompt=None, *, tool_choice=None, **kwargs):
            if tool_choice and ("any" in tool_choice or "tool" in tool_choice):
                tool_choice = {"auto": {}}
            return super().stream(messages, tool_specs, system_prompt, tool_choice=tool_choice, **kwargs)

        def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
            # These models sometimes send a nested object as a JSON string.
            return super().structured_output(_lenient(output_model), prompt,
                                             system_prompt=system_prompt, **kwargs)

    return AutoChoiceBedrockModel


def _decode_json_strings(value):
    """Turn strings that hold a JSON object or list back into them, at any depth."""
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return _decode_json_strings(json.loads(value))
        except ValueError:
            return value
    if isinstance(value, dict):
        return {k: _decode_json_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_json_strings(v) for v in value]
    return value


def _lenient(output_model: type) -> type:
    """The output model, accepting nested objects sent as JSON text."""
    from pydantic import model_validator

    class Lenient(output_model):  # type: ignore[misc, valid-type]
        @model_validator(mode="before")
        @classmethod
        def _decode(cls, data):
            return _decode_json_strings(data)

    Lenient.__name__ = output_model.__name__
    Lenient.__doc__ = output_model.__doc__
    return Lenient


def _extract_tools_used(agent) -> list[str]:
    """Best-effort list of tools the model invoked.

    Recorded because zero tool calls means the model answered without examining
    the data, which materially weakens its judgement and should be visible.
    Strands does not guarantee a stable accessor for this, so failure to read it
    is not treated as an error.
    """
    used: list[str] = []
    try:
        for msg in getattr(agent, "messages", []) or []:
            for block in msg.get("content", []) or []:
                if isinstance(block, dict) and "toolUse" in block:
                    name = (block["toolUse"] or {}).get("name")
                    if name:
                        used.append(name)
    except Exception:
        logger.debug("Could not extract tool usage from agent messages")
    return used


# ---------------------------------------------------------------------------
# Shared helpers, same semantics as v1
# ---------------------------------------------------------------------------


def _determine_mode(collector_output: dict) -> CacheAssessmentMode:
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    return CacheAssessmentMode.FULL if queries else CacheAssessmentMode.DEGRADED


def _degraded_reason(collector_output: dict) -> str | None:
    probe = collector_output.get("pg_stat_statements_probe") or {}
    if probe and not probe.get("available", True):
        return str(probe.get("reason") or "pg_stat_statements unavailable")
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    if not queries:
        return "No query patterns collected; pg_stat_statements likely unavailable"
    return None


def _to_recommendation(table_id: str, profile, breakdown) -> CacheTableRecommendation:
    """Build the deterministic per-table record, unchanged from v1."""
    return CacheTableRecommendation(
        table_id=table_id,
        verdict=classify_verdict(breakdown),
        confidence_score=breakdown.confidence,
        access_suitability_score=breakdown.access_suitability,
        pressure_evidence_score=breakdown.pressure_evidence,
        cache_efficiency_score=breakdown.cache_efficiency,
        efficiency=(breakdown.efficiency.as_dict()
                    if breakdown.efficiency is not None else None),
        disqualifier_penalty=breakdown.disqualifier_penalty,
        **_disqualifier_fields(breakdown),
        hard_excluded=breakdown.hard_excluded,
        exclusion_reason=breakdown.exclusion_reason,
        exclusion_reasons=list(breakdown.exclusion_reasons),
        evidence=breakdown.evidence,
        concerns=breakdown.concerns,
        rationale=_deterministic_rationale(profile, breakdown),
        read_ratio_weighted=profile.read_ratio_weighted,
        calls_per_second=profile.total_calls_per_second,
        buffer_hit_ratio_pct=profile.buffer_hit_ratio_pct,
        weighted_exec_time_ms=profile.weighted_exec_time_ms,
        size_mb=profile.size_mb,
        notes=profile.notes,
        read_calls_per_second=profile.read_calls_per_second,
        write_calls_per_second=profile.write_calls_per_second,
    )


def _deterministic_rationale(profile, breakdown) -> str:
    """Template rationale. Retained so the artifact is readable under fallback."""
    if breakdown.hard_excluded:
        return f"Not recommended for caching. {breakdown.exclusion_reason}"
    parts = [
        f"Read share {profile.read_ratio_weighted:.0%} at "
        f"{profile.total_calls_per_second:.2f} calls/sec"
    ]
    if profile.buffer_hit_ratio_pct is not None:
        parts.append(f"buffer hit ratio {profile.buffer_hit_ratio_pct:.1f}%")
    if profile.weighted_exec_time_ms:
        parts.append(f"{profile.weighted_exec_time_ms:.1f}ms average execution")
    if profile.size_mb is not None:
        parts.append(f"{fmt_mb(profile.size_mb)} MB dataset")
    verdict = classify_verdict(breakdown)
    return (
        f"{verdict.replace('_', ' ').title()} (confidence {breakdown.confidence}). "
        f"{'; '.join(parts)}."
    )


def _serialize_context(context) -> dict:
    """Workload context for the artifact, using the effective host view.

    host_cpu_pct is the window p95 when sampled, None when the snapshot
    contradicts the load average, and the snapshot otherwise. The source and
    note travel with it so a reader knows which.
    """
    host = context.host_metrics or {}
    view = effective_host_view(host) if host else {}
    return {
        "buffer_pool": context.buffer_pool,
        "lock_wait_share": context.lock_wait_share,
        "host_cpu_pct": view.get("cpu_pct"),
        "host_cpu_mean_pct": view.get("cpu_mean_pct"),
        "host_cpu_max_pct": view.get("cpu_max_pct"),
        "host_memory_used_pct": view.get("memory_used_pct"),
        "host_swap_used_pct": view.get("swap_used_pct"),
        "host_vcpu_count": view.get("vcpu_count"),
        "host_metrics_source": view.get("source"),
        "host_metrics_note": view.get("note"),
        "host_load_average": host.get("load_average"),
        "host_snapshot_cpu_pct": (host.get("cpu") or {}).get("utilization_pct"),
        "global_buffer_hit_ratio_pct": context.global_stats.get("cache_hit_ratio_pct"),
        "bgwriter_backend_pct": context.bgwriter.get("buffers_backend_pct"),
        # Which period the figures describe. Plain values only, for the report.
        "statistics_window": {
            k: context.stats_window.get(k) for k in (
                "mode", "seconds", "requested_minutes", "started_at", "ended_at", "notes")
        } | {"calls": (context.stats_window.get("statements") or {}).get("calls"),
             "since_reset_rates": len((context.stats_window.get("statements") or {})
                                      .get("since_reset_rates") or [])},
    }
