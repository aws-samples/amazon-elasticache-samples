"""
Result retrieval routes.

One endpoint per pipeline artifact, so a UI can fetch the summary for a tile, the
report for the full page, and the analysis or raw collection when a reviewer
wants to audit a score. Every JSON route declares its response model, so the
OpenAPI document describes it (spec: .kiro/specs/ux-api, UX-1).
"""

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query
from fastapi.responses import HTMLResponse

from src.api.common import (
    PAGE_DEFAULT_LIMIT,
    PAGE_MAX_LIMIT,
    JobId,
    job_store,
    orchestrator,
)
from src.api.errors import ApiError, error_responses
from src.contracts.api_models import AssessmentSummary, SizingSummary
from src.contracts.cache_analysis_output_v2 import CacheAnalysisOutputV2
from src.contracts.cache_sizing_output import CacheSizingOutput
from src.contracts.report_view_model import (
    QueryItem,
    QueryPage,
    ReportOverview,
    ReportViewModel,
    TableItem,
    TablePage,
)
from src.orchestrator.local_orchestrator import Phase
from src.tools.report.view_model import build_report_view_model, page_queries, page_tables

Verdict = Literal["STRONG_CANDIDATE", "MODERATE_CANDIDATE", "WEAK_CANDIDATE", "NOT_RECOMMENDED"]
CacheType = Literal["QUERY", "OBJECT", "NOT_CACHEABLE", "UNKNOWN"]
# Ids are only ever compared with ids inside the loaded artifact, never used as paths.
QueryIdParam = Annotated[str, Path(max_length=128)]
TableIdParam = Annotated[str, Path(max_length=256)]

logger = logging.getLogger(__name__)
router = APIRouter(responses=error_responses(404))


def _missing(job_id: str, phase: Phase) -> ApiError:
    return ApiError(
        404, "artifact_not_available",
        f"Artifact not available for job {job_id} (phase={phase.value})")


def _load(job_id: str, phase: Phase, filename: str) -> dict:
    try:
        return orchestrator().load(job_id, phase, filename)
    except (FileNotFoundError, ValueError) as e:
        raise _missing(job_id, phase) from e


def _load_analysis(job_id: str) -> dict:
    """The analysis with its sizing block attached, when sizing ran."""
    try:
        return orchestrator().load_analysis(job_id)
    except (FileNotFoundError, ValueError) as e:
        raise _missing(job_id, Phase.ANALYZE) from e


@router.get("/assessments/{job_id}/caching-assessment", response_model=CacheAnalysisOutputV2)
def get_caching_assessment(job_id: JobId) -> dict:
    """The primary payload: the analysis, with the verdicts of record and sizing."""
    return _load_analysis(job_id)


@router.get("/assessments/{job_id}/analysis", response_model=CacheAnalysisOutputV2)
def get_analysis(job_id: JobId) -> dict:
    """Per-query and per-table scores, evidence, concerns, bundles, the agent's reasoning and sizing."""
    return _load_analysis(job_id)


@router.get("/assessments/{job_id}/sizing", response_model=CacheSizingOutput)
def get_sizing(job_id: JobId) -> dict:
    """The Sizing & Cost block: memory, node, On-Demand cost and per-item sizing.

    404 when sizing did not run for the job. Not a capacity plan and not a quote;
    the block carries those caveats itself.
    """
    return _load(job_id, Phase.SIZE, "sizing.json")


@router.get("/assessments/{job_id}/collection", response_model=dict[str, Any])
def get_collection(job_id: JobId) -> dict:
    """Raw collector output.

    Includes query text, which may contain literals. Like every route here it has
    no authentication; see the module docstring of src/api/main.py. Not typed beyond
    an object, because collections are versioned by the collector and older ones need
    not match the current contract.
    """
    return _load(job_id, Phase.COLLECT, "output.json")


@router.get("/assessments/{job_id}/report", response_class=HTMLResponse)
def get_report(job_id: JobId) -> HTMLResponse:
    """The self-contained HTML report.

    Sent with a Content-Security-Policy that allows inline styles and nothing
    else, since the report has no scripts and makes no requests.
    """
    try:
        html = orchestrator().load_text(job_id, Phase.REPORT, "report.html")
    except (FileNotFoundError, ValueError) as e:
        raise _missing(job_id, Phase.REPORT) from e
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _view_model(job_id: str) -> ReportViewModel:
    """The report as data for a job. 404 when the job has no analysis or collection."""
    analysis = _load_analysis(job_id)
    collection = _load(job_id, Phase.COLLECT, "output.json")
    record = job_store().get(job_id)
    try:
        return build_report_view_model(collection, analysis, job_id=job_id,
                                       label=record.label if record else None)
    except ValueError as e:
        raise ApiError(500, "stored_data_invalid", "stored analysis is not valid") from e


