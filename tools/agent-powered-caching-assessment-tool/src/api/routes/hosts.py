"""
SSH hosts: probe one in stages, trust its key, list the SSH keys, suggest hosts.

A probe opens an SSH connection to a host the caller names, and a trust adds a key to the
known_hosts file the API uses. Both are safe only because the API is loopback only (the
caller is the operator of this machine); authentication and target authorization must
cover them before the API is ever exposed. Spec: .kiro/specs/ssh-host-probe.
"""

import logging
import threading

from fastapi import APIRouter

from src.api import audit, common
from src.api.common import MAX_CONCURRENT_HOST_PROBES
from src.api.errors import ApiError, error_responses
from src.contracts.hosts import (
    HostProbeResult,
    HostSuggestionList,
    HostTarget,
    HostTrustRequest,
    HostTrustResult,
    SshKeyList,
)
from src.tools.discovery import host_probe, host_suggestions, ssh_keys
from src.tools.discovery.discover import HostProbeTarget, host_problems

logger = logging.getLogger(__name__)
router = APIRouter()

_slots = threading.BoundedSemaphore(MAX_CONCURRENT_HOST_PROBES)


def _check(target: HostTarget, route: str) -> None:
    """422 invalid_target when a value could break the ssh command. Names fields only."""
    problems = host_problems(HostProbeTarget(target.ssh_target, target.ssh_port,
                                             target.identity_file))
    if problems:
        audit.record("host_refused", route=route, reason="invalid_target", fields=len(problems))
        raise ApiError(422, "invalid_target", "; ".join(problems))


def _take_slot() -> None:
    if not _slots.acquire(blocking=False):
        raise ApiError(429, "host_probe_busy", "Too many host probes are running. Try again "
                       "shortly.", headers={"Retry-After": "10"})


@router.post("/hosts/probe", response_model=HostProbeResult,
             responses=error_responses(413, 429))
def probe_host(req: HostTarget) -> HostProbeResult:
    """Probe one SSH host in stages: resolve, reachable, host key, sign in, shell, then the
    PostgreSQL cluster scan (process list and socket files; no PostgreSQL sign-in).

    The probe stops at the first stage that fails, with fixed text and a fix; the ssh error
    itself is never returned. An unknown host key comes back with its SHA256 fingerprint, to
    check and then trust with POST /hosts/trust. When the SSH port does not answer, the
    answer carries this machine's public IP (looked up at checkip.amazonaws.com). 422 when a
    value is not in an accepted form. 429 when two probes are running.
    """
    _check(req, "probe")
    _take_slot()
    try:
        result = host_probe.probe_host(req)
    finally:
        _slots.release()
    try:
        host_suggestions.record_probe(common.ARTIFACT_ROOT, req.identity_file, result)
    except OSError:
        logger.warning("could not record the probe in the recent hosts")
    failed = next((s.name for s in result.stages if s.status == "fail"), None)
    # Statuses only: never the target, user, key name or fingerprint.
    audit.record("host_probe", ok=result.ok, failed_stage=failed,
                 host_key=result.host_key.state, clusters=len(result.clusters),
                 postgres_access=result.postgres_access, databases=len(result.databases))
    return result


@router.post("/hosts/trust", response_model=HostTrustResult,
             responses=error_responses(413, 429))
def trust_host_key(req: HostTrustRequest) -> HostTrustResult:
    """Trust a host's SSH key after the user checked its fingerprint.

    Scans the host again and appends its key to the known_hosts file the API uses
    (SSH_KNOWN_HOSTS, else ~/.ssh/known_hosts) only when the scan matches `fingerprint`.
    It never replaces a key: when a different key is already trusted the answer is
    `changed` and nothing is written. 200 with `trusted` false for mismatch, changed or
    unavailable. 422 when a value is not in an accepted form. 429 when two probes run.
    """
    _check(req, "trust")
    _take_slot()
    try:
        result = host_probe.trust_host(HostTarget(ssh_target=req.ssh_target,
                                                  ssh_port=req.ssh_port), req.fingerprint)
    finally:
        _slots.release()
    audit.record("host_key_trusted" if result.outcome == "added" else "host_key_trust",
                 outcome=result.outcome, fingerprint=req.fingerprint)
    return result


@router.get("/ssh-keys", response_model=SshKeyList)
def list_ssh_keys() -> SshKeyList:
    """The SSH keys in SSH_KEY_DIR: name, type and fingerprint. Never their contents. A key
    readable by group or others is listed as not usable, with the reason."""
    return ssh_keys.list_keys()


@router.get("/hosts/suggestions", response_model=HostSuggestionList)
def host_suggestion_list() -> HostSuggestionList:
    """Hosts to offer for a probe: saved databases' hosts, recent probes, then ~/.ssh/config
    aliases. Names that look like git or code hosts are flagged with `code_host`. Nothing is
    contacted."""
    return host_suggestions.suggestions(common.ARTIFACT_ROOT)
