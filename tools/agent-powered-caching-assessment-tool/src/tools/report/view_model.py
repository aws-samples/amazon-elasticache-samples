"""
Build the report as data (a ReportViewModel) for a user interface.

The HTML report and this model share the same builders in html_report_v2.py: the
header, the glance cards, the glossary, the scoring text, the workload rows, the
metric sets, the key factors and the report's order. This module calls them and turns
what they return into plain text and typed models, so the prose is written once and
the two cannot drift. Tests compare the model with the HTML.

Some builders return strings that hold the report's small markup (a span for Hard
Exclusion, a link to a card, an escaped character). `html_to_text` strips the tags,
unescapes the entities and collapses white space.

Spec: .kiro/specs/ux-api (UX-11 to UX-15).
"""

import html
import re
from datetime import UTC, datetime

from src.contracts.cache_analysis_output import CacheTableRecommendation
from src.contracts.cache_analysis_output_v2 import (
    CacheAnalysisOutputV2,
    LLMQueryVerdict,
    LLMTableVerdict,
    QueryCacheRecommendation,
)
from src.contracts.cache_sizing_output import CacheSizingOutput
from src.contracts.report_view_model import (
    BundleItem,
    BundleTableCheck,
    Fact,
    Footer,
    GlanceCard,
    GlossaryEntry,
    Headline,
    KeyValue,
    MetricDisplay,
    MetricSet,
    NotCacheableGroup,
    NotCacheableItem,
    ProvenanceSection,
    QueryItem,
    QueryPage,
    ReportMeta,
    ReportOverview,
    ReportViewModel,
    RuledOutTable,
    ScoringBand,
    ScoringPillar,
    ScoringSection,
    SourceInfo,
    StageTiming,
    TableBundleMembership,
    TableItem,
    TablePage,
    WorkloadRow,
)
from src.tools.report import html_report_v2 as h
from src.tools.report.report_common import provenance_data

_DASHES = {"", "\u2013", "\u2014"}
_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")

NO_QUERIES_NOTE = "No query statistics were collected, so queries could not be assessed."


def html_to_text(value: object) -> str:
    """Plain text from a string that may hold the report's markup.

    Args:
        value: A string with tags and entities, or any value.

    Returns:
        The text with tags removed, entities unescaped and white space collapsed.
    """
    return _SPACE.sub(" ", html.unescape(_TAG.sub("", str(value)))).strip()


def display(value: object) -> str | None:
    """A display string from the report's HTML, or None where the report prints a dash."""
    text = html_to_text(value)
    return None if text in _DASHES else text


def _num(value: object) -> float | None:
    """A raw number, or None."""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _facts(pairs: list[tuple[str, str]]) -> list[Fact]:
    """Key factors as the card shows them: a pair with empty text is dropped."""
    return [Fact(label=k, text=html_to_text(v)) for k, v in pairs if v]


# ---------------------------------------------------------------------------
# Metric sets
# ---------------------------------------------------------------------------


def metric_set(m: h.Metrics) -> MetricSet:
    """The standard metric set as raw numbers plus the strings the report prints.

    A score under a hard exclusion or a parser gate is withheld, not zero: the raw
    score and penalty are None and the display strings say why.

    Args:
        m: Metrics from the report builder.
    """
    withheld = m.hard_excluded or m.not_scored
    band = "Not cacheable" if m.not_scored else h._verdict_label(m.band)
    return MetricSet(
        reads_per_sec=m.reads_per_sec, avg_ms=m.avg_ms, db_ms_per_sec=m.db_ms_per_sec,
        writes_per_sec=m.writes_per_sec, read_dominance=m.read_dominance,
        score=None if withheld else m.score,
        access=m.access, pressure=m.pressure, efficiency=m.efficiency,
        penalty=None if withheld else m.penalty,
        band=m.band, model_confidence=m.model_conf,
        db_includes_joins=bool(m.db_mark), not_scored=m.not_scored,
        hard_excluded=m.hard_excluded,
        display=MetricDisplay(
            reads_per_sec=display(h._num(m.reads_per_sec)),
            avg_ms=display(h._num(m.avg_ms)),
            db_ms_per_sec=display(
                f"{h._num(m.db_ms_per_sec)}{h._mark(m.db_ms_per_sec, m.db_mark)}"),
            writes_per_sec=display(h._num(m.writes_per_sec)),
            read_dominance=display(h._pct(m.read_dominance)),
            score=display(h._score_text(m, in_row=True)),
            access_pressure_efficiency=display(h._ape_text(m)),
            model_confidence=display(h._pct(m.model_conf)),
            penalty=display(h._penalty_text(m)),
            band=display(band),
        ),
    )


# ---------------------------------------------------------------------------
# Queries and tables
# ---------------------------------------------------------------------------


