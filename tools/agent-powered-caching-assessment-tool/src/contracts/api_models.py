"""
Models for the API's own resources: jobs, status, lists and summaries.

The analysis, sizing and report view-model have their own contracts. These are the
smaller objects a user interface needs around them. Spec: .kiro/specs/ux-api.

Every timestamp is an ISO 8601 UTC string. `owner` is always null until
authentication exists.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.contracts.cache_sizing_output import CacheSizingOutput

JobState = Literal["queued", "running", "complete", "failed", "cancelled"]
PhaseName = Literal["collect", "analyze", "size", "report"]
PhaseState = Literal["running", "complete", "failed"]

LABEL_MAX_LENGTH = 100
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_label(value: str | None) -> str | None:
    """Validate a job label.

    Args:
        value: Text from the caller, or None.

    Returns:
        The label with surrounding space removed, or None for None or blank.

    Raises:
        ValueError: when the label is longer than LABEL_MAX_LENGTH or holds a
            control character.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > LABEL_MAX_LENGTH:
        raise ValueError(f"label must be at most {LABEL_MAX_LENGTH} characters")
    if _CONTROL.search(text):
        raise ValueError("label must not contain control characters")
    return text


class JobOptions(BaseModel):
    """What the caller asked a job to do."""

    use_llm: bool = False
    size: bool = True
    price: bool = True
    region: str = "us-east-1"
    model_id: str | None = Field(default=None, description="The Bedrock model. None: the "
                                 "server's default")
    require_llm: bool = Field(default=False, description="Fail the job when the model was "
                              "requested but could not be used")

    model_config = ConfigDict(extra="ignore")


class JobInput(BaseModel):
    """The submitted input with everything sensitive removed.

    Never holds the SSH target, a key name, the database user or the password file.
    """

    cluster_endpoint: str | None = None
    port: int | None = None
    database_name: str | None = None
    mode: str | None = None
    schemas: list[str] | None = None
    window_minutes: int | None = None
    start_at: str | None = None

    model_config = ConfigDict(extra="ignore")


