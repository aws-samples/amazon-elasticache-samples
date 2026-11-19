"""
Run one component of a job on its own: collect, analyze, size or report.

A job builds up phase by phase, each call on the same job id:

    POST /assessments/{id}/collect    needs the collector input in the body
    POST /assessments/{id}/analyze    needs the collection
    POST /assessments/{id}/size       needs the collection and the analysis
    POST /assessments/{id}/report     needs the collection and the analysis

Each run starts fresh: it deletes the artifact of its own phase and of every later phase,
then makes them again. Earlier artifacts are kept and used as input. The response lists
what the run replaces (`replaces`). A job is `complete` when the phases it was asked for
are done; `available` on the status says which artifacts it holds.

Spec: .kiro/specs/component-runs.
"""

import logging

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, ConfigDict, Field

from src.agents.collector.target_checks import target_problems
from src.api import audit, limits
from src.api.common import REGION_MAX_LENGTH, JobId, RegionPattern, job_store, orchestrator
from src.api.errors import ApiError, error_responses
from src.api.routes.assessments import check_model_options, check_window, model_kwargs
from src.contracts.api_models import JobOptions, PhaseRunResponse
from src.contracts.collector_input import CollectorInput
from src.contracts.models import MODEL_ID_MAX_LENGTH, MODEL_ID_PATTERN
from src.orchestrator.job_store import JobBusy, JobNotFound
from src.orchestrator.local_orchestrator import JobCancelled, MissingInput, Phase

logger = logging.getLogger(__name__)
router = APIRouter()


class CollectRequest(CollectorInput):
    """The body of a live submit. `job_id` is not needed: the job is the one in the path."""

    job_id: str = Field(default="phase", description="Ignored: the job is the one in the path")


class AnalyzeRequest(BaseModel):
    """What to change for an analysis. Anything left out is what the job used."""

    use_llm: bool | None = Field(
        default=None, description="Run the AI agent. It takes minutes and calls Amazon "
        "Bedrock, so the run is queued.")
    model_id: str | None = Field(default=None, pattern=MODEL_ID_PATTERN.pattern,
                                 max_length=MODEL_ID_MAX_LENGTH,
                                 description="The Bedrock model for the AI review")
    require_llm: bool | None = Field(
        default=None, description="With use_llm, fail the run when the model cannot be used")

    model_config = ConfigDict(extra="forbid")


class SizeRequest(BaseModel):
    """What to change for sizing. Anything left out is what the job used."""

    price: bool | None = Field(
        default=None, description="Call the AWS Pricing API with the server's credentials. "
        "The run is then queued.")
    region: str | None = Field(
        default=None, pattern=RegionPattern, max_length=REGION_MAX_LENGTH,
        description="AWS region to price in")

    model_config = ConfigDict(extra="forbid")


def _start(job_id: str, phase: Phase, background: BackgroundTasks, *,
           collector_input: dict | None = None, **changes) -> PhaseRunResponse:
    """Claim the job for one phase and run it, or queue it.

    A phase that collects, calls the model, or calls the Pricing API is queued. The others
    take seconds and run in the request.

    Raises:
        ApiError: 404 when the job or an input it needs is missing, 409 when it is busy,
            429 when the server is full, 400 when a run inside the request fails.
    """
    store = job_store()
    record = store.get(job_id)
    if record is None:
        raise ApiError(404, "job_not_found", f"Job {job_id} not found")
    options: JobOptions = record.options.model_copy(
        update={k: v for k, v in changes.items() if v is not None})
    check_model_options(options.use_llm, options.require_llm)
    orch = orchestrator(options.use_llm, True, options.price, options.region,
                        **model_kwargs(options.model_id, options.require_llm))
    try:
        orch.check_inputs(job_id, [phase], has_collector_input=collector_input is not None)
    except MissingInput as e:
        raise ApiError(404, "artifact_not_available", str(e)) from e
    replaces = orch.would_clear(job_id, phase)

    in_background = (phase is Phase.COLLECT
                     or (phase is Phase.ANALYZE and options.use_llm)
                     or (phase is Phase.SIZE and options.price))
    try:
        with limits.admission_lock:
            if in_background:
                limits.admit(store)
            run = store.claim_run(job_id, [phase.value])
    except JobNotFound as e:
        raise ApiError(404, "job_not_found", f"Job {job_id} not found") from e
    except JobBusy as e:
        raise ApiError(409, "job_busy",
                       f"Job {job_id} is queued or running. Wait for it, or cancel it.") from e

    audit.record("phase_run", job_id=job_id, phase=phase.value, run=run, replaces=replaces,
                 options=options.model_dump())
    if in_background:
        background.add_task(_run_phase_job, job_id, phase, collector_input, options, run)
        return PhaseRunResponse(job_id=job_id, status="queued", phase=phase.value, run=run,
                                replaces=replaces)
    try:
        orch.run_phases(job_id, [phase], collector_input, claimed_run=run)
    except JobCancelled as e:
        raise ApiError(409, "job_cancelled", f"Job {job_id} was cancelled") from e
    except Exception as e:
        logger.exception("Phase %s of %s failed", phase.value, job_id)
        raise ApiError(400, "phase_failed", f"{phase.value} failed: {e}") from e
    return PhaseRunResponse(job_id=job_id, status="complete", phase=phase.value, run=run,
                            replaces=replaces)