def _query_item(rank: int, r: QueryCacheRecommendation, v: LLMQueryVerdict | None,
                out: CacheAnalysisOutputV2, recommended: set[str]) -> QueryItem:
    ctype = h._ctype(r)
    candidate = ctype in ("OBJECT", "QUERY")
    verdict = out.query_verdict_for(r.queryid) if candidate else None
    key_kind = None
    if ctype == "OBJECT":
        key_kind = ("unique row" if r.key_is_primary_key
                    else "collection" if r.caches_collection
                    else "uniqueness unconfirmed")
    return QueryItem(
        rank=rank, queryid=r.queryid, anchor=h._anchor("q", r.queryid),
        sql=r.query_text or "", sql_short=html_to_text(h._short_sql(r.query_text, 90)),
        cache_type=ctype, is_candidate=candidate,
        verdict=verdict, verdict_label=display(h._verdict_label(verdict)),
        recommended=r.queryid in recommended, judged_by_model=v is not None,
        db_time_share_pct=r.db_time_share_pct,
        key_columns=list(r.key_columns or []), key_kind=key_kind,
        metrics=metric_set(h._query_metrics(r, v)),
        factors=_facts(h._query_factors(r, v)) if candidate else [],
        reasoning=v.reasoning if v else None,
        evidence=list(r.evidence or []), concerns=list(r.concerns or []),
        suggested_cache_key=v.suggested_cache_key if v else None,
        cached_columns=list(getattr(v, "cached_columns", None) or []) if v else [],
        invalidated_by=list(r.write_sources or []),
    )


def _table_item(r: CacheTableRecommendation, t: LLMTableVerdict | None,
                out: CacheAnalysisOutputV2, recommended: set[str],
                consistency: list[dict]) -> TableItem:
    verdict = out.verdict_for(r.table_id)
    return TableItem(
        table_id=r.table_id, anchor=h._anchor("t", r.table_id),
        verdict=verdict, verdict_label=display(h._verdict_label(verdict)),
        recommended=r.table_id in recommended, judged_by_model=t is not None,
        ruled_out=bool(r.hard_excluded), idle=h.table_is_idle(r),
        size_mb=r.size_mb, buffer_hit_ratio_pct=r.buffer_hit_ratio_pct,
        metrics=metric_set(h._table_metrics(r, t)),
        factors=_facts(h.table_factors(r, t, consistency)),
        reasoning=t.reasoning if t else None,
        evidence=list(r.evidence or []), concerns=list(r.concerns or []),
        caching_strategy=t.caching_strategy if t else None,
        invalidation_concern=t.invalidation_concern if t else None,
        cached_columns=list(getattr(t, "cached_columns", None) or []) if t else [],
        bundles=[TableBundleMembership.model_validate(b)
                 for b in (getattr(r, "bundles", None) or [])],
    )


def ordered_queries(out: CacheAnalysisOutputV2) -> list[QueryItem]:
    """Every analysed query in the report's order: verdict first, then database time."""
    verdicts = h._query_verdicts(out)
    recommended = set(out.cached_query_ids())
    ranked = h._by_verdict_then_db_time(out, h._ranked(out))
    return [_query_item(i, r, verdicts.get(r.queryid), out, recommended)
            for i, r in enumerate(ranked, 1)]


def ordered_tables(out: CacheAnalysisOutputV2) -> list[TableItem]:
    """Every table in the report's order: weighed tables first, ruled-out tables last."""
    judged = h._table_verdicts_by_id(out)
    recommended = set(out.candidate_table_ids())
    consistency = out.llm_interpretation.bundle_consistency if out.llm_interpretation else []
    recs = out.table_recommendations
    ordered = (h._tables_by_verdict(out, [r for r in recs if not r.hard_excluded])
               + h._tables_by_verdict(out, [r for r in recs if r.hard_excluded]))
    return [_table_item(r, judged.get(r.table_id), out, recommended, consistency)
            for r in ordered]


# ---------------------------------------------------------------------------
# The other sections
# ---------------------------------------------------------------------------


def _headline(parts: h.HeaderParts) -> Headline:
    notes = []
    if parts.degraded_reason:
        notes.append(f"Degraded collection: {parts.degraded_reason}")
    if parts.why_no_ai:
        notes.append(f"Why no AI review: {parts.why_no_ai}")
    tone = parts.css.removeprefix("verdict-")
    return Headline(
        verdict=parts.verdict, tone=tone if tone in ("yes", "maybe", "no") else "unknown",
        title=parts.headline, finding=parts.finding, bottleneck=parts.bottleneck,
        detail=parts.executive_detail, archetype=parts.archetype, notes=notes)


