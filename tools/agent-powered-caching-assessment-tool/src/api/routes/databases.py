"""
Saved databases: save a connection once, check it, and run assessments from it.

A saved database never holds a password. Checking one opens an SSH connection to its host,
like preflight, so these routes rely on the loopback gate and must be covered by
authentication and target authorization before the API is ever exposed.
Spec: .kiro/specs/databases.
"""

import logging
import sys
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Path, Response

from src.agents.collector.preflight import run_preflight
from src.agents.collector.target_checks import target_problems
from src.api import audit, common
from src.api.common import job_store
from src.api.errors import ApiError, error_responses
from src.api.routes import preflight as preflight_route
from src.api.routes.assessments import submit_live
from src.contracts.api_models import LABEL_MAX_LENGTH, JobSummary, SubmitResponse
from src.contracts.collector_input import CollectionOptions, CollectorInput, LiveConnectionConfig
from src.contracts.databases import (
    DatabaseCheckResponse,
    DatabaseConnection,
    DatabaseCreate,
    DatabaseCreated,
    DatabaseList,
    DatabasePatch,
    DatabaseRecord,
    DatabaseRunRequest,
    DatabaseView,
    LatestAssessment,
    ReadinessState,
    RoleCreateRequest,
    RoleCreateResult,
    readiness_from,
)
from src.orchestrator.database_store import (
    MAX_DATABASES,
    DatabaseExists,
    LocalDatabaseStore,
    TooManyDatabases,
    new_database_id,
    valid_database_id,
)
from src.orchestrator.job_store import UNFINISHED, now_iso
from src.tools.database.role_setup import OUTCOMES, RoleSetupResult, create_role, role_problems
from src.tools.discovery.discover import HostProbeTarget, host_problems, ssh_executor_for

logger = logging.getLogger(__name__)
router = APIRouter()

# No pattern here: DB-4 says an id not in the stored form is 404, so _get() answers it.
DatabaseId = Annotated[str, Path(max_length=64,
                                 description="A saved database id, db- and 12 hex characters")]

# Every job, not one page: counts, the running check and attach-earlier need them all.
_ALL_JOBS = sys.maxsize


def database_store() -> LocalDatabaseStore:
    """The database store over ARTIFACT_ROOT.

    Returns:
        A LocalDatabaseStore whose folder is `<ARTIFACT_ROOT>/_databases/`.
    """
    return LocalDatabaseStore(common.ARTIFACT_ROOT)


def collector_input_for(conn: DatabaseConnection, *, job_id: str = "preflight",
                        window_minutes: int | None = None,
                        start_at: datetime | None = None) -> CollectorInput:
    """The collector input a saved connection means.

    Args:
        conn: The saved connection.
        job_id: The job id to put on the input. "preflight" for a check.
        window_minutes: The collection window, for a run.
        start_at: When the window starts, for a scheduled run.

    Returns:
        A live CollectorInput with the connection's SSH and database values.
    """
    return CollectorInput(
        job_id=job_id, cluster_endpoint=conn.cluster_endpoint, port=conn.port,
        database_name=conn.database_name,
        live_config=LiveConnectionConfig(
            ssh_target=conn.ssh_target, ssh_port=conn.ssh_port,
            identity_file=conn.identity_file, db_user=conn.db_user,
            password_file=conn.password_file, password_secret_arn=conn.password_secret_arn),
        collection_options=CollectionOptions(schemas=conn.schemas,
                                             window_minutes=window_minutes, start_at=start_at))


def _check_shape(conn: DatabaseConnection, route: str) -> None:
    """Refuse a connection whose values could break the ssh command.

    Args:
        conn: The connection to check.
        route: "create" or "update", for the audit entry.

    Raises:
        ApiError: 422 invalid_target. The detail and the audit entry name fields only.
    """
    problems = target_problems(collector_input_for(conn))
    if problems:
        audit.record("database_refused", route=route, reason="invalid_target",
                     fields=[p.split(" ")[0] for p in problems])
        raise ApiError(422, "invalid_target", "; ".join(problems))


