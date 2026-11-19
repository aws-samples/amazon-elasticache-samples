"""
Collector Output Contract.

Wraps what the collector previously returned as a bare dict. The dict was
deliberate during template phase, while the scoring module's expectations were
still moving, but it left the largest artifact in the pipeline unvalidated.

Design choices carried from the rest of the codebase:

  - `extra="allow"` on the nested blocks. PostgreSQL statistics views differ by
    version, and the collector passes through whatever a view returned. Being
    strict here would break on a version change for no benefit.

  - Almost everything is optional. Any collection stage can fail without
    aborting the run, so a partial artifact must still validate. A contract
    that rejects partial collection would defeat the point of `_safe()`.

  - Nothing is required that a degraded run cannot produce. Query patterns,
    host metrics, and per-table detail are all absent in legitimate scenarios.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# Nested blocks mirror raw PostgreSQL output, which varies by version.
_PASSTHROUGH = ConfigDict(extra="allow")


class DeploymentType(StrEnum):
    """Where the database runs.

    The upstream tool only ever set rds_instance, which was wrong for the
    deployment type this tool exists to assess.
    """

    SELF_MANAGED = "self_managed"
    EC2_SELF_MANAGED = "ec2_self_managed"
    ON_PREMISES = "on_premises"
    RDS_INSTANCE = "rds_instance"
    UNKNOWN = "unknown"


class PgStatStatementsProbe(BaseModel):
    """Result of checking whether per-query statistics are available.

    Drives FULL versus DEGRADED assessment mode, so it is recorded explicitly
    rather than inferred from an empty query list.
    """

    available: bool = Field(
        default=False, description="True only when preloaded AND the extension exists"
    )
    in_preload: bool = Field(
        default=False, description="Present in shared_preload_libraries"
    )
    extension_installed: bool = Field(
        default=False, description="CREATE EXTENSION has been run in this database"
    )
    reason: str | None = Field(
        default=None,
        description="Which remediation is needed. Distinguishes the "
        "shared_preload_libraries change, which requires a server restart, from "
        "CREATE EXTENSION, which does not.",
    )

    model_config = _PASSTHROUGH


class SchemaInfo(BaseModel):
    """A discovered schema and how many user tables it holds."""

    schema_name: str | None = None
    table_count: int = 0

    model_config = _PASSTHROUGH


class SourceDatabase(BaseModel):
    """Identity and shape of the assessed database."""

    engine: str = Field(default="postgresql")
    version: str | None = None
    hostname: str | None = None
    database_name: str | None = None
    database_size_gb: float | None = None
    deployment_type: DeploymentType = Field(default=DeploymentType.UNKNOWN)

    model_config = _PASSTHROUGH


class CollectionMetadata(BaseModel):
    """When the collection ran, how long it took, and where time went."""

    collection_timestamp: datetime | None = None
    collector_version: str | None = None
    collection_duration_seconds: float | None = None
    source_database: SourceDatabase = Field(default_factory=SourceDatabase)

    stage_timings_seconds: dict[str, float] = Field(
        default_factory=dict, description="Per-stage elapsed time, slowest first"
    )
    stage_timings_accounted_seconds: float | None = None
    stage_timings_unaccounted_seconds: float | None = None

    model_config = _PASSTHROUGH


class TableInfo(BaseModel):
    """One table's metadata.

    `table_id` is the canonical "schema.table" identifier everything downstream
    joins on. Query-pattern table references are resolved to it.
    """

    table_id: str
    table_name: str | None = None
    schema_name: str | None = None
    row_count: int = 0
    size_mb: float = 0.0
    data_size_mb: float | None = None
    index_size_mb: float | None = None
    full_table_scans: int | None = Field(
        default=None, description="seq_scan from pg_stat_user_tables"
    )
    index_scans: int | None = None
    columns: list[dict] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    reltuples: float | None = Field(
        default=None,
        description="pg_class.reltuples, the planner row estimate. Survives "
        "pg_stat_reset. None when never analyzed (-1) or not collected.",
    )
    column_stats: dict[str, dict] = Field(
        default_factory=dict,
        description="Column name -> {n_distinct, avg_width} from pg_stats. "
        "n_distinct is raw: negative means a fraction of rows.",
    )
    column_widths: dict[str, dict] = Field(
        default_factory=dict,
        description="Column name -> {typlen, typmod, type_name, not_null} from "
        "pg_attribute (collector 0.3.0). typlen > 0 is a fixed width in bytes; "
        "-1/-2 is variable. Used by sizing when pg_stats has no avg_width.",
    )

    model_config = _PASSTHROUGH


class DatabaseSchema(BaseModel):
    tables: list[TableInfo] = Field(default_factory=list)
    views: list[dict] = Field(
        default_factory=list,
        description="pg_views rows: schema_name, view_name, definition, owner. "
        "Analysis parses each definition to charge a query on a view with the "
        "write rates of the view's base tables.",
    )

    model_config = _PASSTHROUGH


class QueryPattern(BaseModel):
    """One normalised query from pg_stat_statements.

    Absent in DEGRADED mode, so every field is optional.
    """

    query_id: str | None = None
    query_text: str | None = None
    query_type: str | None = None
    calls_per_second: float | None = None
    frequency_per_hour: float | None = None

    tables_accessed: list[str] = Field(
        default_factory=list, description="Resolved canonical table_ids"
    )
    tables_accessed_raw: list[str] = Field(
        default_factory=list,
        description="References as written in the query, before resolution. "
        "Kept for debugging when resolution fails.",
    )

    execution_time_ms_avg: float | None = None
    total_time_ms: float | None = Field(
        default=None,
        description="Cumulative execution time. Required to compute the disk-time "
        "share, which must compare cumulative against cumulative.",
    )
    rows_returned_avg: float | None = None

    cache_hit_ratio_pct: float | None = None
    shared_blks_hit: int | None = None
    shared_blks_read: int | None = None
    io_read_time_ms: float | None = None
    temp_blocks_written: int | None = None

    model_config = _PASSTHROUGH


class Queries(BaseModel):
    query_patterns: list[QueryPattern] = Field(default_factory=list)
    total_queries_analyzed: int = 0
    query_log_source: str | None = None

    model_config = _PASSTHROUGH


class CollectedMetrics(BaseModel):
    """Pressure and churn signals.

    Left as loose dicts rather than typed models: these mirror PostgreSQL
    statistics views whose columns differ across versions, and typing them would
    force a contract change for every version difference. `pg_stat_bgwriter`
    alone changed shape in PostgreSQL 17.
    """

    global_stats: dict = Field(default_factory=dict, description="From pg_stat_database")
    bgwriter: dict = Field(default_factory=dict, description="From pg_stat_bgwriter")
    wait_events: list[dict] = Field(
        default_factory=list, description="Point-in-time sample from pg_stat_activity"
    )
    lock_contention: list[dict] = Field(
        default_factory=list, description="Blocked/blocking pairs from pg_locks"
    )
    table_churn: list[dict] = Field(
        default_factory=list, description="Dead tuples and DML counters per table"
    )
    configuration: dict = Field(
        default_factory=dict,
        description="Memory settings. shared_buffers is required to interpret a "
        "buffer hit ratio correctly.",
    )

    model_config = _PASSTHROUGH


class HostMetrics(BaseModel):
    """Host OS metrics.

    Exists because CloudWatch is RDS-only. For self-managed PostgreSQL these are
    read directly from the host, and without them the capacity-relief half of
    Pressure Evidence cannot be scored.
    """

    collection_method: str | None = None
    cpu: dict | None = Field(
        default=None, description="Sampled twice and differenced, not a single read"
    )
    memory: dict | None = None
    load_average: dict | None = None
    swap: dict | None = None
    disk: dict | None = None
    disk_extended: dict | None = None

    iostat_mode: str | None = Field(
        default=None, description="extended, basic, or none"
    )
    sysstat_available: bool = Field(
        default=False, description="True only for GNU sysstat with -x support"
    )
    executor_type: str | None = Field(
        default=None,
        description="Which executor produced these. With a container executor "
        "the numbers describe the container's kernel view, not a dedicated "
        "database host, and must not be read as production-representative.",
    )
    probe_timings_seconds: dict[str, float] = Field(default_factory=dict)
    probe_timings_note: str | None = None
    collection_warnings: list[str] = Field(default_factory=list)

    snapshot_alignment: dict | None = Field(
        default=None,
        description="Whether the instantaneous CPU reading agrees with the "
        "5-minute load average. representative=False means the reading was "
        "taken after the busy period and must not be read as headroom.",
    )
    window: dict | None = Field(
        default=None,
        description="Host samples taken across the load window by HostSampler. "
        "When present, scoring uses these instead of the snapshot, because "
        "they describe the same period as the query statistics.",
    )

    model_config = _PASSTHROUGH


class CollectorOutput(BaseModel):
    """Top-level collection artifact.

    Only `job_id` is required. Every other field has a default so that a run
    where several stages failed still validates and can still be assessed,
    which is the behavior `_safe()` exists to provide.
    """

    contract_version: str = Field(default="1.0", pattern=r"^\d+\.\d+$")
    job_id: str

    metadata: CollectionMetadata = Field(default_factory=CollectionMetadata)
    pg_stat_statements_probe: PgStatStatementsProbe = Field(
        default_factory=PgStatStatementsProbe
    )

    schemas_discovered: list[SchemaInfo] = Field(default_factory=list)
    schemas_collected: list[str] = Field(default_factory=list)

    database_schema: DatabaseSchema = Field(default_factory=DatabaseSchema)
    queries: Queries = Field(default_factory=Queries)
    metrics: CollectedMetrics = Field(default_factory=CollectedMetrics)
    host_metrics: HostMetrics | None = None

    model_config = ConfigDict(extra="allow")

    # ------------------------------------------------------------------
    # Convenience accessors used by the scoring and reporting layers
    # ------------------------------------------------------------------

    @property
    def is_degraded(self) -> bool:
        """True when per-query statistics were unavailable."""
        return not self.pg_stat_statements_probe.available

    @property
    def table_count(self) -> int:
        return len(self.database_schema.tables)

    @property
    def total_size_mb(self) -> float:
        return round(sum(t.size_mb for t in self.database_schema.tables), 2)
