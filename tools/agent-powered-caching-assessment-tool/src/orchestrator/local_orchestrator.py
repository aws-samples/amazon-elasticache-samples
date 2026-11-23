"""
Local orchestrator: four-phase caching assessment.

    COLLECT -> ANALYZE -> SIZE -> REPORT

COLLECT reads the database. ANALYZE runs the deterministic scorer and, when
enabled, the AI agent. SIZE runs the Sizing & Cost agent on what ANALYZE
recommended. REPORT renders the self-contained HTML page. Each phase writes its
artifact to the local filesystem, so any phase can be rerun from the one before
it and nothing needs a server, a queue or S3:

    <root>/<job_id>/job.json
    <root>/<job_id>/state.json
    <root>/<job_id>/collect/output.json
    <root>/<job_id>/analyze/analysis.json
    <root>/<job_id>/analyze/decision_trace.json
    <root>/<job_id>/size/sizing.json
    <root>/<job_id>/report/report.html

analyze/analysis.json is never changed after ANALYZE writes it. The sizing block
lives in size/sizing.json, and load_analysis() returns the two together.

job.json (what the job is) and state.json (where it is, with a history of its
phases) belong to the JobStore (job_store.py). The API creates both when it accepts a
job, so a job is `queued` from the moment it is accepted. A direct call creates them
itself.

The model is off by default (use_llm=False), so a laptop run needs no Bedrock
access. Sizing and pricing are on by default, so a run calls the AWS Pricing API
with the caller's credentials. Pass price=False for a run with no AWS access: the
built-in node table is used and cost is marked unavailable. If the Pricing API
cannot be reached the job still completes, with cost marked unavailable.

A job can be cancelled (JobStore.request_cancel). The orchestrator looks for the
request before each phase and, when it finds one, writes the `cancelled` state and
raises JobCancelled. A phase that has started always runs to its end.

Job ids become directory names, so they are validated before use. A failed
COLLECT, ANALYZE or REPORT is recorded in state.json with its error and then
raised, so a job never sits in "running" after it has died. SIZE is an add-on to
a complete analysis: its failure is logged and recorded under "warnings", and the
job continues without a sizing block.

Specs: .kiro/specs/v2-consolidation (VC-3), .kiro/specs/sizing-orchestrator,
.kiro/specs/ux-api.
"""

import json
import logging
import shutil
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from src.contracts.api_models import JobInput, JobOptions, JobRecord
from src.contracts.regions import REGION_PATTERN, valid_region
from src.orchestrator import safe_paths
from src.orchestrator.job_store import (
    JOB_ID_PATTERN,
    PHASE_FILES,
    JobBusy,
    JobNotFound,
    JobStore,
    LocalJobStore,
    mark_cancelled,
    now_iso,
    sanitize_collector_input,
    seconds_between,
    valid_job_id,
)
from src.tools.analysis.caching_scoring import DEFAULT_THRESHOLDS, CacheScoringThresholds

logger = logging.getLogger(__name__)

# Re-exported: the API and the tests import these from here.
__all__ = ["JOB_ID_PATTERN", "valid_job_id", "REGION_PATTERN", "valid_region",
           "DEFAULT_SIZING_REGION", "Phase", "PHASE_ORDER", "LocalOrchestrator",
           "JobCancelled", "JobBusy", "JobNotFound", "InvalidPhases", "MissingInput", "ModelRequired",
           "normalize_phases", "check_plan", "NEEDS"]


class JobCancelled(Exception):
    """The job was cancelled. Raised between phases, after the state says so.

    It is not a failure: the state is already `cancelled` and the artifacts of the
    phases that finished are kept.
    """


class ModelRequired(RuntimeError):
    """require_llm was set and the model could not be used."""


class InvalidPhases(ValueError):
    """The phases asked for are not a usable list: empty, unknown, repeated, or they do
    not start where this kind of job starts."""


class MissingInput(Exception):
    """A phase needs an artifact the job does not have and no earlier phase of the same
    request would make."""

    def __init__(self, phase: "Phase", artifact: str, made_by: "Phase | None"):
        self.phase, self.artifact, self.made_by = phase, artifact, made_by
        if made_by is None:
            msg = f"{phase.value} needs the collector input, which was not given"
        else:
            msg = (f"{phase.value} needs {artifact}, which this job does not have. "
                   f"Run {made_by.value} first.")
        super().__init__(msg)


