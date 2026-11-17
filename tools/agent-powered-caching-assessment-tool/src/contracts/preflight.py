"""
The answer to a preflight: can this job work, before it is queued.

Messages are fixed text chosen by the server. They never carry the text of an ssh or database
error, because that text can name hosts, users and paths. Spec: .kiro/specs/ssh-collection.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CheckStatus = Literal["pass", "fail", "warn", "skipped"]


class PreflightCheck(BaseModel):
    """One thing that was checked."""

    name: Literal["secret", "ssh", "database", "database_host", "pg_stat_statements"]
    status: CheckStatus
    message: str
    hint: str | None = Field(default=None, description="What to do about a fail or warn")

    model_config = ConfigDict(extra="ignore")


class PreflightResult(BaseModel):
    """What a preflight found."""

    ok: bool = Field(description="False when any check failed. A warning does not fail it.")
    checks: list[PreflightCheck]
    database_version: str | None = None
    pg_stat_statements_available: bool | None = Field(
        default=None, description="False means the job would run in degraded mode")
    duration_seconds: float

    model_config = ConfigDict(extra="ignore")
