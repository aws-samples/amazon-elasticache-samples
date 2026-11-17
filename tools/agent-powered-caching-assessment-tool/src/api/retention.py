"""
Retention: delete finished jobs once they are old enough.

A job holds query text, which may contain literals, so it should not live forever. This is
off unless JOB_RETENTION_DAYS is set to 1 or more, because deleting data on its own is a
choice for the person running the server to make. When it is on, finished jobs (complete,
failed or cancelled) older than that many days, counted from when they finished, are
deleted at start-up and then every RETENTION_SWEEP_SECONDS (default 3600, at least 60).
A queued or running job is never deleted. A sweep that deletes something is audited.

`purge` is the same deletion on request (POST /assessments/purge), with a dry run.

Spec: .kiro/specs/ux-api (UX-35, UX-36).
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import anyio.to_thread

from src.api import audit
from src.api.common import job_store
from src.api.limits import env_int
from src.orchestrator.job_store import JobStore

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_SECONDS = 3600


def settings() -> tuple[int | None, int]:
    """Retention as configured now.

    Returns:
        (days to keep a finished job, or None when retention is off; seconds between
        sweeps). Read from the environment on each call.

    Raises:
        SystemExit: for a value that is not a whole number.
    """
    days = env_int("JOB_RETENTION_DAYS", 0, minimum=0)
    seconds = env_int("RETENTION_SWEEP_SECONDS", DEFAULT_SWEEP_SECONDS, minimum=60)
    return (days or None), seconds


def purge(store: JobStore, older_than_days: int, *, status: str | None = None,
          dry_run: bool = False, now: datetime | None = None) -> list[str]:
    """Delete finished jobs older than a number of days.

    Args:
        store: Where the jobs are.
        older_than_days: Age, counted from when the job finished.
        status: Only jobs with this status (complete, failed or cancelled).
        dry_run: List the jobs without deleting any.
        now: The time to count from. For tests.

    Returns:
        The ids deleted, or that would be deleted. A job that started running again
        between the listing and the delete is skipped.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
    ids = store.expired(cutoff, status)
    if dry_run:
        return ids
    return [job_id for job_id in ids if store.delete(job_id) == "deleted"]


def sweep() -> list[str]:
    """One retention sweep. Does nothing when retention is off. Never raises.

    Returns:
        The ids deleted.
    """
    try:
        days, _ = settings()
        if days is None:
            return []
        deleted = purge(job_store(), days)
        if deleted:  # a sweep that finds nothing is not worth a line an hour
            audit.record("retention_sweep", older_than_days=days, count=len(deleted),
                         job_ids=deleted)
            logger.info("Retention deleted %d job(s) older than %d days", len(deleted), days)
        return deleted
    except Exception:  # noqa: BLE001 - a failed sweep must not stop the server
        logger.exception("Retention sweep failed")
        return []


async def run_forever(interval_seconds: int) -> None:
    """Sweep every `interval_seconds` until cancelled. The first sweep is the caller's."""
    while True:
        await asyncio.sleep(interval_seconds)
        await anyio.to_thread.run_sync(sweep)
