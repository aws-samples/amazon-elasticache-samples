"""
Cache Analysis Output Contract, v2 (LLM-interpreted).

v2 keeps every deterministic measurement and score from v1 and adds an LLM
interpretation layer on top. The LLM decides the verdict; the arithmetic still
decides the scores.

STRUCTURE
---------
There is one verdict in a v2 artifact, and it is the model's. The deterministic
layer is the MEASUREMENT layer, not a competing opinion:

    measurement (deterministic)  ->  judgement (LLM)  ->  verdict

TWO DIMENSIONS
--------------
Contract 2.1 added a per-query dimension, which is now the primary one.

    query_recommendations   PRIMARY. Top N queries by database time, each with a
                            parsed cache type and its cache economics. A query is
                            the unit a customer can act on, and
                            pg_stat_statements is per-query, so the table rollup
                            was always the lossy step.

    table_recommendations   The object-caching dimension. Unchanged from 2.0.

Contract 2.2 scores both dimensions with ONE model: the same four dimensions,
weights, thresholds, bands, and hard exclusions. Only the inputs differ. Each
query record carries its band in `verdict`, which, as for tables, is an input to
the model.

Contract 2.3 removes TTL and staleness entirely. Read dominance uses measured
writes as-is. The model no longer proposes a TTL or a staleness assumption, and
the invalidate-on-write hit ceiling is gone because it equals read dominance.

Contract 2.4 adds exclusion_reasons on query and table records, every hard
exclusion that applied in precedence order. Hard exclusions no longer add
penalty points, so disqualifier_penalty is the soft penalty only. Efficiency is
scored from the cached unit (rows per read and keyability, plus table size for
tables), and write-only traffic is write-dominated. 2.3 artifacts still load.
Artifacts written under 2.1 and 2.2 still load; their removed fields are ignored.

Contract 2.5 replaces Cache Efficiency (spec cache-efficiency-v2): estimated hit
rate over the observation window (75%) and result size, time saved per hit x a
bytes factor (25%). cache_efficiency_score is None when not scored, and the
`efficiency` block on query and table records carries the inputs. A query
whose reuse is deferred (range, IN, LIKE, OFFSET or other parameter use) needs
`reuse_reasoning` in its LLM verdict. 2.4 artifacts still load.

Contract 2.6 rebuilds Disqualifiers (spec disqualifiers-v2). The only
disqualifier hard exclusion is already well served and not busy
(exclusion_kind well_served). Parser gates keep blocking but are
exclusion_kind not_cacheable. Heavy writes, busy-but-fast and locks are flat
penalties listed in `penalties`. A busy_but_fast item needs
`busy_context_reasoning` in its LLM verdict. 2.5 artifacts still load.

Contract 2.7 adds bundles (spec cache-bundles): `bundles` at the top level and
on table records. A Not cacheable read query with 5% or more of DB time whose
tables can all be object cached (not hard excluded, no lock, each row changing
at most hourly) can be computed by the application from cached rows. A table
in a qualifying bundle needs `bundle_reasoning` in its LLM verdict. No score
effect. 2.6 artifacts still load.

Read verdicts through the accessors, never off the records:

    query_verdict_for(queryid)        the model's per-query verdict
    cached_query_ids()                recommended queries, parser-gated
    recommended_by_cache_type()       split into OBJECT and QUERY work
    verdict_for(table_id)             the model's per-table verdict
    candidate_table_ids()             recommended tables

`table_recommendations` records retain a `verdict` field because they reuse the
v1 type, but in a v2 artifact that field is an input, not the answer.

Threshold-band comparison lives in the decision trace, not here. It is a
calibration concern for whoever maintains the thresholds, and it has no place in
an artifact a customer reads.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.cache_analysis_output import (
    AgentMetadata,
    CacheAssessmentMode,
    CacheTableRecommendation,
)


class InterpretationMode(StrEnum):
    """Where the verdicts in this artifact came from.

    LLM                    - a model examined the measurements and decided.
                             This is the normal v2 path.
    DETERMINISTIC_FALLBACK - the model was unavailable, so threshold bands
                             produced the verdicts. Labelled explicitly because
                             band verdicts must never be mistaken for LLM
                             judgement; they are a different and weaker product.
    """

    LLM = "llm"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


class CacheType(StrEnum):
    """What kind of cache a query would need.

    These are different products. Conflating them produces advice nobody can
    act on, because the key, the invalidation trigger, and the failure mode all
    differ.

    OBJECT         One row, or one collection under a non-unique key. Keyed by a
                   primary or unique key. Invalidation is precise: only a write
                   to that same key matters.
    QUERY          One result set. Keyed by query identity plus parameter values.
                   Invalidation is broad: any write to any table it reads.
    NOT_CACHEABLE  A write statement, introspection traffic, or a result that
                   changes per call regardless of the data.
    UNKNOWN        Could not be classified. Never treat this as a candidate.
    """

    OBJECT = "OBJECT"
    QUERY = "QUERY"
    NOT_CACHEABLE = "NOT_CACHEABLE"
    UNKNOWN = "UNKNOWN"


class QueryCacheRecommendation(BaseModel):
    """Measured signals and cache economics for one query.

    This is the MEASUREMENT layer for the per-query dimension. It carries no
    verdict; the model supplies that in LLMQueryVerdict.
    """

    queryid: str
    query_text: str = Field(description="Normalised text from pg_stat_statements")
    query_type: str = Field(default="", description="SELECT, INSERT, UPDATE, ...")

    cache_type: CacheType = Field(
        default=CacheType.UNKNOWN,
        description="Derived from the parse tree, not from a model or a threshold",
    )
    access_shape: str = Field(
        default="UNKNOWN",
        description="POINT_LOOKUP | BOUNDED_RANGE | FULL_SCAN | AGGREGATE | "
        "REPORT | WRITE | SYSTEM | UNKNOWN",
    )

    # --- measured ---
    calls_per_second: float = 0.0
    avg_exec_ms: float = 0.0
    total_time_ms: float = 0.0
    rows_returned_avg: float = 0.0
    buffer_hit_ratio_pct: float | None = None
    io_read_time_share: float | None = None
    temp_blocks_written: int = 0

    # --- economics ---
    db_time_ms_per_sec: float = Field(
        default=0.0,
        description="calls_per_second x avg_exec_ms. Database time avoided at a "
        "100% hit rate, so an explicit upper bound and not a forecast.",
    )
    db_time_share_pct: float = Field(
        default=0.0, description="This query's share of all query time"
    )
    invalidating_write_rate: float = Field(
        default=0.0,
        description="Writes per second against every table this query reads. "
        "Answers whether a cache would actually hold.",
    )
    invalidation_ratio: float | None = Field(
        default=None, description="invalidating_write_rate / calls_per_second"
    )

    # --- shape detail ---
    tables_read: list[str] = Field(default_factory=list)
    tables_written: list[str] = Field(default_factory=list)
    key_columns: list[str] = Field(default_factory=list)
    key_is_primary_key: bool = False
    caches_collection: bool = Field(
        default=False,
        description="True when the key is not unique, so the entry holds several "
        "rows. Value size varies and invalidation is wider than a single row.",
    )
    non_deterministic: list[str] = Field(
        default_factory=list,
        description="Functions like now() that make the result time-dependent",
    )
    clock_uses: list[str] = Field(
        default_factory=list,
        description="Each clock-function use and the granularity it resolves to, "
        "for example 'now: raw' or 'now: day'. Finer than hourly is excluded.",
    )
    unresolved_relations: list[str] = Field(
        default_factory=list,
        description="Relations that are neither a collected table nor a view "
        "with a collected definition. Their write rate is unknown.",
    )
    views_resolved: list[str] = Field(
        default_factory=list,
        description="Views read by the query, charged through their base tables.",
    )
    is_system_query: bool = False
    parse_ok: bool = True
    write_sources: list[str] = Field(default_factory=list)
    classification_reasons: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    # --- the four dimensions (contract 2.2), same scorer as tables ---
    verdict: str = Field(
        default="NOT_RECOMMENDED",
        description="Threshold band from the four-dimension score. An INPUT to "
        "the model, not the answer. Read the answer through query_verdict_for().",
    )
    confidence_score: int = Field(default=0, ge=0, le=100)
    access_suitability_score: int = Field(default=0, ge=0, le=100)
    pressure_evidence_score: int = Field(default=0, ge=0, le=100)
    cache_efficiency_score: int | None = Field(
        default=None, ge=0, le=100,
        description="None when not scored. See `efficiency` for the inputs.")
    efficiency: dict | None = Field(
        default=None,
        description="Cache Efficiency detail (contract 2.5): calls_in_window, "
        "window_seconds, k, k_source, estimated_hit_rate, reuse_score, "
        "reuse_deferred_reason, result_bytes, time_saved_ms, bytes_factor, "
        "result_size_score, status, and any not-scored reason.")
    disqualifier_penalty: int = Field(default=0, ge=0)
    exclusion_kind: str | None = Field(
        default=None,
        description="well_served (the one disqualifier hard exclusion) or "
        "not_cacheable (a parser gate). None when not excluded.")
    penalties: list[dict] = Field(
        default_factory=list, description="Each soft penalty: {points, reason}")
    busy_but_fast: bool = False
    db_share_of_capacity: float | None = None
    well_served: dict | None = None
    hard_excluded: bool = Field(
        default=False,
        description="A gate or disqualifier ruled it out. The model may not "
        "overturn this; cached_query_ids() enforces it.",
    )
    exclusion_reason: str | None = None
    exclusion_reasons: list[str] = Field(
        default_factory=list,
        description="Every hard exclusion that applied, in precedence order. The "
        "first is exclusion_reason. Hard exclusions add no penalty points.",
    )
    evidence: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    read_dominance: float | None = Field(
        default=None,
        description="calls / (calls + invalidating_write_rate). Feeds the same "
        "0.90/0.75/0.50 thresholds and write-dominated exclusion as tables.",
    )

    model_config = ConfigDict(extra="ignore")


class LLMQueryVerdict(BaseModel):
    """The model's judgement for one query. This is the verdict of record."""

    queryid: str
    verdict: str = Field(
        description="STRONG_CANDIDATE | MODERATE_CANDIDATE | WEAK_CANDIDATE | "
        "NOT_RECOMMENDED"
    )
    reasoning: str = Field(
        description="Why this verdict, citing the specific figures retrieved"
    )
    primary_signal: str = Field(default="")
    recommended_cache_type: str | None = Field(
        default=None,
        description="Usually agrees with the parsed cache_type. A disagreement "
        "is worth reading, for example choosing to cache a joined result rather "
        "than the rows behind it.",
    )
    suggested_cache_key: str | None = Field(
        default=None,
        description="Concrete key design, for example customer:{customer_id}",
    )
    invalidation_concern: str | None = None
    confidence_in_judgement: float | None = Field(default=None, ge=0.0, le=1.0)
    reuse_reasoning: str | None = Field(
        default=None,
        description="Required when the query's reuse is deferred (range, IN, "
        "LIKE, OFFSET or other parameter use): how many distinct requests the "
        "application likely sends, judged from what the query is for. A verdict "
        "on a deferred query without it is dropped in validation.",
    )
    busy_context_reasoning: str | None = Field(
        default=None,
        description="Required for a busy_but_fast query: whether caching it is "
        "worth it for the industry inferred from the schema and workload.",
    )
    # No ge=1 here: a bad value must drop one verdict in validation, not fail
    # the whole structured extraction.
    estimated_distinct_requests: float | None = Field(
        default=None,
        description="Required with reuse_reasoning for a deferred query: the "
        "estimated number of distinct requests the application actually sent "
        "over the observation window, at most the calls in the window. Reuse is "
        "scored from it as hit rate = 1 - estimate / calls.",
    )
    cached_columns: list[str] | None = Field(
        default=None,
        description="OBJECT queries only: the columns of the table to store in "
        "each cached object, chosen from what the application's queries on that "
        "table read. Exact names from get_table_columns. Sizing multiplies their "
        "byte widths by the number of keys. Ignored for QUERY caching, where the "
        "cached value is the result set as returned.",
    )

    model_config = ConfigDict(extra="ignore")