def _run_phase_job(job_id: str, phase: Phase, collector_input: dict | None,
                   options: JobOptions, run: int) -> None:
    with limits.running_slots:  # the job stays queued until a slot is free
        try:
            orchestrator(options.use_llm, True, options.price, options.region,
                         **model_kwargs(options.model_id, options.require_llm)).run_phases(
                job_id, [phase], collector_input, claimed_run=run)
        except JobCancelled:
            logger.info("Phase %s of %s was cancelled", phase.value, job_id)
        except Exception:
            # Background tasks swallow exceptions. The orchestrator has recorded the
            # failure in state.json; log the traceback too.
            logger.exception("Phase %s of %s failed", phase.value, job_id)


@router.post("/assessments/{job_id}/collect", response_model=PhaseRunResponse,
             responses=error_responses(404, 409, 413, 429))
def run_collect_phase(job_id: JobId, req: CollectRequest,
                      background: BackgroundTasks) -> PhaseRunResponse:
    """Collect from the database again, on its own. Always queued.

    The body is the same as a live submit. This deletes the job's collection and every
    later artifact (analysis, sizing, report) and makes the collection again; run analyze,
    size and report after it. `replaces` lists what is deleted. 422 when the endpoint,
    port, database name, SSH target, database user, key name or password file is not in
    an accepted form.
    """
    check_window(req)
    problems = target_problems(req)
    if problems:
        audit.record("submit_refused", route="collect", reason="invalid_target",
                     fields=[p.split(" ")[0] for p in problems])
        raise ApiError(422, "invalid_target", "; ".join(problems))
    payload = req.model_dump(mode="json")
    payload["job_id"] = job_id
    return _start(job_id, Phase.COLLECT, background, collector_input=payload)


@router.post("/assessments/{job_id}/analyze", response_model=PhaseRunResponse,
             responses=error_responses(400, 404, 409, 413, 429))
def run_analyze_phase(job_id: JobId, background: BackgroundTasks,
                      body: AnalyzeRequest | None = None) -> PhaseRunResponse:
    """Analyze the job's collection again, on its own.

    Deletes the job's analysis, sizing and report and makes the analysis again. Run size
    and report after it. 404 when the job has no collection. Queued when it uses the
    model; otherwise it runs in the request.
    """
    body = body or AnalyzeRequest()
    return _start(job_id, Phase.ANALYZE, background, use_llm=body.use_llm,
                  model_id=body.model_id, require_llm=body.require_llm)


@router.post("/assessments/{job_id}/size", response_model=PhaseRunResponse,
             responses=error_responses(400, 404, 409, 413, 429))
def run_size_phase(job_id: JobId, background: BackgroundTasks,
                   body: SizeRequest | None = None) -> PhaseRunResponse:
    """Size and price the job's analysis, on its own, and save the result with the job.

    Deletes the job's sizing and report and makes the sizing again; run report after it.
    404 when the job has no analysis. Queued when it prices; otherwise it runs in the
    request. To try other settings without changing the job, use POST .../sizing.
    """
    body = body or SizeRequest()
    return _start(job_id, Phase.SIZE, background, price=body.price, region=body.region)


@router.post("/assessments/{job_id}/report", response_model=PhaseRunResponse,
             responses=error_responses(400, 404, 409))
def run_report_phase(job_id: JobId, background: BackgroundTasks) -> PhaseRunResponse:
    """Build the HTML report again, on its own, from the job's analysis and sizing.

    It shows sizing when the job has it. Runs in the request. 404 when the job has no
    analysis or collection.
    """
    return _start(job_id, Phase.REPORT, background)
