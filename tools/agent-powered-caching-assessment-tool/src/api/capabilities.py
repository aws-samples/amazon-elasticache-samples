"""
What the server can do and the values its inputs accept, for GET /capabilities.

Every figure is read from the constant or the model constraint the routes enforce
(the region pattern, the label length, the bounds of a sizing parameter, the page
sizes), so this document cannot say one thing while a route does another. It makes
no AWS call. Spec: .kiro/specs/ux-api (UX-16).
"""

import importlib.util

from pydantic import BaseModel

from src.api import limits, retention
from src.api.common import (
    AUTHENTICATION,
    LIST_DEFAULT_LIMIT,
    LIST_MAX_LIMIT,
    MAX_CONCURRENT_HOST_PROBES,
    MAX_CONCURRENT_PREFLIGHTS,
    MAX_CONCURRENT_PRICED_WHAT_IFS,
    PAGE_DEFAULT_LIMIT,
    PAGE_MAX_LIMIT,
    REGION_MAX_LENGTH,
)
from src.contracts.api_models import (
    LABEL_MAX_LENGTH,
    Capabilities,
    Features,
    Limits,
    ModelRule,
    OptionInfo,
    ParameterInfo,
    PhaseInfo,
    RegionRule,
    SizingOverrides,
)
from src.contracts.collector_input import (
    WINDOW_SAMPLE_INTERVAL_SEC,
    WINDOW_START_MAX_HOURS,
    CollectionOptions,
    CollectorInput,
)
from src.contracts.models import (
    DEFAULT_MODEL_ID,
    MODEL_ID_MAX_LENGTH,
    MODEL_ID_PATTERN,
    model_suggestions,
)
from src.orchestrator.database_store import MAX_DATABASES
from src.orchestrator.job_store import JOB_ID_PATTERN, PHASE_FILES
from src.orchestrator.local_orchestrator import (
    DEFAULT_SIZING_REGION,
    NEEDS,
    PHASE_ORDER,
    REGION_PATTERN,
    Phase,
)
from src.tools.analysis.cache_sizing import SizingParameters
from src.tools.aws import secrets
from src.version import __version__

JOB_STATES = ["queued", "running", "complete", "failed", "cancelled"]
JOB_ID_MAX_LENGTH = 64

_PHASES = {
    Phase.COLLECT: ("Collect", False, True,
                    "Reads statistics from the database. Only for a live submission."),
    Phase.ANALYZE: ("Analyze", False, True,
                    "Scores every query and table. The AI agent is a separate option."),
    Phase.SIZE: ("Size and cost", True, True,
                 "Sizes what was recommended and picks a Valkey node. Pricing is a "
                 "separate option."),
    Phase.REPORT: ("Report", False, True, "Renders the HTML report."),
}

_PARAMETER_TEXT = {
    name: field.description or ""
    for name, field in SizingOverrides.model_fields.items()
}

NOTES = [
    "Job ids and labels are checked for shape only. A region is checked for shape only: "
    "the server does not know which regions exist.",
    "use_llm needs access to Amazon Bedrock. That is not checked here, because checking "
    "means calling Bedrock.",
    "price (and a priced what-if) calls the AWS Pricing API with the server's own "
    "credentials.",
    "Cancel stops a job at the next phase boundary, or while its collection window is "
    "open. A phase that has started otherwise runs to its end.",
    "A live submission needs collection_options.window_minutes. The collector reads the "
    "statistics at the start and the end of the window and scores the difference, and "
    "samples the host during it. collection_options.start_at schedules the start; the job "
    "is running, and holds a running slot, while it waits.",
    "Preflight checks that SSH connects (the host key must already be in the server's "
    "known_hosts file), the database login, that the SSH host is the database host, and "
    "pg_stat_statements.",
    "Authentication and authorization are not wired. The API accepts local requests only.",
    "A live connection gives exactly one of password_file and password_secret_arn. A secret "
    "is read with the server's own credentials, and only under SECRET_ARN_ALLOWED_PREFIXES.",
    "When use_llm is on and the model cannot be used, the job completes on the score bands "
    "with an analyze warning, unless require_llm is on, which fails it instead.",
]


def bounds(model: type[BaseModel], field: str) -> tuple[float | None, float | None]:
    """The inclusive minimum and maximum a model field declares.

    Args:
        model: A Pydantic model class.
        field: A field name.

    Returns:
        (minimum, maximum). None for a side the field does not limit.
    """
    lo = hi = None
    for meta in model.model_fields[field].metadata:
        lo = getattr(meta, "ge", lo)
        hi = getattr(meta, "le", hi)
    return lo, hi


