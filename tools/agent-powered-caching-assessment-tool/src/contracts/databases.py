"""
Saved databases: a connection kept once, its readiness, and what its assessments found.

A saved database never holds a password: it names a password source (a file on the
database host, or a Secrets Manager ARN). Spec: .kiro/specs/databases.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.contracts.api_models import LABEL_MAX_LENGTH, clean_label
from src.contracts.collector_input import WINDOW_MINUTES_MAX, WINDOW_MINUTES_MIN
from src.contracts.discovery import DiscoverySource
from src.contracts.models import MODEL_ID_MAX_LENGTH, MODEL_ID_PATTERN
from src.contracts.preflight import PreflightResult
from src.contracts.regions import REGION_MAX_LENGTH, REGION_PATTERN

DATABASE_ID_PATTERN = r"^db-[0-9a-f]{12}$"
Readiness = Literal["unchecked", "ready", "warnings", "degraded", "blocked"]


class DatabaseConnection(BaseModel):
    """How the tool reaches the database: over SSH, to the database host itself."""

    kind: Literal["ssh"] = Field(default="ssh", description="Only SSH today")
    ssh_target: str = Field(max_length=320, description="user@host, a host, or an "
                            "ssh_config name. The host key must be in known_hosts")
    ssh_port: int = Field(default=22, ge=1, le=65535)
    identity_file: str | None = Field(default=None, max_length=128,
                                      description="A key file name inside SSH_KEY_DIR")
    db_user: str = Field(max_length=63, description="The read-only PostgreSQL role")
    password_file: str | None = Field(default=None, max_length=256, description="A file "
                                      "on the database host holding the role's password")
    password_secret_arn: str | None = Field(default=None, max_length=600, description="A "
                                            "Secrets Manager secret holding the password")
    cluster_endpoint: str = Field(default="127.0.0.1", max_length=253,
                                  description="PostgreSQL host as seen from the SSH host")
    port: int = Field(default=5432, ge=1, le=65535)
    database_name: str = Field(max_length=63)
    schemas: list[str] | None = Field(default=None, max_length=50)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _one_password_source(self) -> "DatabaseConnection":
        """Require exactly one password source.

        Returns:
            The connection, unchanged.

        Raises:
            ValueError: none or both of password_file and password_secret_arn are set.
        """
        if (self.password_file is None) == (self.password_secret_arn is None):
            raise ValueError("give exactly one of password_file and password_secret_arn")
        return self


class ReadinessState(BaseModel):
    """What the last check found."""

    status: Readiness = "unchecked"
    checked_at: str | None = None
    database_version: str | None = None
    pg_stat_statements_available: bool | None = None
    failed: list[str] = Field(default_factory=list, description="Names of failed checks")
    warned: list[str] = Field(default_factory=list, description="Names of warned checks")

    model_config = ConfigDict(extra="ignore")


class DatabaseRecord(BaseModel):
    """A saved database, as stored."""

    database_id: str = Field(pattern=DATABASE_ID_PATTERN)
    label: str
    connection: DatabaseConnection
    readiness: ReadinessState = Field(default_factory=ReadinessState)
    created_at: str
    updated_at: str
    discovered_from: DiscoverySource | None = Field(
        default=None, description="The discovery source it was picked from")

    model_config = ConfigDict(extra="ignore")


class LatestAssessment(BaseModel):
    """The newest assessment of a database."""

    job_id: str
    status: str
    created_at: str
    label: str | None = None
    workload_verdict: str | None = None


class DatabaseView(DatabaseRecord):
    """A saved database with what its assessments found, for lists and pages."""

    assessments: int = 0
    running: int = 0
    latest: LatestAssessment | None = None
    latest_complete: LatestAssessment | None = Field(
        default=None, description="The newest complete assessment, for the latest verdict")


class DatabaseList(BaseModel):
    """Every saved database, newest first."""

    total: int
    items: list[DatabaseView]


class DatabaseCreate(BaseModel):
    """Save a database."""

    label: str | None = Field(default=None, max_length=LABEL_MAX_LENGTH,
                              description="Defaults to the database name and host")
    connection: DatabaseConnection
    discovered_from: DiscoverySource | None = Field(
        default=None, description="The discovery source it was picked from. One of the "
        "fixed source names, so no free text reaches the audit log")
    attach_earlier: bool = Field(
        default=False, description="Attach earlier assessments with the same endpoint, port "
        "and database name that belong to no database. Job records do not keep the SSH host, "
        "so only ask for this when they are this database's.")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _label(self) -> "DatabaseCreate":
        """Check the label like a job label.

        Returns:
            The request with the label cleaned.
        """
        self.label = clean_label(self.label)
        return self


class DatabaseCreated(DatabaseView):
    """The saved database, and how many earlier assessments were attached."""

    attached: int = 0


class DatabasePatch(BaseModel):
    """Change a saved database. A new connection clears its readiness."""

    label: str | None = Field(default=None, max_length=LABEL_MAX_LENGTH)
    connection: DatabaseConnection | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _label(self) -> "DatabasePatch":
        """Check a new label like a job label.

        Returns:
            The patch with the label cleaned.
        """
        if self.label is not None:
            self.label = clean_label(self.label)
        return self


class DatabaseCheckResponse(BaseModel):
    """The preflight result and the readiness stored from it."""

    readiness: ReadinessState
    result: PreflightResult


class DatabaseRunRequest(BaseModel):
    """Start an assessment of a saved database: run options only."""

    window_minutes: int = Field(ge=WINDOW_MINUTES_MIN, le=WINDOW_MINUTES_MAX,
                                description="How long to measure")
    start_at: datetime | None = Field(default=None, description="When to start, with a "
                                      "time zone. Omit to start at once")
    label: str | None = Field(default=None, max_length=LABEL_MAX_LENGTH)
    use_llm: bool = False
    model_id: str | None = Field(default=None, pattern=MODEL_ID_PATTERN.pattern,
                                 max_length=MODEL_ID_MAX_LENGTH)
    require_llm: bool = False
    size: bool = True
    price: bool = True
    region: str = Field(default="us-east-1", pattern=REGION_PATTERN.pattern,
                        max_length=REGION_MAX_LENGTH)
    allow_degraded: bool = Field(default=False, description="Run even though "
                                 "pg_stat_statements is not usable (degraded mode)")

    model_config = ConfigDict(extra="forbid")


def readiness_from(result: PreflightResult, checked_at: str) -> ReadinessState:
    """The readiness a preflight result means.

    Args:
        result: The preflight result.
        checked_at: When it ran.

    Returns:
        blocked when any check failed; degraded when pg_stat_statements is not usable;
        warnings for any other warning; ready otherwise.
    """
    failed = [c.name for c in result.checks if c.status == "fail"]
    warned = [c.name for c in result.checks if c.status == "warn"]
    if failed:
        status: Readiness = "blocked"
    elif result.pg_stat_statements_available is False:
        status = "degraded"
    elif warned:
        status = "warnings"
    else:
        status = "ready"
    return ReadinessState(status=status, checked_at=checked_at,
                          database_version=result.database_version,
                          pg_stat_statements_available=result.pg_stat_statements_available,
                          failed=failed, warned=warned)


class RoleCreateRequest(BaseModel):
    """Create, or reuse, the read-only role the tool signs in as, on the database host."""

    ssh_target: str = Field(max_length=320, description="The database host: user@host, a "
                            "host, or a name from ssh_config. Its key must be in known_hosts")
    ssh_port: int = Field(default=22, ge=1, le=65535)
    identity_file: str | None = Field(default=None, max_length=128,
                                      description="A key file name inside SSH_KEY_DIR")
    database_name: str = Field(max_length=63, description="The database to sign in to as "
                               "postgres. The role works in every database of the cluster")
    port: int = Field(default=5432, ge=1, le=65535, description="PostgreSQL's port")
    role_name: str = Field(default="apcat_reader", max_length=63,
                           description="Lowercase letters, digits and _. Reused when it "
                           "exists and its password file is there")
    password_file: str | None = Field(
        default=None, max_length=256,
        description="Absolute path on the host for the new password file. Default "
        "$HOME/.<role_name>_pw. For a role that exists, the file that holds its password")

    model_config = ConfigDict(extra="forbid")


class RoleCreateResult(BaseModel):
    """What role setup did. ok means the role can be used now."""

    ok: bool
    outcome: Literal["created", "reused", "exists_no_file", "role_privileged", "file_exists",
                     "no_sudo", "no_python", "no_database", "old_postgres", "file_failed",
                     "psql_failed", "ssh_failed", "unexpected"]
    message: str = Field(description="Fixed text for the form")
    role_name: str
    password_file: str | None = Field(default=None, description="The password file's "
                                      "absolute path on the host, when known")
