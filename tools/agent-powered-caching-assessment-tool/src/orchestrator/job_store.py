"""
Job records and job state, behind an interface.

A job's folder holds its artifacts (collect, analyze, size, report) and two small
files this module owns:

    job.json     what the job is: label, source, options, sanitized input
    state.json   where it is: status, phases, timestamps, error, warnings

`JobStore` is the interface the orchestrator and the API use. `LocalJobStore`
keeps both files in the artifact folder. A hosted deployment can supply another
implementation (a table for the index, an object store for the files) without
changing the routes. Artifacts are still written through the filesystem by the
orchestrator; moving those is a separate piece of work.

Both files are written atomically (temp file, then rename), because a user
interface polls state.json while the job is rewriting it.

Spec: .kiro/specs/ux-api (UX-2 to UX-9).
"""

# The store classes define a method named `list`, which shadows the builtin
# inside the class body, so a later annotation such as `-> list[str]` fails at
# import on Python 3.12 and 3.13. Postponed evaluation keeps annotations as
# strings. (Python 3.14 defers annotations by default, which hides this.)
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from src.contracts.api_models import JobInput, JobRecord, JobSummary
from src.orchestrator import safe_paths

logger = logging.getLogger(__name__)

# A job id is a single path component: letters, digits, dot, underscore, hyphen,
# starting with a letter or digit. No separators and no leading dot, so it cannot
# name a parent directory or a hidden file.
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

UNFINISHED = ("queued", "running")
FINISHED = ("complete", "failed", "cancelled")

# The file each phase leaves in a job's folder. A phase's artifact is present when its
# file is, so this is also how a job says which phases it holds.
PHASE_FILES = {
    "collect": "collect/output.json",
    "analyze": "analyze/analysis.json",
    "size": "size/sizing.json",
    "report": "report/report.html",
}


class JobNotFound(Exception):
    """There is no job with this id."""


class JobBusy(Exception):
    """The job is queued or running, so another run cannot start on it."""
INTERRUPTED = "interrupted: the server stopped before the job finished"

DeleteResult = Literal["deleted", "not_found", "busy"]
# requested: a running job will stop at its next phase boundary.
# cancelled: a queued job was cancelled on the spot and will not start.
CancelResult = Literal["requested", "cancelled", "already_cancelled", "finished", "not_found"]

# A file in the job folder. It is the signal to stop, not a field of state.json,
# because the orchestrator rewrites state.json (read, change, write) on every phase
# and a write from another thread could be lost between its read and its write.
CANCEL_MARKER = "cancel.requested"


def seconds_between(start: str | None, end: str) -> float | None:
    """Seconds from one ISO timestamp to another, or None if either is unusable."""
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 2)
    except (TypeError, ValueError):
        return None


def mark_cancelled(state: dict, note: str) -> dict:
    """The state of a job that has been cancelled.

    Args:
        state: The job's current state.
        note: Where it stopped, for example "Cancelled before the analyze phase."

    Returns:
        A new dict with status `cancelled`, the note and the end time. Any phase still
        marked running is closed as failed. `error` is cleared: a cancel is not an error.
    """
    now = now_iso()
    st = dict(state)
    st["phases"] = [
        {**ph, "status": "failed", "ended_at": now, "error": "cancelled"}
        if ph.get("status") == "running" else ph
        for ph in st.get("phases") or []
    ]
    st.update(status="cancelled", note=note, error=None, updated_at=now, completed_at=now,
              duration_seconds=seconds_between(st.get("started_at"), now))
    return st


def valid_job_id(job_id: str) -> bool:
    """Whether a job id is safe to use as a directory name.

    Args:
        job_id: Candidate id.

    Returns:
        True for 1 to 64 characters of letters, digits, '.', '_' and '-', starting
        with a letter or digit, and not containing '..'.
    """
    # fullmatch: match() with "$" would also accept a trailing newline.
    return bool(isinstance(job_id, str) and JOB_ID_PATTERN.fullmatch(job_id) and ".." not in job_id)