def _latest(job: JobSummary) -> LatestAssessment:
    """The short form of a job for a database view.

    Args:
        job: A job summary.

    Returns:
        Its id, status, creation time, label and workload verdict.
    """
    return LatestAssessment(job_id=job.job_id, status=job.status, created_at=job.created_at,
                            label=job.label, workload_verdict=job.workload_verdict)


def _jobs_of(database_id: str) -> list[JobSummary]:
    """Every job of one saved database, newest first.

    Args:
        database_id: The saved database.

    Returns:
        Its job summaries.
    """
    _total, jobs = job_store().list(limit=_ALL_JOBS, database_id=database_id)
    return jobs


def view(rec: DatabaseRecord, jobs: list[JobSummary] | None = None) -> DatabaseView:
    """A saved database with what its assessments found.

    Args:
        rec: The saved database.
        jobs: Its jobs, newest first, when the caller already has them. Read when None.

    Returns:
        The record with its assessment count, running count, latest assessment and latest
        complete assessment.
    """
    if jobs is None:
        jobs = _jobs_of(rec.database_id)
    done = next((j for j in jobs if j.status == "complete"), None)
    return DatabaseView(**rec.model_dump(), assessments=len(jobs),
                        running=sum(1 for j in jobs if j.status in UNFINISHED),
                        latest=_latest(jobs[0]) if jobs else None,
                        latest_complete=_latest(done) if done else None)


def _get(database_id: str) -> DatabaseRecord:
    """A saved database.

    Args:
        database_id: Its id as the client sent it.

    Returns:
        The record.

    Raises:
        ApiError: 404 database_not_found, also for an id not in the stored form, which is
            refused without touching the disk and is not echoed back.
    """
    if not valid_database_id(database_id):
        raise ApiError(404, "database_not_found", "Database not found")
    rec = database_store().get(database_id)
    if rec is None:
        raise ApiError(404, "database_not_found", f"Database {database_id} not found")
    return rec


def _attach_earlier(rec: DatabaseRecord) -> int:
    """Link earlier live jobs with this endpoint, port and database that have no database.

    Args:
        rec: The database just saved.

    Returns:
        How many jobs were linked.
    """
    store = job_store()
    conn = rec.connection
    _total, jobs = store.list(limit=_ALL_JOBS)
    attached = 0
    for j in jobs:
        inp = j.input
        if (j.source != "live" or j.database_id or inp is None
                or inp.cluster_endpoint != conn.cluster_endpoint
                or (inp.port or 5432) != conn.port or inp.database_name != conn.database_name):
            continue
        if store.update(j.job_id, database_id=rec.database_id) is not None:
            attached += 1
    return attached


@router.get("/databases", response_model=DatabaseList)
def list_databases() -> DatabaseList:
    """Every saved database, newest first, with readiness and its latest assessment."""
    _total, jobs = job_store().list(limit=_ALL_JOBS)
    by_db: dict[str, list[JobSummary]] = {}
    for j in jobs:
        if j.database_id:
            by_db.setdefault(j.database_id, []).append(j)
    items = [view(r, by_db.get(r.database_id, [])) for r in database_store().list()]
    return DatabaseList(total=len(items), items=items)


@router.post("/databases", response_model=DatabaseCreated, status_code=201,
             responses=error_responses(409, 413))