class Phase(StrEnum):
    COLLECT = "collect"
    ANALYZE = "analyze"
    SIZE = "size"
    REPORT = "report"


PHASE_ORDER = [Phase.COLLECT, Phase.ANALYZE, Phase.SIZE, Phase.REPORT]

# What each phase reads, as the phase that makes it. COLLECT reads the collector input,
# which is not an artifact. REPORT also shows sizing when the job has it, but does not
# need it.
NEEDS: dict[Phase, list[Phase]] = {
    Phase.COLLECT: [],
    Phase.ANALYZE: [Phase.COLLECT],
    Phase.SIZE: [Phase.COLLECT, Phase.ANALYZE],
    Phase.REPORT: [Phase.COLLECT, Phase.ANALYZE],
}

# Record fields that describe an artifact, cleared with it.
_FORGET: dict[Phase, dict] = {
    Phase.COLLECT: {"database_name": None, "collected_at": None},
    Phase.ANALYZE: {"workload_verdict": None, "interpretation_mode": None},
    Phase.SIZE: {"sizing_status": None},
}


def check_plan(plan: list[Phase], have: set[Phase], has_collector_input: bool) -> None:
    """Check that every phase of a plan has what it reads.

    Args:
        plan: The phases, in pipeline order.
        have: The phases whose artifacts are available before the plan runs.
        has_collector_input: Whether the collector input was given.

    Raises:
        MissingInput: for the first phase that lacks an input.
    """
    have = set(have)
    for phase in plan:
        if phase is Phase.COLLECT:
            if not has_collector_input:
                raise MissingInput(phase, "the collector input", None)
        else:
            for need in NEEDS[phase]:
                if need not in have:
                    raise MissingInput(phase, PHASE_FILES[need.value], need)
        have.add(phase)


def normalize_phases(phases: Iterable["Phase | str"]) -> list[Phase]:
    """Check a list of phases and put it in pipeline order.

    Args:
        phases: Phase members or their names.

    Returns:
        The phases in the order they run: collect, analyze, size, report.

    Raises:
        InvalidPhases: when the list is empty, names an unknown phase, or names one twice.
    """
    wanted = list(phases)
    if not wanted:
        raise InvalidPhases("name at least one phase")
    try:
        parsed = [Phase(p) for p in wanted]
    except ValueError:
        raise InvalidPhases("unknown phase: use collect, analyze, size or report") from None
    if len(set(parsed)) != len(parsed):
        raise InvalidPhases("a phase is named twice")
    return [p for p in PHASE_ORDER if p in parsed]

DEFAULT_SIZING_REGION = "us-east-1"