class LLMTableVerdict(BaseModel):
    """The model's judgement for one table. This is the verdict of record.

    Scores are deliberately NOT restated here. They live on the deterministic
    CacheTableRecommendation, so a model transcription error cannot become the
    reported score.
    """

    table_id: str
    verdict: str = Field(
        description="STRONG_CANDIDATE | MODERATE_CANDIDATE | WEAK_CANDIDATE | "
        "NOT_RECOMMENDED"
    )
    reasoning: str = Field(
        description="Why this verdict, citing the specific signals that drove it"
    )
    primary_signal: str = Field(
        default="",
        description="The single most decisive signal, named explicitly",
    )
    caching_strategy: str | None = Field(
        default=None,
        description="Suggested approach, for example cache-aside on a point "
        "lookup keyed by primary key, or cache the join result",
    )
    invalidation_concern: str | None = Field(
        default=None,
        description="What would invalidate a cached entry, and how often",
    )
    confidence_in_judgement: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="The model's own confidence in its reasoning. Distinct from "
        "the deterministic confidence score, which measures the workload.",
    )
    busy_context_reasoning: str | None = Field(
        default=None,
        description="Required for a busy_but_fast table: whether caching it is "
        "worth it for the industry inferred from the schema and workload.",
    )
    bundle_reasoning: str | None = Field(
        default=None,
        description="Required for a table in a qualifying bundle: whether the "
        "bundle is worth caching, what the application must do instead of the "
        "query, and that the saving needs every table in the bundle cached.",
    )
    cached_columns: list[str] | None = Field(
        default=None,
        description="For a recommended table: the columns to store in each "
        "cached row object, chosen from what the queries on the table read. "
        "Exact names from get_table_columns. Omit to cache the whole row.",
    )

    model_config = ConfigDict(extra="ignore")