@router.get("/assessments/{job_id}/report-data", response_model=ReportViewModel)
def get_report_data(job_id: JobId) -> ReportViewModel:
    """The whole report as data: everything the HTML report shows, as plain text and
    numbers, in the same order. Large; prefer the overview and the paged lists."""
    return _view_model(job_id)


@router.get("/assessments/{job_id}/report-data/overview", response_model=ReportOverview)
def get_report_overview(job_id: JobId) -> ReportOverview:
    """The report without its query and table lists: headline, at a glance, glossary,
    scoring, workload, bundles, not-cacheable groups, sizing and provenance."""
    return ReportOverview.model_validate(dict(_view_model(job_id)))


@router.get("/assessments/{job_id}/report-data/queries", response_model=QueryPage)
def list_report_queries(
    job_id: JobId,
    verdict: Verdict | None = None,
    cache_type: CacheType | None = None,
    recommended: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX_LIMIT)] = PAGE_DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> QueryPage:
    """Analysed queries in the report's order (verdict, then database time)."""
    return page_queries(_view_model(job_id).queries, verdict=verdict, cache_type=cache_type,
                        recommended=recommended, limit=limit, offset=offset)


@router.get("/assessments/{job_id}/report-data/queries/{queryid}", response_model=QueryItem)
def get_report_query(job_id: JobId, queryid: QueryIdParam) -> QueryItem:
    """One query with its metrics, key factors and reasoning."""
    for q in _view_model(job_id).queries:
        if q.queryid == queryid:
            return q
    raise ApiError(404, "item_not_found", f"Query {queryid} not found in job {job_id}")


@router.get("/assessments/{job_id}/report-data/tables", response_model=TablePage)
def list_report_tables(
    job_id: JobId,
    verdict: Verdict | None = None,
    recommended: bool | None = None,
    ruled_out: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX_LIMIT)] = PAGE_DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TablePage:
    """Tables in the report's order: weighed tables first, ruled-out tables last."""
    return page_tables(_view_model(job_id).tables, verdict=verdict, recommended=recommended,
                       ruled_out=ruled_out, limit=limit, offset=offset)


@router.get("/assessments/{job_id}/report-data/tables/{table_id}", response_model=TableItem)
def get_report_table(job_id: JobId, table_id: TableIdParam) -> TableItem:
    """One table with its metrics, key factors, bundles and reasoning."""
    for t in _view_model(job_id).tables:
        if t.table_id == table_id:
            return t
    raise ApiError(404, "item_not_found", f"Table {table_id} not found in job {job_id}")


@router.get("/assessments/{job_id}/summary", response_model=AssessmentSummary)
def get_summary(job_id: JobId) -> AssessmentSummary:
    """Compact payload for a dashboard tile, built through the contract accessors."""
    raw = _load_analysis(job_id)
    try:
        out = CacheAnalysisOutputV2.model_validate(raw)
    except ValueError as e:
        raise ApiError(500, "stored_data_invalid", "stored analysis is not valid") from e

    record = job_store().get(job_id)
    llm = out.llm_interpretation
    workload = llm.workload if llm else None
    by_type = out.recommended_by_cache_type()
    return AssessmentSummary(
        job_id=job_id,
        label=record.label if record else None,
        database_name=record.database_name if record else None,
        collected_at=record.collected_at if record else None,
        contract_version=out.contract_version,
        assessment_mode=out.assessment_mode.value,
        degraded_reason=out.degraded_reason,
        interpretation_mode=out.interpretation_mode.value,
        fallback_reason=out.fallback_reason,
        verdict=workload.overall_recommendation if workload else None,
        key_finding=workload.key_finding if workload else None,
        executive_summary=workload.executive_summary if workload else None,
        workload_archetype=workload.workload_archetype if workload else None,
        primary_bottleneck=workload.primary_bottleneck if workload else None,
        queries_recommended=len(out.cached_query_ids()),
        query_results_to_cache=len(by_type.get("QUERY", [])),
        key_lookups_to_cache=len(by_type.get("OBJECT", [])),
        tables_recommended=len(out.candidate_table_ids()),
        cacheable_db_time_ms_per_sec=out.total_cacheable_db_time_ms_per_sec(),
        bundles=len(out.bundles),
        bundles_that_qualify=sum(1 for b in out.bundles if b.get("qualifies")),
        model_id=llm.model_id if llm else None,
        prompt_version=llm.prompt_version if llm else None,
        sizing=_sizing_summary(out.sizing),
    )


def _sizing_summary(sizing: dict | None) -> SizingSummary | None:
    """The headline figures of a sizing block, or None when sizing did not run.

    Args:
        sizing: The `sizing` dict from the analysis.

    Returns:
        A small object for a dashboard tile. Not a capacity plan and not a quote.
    """
    if not sizing:
        return None
    try:
        s = CacheSizingOutput.model_validate(sizing)
    except ValueError as e:
        raise ApiError(500, "stored_data_invalid", "stored sizing is not valid") from e
    return SizingSummary.from_sizing(s)