def _scoring(out: CacheAnalysisOutputV2) -> ScoringSection:
    sp = h.scoring_parts(out)
    wa, wp, we = sp.weights
    va, vp, ve = sp.weight_values
    return ScoringSection(
        intro=html_to_text(h.SCORING_INTRO),
        pillars=[ScoringPillar(name=n, weight=w, question=q, drivers=r)
                 for n, w, q, r in sp.pillars],
        weight_access=va, weight_pressure=vp, weight_efficiency=ve,
        formula=html_to_text("Score" + h.score_formula_tail(wa, wp, we)),
        hit_rate_note=html_to_text(h._hit_rate_note(out)) or None,
        bands=[ScoringBand(label=label, min_score=lo, max_score=hi, recommended=rec)
               for label, lo, hi, rec in h.BANDS],
        bands_note=html_to_text("Bands:" + h.BANDS_TAIL),
        judge_note=sp.judge,
    )


def _bundles(out: CacheAnalysisOutputV2) -> list[BundleItem]:
    items = []
    for b in out.bundles or []:
        items.append(BundleItem(
            queryid=str(b.get("queryid")), sql=b.get("query_text") or "",
            db_time_ms_per_sec=b.get("db_time_ms_per_sec"),
            db_time_share_pct=b.get("db_time_share_pct"),
            not_cacheable_reason=b.get("not_cacheable_reason"),
            tables=list(b.get("tables") or []), qualifies=bool(b.get("qualifies")),
            table_checks=[BundleTableCheck.model_validate(c)
                          for c in b.get("table_checks") or []]))
    return items


def _not_cacheable(out: CacheAnalysisOutputV2) -> list[NotCacheableGroup]:
    groups = []
    for label, recs in h.not_cacheable_groups(out):
        groups.append(NotCacheableGroup(label=label, count=len(recs), items=[
            NotCacheableItem(
                queryid=r.queryid, sql=r.query_text or "",
                sql_short=html_to_text(h._short_sql(r.query_text, 80)),
                calls_per_second=r.calls_per_second,
                why=(r.classification_reasons or [""])[0] or None)
            for r in recs]))
    return groups


def _provenance(collection: dict, out: CacheAnalysisOutputV2) -> ProvenanceSection:
    model_rows = h.model_provenance_rows(out)
    data = provenance_data(collection)
    return ProvenanceSection(
        model_rows=(None if model_rows is None else
                    [KeyValue(label=k, value=display(v)) for k, v in model_rows]),
        no_model_reason=out.fallback_reason if model_rows is None else None,
        collection_rows=[KeyValue(label=k, value=display(v)) for k, v in data["rows"]],
        slowest_stages=[StageTiming(stage=str(k), seconds=_num(v))
                        for k, v in data["slowest"]],
        collection_warnings=[str(w) for w in data["warnings"]],
        local_executor_note=data["local_executor_note"],
    )


def build_report_overview(collection: dict, analysis: dict, job_id: str | None = None,
                          label: str | None = None) -> ReportOverview:
    """Everything the report shows except the long query and table lists.

    Args:
        collection: Collector output.
        analysis: The analysis as a dict, with `sizing` attached when sizing ran.
        job_id: Shown in the meta block. Defaults to the artifact's own id.
        label: The job's label, if it has one.

    Returns:
        The overview. See build_report_view_model for the whole report.

    Raises:
        ValueError: when the analysis does not conform to CacheAnalysisOutputV2.
    """
    try:
        out = CacheAnalysisOutputV2.model_validate(analysis)
    except Exception as exc:
        raise ValueError(f"analysis does not conform to CacheAnalysisOutputV2: {exc}") from exc
    return _overview(collection, analysis, out, job_id, label)


