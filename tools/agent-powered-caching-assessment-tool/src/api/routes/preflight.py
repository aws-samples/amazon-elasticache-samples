"""
Preflight: check a live submission before it is queued.

Like a submit, it makes the server open an SSH connection to the host the caller names,
so it inherits the loopback gate and must be covered by authentication and target
authorization when those exist. Spec: .kiro/specs/ssh-collection.
"""

import logging
import threading

from fastapi import APIRouter
from pydantic import Field

from src.agents.collector.preflight import run_preflight
from src.agents.collector.target_checks import target_problems
from src.api import audit
from src.api.common import MAX_CONCURRENT_PREFLIGHTS
from src.api.errors import ApiError, error_responses
from src.contracts.collector_input import CollectorInput
from src.contracts.preflight import PreflightResult

logger = logging.getLogger(__name__)
router = APIRouter()

_slots = threading.BoundedSemaphore(MAX_CONCURRENT_PREFLIGHTS)


class PreflightRequest(CollectorInput):
    """The body of a live submit. `job_id` is not needed."""

    job_id: str = Field(default="preflight", description="Not used; no job is created")


@router.post("/assessments/preflight", response_model=PreflightResult,
             responses=error_responses(413, 429))
def preflight(req: PreflightRequest) -> PreflightResult:
    """Check whether a live assessment could work, before queueing it.

    Checks, in order: that SSH connects (the host key is in known_hosts and the key is
    accepted), that the database answers through psql on that host with the password file,
    that the SSH host is the machine PostgreSQL runs on, and that pg_stat_statements is
    usable. Starts no job and writes nothing.

    The answer is 200 with `ok` false when a check failed: the request itself worked.
    Messages are fixed text and never carry the ssh or database error. 422 when a
    value is not in an accepted form. 429 when four preflights are already running.
    """
    problems = target_problems(req)
    if problems:
        audit.record("submit_refused", route="preflight", reason="invalid_target",
                     fields=[p.split(" ")[0] for p in problems])
        raise ApiError(422, "invalid_target", "; ".join(problems))
    if not _slots.acquire(blocking=False):
        raise ApiError(429, "preflight_busy",
                       "Too many preflights are running. Try again shortly.",
                       headers={"Retry-After": "10"})
    try:
        result = run_preflight(req)
    finally:
        _slots.release()
    audit.record(
        "preflight", endpoint=req.cluster_endpoint, port=req.port, database=req.database_name,
        ok=result.ok, failed=[c.name for c in result.checks if c.status == "fail"])
    return result
