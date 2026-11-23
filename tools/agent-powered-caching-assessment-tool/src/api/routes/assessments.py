"""Assessment submission, listing, deletion and status routes."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from src.agents.collector.target_checks import target_problems
from src.api import audit, limits
from src.api.common import (
    LIST_DEFAULT_LIMIT,
    LIST_MAX_LIMIT,
    REGION_MAX_LENGTH,
    JobId,
    RegionPattern,
    job_store,
    orchestrator,
    require_valid_job_id,
)
from src.api.errors import ApiError, error_responses
from src.contracts.api_models import (
    LABEL_MAX_LENGTH,
    CancelResponse,
    JobList,
    JobOptions,
    JobRecord,
    JobState,
    JobStatus,
    PhaseName,
    SubmitResponse,
    clean_label,
)
from src.contracts.collector_input import (
    WINDOW_MINUTES_MAX,
    WINDOW_MINUTES_MIN,
    WINDOW_START_MAX_HOURS,
    CollectorInput,
)
from src.contracts.databases import DATABASE_ID_PATTERN
from src.contracts.models import MODEL_ID_MAX_LENGTH, MODEL_ID_PATTERN
from src.orchestrator.job_store import UNFINISHED, now_iso, sanitize_collector_input
from src.orchestrator.local_orchestrator import (
    DEFAULT_SIZING_REGION,
    InvalidPhases,
    JobCancelled,
    MissingInput,
    Phase,
    check_plan,
    normalize_phases,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Switches as query parameters, for the live submission.
SizeParam = Annotated[bool, Query(description="Run Sizing & Cost after analysis")]
PriceParam = Annotated[bool, Query(
    description="Price the cluster with the AWS Pricing API (default true). The server "
    "calls AWS with its own credentials. Pass false to size without pricing.")]
RegionParam = Annotated[str, Query(
    pattern=RegionPattern, max_length=REGION_MAX_LENGTH,
    description="AWS region to price in, for example us-east-1")]
ModelPattern = MODEL_ID_PATTERN.pattern
ModelParam = Annotated[str | None, Query(
    pattern=ModelPattern, max_length=MODEL_ID_MAX_LENGTH,
    description="The Bedrock model or inference profile for the AI review. Omit for the "
    "server's default; GET /capabilities lists suggestions.")]
RequireLlmParam = Annotated[bool, Query(
    description="With use_llm, fail the job when the model cannot be used, instead of "
    "completing on the score bands with a warning.")]


def model_kwargs(model_id: str | None, require_llm: bool | None) -> dict:
    """The orchestrator arguments for a model choice, only when one was made."""
    return {**({"model_id": model_id} if model_id else {}),
            **({"require_llm": True} if require_llm else {})}


def model_changes(changes: dict) -> dict:
    """Option changes for a job that reuses another's options (rerun, analyze again).

    Turning the model off without saying anything about require_llm means the model is not
    required either. Otherwise the original's require_llm survives the merge and the
    request is refused with 422 `invalid_options`. An explicit require_llm is kept as
    given, so asking for both still gets the 422.

    Args:
        changes: The options the request set, without the ones it left out.

    Returns:
        The same changes, with require_llm false when use_llm is turned off without it.
    """
    if changes.get("use_llm") is False and changes.get("require_llm") is None:
        return {**changes, "require_llm": False}
    return changes


def check_model_options(use_llm: bool, require_llm: bool) -> None:
    """require_llm only means something when the model was asked for."""
    if require_llm and not use_llm:
        raise ApiError(422, "invalid_options", "require_llm needs use_llm")


def check_window(collector_input: CollectorInput, now: datetime | None = None) -> None:
    """A live collection through the API needs a window, starting within the limit.

    Args:
        collector_input: The submission.
        now: For tests.

    Raises:
        ApiError: 422 window_required without window_minutes; 422 invalid_window when
            start_at is in the past or more than WINDOW_START_MAX_HOURS ahead.
    """
    opts = collector_input.collection_options
    if opts.window_minutes is None:
        raise ApiError(422, "window_required",
                       "collection_options.window_minutes is required for a live "
                       f"collection ({WINDOW_MINUTES_MIN} to {WINDOW_MINUTES_MAX} minutes)")
    if opts.start_at is None:
        return
    now = now or datetime.now(UTC)
    ahead = (opts.start_at - now).total_seconds()
    if ahead < -60:
        raise ApiError(422, "invalid_window", "collection_options.start_at is in the past")
    if ahead > WINDOW_START_MAX_HOURS * 3600:
        raise ApiError(422, "invalid_window",
                       f"collection_options.start_at is more than {WINDOW_START_MAX_HOURS} "
                       "hours ahead")


LabelParam = Annotated[str | None, Query(
    max_length=LABEL_MAX_LENGTH, description="A name for the job, shown in lists")]
PhasesParam = Annotated[list[PhaseName] | None, Query(
    description="Run only these phases, for a job that stops early, for example "
    "phases=collect. They run in pipeline order and must start with collect. Run the rest "
    "later with POST /assessments/{id}/analyze, /size and /report. When given, `size` is "
    "ignored: sizing runs if and only if it is listed.")]


class RunFromCollectionRequest(BaseModel):
    """Run ANALYZE, SIZE and REPORT against an existing collection artifact.

    The path for development, calibration and re-analysis: no live database.
    """

    collector_output: dict = Field(description="A previously collected artifact")
    label: str | None = Field(
        default=None, max_length=LABEL_MAX_LENGTH,
        description="A name for the job, shown in lists")
    use_llm: bool = Field(
        default=False,
        description="Run the AI agent. It takes minutes and calls Amazon Bedrock, "
        "so the job is queued instead of run in the request.",
    )
    model_id: str | None = Field(
        default=None, pattern=ModelPattern, max_length=MODEL_ID_MAX_LENGTH,
        description="The Bedrock model for the AI review. Omit for the server's default.")
    require_llm: bool = Field(
        default=False, description="With use_llm, fail the job when the model cannot be "
        "used, instead of completing on the score bands with a warning.")
    size: bool = Field(default=True, description="Run Sizing & Cost after analysis")
    price: bool = Field(
        default=True,
        description="Price the cluster with the AWS Pricing API. The server calls AWS "
        "with its own credentials, so the job is queued instead of run in the request. "
        "Pass false to size without pricing and get the result in the response.",
    )
    region: str = Field(
        default=DEFAULT_SIZING_REGION, pattern=RegionPattern, max_length=REGION_MAX_LENGTH,
        description="AWS region to price in, for example us-east-1",
    )
    phases: list[PhaseName] | None = Field(
        default=None,
        description="Run only these phases, for example [\"analyze\"]. They run in pipeline "
        "order and must start with analyze. When given, `size` is ignored: sizing runs if "
        "and only if it is listed. Run the rest later with POST /assessments/{id}/size and "
        "/report.")


def _plan(requested: list[str] | None, first: Phase, job_id: str, *,
          has_collector_input: bool, provided: tuple[Phase, ...] = ()) -> list[Phase] | None:
    """Check the phases a submit asked for. None means the usual set.

    Raises:
        ApiError: 422 `invalid_phases` for an empty, unknown or repeated list, one that
            does not start where this kind of job starts, or one with a gap.
    """
    if requested is None:
        return None
    try:
        plan = normalize_phases(requested)
        if plan[0] is not first:
            raise InvalidPhases("a live job starts with collect" if first is Phase.COLLECT
                                else "a job built from a collection starts with analyze")
        check_plan(plan, set(provided), has_collector_input)
    except (InvalidPhases, MissingInput) as e:
        raise ApiError(422, "invalid_phases", str(e)) from e
    return plan


def _label(raw: str | None) -> str | None:
    """Check a label, turning a bad one into a 422."""
    try:
        return clean_label(raw)
    except ValueError as e:
        raise ApiError(422, "invalid_label", str(e)) from e


def _queue(record: JobRecord, background: bool = False) -> None:
    """Store a new job's record, so it is queued from this moment.

    Args:
        record: The job.
        background: True for a job that will wait for a slot and run in the background.
            It is refused when the server already holds as many jobs as it will. The
            count and the creation are one step, so two submits cannot take the last place.

    Raises:
        ApiError: 429 when the server is full, 409 when a job with this id exists.
    """
    store = job_store()
    try:
        if not background:
            store.create(record)
            return
        with limits.admission_lock:
            limits.admit(store)
            store.create(record)
    except FileExistsError as e:
        raise ApiError(409, "job_exists", f"A job named {record.job_id} already exists") from e


@router.post("/assessments", response_model=SubmitResponse,
             responses=error_responses(409, 413, 429))
def create_assessment(
    collector_input: CollectorInput,
    background: BackgroundTasks,
    use_llm: bool = False,
    size: SizeParam = True,
    price: PriceParam = True,
    region: RegionParam = DEFAULT_SIZING_REGION,
    label: LabelParam = None,
    phases: PhasesParam = None,
    model_id: ModelParam = None,
    require_llm: RequireLlmParam = False,
) -> SubmitResponse:
    """Submit a live assessment. Runs COLLECT, ANALYZE, SIZE and REPORT in the background.

    The job is `queued` as soon as this returns: poll ``/assessments/{job_id}/status``.
    `collection_options.window_minutes` is required: the collector measures the
    statistics over that window (422 `window_required`), starting at
    `collection_options.start_at` when given (422 `invalid_window` when it is in the past or
    too far ahead). While the window is open the status carries `collection_window`.
    422 when the endpoint, port, database name, SSH target, database user, key name or
    password file is not in an accepted form, or when the body carries a field this API
    does not take. The same checks as preflight.
    """
    return submit_live(collector_input, background, use_llm=use_llm, size=size, price=price,
                       region=region, label=label, phases=phases, model_id=model_id,
                       require_llm=require_llm)


def submit_live(collector_input: CollectorInput, background: BackgroundTasks, *,
                use_llm: bool = False, size: bool = True, price: bool = True,
                region: str = DEFAULT_SIZING_REGION, label: str | None = None,
                phases: list[str] | None = None, model_id: str | None = None,
                require_llm: bool = False, database_id: str | None = None,
                route: str = "submit") -> SubmitResponse:
    """Check a live submission, record the job as queued, and run it in the background.

    Used by POST /assessments and POST /databases/{id}/assessments, so both apply the same
    checks and limits.

    Raises:
        ApiError: 422 for bad options, window or target values; 429 when the server is
            full; 409 when the job id exists.
    """
    # These values end up on an ssh command line and in a command on the database host.
    # Refuse the ones that could end it, before a job exists.
    check_model_options(use_llm, require_llm)
    check_window(collector_input)
    problems = target_problems(collector_input)
    if problems:
        audit.record("submit_refused", route=route, reason="invalid_target",
                     fields=[p.split(" ")[0] for p in problems])
        raise ApiError(422, "invalid_target", "; ".join(problems))
    job_id = require_valid_job_id(collector_input.job_id or f"job-{uuid.uuid4().hex[:12]}")
    payload = collector_input.model_dump(mode="json")
    payload["job_id"] = job_id
    plan = _plan(phases, Phase.COLLECT, job_id, has_collector_input=True)
    names = [p.value for p in plan] if plan is not None else None

    record = JobRecord(
        job_id=job_id, label=_label(label), source="live", created_at=now_iso(),
        options=JobOptions(use_llm=use_llm, size=size if plan is None else Phase.SIZE in plan,
                           price=price, region=region, model_id=model_id,
                           require_llm=require_llm),
        input=sanitize_collector_input(payload), database_id=database_id)
    _queue(record, background=True)
    # The sanitized input: endpoint, port, database, mode, schemas. No SSH target, key name,
    # database user or password file.
    audit.record("job_submitted", job_id=job_id, source="live", label=record.label,
                 input=record.input.model_dump() if record.input else None,
                 options=record.options.model_dump(), phases=names,
                 **({"database_id": database_id} if database_id else {}))
    background.add_task(_run_job, job_id, payload, use_llm, size, price, region,
                        *([names] if names is not None else []),
                        **model_kwargs(model_id, require_llm))
    logger.info("Queued assessment %s", job_id)
    return SubmitResponse(job_id=job_id, status="queued")


def _run_job(job_id: str, payload: dict, use_llm: bool, size: bool, price: bool,
             region: str, phases: list[str] | None = None, *, model_id: str | None = None,
             require_llm: bool = False) -> None:
    kwargs = {} if phases is None else {"phases": phases}
    with limits.running_slots:  # the job stays queued until a slot is free
        try:
            orchestrator(use_llm, size, price, region,
                         **model_kwargs(model_id, require_llm)).run_job(job_id, payload, **kwargs)
        except JobCancelled:
            logger.info("Assessment %s was cancelled", job_id)
        except Exception:
            # Background tasks swallow exceptions. The orchestrator has already
            # recorded the failure in state.json; log the traceback too.
            logger.exception("Assessment %s failed", job_id)


def _run_from_collection(job_id: str, collector_output: dict, use_llm: bool, size: bool,
                         price: bool, region: str, phases: list[str] | None = None, *,
                         model_id: str | None = None, require_llm: bool = False) -> None:
    kwargs = {} if phases is None else {"phases": phases}
    with limits.running_slots:
        try:
            orchestrator(use_llm, size, price, region,
                         **model_kwargs(model_id, require_llm)).run_from_collection(
                job_id, collector_output, **kwargs)
        except JobCancelled:
            logger.info("Assessment %s was cancelled", job_id)
        except Exception:
            logger.exception("Assessment %s failed", job_id)


@router.post("/assessments/from-collection", response_model=SubmitResponse,
             responses=error_responses(400, 413, 429))
def create_from_collection(
    req: RunFromCollectionRequest, background: BackgroundTasks
) -> SubmitResponse:
    """Analyse, size and report on an existing collection.

    Pricing is on by default, so by default the job is queued and the response is
    ``queued``: poll ``/assessments/{job_id}/status``. With ``price=false`` (or
    ``size=false``) and no model, scoring and sizing take seconds and run in the
    request, and the response is ``complete``.
    """
    check_model_options(req.use_llm, req.require_llm)
    return _submit_collection(
        background, req.collector_output, _label(req.label),
        JobOptions(use_llm=req.use_llm, size=req.size, price=req.price, region=req.region,
                   model_id=req.model_id, require_llm=req.require_llm),
        phases=req.phases)


def _submit_collection(background: BackgroundTasks, collector_output: dict,
                       label: str | None, options: JobOptions, *,
                       rerun_of: str | None = None, known: dict | None = None,
                       phases: list[str] | None = None) -> SubmitResponse:
    """Create a job on a collection, and queue it or run it in the request.

    A job that prices or uses the model is queued. Any other takes seconds and runs
    here, so the response is `complete`.

    Args:
        background: The request's background tasks.
        collector_output: The collection.
        label: A checked label.
        options: What the job should do.
        rerun_of: The job whose collection this is, for a rerun.
        known: Record fields already known (database name, collection time).
        phases: Run only these phases (they must start with analyze). When given, sizing
            runs if and only if it is listed.

    Raises:
        ApiError: 409 for a job id that exists, 422 for bad phases, 400 when the
            in-request run fails.
    """
    job_id = f"{'rerun' if rerun_of else 'local'}-{uuid.uuid4().hex[:12]}"
    plan = _plan(phases, Phase.ANALYZE, job_id, has_collector_input=False,
                 provided=(Phase.COLLECT,))
    names = [p.value for p in plan] if plan is not None else None
    if plan is not None:
        options = options.model_copy(update={"size": Phase.SIZE in plan})
    in_background = options.use_llm or (options.size and options.price)
    _queue(JobRecord(job_id=job_id, label=label, source="collection", created_at=now_iso(),
                     options=options, rerun_of=rerun_of, **(known or {})),
           background=in_background)
    audit.record("job_rerun" if rerun_of else "job_submitted", job_id=job_id,
                 source="collection", rerun_of=rerun_of, label=label,
                 options=options.model_dump(), phases=names)
    if in_background:
        background.add_task(_run_from_collection, job_id, collector_output, options.use_llm,
                            options.size, options.price, options.region,
                            *([names] if names is not None else []),
                            **model_kwargs(options.model_id, options.require_llm))
        return SubmitResponse(job_id=job_id, status="queued")
    try:
        kwargs = {} if names is None else {"phases": names}
        orchestrator(False, options.size, False, options.region).run_from_collection(
            job_id, collector_output, **kwargs)
    except Exception as e:
        logger.exception("Local assessment %s failed", job_id)
        raise ApiError(400, "assessment_failed", f"Assessment failed: {e}") from e
    return SubmitResponse(job_id=job_id, status="complete")


class RerunRequest(BaseModel):
    """What to change for a rerun. Anything left out is what the original job used."""

    label: str | None = Field(
        default=None, max_length=LABEL_MAX_LENGTH,
        description="A name for the new job. Defaults to the original's, followed by (rerun).")
    use_llm: bool | None = Field(default=None, description="Run the AI agent")
    size: bool | None = Field(default=None, description="Run Sizing & Cost")
    price: bool | None = Field(default=None, description="Price with the AWS Pricing API")
    region: str | None = Field(default=None, pattern=RegionPattern, max_length=REGION_MAX_LENGTH,
                               description="AWS region to price in")
    model_id: str | None = Field(default=None, pattern=ModelPattern,
                                 max_length=MODEL_ID_MAX_LENGTH,
                                 description="The Bedrock model for the AI review")
    require_llm: bool | None = Field(
        default=None, description="With use_llm, fail the job when the model cannot be used")

    model_config = ConfigDict(extra="forbid")


@router.post("/assessments/{job_id}/rerun", response_model=SubmitResponse,
             responses=error_responses(404, 413, 429))
def rerun_assessment(job_id: JobId, background: BackgroundTasks,
                     body: RerunRequest | None = None) -> SubmitResponse:
    """Start a new job on the collection a job saved.

    The UX does not have to download the collection and post it back. Use it to run the
    model on a job that ran without it, to price a job that did not, or to analyse again
    after a scoring change. The original job is not changed; the new job's record has
    `rerun_of` set to its id.

    Queued or run in the request by the same rule as from-collection. 404 when the job,
    or its collection, does not exist (a live job that failed while collecting has none).
    """
    body = body or RerunRequest()
    record = job_store().get(job_id)
    if record is None:
        raise ApiError(404, "job_not_found", f"Job {job_id} not found")
    try:
        collection = orchestrator().load(job_id, Phase.COLLECT, "output.json")
    except (FileNotFoundError, ValueError) as e:
        raise ApiError(404, "artifact_not_available",
                       f"Job {job_id} has no saved collection to rerun") from e

    chosen = model_changes(
        {k: v for k, v in body.model_dump(exclude={"label"}).items() if v is not None})
    options = JobOptions.model_validate({**record.options.model_dump(), **chosen})
    check_model_options(options.use_llm, options.require_llm)
    if body.label is not None:
        label = _label(body.label)
    elif record.label:
        label = f"{record.label} (rerun)"[:LABEL_MAX_LENGTH]
    else:
        label = None
    known = {"database_name": record.database_name, "collected_at": record.collected_at}
    logger.info("Rerunning %s", job_id)
    return _submit_collection(background, collection, label, options, rerun_of=job_id,
                              known=known)


@router.get("/assessments", response_model=JobList)
def list_assessments(
    limit: Annotated[int, Query(ge=1, le=LIST_MAX_LIMIT)] = LIST_DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: JobState | None = None,
    q: Annotated[str | None, Query(
        max_length=100, description="Matches the label, job id or database name")] = None,
    database_id: Annotated[str | None, Query(
        pattern=DATABASE_ID_PATTERN, description="Only this saved database's jobs")] = None,
) -> JobList:
    """Jobs, newest first."""
    total, items = job_store().list(limit=limit, offset=offset, status=status, q=q,
                                    database_id=database_id)
    return JobList(total=total, offset=offset, limit=limit, items=items)


@router.delete("/assessments/{job_id}", status_code=204,
               responses=error_responses(404, 409))
def delete_assessment(job_id: JobId) -> Response:
    """Delete a job and every artifact it produced.

    404 when there is no such job. 409 while it is queued or running: cancel it first.
    """
    result = job_store().delete(job_id)
    if result == "not_found":
        raise ApiError(404, "job_not_found", f"Job {job_id} not found")
    if result == "busy":
        raise ApiError(409, "job_busy",
                       f"Job {job_id} is still queued or running. Cancel it first.")
    audit.record("job_deleted", job_id=job_id)
    return Response(status_code=204)


@router.post("/assessments/{job_id}/cancel", response_model=CancelResponse,
             responses=error_responses(404, 409))
def cancel_assessment(job_id: JobId) -> CancelResponse:
    """Ask a queued or running job to stop.

    A queued job is cancelled at once and never starts. A running job stops at the
    next phase boundary: a collection or a model call that has started runs to its
    end, and the job stays `running` (with `cancel_requested` true) until then. A
    cancel that arrives during the last phase has no effect and the job completes.

    404 when there is no such job. 409 when it has already completed or failed.
    Cancelling a cancelled job changes nothing and answers 200.
    """
    result = job_store().request_cancel(job_id)
    if result == "not_found":
        raise ApiError(404, "job_not_found", f"Job {job_id} not found")
    if result == "finished":
        raise ApiError(409, "job_finished",
                       f"Job {job_id} has already finished and cannot be cancelled")
    if result in ("requested", "cancelled"):
        audit.record("job_cancel", job_id=job_id, result=result)
    if result == "requested":
        return CancelResponse(
            job_id=job_id, status="running", cancel_requested=True,
            message="Cancel requested. The job stops when its current phase ends.")
    return CancelResponse(
        job_id=job_id, status="cancelled", cancel_requested=False,
        message="The job was cancelled." if result == "cancelled"
        else "The job was already cancelled.")


@router.get("/assessments/{job_id}/status", response_model=JobStatus,
            responses=error_responses(404))
def get_status(job_id: JobId) -> JobStatus:
    """Where the job is: status, current phase, the history of its phases, any error."""
    state = orchestrator().get_state(job_id)
    if state.get("status") == "not_found":
        raise ApiError(404, "job_not_found", f"Job {job_id} not found")
    status = JobStatus.model_validate(state)
    # Only while the job is unfinished: a request that lost the race to the end of
    # the job leaves a marker behind that means nothing.
    status.cancel_requested = status.status in UNFINISHED and job_store().cancel_requested(job_id)
    status.available = job_store().available_phases(job_id)  # read from the folder
    return status
