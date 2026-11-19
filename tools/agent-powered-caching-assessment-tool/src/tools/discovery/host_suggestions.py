"""
Hosts to suggest for a probe, and the record of recent probes.

Order: hosts of saved databases, then hosts probed recently, then ~/.ssh/config aliases. Names
that look like git or code hosts are flagged, so the console can hide them. Nothing here
connects to anything.

Recent probes are kept in `<ARTIFACT_ROOT>/_hosts/recent.json` (folder 0700, file 0600), at
most 20, newest first: the target, port, key name, when, and which stage failed. Never a
password or an error text. Spec: .kiro/specs/ssh-host-probe.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path

from src.contracts.hosts import HostProbeResult, HostSuggestion, HostSuggestionList
from src.orchestrator.database_store import LocalDatabaseStore
from src.orchestrator.job_store import now_iso
from src.tools.discovery import ssh_config

logger = logging.getLogger(__name__)

STORE_DIR = "_hosts"
MAX_RECENT = 20
_lock = threading.Lock()

# Names of hosts that serve git, not databases. A name match only: the console hides them
# behind "Show all", and they can still be probed.
CODE_HOST = re.compile(
    r"(^|[.@-])(gitlab|github|bitbucket|codecommit|gitea|gerrit)([.-]|$)"
    r"|(^|@)(code|git)\.|^git@", re.I)


def is_code_host(*names: str | None) -> bool:
    """Whether any of the names looks like a git or code host."""
    return any(n and CODE_HOST.search(n) for n in names)


def _path(root: str | Path) -> Path:
    d = Path(root) / STORE_DIR
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d / "recent.json"


def read_recent(root: str | Path) -> list[dict]:
    """The recent probes, newest first. An unreadable file reads as none."""
    p = _path(root)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [r for r in data if isinstance(r, dict) and isinstance(r.get("ssh_target"), str)]
    except (OSError, ValueError):
        logger.warning("recent host probes are not readable")
        return []


def record_probe(root: str | Path, identity_file: str | None, result: HostProbeResult) -> None:
    """Put a probe at the top of the recent list (one entry per target and port)."""
    failed = next((s.name for s in result.stages if s.status == "fail"), None)
    entry = {"ssh_target": result.ssh_target, "ssh_port": result.ssh_port,
             "identity_file": identity_file, "resolved_host": result.resolved_host,
             "last_result": "ok" if result.ok else (failed or "failed"),
             "last_probed_at": now_iso()}
    with _lock:
        p = _path(root)
        rows = [r for r in read_recent(root)
                if (r["ssh_target"].lower(), r.get("ssh_port")) !=
                (entry["ssh_target"].lower(), entry["ssh_port"])]
        rows = [entry, *rows][:MAX_RECENT]
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".recent.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, indent=2)
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def suggestions(root: str | Path, ssh_config_path: Path | None = None) -> HostSuggestionList:
    """Hosts to offer in the probe panel, saved databases first, without duplicates.

    Args:
        root: The artifact folder (saved databases and recent probes).
        ssh_config_path: Defaults to ~/.ssh/config.
    """
    out: list[HostSuggestion] = []
    seen: set[tuple[str, int | None]] = set()

    def add(s: HostSuggestion) -> None:
        key = (s.ssh_target.lower(), s.ssh_port or 22)
        if key not in seen:
            seen.add(key)
            out.append(s)

    for rec in LocalDatabaseStore(root).list():
        c = rec.connection
        add(HostSuggestion(ssh_target=c.ssh_target, ssh_port=c.ssh_port,
                           identity_file=c.identity_file, label=f"{c.ssh_target} ({rec.label})",
                           source="saved_database", code_host=is_code_host(c.ssh_target)))
    for r in read_recent(root):
        add(HostSuggestion(ssh_target=r["ssh_target"], ssh_port=r.get("ssh_port"),
                           identity_file=r.get("identity_file"), label=r["ssh_target"],
                           source="recent_probe", last_result=r.get("last_result"),
                           last_probed_at=r.get("last_probed_at"),
                           code_host=is_code_host(r["ssh_target"], r.get("resolved_host"))))
    try:
        parsed = ssh_config.read_ssh_config(ssh_config_path)
    except (OSError, ValueError):
        parsed = None
    for h in (parsed.hosts if parsed else []):
        add(HostSuggestion(ssh_target=h.alias, ssh_port=h.port,
                           label=h.alias + (f" ({h.hostname})" if h.hostname and h.hostname != h.alias else ""),
                           source="ssh_config",
                           code_host=is_code_host(h.alias, h.hostname, f"{h.user}@" if h.user == "git" else None)))
    return HostSuggestionList(items=out)