def now_iso() -> str:
    """The current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def sanitize_collector_input(payload: dict) -> JobInput:
    """Keep the harmless parts of a CollectorInput.

    Args:
        payload: A CollectorInput as a dict.

    Returns:
        The endpoint, port, database, mode and schemas. The SSH target, key name, database
        user and password file are left out on purpose.
    """
    opts = payload.get("collection_options") or {}
    return JobInput(
        cluster_endpoint=payload.get("cluster_endpoint"),
        port=payload.get("port"),
        database_name=payload.get("database_name"),
        mode=str(payload["mode"]) if payload.get("mode") is not None else None,
        schemas=opts.get("schemas"),
        window_minutes=opts.get("window_minutes"),
        start_at=str(opts["start_at"]) if opts.get("start_at") else None,
    )


class JobStore(Protocol):
    """What the orchestrator and the API need from job storage."""

    def create(self, record: JobRecord) -> JobRecord:
        """Store a new job and mark it queued. Raises FileExistsError if it exists."""

    def get(self, job_id: str) -> JobRecord | None:
        """The job's record, or None."""

    def update(self, job_id: str, **fields) -> JobRecord | None:
        """Change fields of a record. Returns the new record, or None if absent."""

    def read_state(self, job_id: str) -> dict:
        """The job's state, or {'status': 'not_found'}."""

    def write_state(self, job_id: str, state: dict) -> None:
        """Replace the job's state."""

    def list(self, *, limit: int = 25, offset: int = 0, status: str | None = None,
             q: str | None = None, database_id: str | None = None,
             ) -> tuple[int, list[JobSummary]]:
        """(total matching, one page), newest first."""

    def delete(self, job_id: str) -> DeleteResult:
        """Remove a job and its artifacts, unless it is still queued or running."""

    def request_cancel(self, job_id: str) -> CancelResult:
        """Ask a queued or running job to stop."""

    def cancel_requested(self, job_id: str) -> bool:
        """Whether a cancel has been requested for the job."""

    def clear_cancel(self, job_id: str) -> None:
        """Forget a cancel request, for a job that is about to run again."""

    def count_unfinished(self) -> int:
        """How many jobs are queued or running."""

    def available_phases(self, job_id: str) -> list[str]:
        """The phases whose artifact is present, in pipeline order."""

    def claim_run(self, job_id: str, phases: list[str]) -> int:
        """Take a job for a new run of some phases. Returns the run number."""

    def expired(self, cutoff: datetime, status: str | None = None) -> list[str]:
        """Ids of finished jobs that finished before the cutoff."""

    def interrupt_unfinished(self) -> list[str]:
        """Mark jobs left queued or running as failed. Returns their ids."""


_CLAIM_LOCK = threading.Lock()