class LLMWorkloadAssessment(BaseModel):
    """The model's workload-level reading, across all tables."""

    overall_recommendation: str = Field(
        description="RECOMMENDED | PARTIALLY_RECOMMENDED | NOT_RECOMMENDED | "
        "INSUFFICIENT_DATA"
    )
    executive_summary: str = Field(
        description="Two or three sentences a non-specialist can act on"
    )
    key_finding: str = Field(
        default="",
        description="The single most important thing observed, stated plainly",
    )
    workload_archetype: str | None = Field(
        default=None,
        description="What kind of system this appears to be, for example "
        "e-commerce OLTP or reporting. Inferred, not measured.",
    )
    primary_bottleneck: str | None = Field(
        default=None,
        description="What actually limits this workload, which may not be "
        "something a cache addresses",
    )
    cheaper_alternatives: list[str] = Field(
        default_factory=list,
        description="Interventions that would deliver the same benefit for less, "
        "such as raising shared_buffers or adding an index. Populated when the "
        "evidence points away from caching.",
    )
    risks_if_proceeding: list[str] = Field(default_factory=list)
    recommended_next_steps: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class LLMInterpretation(BaseModel):
    """Full model output plus the provenance needed to audit it."""

    workload: LLMWorkloadAssessment
    queries: list[LLMQueryVerdict] = Field(
        default_factory=list,
        description="Per-query judgements. The primary output, because this is "
        "the unit a customer can act on.",
    )
    tables: list[LLMTableVerdict] = Field(
        default_factory=list,
        description="Per-table judgements for the object-caching dimension",
    )

    model_id: str | None = None
    prompt_version: str | None = None
    tool_calls_made: int = Field(
        default=0,
        description="How many data tools the model invoked. Zero suggests it "
        "answered without examining the data, which is worth noticing.",
    )
    tools_used: list[str] = Field(default_factory=list)
    interpretation_duration_seconds: float | None = None
    bundle_consistency: list[dict] = Field(
        default_factory=list,
        description="Bundles whose table verdicts were more than the allowed "
        "number of bands apart (spec CB-8). Each: queryid, db_time_share_pct, "
        "tables, before, after, resolved. Unresolved ones are shown as warnings.",
    )

    model_config = ConfigDict(extra="ignore")