def create_database(req: DatabaseCreate) -> DatabaseCreated:
    """Save a database connection. Never a password: a password file on the host or a
    Secrets Manager ARN.

    The values get the same checks as a live submit (422 `invalid_target`). 409
    `database_exists` when another saved database has the same SSH target, SSH port,
    endpoint, port and database name; 409 `too_many_databases` at the limit. With
    `attach_earlier`, earlier assessments with the same endpoint, port and database name
    that belong to no database are attached; the response says how many.
    """
    conn = req.connection
    _check_shape(conn, "create")
    now = now_iso()
    label = req.label or f"{conn.database_name} on {conn.ssh_target.split('@')[-1]}"
    rec = DatabaseRecord(database_id=new_database_id(), label=label[:LABEL_MAX_LENGTH],
                         connection=conn, created_at=now, updated_at=now,
                         discovered_from=req.discovered_from)
    try:
        database_store().create(rec)
    except DatabaseExists as e:
        raise ApiError(409, "database_exists",
                       f"This connection is already saved as {e.database_id}") from e
    except TooManyDatabases as e:
        raise ApiError(409, "too_many_databases", f"The server keeps at most "
                       f"{MAX_DATABASES} databases. Delete one first.") from e
    attached = _attach_earlier(rec) if req.attach_earlier else 0
    audit.record("database_created", database_id=rec.database_id, attached=attached,
                 discovered_from=req.discovered_from)
    return DatabaseCreated(**view(rec).model_dump(), attached=attached)


@router.post("/databases/create-role", response_model=RoleCreateResult,
             responses=error_responses(413, 429))
def create_role_route(req: RoleCreateRequest) -> RoleCreateResult:
    """Create, or reuse, the read-only role the tool signs in as, on the database host.

    Runs one fixed script over SSH as the SSH user. It signs in to PostgreSQL as the postgres
    OS user with `sudo -n` (no admin password is typed, sent or stored), makes a random
    password in a new file on the host (mode 600) and creates the role with only its
    SCRAM verifier in the SQL: `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE`, read only by
    default, with `pg_monitor` and `pg_read_all_data`. A role that exists is reused when its
    password file is there; otherwise nothing changes and `ok` is false. It never touches a
    privileged role, pg_hba.conf, postgresql.conf or pg_stat_statements.

    200 with `ok` false when the host refused (no sudo, no python3, the role exists without
    its file, ...); the message is fixed text. 422 when a value is not in an accepted form.
    429 when four checks are already running.
    """
    problems = role_problems(req.role_name, req.database_name, req.port, req.password_file)
    target = HostProbeTarget(req.ssh_target, req.ssh_port, req.identity_file)
    problems += host_problems(target)
    if problems:
        audit.record("role_setup_refused", reason="invalid_target", fields=len(problems))
        raise ApiError(422, "invalid_target", "; ".join(problems))
    if not preflight_route._slots.acquire(blocking=False):
        raise ApiError(429, "preflight_busy", "Too many checks are running. Try again shortly.",
                       headers={"Retry-After": "10"})
    try:
        executor = ssh_executor_for(target)
        try:
            r = create_role(executor, req.role_name, req.database_name, req.port,
                            req.password_file)
        finally:
            executor.close()
    except (ValueError, FileNotFoundError):
        r = RoleSetupResult("ssh_failed", False, OUTCOMES["ssh_failed"][1], req.role_name, None)
    finally:
        preflight_route._slots.release()
    # The outcome and the role name only: never the host, the path or any password.
    audit.record("role_setup", outcome=r.outcome, role=r.role_name)
    return RoleCreateResult(ok=r.ok, outcome=r.outcome, message=r.message,
                            role_name=r.role_name, password_file=r.password_file)


@router.get("/databases/{database_id}", response_model=DatabaseView,
            responses=error_responses(404))
def get_database(database_id: DatabaseId) -> DatabaseView:
    """One saved database."""
    return view(_get(database_id))


@router.patch("/databases/{database_id}", response_model=DatabaseView,
              responses=error_responses(404, 409, 413))
def update_database(database_id: DatabaseId, req: DatabasePatch) -> DatabaseView:
    """Change the label or the connection. A new connection clears the readiness, so it
    must be checked again before a run."""
    rec = _get(database_id)
    changes: dict = {"updated_at": now_iso()}
    if req.label is not None:
        changes["label"] = req.label
    if req.connection is not None and req.connection != rec.connection:
        _check_shape(req.connection, "update")
        changes["connection"] = req.connection
        changes["readiness"] = ReadinessState()
    new = DatabaseRecord.model_validate({**rec.model_dump(), **changes})
    try:
        database_store().save(new)
    except DatabaseExists as e:
        raise ApiError(409, "database_exists",
                       f"This connection is already saved as {e.database_id}") from e
    audit.record("database_updated", database_id=database_id,
                 fields=sorted(k for k in changes if k != "updated_at"))
    return view(new)