class JobRecord(BaseModel):
    """What a job is. Written at submit, updated as facts become known."""

    job_id: str
    label: str | None = None
    source: Literal["live", "collection", "unknown"] = "unknown"
    created_at: str
    options: JobOptions = Field(default_factory=JobOptions)
    input: JobInput | None = None
    owner: str | None = Field(default=None, description="Reserved for authentication")
    rerun_of: str | None = Field(
        default=None, description="For a rerun, the id of the job whose collection it used")
    database_id: str | None = Field(
        default=None, description="The saved database this assessment belongs to, if any")
    # Filled in as the job learns them, so a list needs no artifact reads.
    database_name: str | None = None
    collected_at: str | None = None
    workload_verdict: str | None = None
    interpretation_mode: str | None = None
    sizing_status: str | None = None

    model_config = ConfigDict(extra="ignore")

    @field_validator("label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        return clean_label(v)


class PhaseStatus(BaseModel):
    """One phase of a job."""

    phase: str
    status: PhaseState
    started_at: str
    ended_at: str | None = None
    error: str | None = Field(
        default=None, description="Why the phase failed. A sizing failure is recorded "
        "here and in the job's warnings, and does not fail the job.")
    run: int | None = Field(
        default=None, description="Which run of the job this phase belonged to. 1 for the "
        "first run, 2 after the next phase run on the same job, and so on.")

    model_config = ConfigDict(extra="ignore")


class CollectionWindowStatus(BaseModel):
    """Where a live job's collection window is, while it collects."""

    state: Literal["waiting", "measuring", "reading"] = Field(
        description="waiting: before start_at. measuring: between the two readings of the "
        "statistics. reading: the window has ended and the rest is being collected.")
    window_minutes: int | None = None
    starts_at: str | None = Field(default=None, description="ISO 8601, UTC")
    ends_at: str | None = Field(default=None, description="ISO 8601, UTC")
    updated_at: str | None = None

    model_config = ConfigDict(extra="ignore")


class JobStatus(BaseModel):
    """Where a job is."""

    job_id: str
    status: JobState
    current_phase: str | None = None
    phases: list[PhaseStatus] = Field(default_factory=list)
    collection_window: CollectionWindowStatus | None = Field(
        default=None, description="Set during the collect phase of a live job with a "
        "collection window, so a client can show when it starts and ends.")
    created_at: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    error: str | None = None
    note: str | None = Field(
        default=None, description="For a cancelled job, where it stopped. Not an error.")
    run: int | None = Field(default=None, description="The latest run of the job")
    requested: list[str] = Field(
        default_factory=list, description="The phases the latest run was asked to do")
    available: list[str] = Field(
        default_factory=list, description="The phases whose artifact the job holds now, in "
        "pipeline order: collect, analyze, size, report. A job is `complete` when its "
        "requested phases are done, so this says what it has.")
    cancel_requested: bool = Field(
        default=False, description="True while a queued or running job has been asked "
        "to stop and has not yet. A running job stops at its next phase boundary.")
    warnings: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class JobSummary(JobRecord):
    """A job record with its status, for lists."""

    status: JobState = "queued"
    current_phase: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    error: str | None = None
    note: str | None = None
    warnings: list[str] = Field(default_factory=list)
    available: list[str] = Field(
        default_factory=list, description="The phases whose artifact the job holds")


class JobList(BaseModel):
    """One page of jobs, newest first."""

    total: int
    offset: int
    limit: int
    items: list[JobSummary]


class SubmitResponse(BaseModel):
    """The answer to a submit. `queued` means poll the status route."""

    job_id: str
    status: Literal["queued", "complete"]


class PhaseRunResponse(SubmitResponse):
    """The answer to running one phase of an existing job."""

    phase: PhaseName
    run: int = Field(description="The run number of the job this belongs to")
    replaces: list[PhaseName] = Field(
        description="Phases whose artifacts this run deletes, because it starts fresh: the "
        "phase itself and every later phase that has an artifact now")


class SizingSummary(BaseModel):
    """Headline figures of a sizing block. Not a capacity plan and not a quote."""

    status: str
    items_sized: int
    items_not_sized: int
    memory_needed_gb: float
    instance_type: str | None = None
    total_nodes: int | None = None
    cost_available: bool
    monthly_total: float | None = None

    @classmethod
    def from_sizing(cls, s: CacheSizingOutput) -> "SizingSummary":
        """The headline figures of a sizing block."""
        cost_ok = bool(s.cost and s.cost.available)
        return cls(
            status=s.status,
            items_sized=s.totals.items_sized,
            items_not_sized=s.totals.items_not_sized,
            memory_needed_gb=s.totals.memory_needed_gb,
            instance_type=s.node.instance_type if s.node else None,
            total_nodes=s.node.total_nodes if s.node else None,
            cost_available=cost_ok,
            monthly_total=s.cost.monthly_total if cost_ok else None,
        )


class AssessmentSummary(BaseModel):
    """A dashboard tile for one finished job."""

    job_id: str
    label: str | None = None
    database_name: str | None = None
    collected_at: str | None = None
    contract_version: str
    assessment_mode: str
    degraded_reason: str | None = None
    interpretation_mode: str
    fallback_reason: str | None = None
    verdict: str | None = None
    key_finding: str | None = None
    executive_summary: str | None = None
    workload_archetype: str | None = None
    primary_bottleneck: str | None = None
    queries_recommended: int
    query_results_to_cache: int
    key_lookups_to_cache: int
    tables_recommended: int
    cacheable_db_time_ms_per_sec: float | None = None
    bundles: int
    bundles_that_qualify: int
    model_id: str | None = None
    prompt_version: str | None = None
    sizing: SizingSummary | None = None


class Health(BaseModel):
    """Liveness and version."""

    status: str
    service: str
    version: str
    authentication: str


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


class CancelResponse(BaseModel):
    """The answer to a cancel request."""

    job_id: str
    status: JobState = Field(description="`cancelled` when the job never started or was "
                             "already cancelled; `running` while it finishes its phase.")
    cancel_requested: bool
    message: str


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------


class PurgeResponse(BaseModel):
    """What a purge deleted, or with a dry run would delete."""

    dry_run: bool = Field(description="True: nothing was deleted")
    older_than_days: int
    status: Literal["complete", "failed", "cancelled"] | None = None
    count: int
    job_ids: list[str] = Field(description="The jobs deleted, or that would be")


# ---------------------------------------------------------------------------
# What-if sizing
# ---------------------------------------------------------------------------


class SizingOverrides(BaseModel):
    """Sizing constants a what-if may change. Omit one to keep the job's own value.

    The same bounds are published by GET /capabilities. They keep a typing slip
    from producing a nonsense node count, they do not make a value realistic.
    """

    entry_overhead_bytes: int | None = Field(
        default=None, ge=0, le=1024,
        description="Valkey per-key overhead: dict entry, object header, allocator rounding")
    query_key_bytes: int | None = Field(
        default=None, ge=1, le=1024, description="Length of a query-cache key")
    object_key_prefix_bytes: int | None = Field(
        default=None, ge=0, le=256, description="Length of an object-cache key prefix")
    value_encoding_factor: float | None = Field(
        default=None, ge=1.0, le=10.0,
        description="Serialised value size versus raw column widths")
    fragmentation_factor: float | None = Field(
        default=None, ge=1.0, le=3.0, description="Allocator fragmentation on a long-running node")
    growth_headroom: float | None = Field(
        default=None, ge=0.0, le=5.0, description="Room for growth, as a fraction of today's data")
    peak_factor: float | None = Field(
        default=None, ge=0.1, le=20.0,
        description="Traffic multiplier for peak. 1.0 sizes on the window average")
    replicas_per_shard: int | None = Field(
        default=None, ge=0, le=5, description="Replicas per shard")
    ops_per_invalidation: float | None = Field(
        default=None, ge=0.0, le=100.0,
        description="Cache operations needed to invalidate one entry or entry group")

    model_config = ConfigDict(extra="forbid")


class WhatIfChange(BaseModel):
    """One setting that differs between the job's sizing and the what-if."""

    name: str
    baseline: str | int | float | bool | None = None
    scenario: str | int | float | bool | None = None


class WhatIfResult(BaseModel):
    """A sizing run with the caller's settings, next to the job's own.

    The job itself is not changed. Not a capacity plan and not a quote.
    """

    job_id: str
    region: str
    priced: bool = Field(description="Whether the AWS Pricing API was called")
    baseline: SizingSummary | None = Field(
        default=None, description="Headline figures of the job's recorded sizing, "
        "null when sizing did not run for the job")
    scenario_summary: SizingSummary
    scenario: CacheSizingOutput
    changes: list[WhatIfChange]


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class PhaseInfo(BaseModel):
    phase: str
    label: str
    optional: bool
    default_on: bool
    description: str
    needs: list[str] = Field(
        default_factory=list, description="The phases whose artifacts this one reads. "
        "Collect reads the collector input instead.")
    makes: str = Field(default="", description="The file this phase leaves in the job")


class OptionInfo(BaseModel):
    """A submit option and its default."""

    name: str
    type: Literal["boolean", "string"]
    default: bool | str
    description: str


class ModelRule(BaseModel):
    """Which Bedrock model the AI review and the chat use, and what a caller may pass."""

    default: str
    suggestions: list[str] = Field(description="A hint for a picker, default first. Any "
                                   "id matching the pattern is accepted")
    pattern: str
    max_length: int
    note: str


class RegionRule(BaseModel):
    pattern: str
    default: str
    max_length: int
    note: str


class Limits(BaseModel):
    label_max_length: int
    job_id_pattern: str
    job_id_max_length: int
    list_default_limit: int
    list_max_limit: int
    report_page_default_limit: int
    report_page_max_limit: int
    collection_timeout_minutes_min: int
    collection_timeout_minutes_max: int
    window_minutes_min: int
    window_minutes_max: int
    window_start_max_hours: int
    window_sample_interval_seconds: int
    query_pattern_limit_max: int
    max_concurrent_preflights: int
    max_concurrent_priced_what_ifs: int
    max_request_bytes: int
    max_running_jobs: int
    max_queued_jobs: int
    max_databases: int = Field(description="Saved databases the server keeps. Saving one "
                               "more is 409 too_many_databases")
    max_concurrent_host_probes: int = Field(default=2, description="Host probes and trusts "
                                            "at once. One more is 429 host_probe_busy")
    job_retention_days: int | None = Field(
        default=None, description="Finished jobs are deleted after this many days. Null: never.")


class ParameterInfo(BaseModel):
    """A sizing constant a what-if may change."""

    name: str
    type: Literal["integer", "number"]
    default: float
    minimum: float
    maximum: float
    description: str


class Features(BaseModel):
    """Which optional parts of the API this server has."""

    list_and_delete: bool = True
    cancel: bool = True
    preflight: bool = True
    what_if_sizing: bool = True
    report_data: bool = True
    rerun: bool = True
    purge: bool = True
    component_runs: bool = True
    model_framework: bool = Field(default=False, description="strands-agents is installed, "
                                  "so use_llm can run the AI review")
    secrets_manager: bool = Field(default=False, description="Live connections may name a "
                                  "Secrets Manager password secret (password_secret_arn)")


class Capabilities(BaseModel):
    """What this server can do and the values its inputs accept.

    Built from the constants the routes enforce, so it cannot disagree with them.
    It makes no AWS call.
    """

    version: str
    authentication: str
    job_states: list[JobState]
    phases: list[PhaseInfo]
    options: list[OptionInfo]
    region: RegionRule
    model: ModelRule
    limits: Limits
    sizing_parameters: list[ParameterInfo]
    features: Features
    notes: list[str]