class CacheAnalysisOutputV2(BaseModel):
    """v2 analysis artifact.

    Every v1 field is preserved with identical meaning, so a v2 artifact can be
    read by anything that understands v1 plus two extra keys.
    """

    # 2.1 added the per-query dimension. 2.2 scores it with the same four
    # dimensions and thresholds as tables. 2.3 removes TTL and staleness. 2.4
    # adds exclusion_reasons and drops stacked penalties for hard exclusions.
    # 2.5 replaces Cache Efficiency with estimated hit rate and result size.
    # 2.6 rebuilds Disqualifiers: one hard exclusion, flat listed penalties.
    # 2.7 adds bundles: Not cacheable queries computable from cached rows.
    contract_version: str = Field(default="2.7", pattern=r"^\d+\.\d+$")
    agent_metadata: AgentMetadata

    assessment_mode: CacheAssessmentMode = Field(default=CacheAssessmentMode.FULL)
    degraded_reason: str | None = None

    # MEASUREMENT layer, per query. The primary dimension, because a query is
    # the unit a customer can act on and pg_stat_statements is per-query.
    query_recommendations: list[QueryCacheRecommendation] = Field(
        default_factory=list,
        description="Top N queries by database time, with parsed cache type and "
        "cache economics. Carries no verdict; the model supplies that.",
    )
    query_analysis_summary: dict = Field(
        default_factory=dict,
        description="Coverage and distribution for the per-query pass, including "
        "what the top-N cap dropped so a reader can tell whether the analysed "
        "set is the whole story.",
    )

    # MEASUREMENT layer, per table. The object-caching dimension.
    table_recommendations: list[CacheTableRecommendation] = Field(
        default_factory=list,
        description="Measured signals, computed scores, evidence, and concerns "
        "per table. Supporting data for the verdict, not the verdict itself.",
    )
    workload_context: dict = Field(default_factory=dict)
    thresholds_used: dict = Field(default_factory=dict)
    bundles: list[dict] = Field(
        default_factory=list,
        description="Not cacheable read queries with at least the bundle share "
        "of DB time, and whether every table they read can be object cached "
        "(spec cache-bundles). Facts for the model; no score effect.",
    )

    # JUDGEMENT layer: the verdict of record.
    interpretation_mode: InterpretationMode = Field(
        default=InterpretationMode.DETERMINISTIC_FALLBACK
    )
    llm_interpretation: LLMInterpretation | None = Field(
        default=None,
        description="The model's assessment. Present when interpretation_mode "
        "is LLM. This is the verdict of record.",
    )
    fallback_reason: str | None = Field(
        default=None,
        description="Why the model was not used. Present whenever "
        "interpretation_mode is DETERMINISTIC_FALLBACK, and the reason a reader "
        "should treat the verdicts as threshold output rather than judgement.",
    )
    # Attached by the separate Sizing & Cost agent (src/agents/sizing), contract
    # src/contracts/cache_sizing_output.py. A dict so the analysis contract does
    # not depend on the sizing one. None until sizing has run.
    sizing: dict | None = Field(
        default=None,
        description="Sizing & Cost result (CacheSizingOutput). Computed "
        "deterministically from the recommended items only.",
    )

    model_config = ConfigDict(extra="ignore")

    @property
    def used_llm(self) -> bool:
        return self.interpretation_mode is InterpretationMode.LLM

    @property
    def workload_verdict(self) -> str | None:
        """Authoritative workload-level verdict.

        Returns the model's recommendation when available. Under fallback there
        is no model-level workload verdict, so None is returned and the caller
        must state that the assessment is deterministic-only rather than
        inventing a conclusion.
        """
        if self.llm_interpretation:
            return self.llm_interpretation.workload.overall_recommendation
        return None

    def verdict_for(self, table_id: str) -> str | None:
        """Authoritative verdict for one table.

        Under LLM mode this is the model's judgement. Under fallback it is the
        threshold band, which the artifact labels as such.

        Args:
            table_id: Canonical schema.table identifier.

        Returns:
            The verdict string, or None when the table is unknown.
        """
        if self.llm_interpretation:
            for t in self.llm_interpretation.tables:
                if t.table_id == table_id:
                    return t.verdict
        for r in self.table_recommendations:
            if r.table_id == table_id:
                return r.verdict
        return None

    def query_verdict_for(self, queryid: str) -> str | None:
        """Authoritative verdict for one query.

        The model's verdict where it gave one. Otherwise the four-dimension band,
        exactly as tables fall back, so fallback output is labelled bands rather
        than nothing. A hard-excluded query is always NOT_RECOMMENDED, whatever
        the model said.

        Args:
            queryid: The pg_stat_statements query identifier.

        Returns:
            The verdict string, or None when the query is unknown.
        """
        rec = next((r for r in self.query_recommendations if r.queryid == queryid), None)
        if rec is not None and rec.hard_excluded:
            return "NOT_RECOMMENDED"
        if self.llm_interpretation:
            for q in self.llm_interpretation.queries:
                if q.queryid == queryid:
                    return q.verdict
        return rec.verdict if rec is not None else None

    def cached_query_ids(self) -> list[str]:
        """Queries judged worth caching, for downstream sizing.

        Excludes anything the parser or a disqualifier ruled out, so a model
        verdict on a write, an introspection query, or a hard-excluded query
        cannot promote it.
        """
        wanted = {"STRONG_CANDIDATE", "MODERATE_CANDIDATE"}
        cacheable = {
            r.queryid for r in self.query_recommendations
            if r.cache_type in (CacheType.OBJECT, CacheType.QUERY)
            and not r.hard_excluded
        }
        return [
            r.queryid for r in self.query_recommendations
            if r.queryid in cacheable
            and (self.query_verdict_for(r.queryid) or "") in wanted
        ]

    def recommended_by_cache_type(self) -> dict[str, list[str]]:
        """Recommended query ids grouped by cache type.

        The two groups are different pieces of work with different designs, so
        a report must not present them as one list.

        Returns:
            {"OBJECT": [...], "QUERY": [...]}
        """
        by_id = {r.queryid: r for r in self.query_recommendations}
        out: dict[str, list[str]] = {"OBJECT": [], "QUERY": []}
        for qid in self.cached_query_ids():
            rec = by_id.get(qid)
            if rec is None:
                continue
            key = rec.cache_type.value if hasattr(rec.cache_type, "value") else str(
                rec.cache_type)
            if key in out:
                out[key].append(qid)
        return out

    def total_cacheable_db_time_ms_per_sec(self) -> float:
        """Database time per second across recommended queries only.

        The headline benefit ceiling. Sums only what the model recommended, so it
        cannot overstate by including queries that were declined.
        """
        wanted = set(self.cached_query_ids())
        return round(
            sum(r.db_time_ms_per_sec for r in self.query_recommendations
                if r.queryid in wanted),
            2,
        )

    def candidate_table_ids(self) -> list[str]:
        """Tables judged worth caching, for downstream sizing.

        Reads the authoritative verdict, so under LLM mode this reflects the
        model's judgement rather than the threshold bands. Without this, sizing
        would be computed for tables the model declined.
        """
        wanted = {"STRONG_CANDIDATE", "MODERATE_CANDIDATE"}
        excluded = {r.table_id for r in self.table_recommendations if r.hard_excluded}
        out = []
        for r in self.table_recommendations:
            if r.table_id in excluded:
                continue
            if (self.verdict_for(r.table_id) or "") in wanted:
                out.append(r.table_id)
        return out