class LocalOrchestrator:
    """Runs the phases in order, persisting each artifact to disk."""

    def __init__(
        self,
        artifact_root: str = "./artifacts",
        use_llm: bool = False,
        thresholds: CacheScoringThresholds = DEFAULT_THRESHOLDS,
        model_id: str | None = None,
        region: str | None = None,
        size: bool = True,
        price: bool = True,
        sizing_region: str = DEFAULT_SIZING_REGION,
        store: JobStore | None = None,
        require_llm: bool = False,
    ):
        """
        Args:
            artifact_root: Folder that holds one sub-folder per job.
            use_llm: Run the AI agent in ANALYZE. False scores only.
            thresholds: Scoring thresholds, recorded in every analysis.
            model_id: Bedrock model id override.
            region: AWS region override for Bedrock.
            size: Run the SIZE phase.
            price: Call the AWS Pricing API in SIZE, with the process's AWS
                credentials. False uses the built-in node table and marks cost
                unavailable.
            sizing_region: Region to price in.
            store: Where job records and state live. Defaults to a LocalJobStore
                over artifact_root.
            require_llm: With use_llm, fail ANALYZE when the model could not be used,
                instead of completing on the score bands with a warning.

        Raises:
            ValueError: when sizing_region is not shaped like a region name.
        """
        if not valid_region(sizing_region):
            raise ValueError(f"invalid sizing region: {sizing_region!r}")
        self.root = Path(artifact_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store: JobStore = store or LocalJobStore(self.root)
        self.use_llm = use_llm
        self.thresholds = thresholds
        self.model_id = model_id
        self.region = region
        self.size = size
        self.price = price
        self.sizing_region = sizing_region
        self.require_llm = require_llm

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    @property
    def options(self) -> JobOptions:
        """The options this orchestrator runs with, as stored on a job record."""
        return JobOptions(use_llm=self.use_llm, size=self.size, price=self.price,
                          region=self.sizing_region, model_id=self.model_id,
                          require_llm=self.require_llm)

    def run_job(self, job_id: str, collector_input: dict,
                phases: list[Phase | str] | None = None) -> dict:
        """Run a new job against a live database.

        Args:
            job_id: Identifier, validated as a directory name.
            collector_input: A CollectorInput payload.
            phases: The phases to run, for a job that stops early, for example
                ["collect"]. They run in pipeline order and must start with collect.
                Default: collect, analyze, sizing (when this orchestrator has size=True)
                and report. Run the rest later with run_phases.

        Returns:
            The last thing the run made: the analysis (CacheAnalysisOutputV2 as a dict)
            with `sizing` attached when sizing ran, or the collection for a collect-only
            job.

        Raises:
            ValueError: for an invalid job id.
            InvalidPhases: for a list that is empty, repeats a phase, or does not start
                with collect.
            MissingInput: for a list with a gap, such as collect and size.
            JobCancelled: when the job was cancelled before or between phases.
            Exception: whatever the failed phase raised, after recording it.
        """
        self._check_job_id(job_id)
        plan = self._plan_new(phases, first=Phase.COLLECT)
        self.check_inputs(job_id, plan, has_collector_input=True)
        logger.info("=== Job %s starting (%s) ===", job_id, ", ".join(p.value for p in plan))
        self._start_job(job_id, "live", sanitize_collector_input(collector_input), plan)
        self._clear_from(job_id, Phase.COLLECT)
        return self._execute(job_id, plan, collector_input=collector_input)

    def run_from_collection(self, job_id: str, collector_output: dict,
                            phases: list[Phase | str] | None = None) -> dict:
        """Run a new job against an existing collection.

        The path for development, calibration and re-analysis: it never touches
        the database, so a scoring or prompt change can be tried on a saved
        collection in seconds.

        Args:
            job_id: Identifier, validated as a directory name.
            collector_output: A collection artifact. It is saved as the job's collection.
            phases: The phases to run, for example ["analyze"]. They run in pipeline
                order and must start with analyze. Default: analyze, sizing (when
                size=True) and report.

        Returns:
            The last thing the run made, normally the analysis (CacheAnalysisOutputV2 as
            a dict).

        Raises:
            InvalidPhases: for a list that is empty, repeats a phase, or does not start
                with analyze.
        """
        self._check_job_id(job_id)
        plan = self._plan_new(phases, first=Phase.ANALYZE)
        logger.info("=== Job %s starting from an existing collection (%s) ===", job_id,
                    ", ".join(p.value for p in plan))
        self._start_job(job_id, "collection", None, plan)
        self._save(job_id, Phase.COLLECT, "output.json", collector_output)
        self._learn_from_collection(job_id, collector_output)
        self._clear_from(job_id, Phase.ANALYZE)
        return self._execute(job_id, plan, collector_output=collector_output)

    def run_phases(self, job_id: str, phases: list[Phase | str],
                   collector_input: dict | None = None, *,
                   claimed_run: int | None = None) -> dict:
        """Run some phases on a job that already exists, one after the other if you like.

        Each phase reads what the earlier ones left in the job's folder, so a job can be
        built a phase at a time: collect, then analyze, then size, then report. A run
        starts fresh: it deletes the artifacts of its first phase and of every later
        phase, then makes them again. Earlier artifacts are kept and used as input.

        Args:
            job_id: The job.
            phases: The phases to run, in any order; they run in pipeline order.
            collector_input: Needed when the run includes collect.
            claimed_run: The run number from `store.claim_run`, when the caller already
                took the job (the API does, before it queues the run). Leave it out and
                this method takes the job itself.

        Returns:
            The last thing the run made.

        Raises:
            JobNotFound: when there is no such job.
            JobBusy: when the job is queued or running.
            InvalidPhases: for a list that is empty, unknown or repeated.
            MissingInput: when a phase needs an artifact the job lacks, before anything
                is changed.
            JobCancelled: when the run was cancelled, or superseded by a later claim.
        """
        self._check_job_id(job_id)
        plan = normalize_phases(phases)
        if self.store.read_state(job_id).get("status") == "not_found":
            raise JobNotFound(job_id)
        try:
            self.check_inputs(job_id, plan, has_collector_input=collector_input is not None)
        except MissingInput as e:
            if claimed_run is not None:       # the caller already took the job; free it
                self._fail_claimed(job_id, claimed_run, str(e))
            raise
        run = claimed_run if claimed_run is not None else self.store.claim_run(
            job_id, [p.value for p in plan])
        return self._run_claimed(job_id, plan, run, collector_input)

    def default_phases(self, *, from_collection: bool = False) -> list[Phase]:
        """The phases a whole run does: sizing only when this orchestrator has size=True."""
        plan = [] if from_collection else [Phase.COLLECT]
        plan.append(Phase.ANALYZE)
        if self.size:
            plan.append(Phase.SIZE)
        plan.append(Phase.REPORT)
        return plan

    def check_inputs(self, job_id: str, plan: list[Phase], *, has_collector_input: bool,
                     provided: Iterable[Phase] = ()) -> None:
        """Refuse a run whose phases lack what they need, before anything changes.

        A phase may use an artifact the job has from before the run, or one an earlier
        phase of the same run makes. Artifacts the run is about to clear (its first phase
        and everything after) do not count as present.

        Args:
            job_id: The job.
            plan: The phases, in pipeline order.
            has_collector_input: Whether the collector input was given.
            provided: Artifacts the caller supplies with the request, such as the collection
                for a job built from one.

        Raises:
            MissingInput: naming the phase, the missing artifact and the phase that makes it.
        """
        first = PHASE_ORDER.index(plan[0])
        have = {Phase(n) for n in self.store.available_phases(job_id)
                if PHASE_ORDER.index(Phase(n)) < first} | set(provided)
        check_plan(plan, have, has_collector_input)

    def would_clear(self, job_id: str, first: Phase) -> list[str]:
        """The phases whose artifacts a run starting at `first` would delete."""
        start = PHASE_ORDER.index(first)
        return [n for n in self.store.available_phases(job_id)
                if PHASE_ORDER.index(Phase(n)) >= start]

    def _plan_new(self, phases: list[Phase | str] | None, *, first: Phase) -> list[Phase]:
        """The phases of a new job. Default when none are given; otherwise checked."""
        from_collection = first is Phase.ANALYZE
        plan = (self.default_phases(from_collection=from_collection) if phases is None
                else normalize_phases(phases))
        if plan[0] is not first:
            raise InvalidPhases("a live job starts with collect" if not from_collection
                                else "a job built from a collection starts with analyze")
        return plan

    # -------------------------------------------------------------------
    # Phases
    # -------------------------------------------------------------------

    def _run_claimed(self, job_id: str, plan: list[Phase], run: int,
                     collector_input: dict | None) -> dict:
        """Start a run the caller has claimed: check it is still wanted, clear, execute."""
        st = self.store.read_state(job_id)
        if st.get("run") != run:
            logger.info("Run %s of job %s was superseded", run, job_id)
            raise JobCancelled(job_id)
        if st.get("status") == "cancelled":
            logger.info("Job %s was cancelled before this run started", job_id)
            raise JobCancelled(job_id)
        self._checkpoint(job_id, None)
        now = now_iso()
        st.update(status="running", started_at=now, updated_at=now)
        self.store.write_state(job_id, st)

        try:
            cleared = self._clear_from(job_id, plan[0])
            if collector_input is not None:
                self.store.update(job_id, input=sanitize_collector_input(collector_input))
            rec = self.store.get(job_id)
            if rec is not None:
                self.store.update(job_id, options=rec.options.model_copy(update={
                    "use_llm": self.use_llm, "price": self.price,
                    "region": self.sizing_region}))
        except Exception as e:
            # Before any phase began, so no phase entry will record this. Do not leave the
            # job saying it is running.
            self._fail_claimed(job_id, run, f"{type(e).__name__}: {e}")
            raise
        logger.info("=== Job %s run %s: %s (cleared: %s) ===", job_id, run,
                    ", ".join(p.value for p in plan), ", ".join(cleared) or "nothing")
        return self._execute(job_id, plan, collector_input=collector_input)

    def _fail_claimed(self, job_id: str, run: int, error: str) -> None:
        """Mark a claimed run failed, if it is still the latest. For a failure before any phase."""
        if self.store.read_state(job_id).get("run") == run:
            self._fail_job(job_id, error)

    def _fail_job(self, job_id: str, error: str) -> None:
        """Mark the job failed, closing any phase still marked running."""
        st = self.store.read_state(job_id)
        now = now_iso()
        for entry in st.get("phases") or []:
            if entry.get("status") == "running":
                entry.update(status="failed", ended_at=now, error=error)
        st.update(status="failed", error=error, updated_at=now, completed_at=now,
                  duration_seconds=seconds_between(st.get("started_at"), now))
        self.store.write_state(job_id, st)

    def _execute(self, job_id: str, plan: list[Phase], *, collector_input: dict | None = None,
                 collector_output: dict | None = None) -> dict:
        """Run the planned phases in order, taking what each needs from memory or disk."""
        warnings: list[str] = []
        analysis: dict | None = None        # the analysis without sizing
        full: dict | None = None            # the analysis as the report will show it
        made: dict | None = None
        try:
            for i, phase in enumerate(plan):
                if phase is Phase.COLLECT:
                    collector_output = made = self._phase(
                        job_id, phase, self._run_collect, job_id, collector_input)
                    # What the collection window could not measure, so the job says so.
                    notes = ((collector_output.get("metadata") or {})
                             .get("collection_window") or {}).get("notes") or []
                    warnings.extend(f"{Phase.COLLECT.value}: {n}" for n in notes)
                elif phase is Phase.ANALYZE:
                    collector_output = collector_output or self._saved(job_id, Phase.COLLECT)
                    analysis = full = made = self._phase(
                        job_id, phase, self._run_analyze, job_id, collector_output)
                    self._learn_from_analysis(job_id, analysis)
                    unused = self._model_not_used(analysis)
                    if unused:
                        warnings.append(f"{Phase.ANALYZE.value}: the model was requested "
                                        f"but not used: {unused}")
                elif phase is Phase.SIZE:
                    collector_output = collector_output or self._saved(job_id, Phase.COLLECT)
                    analysis = analysis or self._saved(job_id, Phase.ANALYZE)
                    # A sizing failure is a warning when the report can still be built. When
                    # sizing was the last thing asked for, nothing else was made, so it fails.
                    full = made = self._size_step(
                        job_id, collector_output, analysis, warnings,
                        fatal=Phase.REPORT not in plan[i + 1:])
                else:
                    collector_output = collector_output or self._saved(job_id, Phase.COLLECT)
                    report_from = full if full is not None else self.load_analysis(job_id)
                    self._phase(job_id, phase, self._run_report, job_id, collector_output,
                                report_from)
                    made = report_from
        except JobCancelled:
            raise
        except Exception as e:
            # A phase records its own failure. This catches one outside a phase, such as
            # a saved artifact that cannot be read, so the job is not left "running".
            if self.store.read_state(job_id).get("status") == "running":
                self._fail_job(job_id, f"{type(e).__name__}: {e}")
            raise
        self._finish_job(job_id, warnings, plan[-1])
        logger.info("=== Job %s complete: %s ===", job_id, ", ".join(p.value for p in plan))
        return made if made is not None else {}

    def _saved(self, job_id: str, phase: Phase) -> dict:
        """The JSON artifact an earlier run left for a phase."""
        return self.load(job_id, phase, PHASE_FILES[phase.value].split("/", 1)[1])

    def _clear_from(self, job_id: str, first: Phase) -> list[str]:
        """Start fresh: delete the artifacts of `first` and every later phase.

        The record fields that describe a deleted artifact are cleared with it.

        Returns:
            The phases whose folders were deleted.
        """
        self._check_job_id(job_id)
        cleared, forget = [], {}
        for phase in PHASE_ORDER[PHASE_ORDER.index(first):]:
            folder = self._job_path(job_id) / phase.value
            if folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder)
                cleared.append(phase.value)
            forget.update(_FORGET.get(phase, {}))
        if forget:
            self.store.update(job_id, **forget)
        return cleared

    def _size_step(self, job_id: str, collector_output: dict, analysis: dict,
                   warnings: list[str], fatal: bool = False) -> dict:
        """Run SIZE and return the analysis with `sizing` attached.

        In a run that goes on to build the report, a failure here does not fail the job.
        The analysis is already complete, SAC handles a pricing outage itself, and any
        other error is a bug that should not cost the user their report. It is logged with
        its traceback and added to `warnings`, which end up in state.json. The returned
        analysis has no `sizing` block in that case, and the report says sizing was not
        run. When sizing is the last phase asked for, `fatal` is true and a failure fails
        the run, because nothing else was made.
        """
        self._checkpoint(job_id, Phase.SIZE)
        logger.info("[%s] Phase: %s", job_id, Phase.SIZE.value.upper())
        self._begin_phase(job_id, Phase.SIZE)
        try:
            sizing = self._run_size(job_id, collector_output, analysis)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if fatal:
                self._end_phase(job_id, Phase.SIZE, "failed", error=msg)
                logger.exception("[%s] Phase %s failed", job_id, Phase.SIZE.value.upper())
                raise
            logger.exception("[%s] Phase %s failed; continuing without sizing",
                             job_id, Phase.SIZE.value.upper())
            warnings.append(f"{Phase.SIZE.value}: {msg}")
            self._end_phase(job_id, Phase.SIZE, "failed", error=msg, fatal=False)
            return analysis
        self._end_phase(job_id, Phase.SIZE, "complete")
        self.store.update(job_id, sizing_status=sizing.get("status"))
        return {**analysis, "sizing": sizing}

    def _phase(self, job_id: str, phase: Phase, fn, *args):
        """Run one phase; on failure record it in state.json and re-raise.

        A cancel requested before the phase stops the job here. Once the phase has
        started it runs to its end.
        """
        self._checkpoint(job_id, phase)
        logger.info("[%s] Phase: %s", job_id, phase.value.upper())
        self._begin_phase(job_id, phase)
        from src.agents.collector.postgres_collector import CollectionCancelled
        try:
            result = fn(*args)
        except CollectionCancelled:
            # Cancelled while the collection window was open: not a failure.
            self.store.write_state(job_id, mark_cancelled(
                self.store.read_state(job_id),
                f"Cancelled during the {phase.value} phase, while the collection window "
                "was open."))
            logger.info("[%s] Cancelled during %s", job_id, phase.value.upper())
            raise JobCancelled(job_id) from None
        except Exception as e:
            self._end_phase(job_id, phase, "failed", error=f"{type(e).__name__}: {e}")
            logger.exception("[%s] Phase %s failed", job_id, phase.value.upper())
            raise
        self._end_phase(job_id, phase, "complete")
        return result

    def _run_collect(self, job_id: str, collector_input: dict) -> dict:
        from src.agents.collector.handler import run_collector

        output = run_collector(
            collector_input,
            should_stop=lambda: self.store.cancel_requested(job_id),
            on_progress=lambda p: self._window_progress(job_id, p))
        self._save(job_id, Phase.COLLECT, "output.json", output)
        self._learn_from_collection(job_id, output)
        return output

    def _window_progress(self, job_id: str, progress: dict) -> None:
        """Put the collection window on the job's state, so a client can count down."""
        st = self.store.read_state(job_id)
        st["collection_window"] = {**progress, "updated_at": now_iso()}
        st["updated_at"] = now_iso()
        self.store.write_state(job_id, st)

    def _run_analyze(self, job_id: str, collector_output: dict) -> dict:
        from src.agents.analysis.handler_v2 import run_analysis_v2

        output, trace = run_analysis_v2(
            collector_output,
            thresholds=self.thresholds,
            model_id=self.model_id,
            region=self.region,
            use_llm=self.use_llm,
        )
        unused = self._model_not_used(output)
        if unused and self.require_llm:
            # Nothing is saved: a band-only analysis is not what this run asked for.
            raise ModelRequired(f"the model was required but not used: {unused}")
        self._save(job_id, Phase.ANALYZE, "analysis.json", output)
        self._save(job_id, Phase.ANALYZE, "decision_trace.json", trace)
        return output

    def _model_not_used(self, analysis: dict) -> str | None:
        """Why the model was not used, when this run asked for it. None otherwise.

        Turning the model off is a choice, not a problem, so a run with use_llm off never
        gets a reason here.
        """
        if not self.use_llm or analysis.get("interpretation_mode") == "llm":
            return None
        return analysis.get("fallback_reason") or "no reason was recorded"

    def _run_size(self, job_id: str, collector_output: dict, analysis: dict) -> dict:
        from src.agents.sizing.handler import run_sizing

        # run_sizing attaches its result to the dict it is given. Give it a copy so
        # the analysis ANALYZE produced is not changed in memory either.
        working = dict(analysis)
        run_sizing(working, collector_output, region=self.sizing_region,
                   price=self.price)
        sizing = working["sizing"]
        self._save(job_id, Phase.SIZE, "sizing.json", sizing)
        return sizing

    def _run_report(self, job_id: str, collector_output: dict, analysis: dict) -> str:
        from src.tools.report.html_report_v2 import build_html_report_v2

        html = build_html_report_v2(collector_output, analysis, job_id=job_id)
        self._save_text(job_id, Phase.REPORT, "report.html", html)
        return html

    # -------------------------------------------------------------------
    # Job record and state
    # -------------------------------------------------------------------

    def _start_job(self, job_id: str, source: str, job_input: JobInput | None,
                   plan: list[Phase]) -> None:
        """Create the job's record if the API has not, and mark the job running.

        A job that already ran under this id starts afresh: its phase history,
        error and warnings are cleared, as its artifacts are about to be rewritten.
        """
        st = self.store.read_state(job_id)
        now = now_iso()
        # A job cancelled while it was queued must not start. Delete it to reuse the id.
        if st.get("status") == "cancelled":
            logger.info("Job %s was cancelled before it started", job_id)
            raise JobCancelled(job_id)
        if st.get("status") in ("complete", "failed"):
            # A cancel that lost the race to the end of the earlier run must not
            # cancel this one.
            self.store.clear_cancel(job_id)
        elif st.get("status") != "not_found":
            self._checkpoint(job_id, None)
        if st.get("status") == "not_found":
            self.store.create(JobRecord(job_id=job_id, source=source, created_at=now,
                                        options=self.options, input=job_input))
            st = self.store.read_state(job_id)
            st["run"] = 1
        elif st.get("status") in ("complete", "failed"):
            for key in ("error", "warnings", "completed_at", "duration_seconds",
                        "started_at"):
                st.pop(key, None)
            st["phases"] = []
            st["run"] = int(st.get("run") or 1) + 1
        st.setdefault("run", 1)
        st["requested"] = [p.value for p in plan]
        st.setdefault("phases", [])
        st["status"] = "running"
        st["updated_at"] = now
        st.setdefault("started_at", now)
        self.store.write_state(job_id, st)

    def _checkpoint(self, job_id: str, next_phase: Phase | None) -> None:
        """Stop here if the job has been cancelled.

        Called before each phase. A phase that has started is not interrupted.

        Args:
            job_id: The job.
            next_phase: The phase about to start, or None before the job starts.

        Raises:
            JobCancelled: after writing the `cancelled` state.
        """
        if not self.store.cancel_requested(job_id):
            return
        where = f"the {next_phase.value} phase" if next_phase else "it started"
        self.store.write_state(
            job_id, mark_cancelled(self.store.read_state(job_id), f"Cancelled before {where}."))
        logger.info("Job %s cancelled before %s", job_id, where)
        raise JobCancelled(job_id)

    def _begin_phase(self, job_id: str, phase: Phase) -> None:
        st = self.store.read_state(job_id)
        now = now_iso()
        st.setdefault("phases", []).append(
            {"phase": phase.value, "status": "running", "started_at": now,
             "ended_at": None, "error": None, "run": st.get("run") or 1})
        st.update(status="running", current_phase=phase.value, updated_at=now)
        if phase is Phase.COLLECT:
            st.pop("collection_window", None)  # from an earlier run
        self.store.write_state(job_id, st)

    def _end_phase(self, job_id: str, phase: Phase, status: str,
                   error: str | None = None, fatal: bool = True) -> None:
        """Close the phase's history entry. A fatal failure fails the whole job."""
        st = self.store.read_state(job_id)
        now = now_iso()
        for entry in reversed(st.get("phases") or []):
            if entry.get("phase") == phase.value and entry.get("status") == "running":
                entry.update(status=status, ended_at=now, error=error)
                break
        st["updated_at"] = now
        if status == "failed" and fatal:
            st.update(status="failed", error=error, completed_at=now,
                      duration_seconds=seconds_between(st.get("started_at"), now))
        self.store.write_state(job_id, st)

    def _finish_job(self, job_id: str, warnings: list[str], last: Phase) -> None:
        st = self.store.read_state(job_id)
        now = now_iso()
        st.update(status="complete", current_phase=last.value, updated_at=now,
                  completed_at=now,
                  duration_seconds=seconds_between(st.get("started_at"), now))
        if warnings:
            st["warnings"] = warnings
        self.store.write_state(job_id, st)

    def _learn_from_collection(self, job_id: str, collection: dict) -> None:
        """Put the database name on the record, once the collection is known."""
        src = (collection.get("metadata") or {}).get("source_database") or {}
        name = src.get("database_name")
        collected = (collection.get("metadata") or {}).get("collection_timestamp")
        fields = {k: v for k, v in (("database_name", name), ("collected_at", collected)) if v}
        if fields:
            self.store.update(job_id, **fields)

    def _learn_from_analysis(self, job_id: str, analysis: dict) -> None:
        """Put the verdict and mode on the record, so a list needs no artifact reads."""
        workload = (analysis.get("llm_interpretation") or {}).get("workload") or {}
        self.store.update(
            job_id,
            workload_verdict=workload.get("overall_recommendation"),
            interpretation_mode=analysis.get("interpretation_mode"),
        )

    # -------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------

    @staticmethod
    def _check_job_id(job_id: str) -> None:
        if not valid_job_id(job_id):
            raise ValueError(
                "invalid job id: use 1 to 64 letters, digits, '.', '_' or '-', "
                "starting with a letter or digit"
            )

    def _job_path(self, job_id: str) -> Path:
        """The job's folder, after its id is checked; refused if it resolves elsewhere."""
        self._check_job_id(job_id)
        return safe_paths.child(self.root, job_id)

    def _job_dir(self, job_id: str) -> Path:
        d = self._job_path(job_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _phase_path(self, job_id: str, phase: Phase, filename: str) -> Path:
        d = self._job_dir(job_id) / phase.value
        d.mkdir(parents=True, exist_ok=True)
        return d / filename

    def _save(self, job_id: str, phase: Phase, filename: str, data) -> Path:
        path = self._phase_path(job_id, phase, filename)
        path.write_text(json.dumps(data, indent=2, default=str))
        logger.debug("Wrote %s", path)
        return path

    def _save_text(self, job_id: str, phase: Phase, filename: str, text: str) -> Path:
        path = self._phase_path(job_id, phase, filename)
        path.write_text(text, encoding="utf-8")
        logger.debug("Wrote %s", path)
        return path

    def load(self, job_id: str, phase: Phase, filename: str) -> dict:
        """Load a JSON artifact.

        Raises:
            FileNotFoundError: when the job or artifact does not exist.
            ValueError: for an invalid job id.
        """
        path = self._job_path(job_id) / phase.value / filename
        return json.loads(path.read_text())

    def load_analysis(self, job_id: str) -> dict:
        """The analysis with its sizing block attached, when sizing ran.

        analyze/analysis.json stays as ANALYZE wrote it; the block is read from
        size/sizing.json. Use this rather than loading analysis.json directly, so
        no reader sees an analysis that is missing its sizing.

        Raises:
            FileNotFoundError: when the job or its analysis does not exist.
            ValueError: for an invalid job id.
        """
        analysis = self.load(job_id, Phase.ANALYZE, "analysis.json")
        try:
            analysis["sizing"] = self.load(job_id, Phase.SIZE, "sizing.json")
        except FileNotFoundError:
            pass
        return analysis

    def load_text(self, job_id: str, phase: Phase, filename: str) -> str:
        """Load a text artifact such as report.html. Raises as load() does."""
        return (self._job_path(job_id) / phase.value / filename).read_text(encoding="utf-8")

    def get_state(self, job_id: str) -> dict:
        """The job's state, or {'status': 'not_found'} (also for an invalid id)."""
        return self.store.read_state(job_id)