def _atomic_write(path: Path, text: str) -> None:
    """Write a file so a reader sees the old content or the new, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class LocalJobStore:
    """Job records and state in the artifact folder."""

    def __init__(self, root: str | Path):
        """
        Args:
            root: The artifact folder. Created if missing.
        """
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # --- paths -----------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        """The job's folder, created on demand.

        Raises:
            ValueError: for an invalid job id.
        """
        d = self._dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _dir(self, job_id: str) -> Path:
        if not valid_job_id(job_id):
            raise ValueError(
                "invalid job id: use 1 to 64 letters, digits, '.', '_' or '-', "
                "starting with a letter or digit"
            )
        return safe_paths.child(self.root, job_id)

    def _found(self, job_id: str) -> Path | None:
        """The job's folder for a lookup, or None for an invalid id or an unsafe path."""
        return safe_paths.child_or_none(self.root, job_id) if valid_job_id(job_id) else None

    # --- records ---------------------------------------------------------

    def create(self, record: JobRecord) -> JobRecord:
        """Store a new job and mark it queued.

        Raises:
            ValueError: for an invalid job id.
            FileExistsError: when a job with this id already exists.
        """
        d = self._dir(record.job_id)
        if d.exists() and ((d / "job.json").exists() or (d / "state.json").exists()):
            raise FileExistsError(f"job {record.job_id} already exists")
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write(d / "job.json", record.model_dump_json(indent=2))
        self.write_state(record.job_id, {
            "job_id": record.job_id, "status": "queued", "current_phase": None,
            "phases": [], "created_at": record.created_at,
            "updated_at": record.created_at,
        })
        return record

    def get(self, job_id: str) -> JobRecord | None:
        """The job's record, or None when absent or the id is invalid."""
        d = self._found(job_id)
        if d is None:
            return None
        path = d / "job.json"
        if path.is_file():
            try:
                return JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except ValueError:
                logger.warning("job.json for %s is not valid", job_id)
                return None
        state = self.read_state(job_id)
        if state.get("status") == "not_found":
            return None
        # A job from before records existed: only its state is known.
        return JobRecord(job_id=job_id,
                         created_at=state.get("created_at") or state.get("updated_at") or "")

    def update(self, job_id: str, **fields) -> JobRecord | None:
        """Change fields of a record.

        Args:
            job_id: The job.
            **fields: Record fields to set.

        Returns:
            The new record, or None when the job has no record.
        """
        rec = self.get(job_id)
        if rec is None:
            return None
        new = JobRecord.model_validate({**rec.model_dump(), **fields})
        _atomic_write(self.job_dir(job_id) / "job.json", new.model_dump_json(indent=2))
        return new

    # --- state -----------------------------------------------------------

    def read_state(self, job_id: str) -> dict:
        """The job's state, or {'status': 'not_found'} (also for an invalid id)."""
        d = self._found(job_id)
        if d is None:
            return {"status": "not_found"}
        path = d / "state.json"
        if not path.is_file():
            return {"status": "not_found"}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            logger.warning("state.json for %s is not valid JSON", job_id)
            return {"status": "not_found"}

    def write_state(self, job_id: str, state: dict) -> None:
        """Replace the job's state."""
        _atomic_write(self.job_dir(job_id) / "state.json", json.dumps(state, indent=2))

    # --- list, delete, sweep ----------------------------------------------

    def _job_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return [p.name for p in self.root.iterdir()
                if p.is_dir() and not p.is_symlink() and valid_job_id(p.name)
                and ((p / "job.json").is_file() or (p / "state.json").is_file())]

    def _summary(self, job_id: str) -> JobSummary | None:
        rec = self.get(job_id)
        if rec is None:
            return None
        st = self.read_state(job_id)
        status = st.get("status")
        if status not in ("queued", "running", "complete", "failed", "cancelled"):
            return None
        return JobSummary(
            **rec.model_dump(), status=status, current_phase=st.get("current_phase"),
            updated_at=st.get("updated_at"), completed_at=st.get("completed_at"),
            duration_seconds=st.get("duration_seconds"), error=st.get("error"),
            note=st.get("note"), warnings=st.get("warnings") or [],
            available=self.available_phases(job_id),
        )

    def list(self, *, limit: int = 25, offset: int = 0, status: str | None = None,
             q: str | None = None, database_id: str | None = None,
             ) -> tuple[int, list[JobSummary]]:
        """Jobs, newest first.

        Args:
            limit: Page size.
            offset: Jobs to skip.
            status: Keep only this status.
            q: Keep jobs whose label, id or database name contains this text,
                ignoring case.
            database_id: Keep only the jobs of this saved database.

        Returns:
            (number of matching jobs, the requested page).
        """
        rows = [s for s in (self._summary(j) for j in self._job_ids()) if s is not None]
        if database_id:
            rows = [r for r in rows if r.database_id == database_id]
        if status:
            rows = [r for r in rows if r.status == status]
        if q:
            needle = q.lower()
            rows = [r for r in rows if any(
                needle in (v or "").lower() for v in (r.label, r.job_id, r.database_name))]
        rows.sort(key=lambda r: (r.created_at, r.job_id), reverse=True)
        return len(rows), rows[offset:offset + limit]

    def delete(self, job_id: str) -> DeleteResult:
        """Remove a job and its artifacts.

        Args:
            job_id: The job.

        Returns:
            'deleted'; 'not_found' when there is no such job; 'busy' while the job is
            queued or running. Cancel it first.
        """
        d = self._found(job_id)
        if d is None or not d.is_dir():
            return "not_found"
        if self.read_state(job_id).get("status") in UNFINISHED:
            return "busy"
        shutil.rmtree(d)
        return "deleted"

    # --- cancel ------------------------------------------------------------

    def request_cancel(self, job_id: str) -> CancelResult:
        """Ask a queued or running job to stop.

        A queued job has not started, so it is cancelled on the spot and its
        background task does nothing when it starts. A running job is only asked: the
        orchestrator looks for the request between phases.

        Args:
            job_id: The job.

        Returns:
            'cancelled' for a job that was queued; 'requested' for a job that is
            running; 'already_cancelled'; 'finished' for a job that completed or
            failed; 'not_found'.
        """
        if not valid_job_id(job_id):
            return "not_found"
        status = self.read_state(job_id).get("status")
        if status == "not_found":
            return "not_found"
        if status == "cancelled":
            return "already_cancelled"
        if status not in UNFINISHED:
            return "finished"
        # The marker first, so a task that starts now sees it before it can mark the
        # job running.
        _atomic_write(self._dir(job_id) / CANCEL_MARKER, now_iso())
        st = self.read_state(job_id)
        if st.get("status") == "queued":
            self.write_state(job_id, mark_cancelled(st, "Cancelled before it started."))
            return "cancelled"
        return "requested"

    def cancel_requested(self, job_id: str) -> bool:
        """Whether a cancel has been requested for the job. False for an invalid id."""
        d = self._found(job_id)
        return d is not None and (d / CANCEL_MARKER).is_file()

    def clear_cancel(self, job_id: str) -> None:
        """Forget a cancel request, for a job that is about to run again."""
        d = self._found(job_id)
        if d is not None:
            (d / CANCEL_MARKER).unlink(missing_ok=True)

    def expired(self, cutoff: datetime, status: str | None = None) -> list[str]:
        """Finished jobs that finished before a time.

        Args:
            cutoff: A timezone-aware time.
            status: Only this status (complete, failed or cancelled).

        Returns:
            Sorted ids. A queued or running job is never listed, and neither is one whose
            time cannot be read, because its age is unknown.
        """
        out = []
        for job_id in self._job_ids():
            st = self.read_state(job_id)
            if st.get("status") not in FINISHED or (status and st.get("status") != status):
                continue
            stamp = st.get("completed_at") or st.get("updated_at") or st.get("created_at")
            try:
                when = datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            if when < cutoff:
                out.append(job_id)
        return sorted(out)

    def available_phases(self, job_id: str) -> list[str]:
        """The phases whose artifact is present, in pipeline order.

        Read from the job's folder, so it is true whatever the state says. An invalid
        id has none.
        """
        d = self._found(job_id)
        if d is None:
            return []
        return [name for name, rel in PHASE_FILES.items() if (d / rel).is_file()]

    def claim_run(self, job_id: str, phases: list[str]) -> int:
        """Take a job for a new run of some of its phases.

        The job becomes `queued` for this run. The check that it is not busy and the
        change of state are one step, so two requests cannot both take it. Artifacts are
        not touched here: the run clears what it replaces when it starts, so a run that
        is cancelled while it waits leaves the job as it was.

        Args:
            job_id: The job.
            phases: The phases this run will do.

        Returns:
            The run number: 2 for the second run of a job, and so on. The run carries
            it, and refuses to start if a later claim has superseded it.

        Raises:
            JobNotFound: when there is no such job.
            JobBusy: when the job is queued or running.
        """
        if not valid_job_id(job_id):
            raise JobNotFound(job_id)
        with _CLAIM_LOCK:
            st = self.read_state(job_id)
            status = st.get("status")
            if status == "not_found":
                raise JobNotFound(job_id)
            if status in UNFINISHED:
                raise JobBusy(job_id)
            run = int(st.get("run") or 1) + 1
            now = now_iso()
            for key in ("error", "note", "warnings", "completed_at", "duration_seconds"):
                st.pop(key, None)
            st.update(status="queued", current_phase=None, run=run, requested=list(phases),
                      updated_at=now)
            self.clear_cancel(job_id)
            self.write_state(job_id, st)
            return run

    def count_unfinished(self) -> int:
        """How many jobs are queued or running."""
        return sum(1 for j in self._job_ids() if self.read_state(j).get("status") in UNFINISHED)

    def interrupt_unfinished(self) -> list[str]:
        """Mark every job left queued or running as failed.

        Jobs run inside the API process, so at start-up none of them is alive.
        Assumes one server process.

        Returns:
            The ids that were changed.
        """
        changed: list[str] = []
        for job_id in self._job_ids():
            st = self.read_state(job_id)
            if st.get("status") not in UNFINISHED:
                continue
            ended = now_iso()
            for ph in st.get("phases") or []:
                if ph.get("status") == "running":
                    ph["status"], ph["ended_at"] = "failed", ended
            st.update(status="failed", error=INTERRUPTED, updated_at=ended,
                      completed_at=ended)
            self.write_state(job_id, st)
            changed.append(job_id)
            logger.warning("Marked job %s as failed: it was left %s", job_id, INTERRUPTED)
        return changed
