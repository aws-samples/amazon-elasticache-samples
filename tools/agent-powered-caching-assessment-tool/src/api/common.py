"""Shared pieces for the API routes: where artifacts live and who may ask for them."""

import os
from typing import Annotated

from fastapi import Path

from src.api.errors import ApiError
from src.orchestrator.job_store import JOB_ID_PATTERN, LocalJobStore, valid_job_id
from src.orchestrator.local_orchestrator import (
    DEFAULT_SIZING_REGION,
    REGION_PATTERN,
    LocalOrchestrator,
)

ARTIFACT_ROOT = os.environ.get("ARTIFACT_ROOT", "./artifacts")

# A job id in a URL. The pattern rejects separators and a leading dot at the edge;
# the orchestrator checks it again before it names a directory.
JobId = Annotated[str, Path(pattern=JOB_ID_PATTERN.pattern, max_length=64)]

# The region a job is priced in. Checked here so a bad value is a 422 before any job
# is queued, and again by the orchestrator.
RegionPattern = REGION_PATTERN.pattern
REGION_MAX_LENGTH = 32


AUTHENTICATION = "none: loopback only"

# Concurrency caps on the routes that call out to AWS. GET /capabilities publishes them.
MAX_CONCURRENT_PREFLIGHTS = 4
MAX_CONCURRENT_PRICED_WHAT_IFS = 2
# Chat turns: each calls Bedrock with the server's credentials and holds a thread.
MAX_CONCURRENT_AGENT_TURNS = 4
# Database discovery: each runs local commands and may open one SSH connection.
MAX_CONCURRENT_DISCOVERIES = 2
# Host probes and trusts share these slots: each opens SSH to a host the caller names.
MAX_CONCURRENT_HOST_PROBES = 2

# Page sizes. The routes enforce these and GET /capabilities publishes them.
LIST_DEFAULT_LIMIT = 25
LIST_MAX_LIMIT = 100
PAGE_DEFAULT_LIMIT = 25
PAGE_MAX_LIMIT = 200


def job_store() -> LocalJobStore:
    """The job store over ARTIFACT_ROOT: records, state, list, delete."""
    return LocalJobStore(ARTIFACT_ROOT)


def orchestrator(use_llm: bool = False, size: bool = True, price: bool = True,
                 region: str = DEFAULT_SIZING_REGION, model_id: str | None = None,
                 require_llm: bool = False) -> LocalOrchestrator:
    """An orchestrator over ARTIFACT_ROOT.

    Args:
        use_llm: Whether ANALYZE runs the AI agent.
        size: Whether the SIZE phase runs.
        price: Whether SIZE calls the AWS Pricing API. This makes the server call
            AWS with its own credentials, so it runs for every sized job unless the
            caller passes price=False.
        region: Region to price in.
        model_id: The Bedrock model for ANALYZE. None: the server's default.
        require_llm: Fail ANALYZE when the model was requested but could not be used.
    """
    return LocalOrchestrator(ARTIFACT_ROOT, use_llm=use_llm, size=size, price=price,
                             model_id=model_id, require_llm=require_llm,
                             sizing_region=region)


def require_valid_job_id(job_id: str) -> str:
    """Return job_id, or raise a 422 when it cannot be a directory name."""
    if not valid_job_id(job_id):
        raise ApiError(
            422, "invalid_job_id",
            "invalid job id: 1 to 64 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit",
        )
    return job_id
