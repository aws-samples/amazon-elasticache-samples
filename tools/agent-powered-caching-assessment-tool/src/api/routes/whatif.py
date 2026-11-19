"""
What-if sizing: run Sizing & Cost again on a finished job with other settings.

It changes nothing about the job. Spec: .kiro/specs/ux-api (UX-25 to UX-28).
"""

import logging
import threading

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from src.agents.sizing.whatif import run_whatif
from src.api import audit
from src.api.common import (
    MAX_CONCURRENT_PRICED_WHAT_IFS,
    REGION_MAX_LENGTH,
    JobId,
    RegionPattern,
    job_store,
    orchestrator,
)
from src.api.errors import ApiError, error_responses
from src.contracts.api_models import SizingOverrides, WhatIfResult
from src.orchestrator.local_orchestrator import DEFAULT_SIZING_REGION, Phase

logger = logging.getLogger(__name__)
router = APIRouter()

# A priced run calls the AWS Pricing API with the server's credentials.
_priced_slots = threading.BoundedSemaphore(MAX_CONCURRENT_PRICED_WHAT_IFS)


class SizingWhatIf(BaseModel):
    """Settings for a what-if. Anything omitted is what the job used."""

    region: str | None = Field(
        default=None, pattern=RegionPattern, max_length=REGION_MAX_LENGTH,
        description="AWS region to price in. Defaults to the job's region.")
    price: bool | None = Field(
        default=None,
        description="Call the AWS Pricing API. Defaults to what the job asked for. "
        "False never calls AWS; node specs then come from a built-in table, so the "
        "result is not comparable with a priced baseline.")
    parameters: SizingOverrides = Field(default_factory=SizingOverrides)

    model_config = ConfigDict(extra="forbid")


def _artifact(job_id: str, phase: Phase, filename: str) -> dict:
    try:
        return orchestrator().load(job_id, phase, filename)
    except (FileNotFoundError, ValueError) as e:
        raise ApiError(
            404, "artifact_not_available",
            f"Artifact not available for job {job_id} (phase={phase.value})") from e


@router.post("/assessments/{job_id}/sizing", response_model=WhatIfResult,
             responses=error_responses(404, 413, 429))
def what_if_sizing(job_id: JobId, body: SizingWhatIf | None = None) -> WhatIfResult:
    """Size the job again with other settings, without changing the job.

    Runs only the Sizing & Cost step on the saved analysis and collection. The job's
    own sizing, its analysis and its report are not changed, and nothing is saved:
    the result is returned. Settings left out are what the job used, so send only
    what you want to try.

    Not a capacity plan and not a quote. A priced run calls the AWS Pricing API with
    the server's credentials, and at most two run at once (429 beyond that).

    404 when the job has no analysis. 422 for a value out of range or an unknown
    setting; the ranges are in GET /capabilities.
    """
    body = body or SizingWhatIf()
    analysis = _artifact(job_id, Phase.ANALYZE, "analysis.json")
    collection = _artifact(job_id, Phase.COLLECT, "output.json")
    try:
        baseline = orchestrator().load(job_id, Phase.SIZE, "sizing.json")
    except (FileNotFoundError, ValueError):
        baseline = None

    record = job_store().get(job_id)
    base_region = record.options.region if record else DEFAULT_SIZING_REGION
    base_price = record.options.price if record else True
    region = body.region or base_region
    price = base_price if body.price is None else body.price

    if price and not _priced_slots.acquire(blocking=False):
        raise ApiError(429, "what_if_busy",
                       "Too many priced what-ifs are running. Try again shortly.",
                       headers={"Retry-After": "10"})
    if price:  # it calls the AWS Pricing API with the server's credentials
        audit.record("what_if_priced", job_id=job_id, aws_region=region)
    try:
        return run_whatif(
            job_id, analysis, collection, baseline, region=region, price=price,
            base_region=base_region, base_price=base_price,
            overrides=body.parameters.model_dump())
    except ValueError as e:
        logger.warning("What-if for %s failed: %s", job_id, e)
        raise ApiError(500, "stored_data_invalid",
                       "stored analysis or sizing is not valid") from e
    finally:
        if price:
            _priced_slots.release()
