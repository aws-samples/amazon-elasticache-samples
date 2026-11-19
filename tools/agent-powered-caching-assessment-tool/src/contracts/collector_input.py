"""
Collector Input Contract — PostgreSQL caching assessment.

Version: 1.0

Adapted from the upstream multi-engine contract. Changes:
  - DatabaseEngine collapsed to postgresql only (v1 scope).
  - DDL and offline collection modes REMOVED. A caching assessment is driven by
    runtime statistics; parsing a DDL dump yields no access patterns, no buffer
    hit ratios, and no host metrics, so those modes cannot produce an assessment.
  - Sample-data options REMOVED. We need access statistics, not row contents,
    which also removes the PII-handling burden.
  - Host-metrics collection toggle ADDED.
  - No AWS block: CloudWatch/Performance Insights are RDS-only and unavailable for
    self-managed PostgreSQL, and the database is reached over SSH, not through AWS.

Spec: .kiro/specs/ssh-collection.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The collection window (spec .kiro/specs/collection-window). Bounds the API publishes
# through GET /capabilities.
WINDOW_MINUTES_MIN = 5
WINDOW_MINUTES_MAX = 240
WINDOW_START_MAX_HOURS = 24
WINDOW_SAMPLE_INTERVAL_SEC = 15


class DatabaseEngine(StrEnum):
    """v1 targets PostgreSQL only.

    Retained as an enum rather than dropped so that adding MySQL in a later
    version does not require a breaking contract change.
    """

    postgresql = "postgresql"


class CollectionMode(StrEnum):
    """Only live collection is supported.

    DDL and offline modes from the upstream tool are intentionally absent: they
    provide schema without runtime statistics, and a caching assessment is
    entirely statistics-driven.
    """

    live = "live"


class LiveConnectionConfig(BaseModel):
    """DB and host access over SSH, to the database host itself.

    `psql` and the host-metric commands run on that host, so it must be the database host,
    not a jump host: the OS metrics describe the machine connected to. Preflight proves it.
    The host key must already be in the server's known_hosts file.
    """

    ssh_target: str = Field(
        description="The database host: `user@host`, a host, or a name from ssh_config"
    )
    ssh_port: int = Field(default=22, ge=1, le=65535, description="The SSH port")
    identity_file: str | None = Field(
        default=None,
        description="Name of a private key in the server's SSH key directory "
        "(SSH_KEY_DIR). A name, never a path. When omitted, ssh uses its own defaults.",
    )
    db_user: str = Field(description="PostgreSQL role to connect as")
    password_file: str | None = Field(
        default=None,
        description="Absolute path ON the database host to a file holding the role's "
        "password. The host reads it; the password never reaches this server or the API. "
        "Give exactly one of password_file and password_secret_arn.")
    password_secret_arn: str | None = Field(
        default=None, max_length=600,
        description="ARN of an AWS Secrets Manager secret holding the role's password, as "
        "text or JSON with a password key. The server reads it with its own credentials "
        "and sends it to psql over the SSH session, never on a command line. Only secrets "
        "under the server's SECRET_ARN_ALLOWED_PREFIXES are accepted. Give exactly one of "
        "password_file and password_secret_arn.")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _one_password_source(self) -> "LiveConnectionConfig":
        if (self.password_file is None) == (self.password_secret_arn is None):
            raise ValueError("give exactly one of password_file and password_secret_arn")
        return self


class CollectionOptions(BaseModel):
    """What to collect."""

    collect_query_patterns: bool = Field(
        default=True,
        description="Collect per-query statistics from pg_stat_statements. "
        "Automatically skipped when the extension is unavailable, which puts the "
        "assessment into DEGRADED mode.",
    )
    collect_host_metrics: bool = Field(
        default=True,
        description="Collect OS-level CPU/memory/disk/swap metrics from the "
        "database host. Required to score the capacity-relief portion of the "
        "Pressure Evidence dimension.",
    )
    collect_lock_contention: bool = Field(
        default=True,
        description="Collect pg_locks and pg_stat_activity wait events, used by "
        "the Disqualifiers dimension to distinguish I/O-bound slowness (which a "
        "cache fixes) from lock-bound slowness (which it does not).",
    )
    schemas: list[str] | None = Field(
        default=None,
        description="Schemas to collect from. When None (the default) the "
        "collector auto-discovers every non-system schema containing user "
        "tables, so it works against a database whose layout is unknown. Set "
        "this to narrow collection to specific schemas.",
    )
    min_query_calls: int = Field(
        default=10,
        ge=1,
        description="Ignore query patterns below this cumulative call count",
    )
    query_pattern_limit: int = Field(
        default=1000, ge=1, le=5000, description="Max query patterns to collect"
    )
    window_minutes: int | None = Field(
        default=None, ge=WINDOW_MINUTES_MIN, le=WINDOW_MINUTES_MAX,
        description="Length of the collection window. The collector reads the statistics "
        "at the start and at the end of the window and scores the difference, so rates, "
        "pressure and exclusions describe that period only. The host is sampled every "
        f"{WINDOW_SAMPLE_INTERVAL_SEC} seconds during it. Required for a live job "
        "submitted through the API. When omitted (scripts only), the counters since the "
        "last statistics reset are used, as before.")
    start_at: datetime | None = Field(
        default=None,
        description="When the window starts, with a time zone. Omit to start at once. "
        f"At most {WINDOW_START_MAX_HOURS} hours ahead. The job waits as `running` until "
        "then and holds a running slot while it waits.")

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def _start_needs_window(self) -> "CollectionOptions":
        if self.start_at is not None and self.window_minutes is None:
            raise ValueError("start_at needs window_minutes")
        if self.start_at is not None and self.start_at.tzinfo is None:
            raise ValueError("start_at needs a time zone, for example 2026-10-29T14:00:00Z")
        return self


class CollectorInput(BaseModel):
    """Input contract for the PostgreSQL caching-assessment collector."""

    contract_version: str = Field(default="1.0", pattern=r"^\d+\.\d+$")
    job_id: str = Field(description="Unique job identifier")

    engine: DatabaseEngine = Field(
        default=DatabaseEngine.postgresql, description="Source engine (PostgreSQL in v1)"
    )
    cluster_endpoint: str = Field(
        description="PostgreSQL host, as seen FROM the SSH host (usually 127.0.0.1)"
    )
    port: int = Field(default=5432, description="PostgreSQL port")
    database_name: str = Field(description="Database to analyze")

    mode: CollectionMode = Field(default=CollectionMode.live)
    live_config: LiveConnectionConfig | None = Field(
        default=None, description="Required when mode=live"
    )

    collection_options: CollectionOptions = Field(default_factory=CollectionOptions)

    timeout_minutes: int = Field(default=120, ge=10, le=720)

    @model_validator(mode="after")
    def validate_mode_config(self):
        if self.mode == CollectionMode.live and not self.live_config:
            raise ValueError("live_config is required when mode=live")
        return self

    model_config = ConfigDict(
        # Unknown fields are refused, so a client still sending the fields of the old
        # access method gets a 422 instead of having them silently ignored.
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "job_id": "cache-assess-001",
                    "engine": "postgresql",
                    "cluster_endpoint": "127.0.0.1",
                    "port": 5432,
                    "database_name": "production",
                    "mode": "live",
                    "live_config": {
                        "ssh_target": "ec2-user@10.0.1.50",
                        "ssh_port": 22,
                        "identity_file": "apcat_ed25519",
                        "db_user": "apcat_ro",
                        "password_file": "/home/ec2-user/.apcat-db-password",  # nosec B105 - an example path, not a password
                    },
                    "collection_options": {
                        "collect_query_patterns": True,
                        "collect_host_metrics": True,
                        "collect_lock_contention": True,
                    },
                }
            ]
        },
    )
