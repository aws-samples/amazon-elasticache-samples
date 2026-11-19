"""
The report as data: everything the HTML report shows, for a user interface to draw.

The HTML report and this model are built from the same builders in
src/tools/report/html_report_v2.py, so the facts, the order and the prose are the
same. Every string is plain text; none holds markup. Numbers are raw numbers, with a
display string where the report formats them (one precision rule for every rate).

Spec: .kiro/specs/ux-api (UX-11 to UX-15).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.cache_sizing_output import CacheSizingOutput

VIEW_MODEL_VERSION = "1.0"

Tone = Literal["yes", "maybe", "no", "unknown"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Fact(_Model):
    """One labelled line on a card: a key factor."""

    label: str
    text: str


class KeyValue(_Model):
    """A labelled value, for provenance tables."""

    label: str
    value: str | None = None


class ReportMeta(_Model):
    """Which run this is and how it was judged."""

    view_model_version: str = VIEW_MODEL_VERSION
    job_id: str
    label: str | None = None
    generated_at: str
    contract_version: str
    assessment_mode: str
    degraded_reason: str | None = None
    used_llm: bool
    interpretation_mode: str
    fallback_reason: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None


class SourceInfo(_Model):
    """The database that was assessed."""

    version: str | None = None
    database_name: str | None = None
    hostname: str | None = None
    deployment_type: str | None = None
    collected_at: str | None = None
    line: str = Field(description="The one-line description the report prints under the title")


class Headline(_Model):
    """The verdict banner."""

    verdict: str | None = Field(
        default=None, description="Workload verdict. None when no model reviewed the run")
    tone: Tone
    title: str
    finding: str
    bottleneck: str | None = None
    detail: str | None = Field(default=None, description="The executive summary, when the "
                               "finding is something shorter")
    archetype: str | None = None
    notes: list[str] = Field(default_factory=list)


class GlanceCard(_Model):
    """One headline figure."""

    id: str
    label: str
    value: float | None = Field(default=None, description="The number behind the display")
    display: str | None = None
    note: str


class GlossaryEntry(_Model):
    """A metric or term, in plain language."""

    term: str
    definition: str


class ScoringPillar(_Model):
    """One row of the scoring table."""

    name: str
    weight: str = Field(description="As printed: a percentage, or subtracted or rules it out")
    question: str
    drivers: str


class ScoringBand(_Model):
    """A band of the score and what it means."""

    label: str
    min_score: int
    max_score: int
    recommended: bool


class ScoringSection(_Model):
    """How scoring works, with the weights of this run."""

    intro: str
    pillars: list[ScoringPillar]
    weight_access: float
    weight_pressure: float
    weight_efficiency: float
    formula: str
    hit_rate_note: str | None = None
    bands: list[ScoringBand]
    bands_note: str
    judge_note: str


class WorkloadRow(_Model):
    """One server-wide signal."""

    id: str
    label: str
    value: float | None = None
    display: str | None = None
    note: str = ""


class MetricDisplay(_Model):
    """The metric set as the report prints it."""

    reads_per_sec: str | None = None
    avg_ms: str | None = None
    db_ms_per_sec: str | None = None
    writes_per_sec: str | None = None
    read_dominance: str | None = None
    score: str | None = None
    access_pressure_efficiency: str | None = None
    model_confidence: str | None = None
    penalty: str | None = None
    band: str | None = None


class MetricSet(_Model):
    """The one metric set shown for every query and every table."""

    reads_per_sec: float | None = None
    avg_ms: float | None = None
    db_ms_per_sec: float | None = None
    writes_per_sec: float | None = None
    read_dominance: float | None = Field(default=None, description="0 to 1")
    score: int | None = None
    access: int | None = None
    pressure: int | None = None
    efficiency: int | None = None
    penalty: int | None = None
    band: str | None = None
    model_confidence: float | None = Field(default=None, description="0 to 1")
    db_includes_joins: bool = Field(
        default=False, description="A table's DB time counts every query that reads it, "
        "joins included, so figures overlap")
    not_scored: bool = Field(
        default=False, description="Stopped by a parser gate before scoring")
    hard_excluded: bool = Field(
        default=False, description="Ruled out by the well-served rule. The score is "
        "withheld, not zero")
    display: MetricDisplay


class TableBundleMembership(_Model):
    """A bundle a table belongs to."""

    queryid: str
    db_time_share_pct: float | None = None
    qualifies: bool = False
    other_tables: list[str] = Field(default_factory=list)
    this_table_qualifies: bool = False
    this_table_reason: str | None = None


class QueryItem(_Model):
    """One analysed query, as the report shows it."""

    rank: int = Field(description="Position in the report's order: verdict, then DB time")
    queryid: str
    anchor: str = Field(description="The fragment id the HTML report uses for its card")
    sql: str
    sql_short: str
    cache_type: str = Field(description="QUERY, OBJECT, NOT_CACHEABLE or UNKNOWN")
    is_candidate: bool = Field(description="True for QUERY and OBJECT shapes, which get "
                               "a score and a card")
    verdict: str | None = None
    verdict_label: str | None = None
    recommended: bool
    judged_by_model: bool
    db_time_share_pct: float | None = None
    key_columns: list[str] = Field(default_factory=list)
    key_kind: str | None = Field(
        default=None, description="For key lookups: unique row, collection, or "
        "uniqueness unconfirmed")
    metrics: MetricSet
    factors: list[Fact] = Field(default_factory=list)
    reasoning: str | None = Field(default=None, description="The model's full reasoning")
    evidence: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    suggested_cache_key: str | None = None
    cached_columns: list[str] = Field(default_factory=list)
    invalidated_by: list[str] = Field(default_factory=list)


class TableItem(_Model):
    """One table, as the report shows it."""

    table_id: str
    anchor: str
    verdict: str | None = None
    verdict_label: str | None = None
    recommended: bool
    judged_by_model: bool
    ruled_out: bool = Field(description="Hard excluded before any judgement")
    idle: bool = Field(description="Ruled out and with no traffic, so the report prints "
                       "no metric strip for it")
    size_mb: float | None = None
    buffer_hit_ratio_pct: float | None = None
    metrics: MetricSet
    factors: list[Fact] = Field(default_factory=list)
    reasoning: str | None = None
    evidence: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    caching_strategy: str | None = None
    invalidation_concern: str | None = None
    cached_columns: list[str] = Field(default_factory=list)
    bundles: list[TableBundleMembership] = Field(default_factory=list)


class BundleTableCheck(_Model):
    """Whether one table of a bundle can be object cached."""

    table_id: str
    qualifies: bool = False
    reason: str | None = None
    row_change_interval_hours: float | None = None


class BundleItem(_Model):
    """A Not cacheable query the application could compute from cached rows."""

    queryid: str
    sql: str
    db_time_ms_per_sec: float | None = None
    db_time_share_pct: float | None = None
    not_cacheable_reason: str | None = None
    tables: list[str] = Field(default_factory=list)
    qualifies: bool = False
    table_checks: list[BundleTableCheck] = Field(default_factory=list)


class NotCacheableItem(_Model):
    """A query the parser ruled out."""

    queryid: str
    sql: str
    sql_short: str
    calls_per_second: float | None = None
    why: str | None = None


class NotCacheableGroup(_Model):
    """Not cacheable queries that share a reason."""

    label: str
    count: int
    items: list[NotCacheableItem]


class RuledOutTable(_Model):
    """A table excluded before any judgement."""

    table_id: str
    reason: str | None = None


class StageTiming(_Model):
    """How long a collection stage took."""

    stage: str
    seconds: float | None = None


class ProvenanceSection(_Model):
    """How the judgement and the data were produced."""

    model_rows: list[KeyValue] | None = Field(
        default=None, description="None when no model was consulted")
    no_model_reason: str | None = None
    collection_rows: list[KeyValue] = Field(default_factory=list)
    slowest_stages: list[StageTiming] = Field(default_factory=list)
    collection_warnings: list[str] = Field(default_factory=list)
    local_executor_note: str | None = None


class Footer(_Model):
    """The notes the report ends with."""

    mode_note: str
    sensitivity_note: str


class ReportOverview(_Model):
    """Everything except the long lists of queries and tables."""

    meta: ReportMeta
    source: SourceInfo
    headline: Headline
    at_a_glance: list[GlanceCard]
    keep_in_mind: list[str]
    glossary: list[GlossaryEntry]
    scoring: ScoringSection
    workload: list[WorkloadRow]
    queries_lead: str | None = None
    queries_note: str | None = Field(
        default=None, description="Why there are no queries, when there are none")
    bundles: list[BundleItem]
    not_cacheable: list[NotCacheableGroup]
    ruled_out_tables: list[RuledOutTable]
    sizing: CacheSizingOutput | None = None
    provenance: ProvenanceSection
    footer: Footer
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="queries, query_candidates, queries_recommended, tables, "
        "tables_recommended, tables_ruled_out, bundles")


class ReportViewModel(ReportOverview):
    """The whole report as data."""

    queries: list[QueryItem]
    tables: list[TableItem]


class QueryPage(_Model):
    """One page of queries, in report order."""

    total: int
    offset: int
    limit: int
    items: list[QueryItem]


class TablePage(_Model):
    """One page of tables, in report order."""

    total: int
    offset: int
    limit: int
    items: list[TableItem]
