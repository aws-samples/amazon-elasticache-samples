"""
Database discovery: POST /databases/discover.

Lists PostgreSQL servers the user could assess, from this machine (server processes,
~/.ssh/config, ~/.pg_service.conf, earlier assessments) and, when asked, from one database
host over SSH. Reads no password and makes no database connection.

It reads the server process's own home directory and can open an SSH connection to a host
the caller names, like preflight. That is safe only because the API is loopback only (the
caller is the operator of this machine). Authentication and target authorization must cover
it before the API is ever exposed. Spec: .kiro/specs/database-discovery.
"""

import logging
import threading
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from src.api import audit, common
from src.api.common import MAX_CONCURRENT_DISCOVERIES
from src.api.errors import ApiError, error_responses
from src.contracts.discovery import LOCAL_SOURCES, DiscoveryResult
from src.tools.discovery.discover import HostProbeTarget, discover, host_problems

logger = logging.getLogger(__name__)
router = APIRouter()

_slots = threading.BoundedSemaphore(MAX_CONCURRENT_DISCOVERIES)

LocalSource = Literal["local_server", "ssh_config", "pg_service", "earlier_assessment"]


class ProbeHost(BaseModel):
    """A database host to look on, over SSH."""

    model_config = ConfigDict(extra="forbid")

    ssh_target: str = Field(max_length=320, description="user@host, a host, or a name from "
                            "ssh_config. Its key must already be in known_hosts")
    ssh_port: int = Field(default=22, ge=1, le=65535)
    identity_file: str | None = Field(default=None, max_length=128,
                                      description="A key file name inside SSH_KEY_DIR")


class DiscoverRequest(BaseModel):
    """What to look at."""

    model_config = ConfigDict(extra="forbid")

    sources: list[LocalSource] = Field(
        default_factory=lambda: list(LOCAL_SOURCES), max_length=len(LOCAL_SOURCES),
        description="Local sources to read. Default: all four")
    host: ProbeHost | None = Field(default=None, description="Also look for PostgreSQL "
                                   "servers on this host over SSH (clusters only)")


@router.post("/databases/discover", response_model=DiscoveryResult,
             responses=error_responses(413, 429))
def discover_databases(req: DiscoverRequest) -> DiscoveryResult:
    """Find PostgreSQL servers the user could assess.

    Reads this machine's server processes and socket files, ~/.ssh/config Host entries,
    ~/.pg_service.conf services (never a password) and earlier assessments. With `host`,
    also lists the PostgreSQL clusters running on that host over SSH (port, data directory,
    version where visible, listen addresses), without logging in to PostgreSQL.

    Every source reports its own status; one that cannot run does not fail the request.
    422 when a source name or a host value is not in an accepted form. 429 when two
    discoveries are already running.
    """
    target = None
    if req.host is not None:
        target = HostProbeTarget(req.host.ssh_target, req.host.ssh_port, req.host.identity_file)
        problems = host_problems(target)
        if problems:
            audit.record("discovery_refused", reason="invalid_target", fields=len(problems))
            raise ApiError(422, "invalid_target", "; ".join(problems))
    if not _slots.acquire(blocking=False):
        raise ApiError(429, "discovery_busy",
                       "Too many discoveries are running. Try again shortly.",
                       headers={"Retry-After": "10"})
    try:
        result = discover(sources=req.sources, host=target, artifact_root=common.ARTIFACT_ROOT)
    finally:
        _slots.release()
    # Statuses and counts only: never a host, alias, path or target.
    audit.record("discovery", probed=target is not None,
                 sources={r.source: f"{r.status}:{r.found}" for r in result.sources})
    return result
