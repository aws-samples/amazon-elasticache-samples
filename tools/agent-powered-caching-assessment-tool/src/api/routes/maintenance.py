"""
Maintenance: delete old finished jobs on request.

The same deletion the retention sweep does, with a dry run that is on unless the caller
turns it off. Spec: .kiro/specs/ux-api (UX-36).
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from src.api import audit, retention
from src.api.common import job_store
from src.api.errors import error_responses
from src.contracts.api_models import PurgeResponse

router = APIRouter()


class PurgeRequest(BaseModel):
    """Which finished jobs to delete."""

    # strict: this deletes data, so "30" or "no" is refused rather than read as 30 or false.
    older_than_days: int = Field(
        strict=True, ge=1, le=3650,
        description="Delete jobs that finished more than this many days ago")
    status: Literal["complete", "failed", "cancelled"] | None = Field(
        default=None, description="Only jobs with this status. Default: all finished jobs")
    dry_run: bool = Field(
        default=True, strict=True,
        description="True (the default) lists what would be deleted and deletes nothing. "
        "Send false to delete.")

    model_config = ConfigDict(extra="forbid")


@router.post("/assessments/purge", response_model=PurgeResponse,
             responses=error_responses(413))
def purge_assessments(body: PurgeRequest) -> PurgeResponse:
    """Delete finished jobs older than a number of days, or list them first.

    A queued or running job is never touched. A deleted job's artifacts are gone for good.
    The default is a dry run, so a first call only shows what a second call would delete.
    """
    ids = retention.purge(job_store(), body.older_than_days, status=body.status,
                          dry_run=body.dry_run)
    audit.record("jobs_purged", dry_run=body.dry_run, older_than_days=body.older_than_days,
                 status=body.status, count=len(ids), job_ids=ids)
    return PurgeResponse(dry_run=body.dry_run, older_than_days=body.older_than_days,
                         status=body.status, count=len(ids), job_ids=ids)