@router.delete("/databases/{database_id}", status_code=204,
               responses=error_responses(404, 409))
def delete_database(database_id: DatabaseId) -> Response:
    """Forget a saved database. Its assessments and their results stay.

    409 `database_busy` while one of its assessments is queued or running.
    """
    _get(database_id)
    jobs = _jobs_of(database_id)
    if any(j.status in UNFINISHED for j in jobs):
        raise ApiError(409, "database_busy",
                       "An assessment of this database is still running. Cancel it first.")
    database_store().delete(database_id)
    audit.record("database_deleted", database_id=database_id, assessments_kept=len(jobs))
    return Response(status_code=204)


@router.post("/databases/{database_id}/check", response_model=DatabaseCheckResponse,
             responses=error_responses(404, 413, 429))
def check_database(database_id: DatabaseId) -> DatabaseCheckResponse:
    """Run the readiness checks (the preflight) and store the result.

    Readiness is `blocked` when a check failed, `degraded` when pg_stat_statements is not
    usable, `warnings` for another warning, `ready` otherwise. Messages are fixed text.
    429 when four preflights are already running.
    """
    rec = _get(database_id)
    inp = collector_input_for(rec.connection)
    if target_problems(inp):
        raise ApiError(422, "invalid_target", "The saved connection is not in an accepted "
                       "form. Edit it.")
    if not preflight_route._slots.acquire(blocking=False):
        raise ApiError(429, "preflight_busy",
                       "Too many checks are running. Try again shortly.",
                       headers={"Retry-After": "10"})
    try:
        result = run_preflight(inp)
    finally:
        preflight_route._slots.release()
    readiness = readiness_from(result, now_iso())
    current = database_store().get(database_id)
    if current is not None and current.connection == rec.connection:
        database_store().save(current.model_copy(update={"readiness": readiness}))
    audit.record("database_check", database_id=database_id, readiness=readiness.status,
                 failed=readiness.failed)
    return DatabaseCheckResponse(readiness=readiness, result=result)


@router.post("/databases/{database_id}/assessments", response_model=SubmitResponse,
             responses=error_responses(404, 409, 413, 429))
def run_database_assessment(database_id: DatabaseId, req: DatabaseRunRequest,
                            background: BackgroundTasks) -> SubmitResponse:
    """Start an assessment of a saved database. Only the run options are sent.

    409 `database_not_checked` before any readiness check, `database_blocked` when the last
    check failed, `degraded_not_allowed` when pg_stat_statements is not usable and
    `allow_degraded` is false. Then the same checks and limits as POST /assessments.
    The job's label is the run's `label` only. The database label is not copied, because
    the default one names the SSH host, which a job record never holds; the job's
    `database_id` links it to the database and its label.
    """
    rec = _get(database_id)
    status = rec.readiness.status
    if status == "unchecked":
        raise ApiError(409, "database_not_checked", "Check this database before running an "
                       "assessment.")
    if status == "blocked":
        raise ApiError(409, "database_blocked", "The last check failed. Fix it and check "
                       "again.")
    if status == "degraded" and not req.allow_degraded:
        raise ApiError(409, "degraded_not_allowed", "pg_stat_statements is not usable, so "
                       "the assessment would run in degraded mode. Set allow_degraded to run.")
    job_id = f"job-{uuid.uuid4().hex[:12]}"
    inp = collector_input_for(rec.connection, job_id=job_id,
                              window_minutes=req.window_minutes, start_at=req.start_at)
    return submit_live(inp, background, use_llm=req.use_llm, size=req.size, price=req.price,
                       region=req.region, label=req.label,
                       model_id=req.model_id, require_llm=req.require_llm,
                       database_id=database_id, route="database_run")
