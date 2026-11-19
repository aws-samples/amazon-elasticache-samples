"""
Shared types for the analysis contract.

CacheAnalysisOutputV2 (cache_analysis_output_v2) is the artifact. It reuses the
types defined here: the assessment mode, agent metadata and the per-table
record. The retired v1 top-level model lived in this file; see
.kiro/specs/v2-consolidation.

Design principles carried over from upstream:
  - Include everything, require only the essentials.
  - Every score is auditable: the signals that produced it travel with it.
  - Thresholds are recorded in the output so a score is reproducible.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CacheAssessmentMode(StrEnum):
    """How complete the underlying data was.

    FULL     — pg_stat_statements available; query-level signals present.
    DEGRADED — pg_stat_statements unavailable; scored from table, global, and
               host signals only, with confidence capped at 60.
    """

    FULL = "full"
    DEGRADED = "degraded"


class AgentMetadata(BaseModel):
    agent_name: str
    agent_version: str
    analysis_timestamp: datetime
    analysis_duration_seconds: float

    model_config = ConfigDict(extra="ignore")


class CacheTableRecommendation(BaseModel):
    """Per-table cache-candidacy result with full score provenance."""

    table_id: str = Field(description="Fully qualified table identifier")
    verdict: str = Field(description="STRONG_CANDIDATE | MODERATE_CANDIDATE | WEAK_CANDIDATE | NOT_RECOMMENDED")
    confidence_score: int = Field(ge=0, le=100)

    # --- The four dimensions ---
    access_suitability_score: int = Field(
        ge=0, le=100, description="Read dominance, frequency, access skew"
    )
    pressure_evidence_score: int = Field(
        ge=0, le=100, description="Buffer misses, disk I/O, spill, host pressure"
    )
    cache_efficiency_score: int | None = Field(
        default=None, ge=0, le=100,
        description="How efficiently a cache would serve the reads: estimated hit "
        "rate (75%) and result size (25%). None when not scored, never 0 for "
        "missing data.",
    )
    efficiency: dict | None = Field(
        default=None,
        description="Cache Efficiency detail: calls_in_window, window_seconds, k, "
        "k_source, estimated_hit_rate, reuse_score, reuse_deferred_reason, "
        "result_bytes, time_saved_ms, bytes_factor, result_size_score, status, and "
        "any not-scored reason.",
    )
    disqualifier_penalty: int = Field(
        default=0, ge=0, description="Positive penalty subtracted from the weighted score"
    )

    hard_excluded: bool = Field(
        default=False,
        description="True when it cannot be recommended: see exclusion_kind",
    )
    exclusion_kind: str | None = Field(
        default=None,
        description="well_served: the one disqualifier hard exclusion (already "
        "well served and not busy). not_cacheable: a parser gate. None otherwise.",
    )
    penalties: list[dict] = Field(
        default_factory=list,
        description="Each soft penalty: {points, reason}. Sums to disqualifier_penalty.",
    )
    busy_but_fast: bool = Field(
        default=False,
        description="Meets every well-served criterion but uses at least the busy "
        "line of host CPU capacity. Penalised and reviewed by the agent.",
    )
    db_share_of_capacity: float | None = Field(
        default=None, description="DB time / (vCPU x 1000 ms/s), capped at 1.0. "
        "DB time is wall-clock, so under saturation it includes CPU queueing.")
    bundles: list[dict] = Field(
        default_factory=list,
        description="Bundles this table is in (spec cache-bundles): Not cacheable "
        "queries the application could compute from cached rows. Each: queryid, "
        "db_time_share_pct, qualifies, other_tables, this_table_qualifies, "
        "this_table_reason. No score effect.")
    well_served: dict | None = Field(
        default=None, description="The five well-served criteria and whether each held")
    exclusion_reason: str | None = None
    exclusion_reasons: list[str] = Field(
        default_factory=list,
        description="Every hard exclusion that applied, in precedence order. The "
        "first is exclusion_reason. Hard exclusions add no penalty points.",
    )

    # --- Auditability ---
    evidence: list[str] = Field(
        default_factory=list, description="Signals supporting caching"
    )
    concerns: list[str] = Field(
        default_factory=list, description="Signals arguing against caching"
    )
    rationale: str = Field(default="", description="Deterministic prose summary")
    notes: list[str] = Field(
        default_factory=list, description="Provenance notes, e.g. degraded-mode caveats"
    )

    # --- Raw signals echoed for report transparency ---
    read_ratio_weighted: float | None = Field(
        default=None, description="Frequency-weighted read share of calls (0-1)"
    )
    calls_per_second: float | None = None
    buffer_hit_ratio_pct: float | None = None
    weighted_exec_time_ms: float | None = None
    size_mb: float | None = None

    # --- Added for the v2 report, so tables and queries show the same metrics.
    # Optional: v1 never sets them and older artifacts load without them.
    read_calls_per_second: float | None = None
    write_calls_per_second: float | None = Field(
        default=None, description="Measured write statements/s from parsed write targets"
    )
    db_time_ms_per_sec_incl_joins: float | None = Field(
        default=None,
        description="Database time of every analysed read that reads this table. "
        "A join counts toward each table it reads, so values overlap across tables "
        "and must not be summed.",
    )

    model_config = ConfigDict(extra="ignore")