def sizing_parameters() -> list[ParameterInfo]:
    """The sizing constants a what-if may change, with defaults and bounds."""
    defaults = SizingParameters().as_dict()
    out = []
    for name in SizingOverrides.model_fields:
        lo, hi = bounds(SizingOverrides, name)
        default = defaults[name]
        out.append(ParameterInfo(
            name=name, type="integer" if isinstance(default, int) else "number",
            default=default, minimum=lo, maximum=hi, description=_PARAMETER_TEXT[name]))
    return out


def _model_framework() -> bool:
    """Whether the AI review can run here: strands-agents is importable."""
    return importlib.util.find_spec("strands") is not None


def build_capabilities() -> Capabilities:
    """The capabilities document. Reads constants only; calls nothing."""
    timeout_lo, timeout_hi = bounds(CollectorInput, "timeout_minutes")
    _, pattern_limit_hi = bounds(CollectionOptions, "query_pattern_limit")
    window_lo, window_hi = bounds(CollectionOptions, "window_minutes")
    return Capabilities(
        version=__version__,
        authentication=AUTHENTICATION,
        job_states=JOB_STATES,
        phases=[
            PhaseInfo(phase=p.value, label=_PHASES[p][0], optional=_PHASES[p][1],
                      default_on=_PHASES[p][2], description=_PHASES[p][3],
                      needs=[n.value for n in NEEDS[p]], makes=PHASE_FILES[p.value])
            for p in PHASE_ORDER
        ],
        options=[
            OptionInfo(name="use_llm", type="boolean", default=False,
                       description="An AI model on Amazon Bedrock reviews the scores and "
                       "gives the final verdicts. Adds a few minutes."),
            OptionInfo(name="size", type="boolean", default=True,
                       description="Run Sizing & Cost after analysis."),
            OptionInfo(name="price", type="boolean", default=True,
                       description="Price the cluster with the AWS Pricing API. The job "
                       "is queued."),
            OptionInfo(name="region", type="string", default=DEFAULT_SIZING_REGION,
                       description="AWS region to price in."),
            OptionInfo(name="model_id", type="string", default=DEFAULT_MODEL_ID,
                       description="The Bedrock model that does the AI review. Turn on AI "
                       "review to use it."),
            OptionInfo(name="require_llm", type="boolean", default=False,
                       description="Needs AI review on. If the model cannot be used, fail "
                       "the job instead of finishing on the scores alone with a warning."),
        ],
        model=ModelRule(
            default=DEFAULT_MODEL_ID, suggestions=model_suggestions(),
            pattern=MODEL_ID_PATTERN.pattern, max_length=MODEL_ID_MAX_LENGTH,
            note="Any Bedrock model id, inference profile or ARN the server's account can "
                 "call. The suggestions are a hint, not a limit."),
        region=RegionRule(
            pattern=REGION_PATTERN.pattern, default=DEFAULT_SIZING_REGION,
            max_length=REGION_MAX_LENGTH,
            note="Shape only. The server does not know which regions exist."),
        limits=Limits(
            label_max_length=LABEL_MAX_LENGTH,
            job_id_pattern=JOB_ID_PATTERN.pattern, job_id_max_length=JOB_ID_MAX_LENGTH,
            list_default_limit=LIST_DEFAULT_LIMIT, list_max_limit=LIST_MAX_LIMIT,
            report_page_default_limit=PAGE_DEFAULT_LIMIT,
            report_page_max_limit=PAGE_MAX_LIMIT,
            collection_timeout_minutes_min=int(timeout_lo or 0),
            collection_timeout_minutes_max=int(timeout_hi or 0),
            window_minutes_min=int(window_lo or 0),
            window_minutes_max=int(window_hi or 0),
            window_start_max_hours=WINDOW_START_MAX_HOURS,
            window_sample_interval_seconds=WINDOW_SAMPLE_INTERVAL_SEC,
            query_pattern_limit_max=int(pattern_limit_hi or 0),
            max_concurrent_preflights=MAX_CONCURRENT_PREFLIGHTS,
            max_concurrent_priced_what_ifs=MAX_CONCURRENT_PRICED_WHAT_IFS,
            max_request_bytes=limits.MAX_REQUEST_BYTES,
            max_running_jobs=limits.MAX_RUNNING_JOBS,
            max_queued_jobs=limits.MAX_QUEUED_JOBS,
            max_databases=MAX_DATABASES,
            max_concurrent_host_probes=MAX_CONCURRENT_HOST_PROBES,
            job_retention_days=retention.settings()[0]),
        sizing_parameters=sizing_parameters(),
        features=Features(model_framework=_model_framework(), secrets_manager=secrets.enabled()),
        notes=NOTES,
    )
