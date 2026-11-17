"""
Earlier assessment targets, from the job records in the artifact folder.

A job record keeps the endpoint, port and database, not the SSH target (left out of job.json
on purpose), so jobs on different SSH hosts with the same endpoint, port and database are one
entry here. Spec: .kiro/specs/database-discovery (DISC-10).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.orchestrator.job_store import LocalJobStore

MAX_JOBS_READ = 2_000


@dataclass
class PastTarget:
    """One (endpoint, port, database) assessed before."""

    host: str
    port: int | None
    database: str
    runs: int
    last_assessed_at: str
    last_verdict: str | None


def past_targets(root: str | Path) -> list[PastTarget]:
    """Group live jobs by target.

    Args:
        root: The artifact folder.

    Returns:
        One entry per (endpoint, port, database), most recently assessed first. Jobs from a
        saved collection, and jobs whose record has no target, are left out.
    """
    store = LocalJobStore(root)
    _total, jobs = store.list(limit=MAX_JOBS_READ)
    groups: dict[tuple[str, int | None, str], PastTarget] = {}
    for summary in jobs:
        rec = store.get(summary.job_id)
        if rec is None or rec.source != "live" or rec.input is None:
            continue
        inp = rec.input
        if not inp.cluster_endpoint or not inp.database_name:
            continue
        key = (inp.cluster_endpoint, inp.port, inp.database_name)
        g = groups.get(key)
        created = rec.created_at or ""
        if g is None:
            groups[key] = PastTarget(host=key[0], port=key[1], database=key[2], runs=1,
                                     last_assessed_at=created, last_verdict=rec.workload_verdict)
            continue
        g.runs += 1
        if created > g.last_assessed_at:
            g.last_assessed_at, g.last_verdict = created, rec.workload_verdict
    return sorted(groups.values(), key=lambda t: t.last_assessed_at, reverse=True)