def _overview(collection: dict, analysis: dict, out: CacheAnalysisOutputV2,
              job_id: str | None, label: str | None) -> ReportOverview:
    parts = h.header_parts(collection, out)
    md = collection.get("metadata") or {}
    src = md.get("source_database") or {}
    li = out.llm_interpretation
    ranked = h._ranked(out)
    summary = out.query_analysis_summary or {}
    tables = out.table_recommendations

    return ReportOverview(
        meta=ReportMeta(
            job_id=job_id or analysis.get("job_id") or collection.get("job_id") or "unknown",
            label=label, generated_at=datetime.now(UTC).isoformat(),
            contract_version=out.contract_version, assessment_mode=h._mode(out),
            degraded_reason=out.degraded_reason, used_llm=bool(out.used_llm),
            interpretation_mode=out.interpretation_mode.value,
            fallback_reason=out.fallback_reason,
            model_id=li.model_id if li else None,
            prompt_version=li.prompt_version if li else None),
        source=SourceInfo(
            version=src.get("version"), database_name=src.get("database_name"),
            hostname=src.get("hostname"), deployment_type=src.get("deployment_type"),
            collected_at=md.get("collection_timestamp"),
            line=" \u00b7 ".join(parts.db_parts) or "Source database details unavailable"),
        headline=_headline(parts),
        at_a_glance=[GlanceCard(id=i, label=k, value=_num(raw), display=display(v),
                                note=html_to_text(n))
                     for i, k, v, n, raw in h.glance_cards(collection, out)],
        keep_in_mind=[html_to_text(i) for i in h.keep_in_mind(out)],
        glossary=[GlossaryEntry(term=k, definition=html_to_text(v))
                  for k, v in h.glossary_entries(out)],
        scoring=_scoring(out),
        workload=[WorkloadRow(id=i, label=k, value=_num(raw), display=display(v),
                              note=html_to_text(n))
                  for i, k, v, n, raw in h.workload_rows(out)],
        queries_lead=html_to_text(h.top_queries_lead(out)) if ranked else None,
        queries_note=None if ranked else (summary.get("note") or NO_QUERIES_NOTE),
        bundles=_bundles(out),
        not_cacheable=_not_cacheable(out),
        ruled_out_tables=[RuledOutTable(table_id=r.table_id, reason=r.exclusion_reason)
                          for r in tables if r.hard_excluded],
        sizing=CacheSizingOutput.model_validate(out.sizing) if out.sizing else None,
        provenance=_provenance(collection, out),
        footer=Footer(mode_note=h.footer_mode_note(out),
                      sensitivity_note=h.SENSITIVITY_NOTE),
        counts={
            "queries": len(ranked),
            "query_candidates": sum(1 for r in ranked if h._ctype(r) in ("OBJECT", "QUERY")),
            "queries_recommended": len(out.cached_query_ids()),
            "tables": len(tables),
            "tables_recommended": len(out.candidate_table_ids()),
            "tables_ruled_out": sum(1 for r in tables if r.hard_excluded),
            "bundles": len(out.bundles or []),
        },
    )


def build_report_view_model(collection: dict, analysis: dict, job_id: str | None = None,
                            label: str | None = None) -> ReportViewModel:
    """The whole report as data.

    Args:
        collection: Collector output.
        analysis: The analysis as a dict, with `sizing` attached when sizing ran.
        job_id: Shown in the meta block. Defaults to the artifact's own id.
        label: The job's label, if it has one.

    Returns:
        A ReportViewModel: the overview, every analysed query and every table, in the
        order the HTML report prints them.

    Raises:
        ValueError: when the analysis does not conform to CacheAnalysisOutputV2.
    """
    try:
        out = CacheAnalysisOutputV2.model_validate(analysis)
    except Exception as exc:
        raise ValueError(f"analysis does not conform to CacheAnalysisOutputV2: {exc}") from exc
    overview = _overview(collection, analysis, out, job_id, label)
    return ReportViewModel(**dict(overview), queries=ordered_queries(out),
                           tables=ordered_tables(out))


# ---------------------------------------------------------------------------
# Slices
# ---------------------------------------------------------------------------


def page_queries(queries: list[QueryItem], *, verdict: str | None = None,
                 cache_type: str | None = None, recommended: bool | None = None,
                 limit: int = 25, offset: int = 0) -> QueryPage:
    """Filter the queries and return one page, keeping the report's order.

    Args:
        queries: Items from ordered_queries.
        verdict: Keep only this verdict, for example STRONG_CANDIDATE.
        cache_type: Keep only QUERY, OBJECT, NOT_CACHEABLE or UNKNOWN.
        recommended: Keep only recommended (True) or not recommended (False).
        limit: Page size.
        offset: Items to skip.
    """
    rows = [q for q in queries
            if (verdict is None or q.verdict == verdict)
            and (cache_type is None or q.cache_type == cache_type)
            and (recommended is None or q.recommended == recommended)]
    return QueryPage(total=len(rows), offset=offset, limit=limit,
                     items=rows[offset:offset + limit])


def page_tables(tables: list[TableItem], *, verdict: str | None = None,
                recommended: bool | None = None, ruled_out: bool | None = None,
                limit: int = 25, offset: int = 0) -> TablePage:
    """Filter the tables and return one page, keeping the report's order.

    Args:
        tables: Items from ordered_tables.
        verdict: Keep only this verdict.
        recommended: Keep only recommended (True) or not recommended (False).
        ruled_out: Keep only ruled-out (True) or weighed (False) tables.
        limit: Page size.
        offset: Items to skip.
    """
    rows = [t for t in tables
            if (verdict is None or t.verdict == verdict)
            and (recommended is None or t.recommended == recommended)
            and (ruled_out is None or t.ruled_out == ruled_out)]
    return TablePage(total=len(rows), offset=offset, limit=limit,
                     items=rows[offset:offset + limit])
