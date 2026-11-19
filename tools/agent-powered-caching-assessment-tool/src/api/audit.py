"""
Audit log: who did what, to which job, from where.

An append-only file of JSON lines, one per security-relevant action: a submit, a rerun, a
cancel, a delete, a purge, a preflight, a priced what-if, a retention sweep, and a submit
refused for its target. It answers "who pointed this tool at that database, and when".

What an entry holds: the time, the event, the request id (also sent to the caller in
X-Request-ID), the caller, the job id, and the fields of the event. What it never holds:
the SSH target, a key name, the database user, the password file, query text or any
credential. Callers
pass only the fields they mean to record, and `record` removes the known sensitive names
as a second line of defence. Values are JSON-encoded, so a newline or quote in a label or
host name cannot forge a second entry, and long values are cut.

The caller is the peer address until authentication exists, and `anonymous` is the actor.
When it does, bind the user in `bind` and every entry carries it.

A failure to write is logged as an error and does not fail the request. The file is
rotated at 10 MiB with 5 files kept, and created readable by its owner only. There is no
route to read it: that needs authorization this API does not have yet.

Spec: .kiro/specs/ux-api (UX-37).
"""

import contextvars
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from src.api import common
from src.orchestrator.job_store import now_iso

logger = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5
MAX_VALUE_LENGTH = 300

# Names that must never reach the log, whoever passes them.
SENSITIVE = frozenset({
    "ssh_target", "identity_file", "password_file", "db_user", "password",
    "query_text", "queries", "collector_output", "live_config",
})

_NO_CALLER: dict[str, str | None] = {"actor": "anonymous", "peer": None, "request_id": None}
_caller: contextvars.ContextVar[dict[str, str | None] | None] = contextvars.ContextVar(
    "apcat_caller", default=None)
_lock = threading.Lock()


def bind(peer: str | None, request_id: str, actor: str = "anonymous") -> contextvars.Token:
    """Say who is making the request, for every entry written while it is handled.

    Returns:
        A token for `unbind`.
    """
    return _caller.set({"actor": actor, "peer": peer, "request_id": request_id})


def unbind(token: contextvars.Token) -> None:
    """Forget the caller bound by `bind`."""
    _caller.reset(token)


def path() -> Path:
    """Where the log is: AUDIT_LOG_PATH, or audit.log in the artifact folder."""
    configured = os.environ.get("AUDIT_LOG_PATH", "").strip()
    return Path(configured) if configured else Path(common.ARTIFACT_ROOT) / "audit.log"


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_VALUE_LENGTH else value[:MAX_VALUE_LENGTH] + "..."
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items() if k not in SENSITIVE}
    if isinstance(value, (list, tuple, set)):
        return [_clean(v) for v in list(value)[:200]]
    return value


def _rotate(target: Path) -> None:
    """Shift audit.log to audit.log.1 and so on, dropping the oldest."""
    for n in range(BACKUPS - 1, 0, -1):
        older = target.with_name(f"{target.name}.{n}")
        if older.exists():
            older.replace(target.with_name(f"{target.name}.{n + 1}"))
    target.replace(target.with_name(f"{target.name}.1"))


def record(event: str, **fields: Any) -> None:
    """Write one entry. Never raises.

    Args:
        event: A short name such as `job_submitted`.
        **fields: What to record. Names in SENSITIVE are dropped.
    """
    caller = _caller.get() or _NO_CALLER
    entry = {"ts": now_iso(), "event": event, **caller,
             **{k: _clean(v) for k, v in fields.items() if k not in SENSITIVE}}
    line = json.dumps(entry, separators=(",", ":"), default=str) + "\n"
    try:
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            if target.exists() and target.stat().st_size + len(line) > MAX_BYTES:
                _rotate(target)
            fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:  # noqa: BLE001 - an unwritable log must not fail the request
        logger.exception("Could not write the audit log entry %s", event)
