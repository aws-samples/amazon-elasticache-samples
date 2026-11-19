"""
The chat agent's tool catalog, mapped one to one to API operations.

Every tool's input schema is a pydantic model, so the model sees the same constraints the
routes enforce (job id pattern, region pattern, the sizing bounds). Two kinds of tool:

  server  Read-only. Runs here, in-process, through the route function itself, so it
          answers exactly what GET on that route answers. Render tools (present_answer,
          open_report) also run here: they only tell the browser to draw something.
  client  Changes something (submit, cancel, rerun, delete, what-if, preflight, a saved
          database's check or run), or needs
          values the model must not see (connection details, a collection file). Never
          runs here. The stream stops with action_required, the browser shows the exact
          parameters, and only after the user confirms does it call the real route.

Spec: .kiro/specs/chat-agent.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agents.chat import elasticache_skill
from src.api.capabilities import build_capabilities
from src.api.common import LIST_MAX_LIMIT, PAGE_MAX_LIMIT, REGION_MAX_LENGTH, RegionPattern
from src.api.errors import ApiError
from src.api.routes import assessments, databases, discovery, results
from src.contracts.api_models import LABEL_MAX_LENGTH, JobState, SizingOverrides, SizingSummary
from src.contracts.cache_sizing_output import CacheSizingOutput
from src.contracts.collector_input import WINDOW_MINUTES_MAX, WINDOW_MINUTES_MIN
from src.contracts.databases import DATABASE_ID_PATTERN, DatabaseConnection, DatabaseView
from src.contracts.models import MODEL_ID_MAX_LENGTH, MODEL_ID_PATTERN
from src.orchestrator.job_store import JOB_ID_PATTERN

JOB_ID = JOB_ID_PATTERN.pattern
ToolKind = Literal["read", "render", "confirm", "form"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoInput(_In):
    pass


class JobRef(_In):
    job_id: str = Field(pattern=JOB_ID, max_length=64, description="The assessment id")


class ListInput(_In):
    q: str | None = Field(default=None, max_length=100,
                          description="Matches the label, job id or database name")
    status: JobState | None = None
    limit: int = Field(default=10, ge=1, le=LIST_MAX_LIMIT)


class ReportItemsInput(JobRef):
    kind: Literal["query", "table"]
    recommended: bool | None = Field(default=None, description="Only recommended items")
    ruled_out: bool | None = Field(default=None, description="Tables only: only ruled-out ones")
    limit: int = Field(default=10, ge=1, le=PAGE_MAX_LIMIT)
    offset: int = Field(default=0, ge=0)


class AskInput(JobRef):
    kind: Literal["query", "table", "workload"]
    item_id: str | None = Field(default=None, max_length=256,
                                description="The queryid or table_id. Omit for the workload.")


class Citation(_In):
    label: str = Field(max_length=80, description="What the signal is, e.g. Pressure")
    value: str = Field(max_length=500, description="The value exactly as the report shows it")
    field: str = Field(max_length=200, description="The report field it came from, e.g. "
                       "metrics.display.score or concerns[0]")


class PresentAnswerInput(AskInput):
    answer: str = Field(max_length=4000, description="The answer, in plain text")
    citations: list[Citation] = Field(default_factory=list, max_length=20)
    grounded: bool = Field(description="False when the report cannot support an answer")


class OpenReportInput(JobRef):
    view: Literal["full", "html"] = Field(
        default="full", description="full: the report page; html: the backend's HTML report")


class FormInput(_In):
    reason: str = Field(default="", max_length=300, description="One line on why it is needed")


class ConnectionRef(_In):
    connection_id: str = Field(min_length=1, max_length=64,
                               description="From request_connection_details")


class StartInput(_In):
    connection_id: str | None = Field(default=None, max_length=64,
                                      description="For a live database. One per database: "
                                      "call start_assessment once for each")
    collection_id: str | None = Field(default=None, max_length=64,
                                      description="For a saved collection")
    label: str | None = Field(default=None, max_length=LABEL_MAX_LENGTH)
    use_llm: bool | None = Field(default=None, description="Run the AI review")
    model_id: str | None = Field(default=None, pattern=MODEL_ID_PATTERN.pattern,
                                 max_length=MODEL_ID_MAX_LENGTH,
                                 description="Only when the user names a model")
    require_llm: bool | None = Field(default=None, description="Fail if the model cannot "
                                     "be used. Only with use_llm")
    size: bool | None = Field(default=None, description="Run Sizing & Cost")
    price: bool | None = Field(default=None, description="Price with the AWS Pricing API")
    region: str | None = Field(default=None, pattern=RegionPattern, max_length=REGION_MAX_LENGTH)


class RerunInput(JobRef):
    label: str | None = Field(default=None, max_length=LABEL_MAX_LENGTH)
    use_llm: bool | None = None
    model_id: str | None = Field(default=None, pattern=MODEL_ID_PATTERN.pattern,
                                 max_length=MODEL_ID_MAX_LENGTH)
    require_llm: bool | None = None
    size: bool | None = None
    price: bool | None = None
    region: str | None = Field(default=None, pattern=RegionPattern, max_length=REGION_MAX_LENGTH)


class WhatIfInput(JobRef):
    region: str | None = Field(default=None, pattern=RegionPattern, max_length=REGION_MAX_LENGTH)
    price: bool | None = None
    parameters: SizingOverrides = Field(default_factory=SizingOverrides)


class DatabaseRef(_In):
    database_id: str = Field(pattern=DATABASE_ID_PATTERN, max_length=64,
                             description="A saved database id, from list_databases")


class RunDatabaseInput(DatabaseRef):
    window_minutes: int | None = Field(default=None, ge=WINDOW_MINUTES_MIN,
                                       le=WINDOW_MINUTES_MAX,
                                       description="How long to measure, in minutes. Ask the "
                                       "user if they did not say")
    start_at: str | None = Field(default=None, max_length=40, description="When to start, "
                                 "ISO 8601 with a time zone. Omit to start at once")
    label: str | None = Field(default=None, max_length=LABEL_MAX_LENGTH)
    use_llm: bool | None = Field(default=None, description="Run the AI review")
    model_id: str | None = Field(default=None, pattern=MODEL_ID_PATTERN.pattern,
                                 max_length=MODEL_ID_MAX_LENGTH,
                                 description="Only when the user names a model")
    require_llm: bool | None = Field(default=None, description="Fail if the model cannot "
                                     "be used. Only with use_llm")
    size: bool | None = Field(default=None, description="Run Sizing & Cost")
    price: bool | None = Field(default=None, description="Price with the AWS Pricing API")
    region: str | None = Field(default=None, pattern=RegionPattern, max_length=REGION_MAX_LENGTH)
    allow_degraded: bool | None = Field(default=None, description="Run although "
                                        "pg_stat_statements is not usable. Only when the "
                                        "user agreed to degraded mode")


class GuideInput(_In):
    path: str = Field(pattern=elasticache_skill.GUIDE_PATH.pattern, max_length=120,
                      description="A guide the skill router names, e.g. "
                      "references/data-modeling/instructions.md")


# ---------------------------------------------------------------------------
# Results, kept small: the model reads them, and the browser redraws from the API.
# ---------------------------------------------------------------------------


_NEEDS = {"analyze": "analysis", "size": "sizing", "report": "HTML report"}


def not_ready(job_id: str, need: str) -> dict | None:
    """None when the job holds what a report tool needs; otherwise where it is.

    Lets the model and the browser show progress instead of an error when someone asks
    about a report that is still being made.
    """
    st = assessments.get_status(job_id)
    if need in st.available:
        return None
    if st.status in ("queued", "running"):
        where = f"is {st.status}" + (f", in the {st.current_phase} phase" if st.current_phase else "")
        detail = f"The {_NEEDS[need]} is not ready yet: the assessment {where}."
    elif st.status == "complete":
        detail = f"This assessment has no {_NEEDS[need]}: that phase was not part of the run."
    else:
        detail = f"This assessment {st.status} before its {_NEEDS[need]} was made."
    return {"ready": False, "job_id": job_id, "status": st.status,
            "current_phase": st.current_phase, "available": st.available,
            "error": st.error, "detail": detail}


def _when_ready(need: str, run: Callable[[Any], dict]) -> Callable[[Any], dict]:
    def guarded(i: Any) -> dict:
        return not_ready(i.job_id, need) or run(i)
    return guarded


def _jobs(i: ListInput) -> dict:
    page = assessments.list_assessments(limit=i.limit, offset=0, status=i.status, q=i.q)
    keep = ("job_id", "label", "status", "current_phase", "database_name", "source",
            "workload_verdict", "interpretation_mode", "created_at", "available")
    return {"total": page.total, "items": [{k: getattr(j, k) for k in keep} for j in page.items]}


def _status(i: JobRef) -> dict:
    return assessments.get_status(i.job_id).model_dump()


def _summary(i: JobRef) -> dict:
    return results.get_summary(i.job_id).model_dump()


def _report(i: JobRef) -> dict:
    o = results.get_report_overview(i.job_id)
    return {
        "meta": o.meta.model_dump(),
        "source": o.source.line,
        "headline": o.headline.model_dump(),
        "at_a_glance": [g.model_dump(include={"id", "label", "display", "note"}) for g in o.at_a_glance],
        "keep_in_mind": o.keep_in_mind,
        "workload": [w.model_dump(include={"label", "display", "note"}) for w in o.workload],
        "counts": o.counts,
        "scoring": o.scoring.model_dump(include={"formula", "bands", "bands_note", "judge_note"}),
        "not_cacheable": [{"label": g.label, "count": g.count} for g in o.not_cacheable],
        "ruled_out_tables": [r.model_dump() for r in o.ruled_out_tables],
        "bundles": len(o.bundles),
        "sizing": SizingSummary.from_sizing(o.sizing).model_dump() if o.sizing else None,
    }


def _item_row(item: Any, kind: str) -> dict:
    m = item.metrics
    row = {
        "verdict_label": item.verdict_label, "recommended": item.recommended,
        "score": m.display.score, "hard_excluded": m.hard_excluded, "not_scored": m.not_scored,
        "db_ms_per_sec": m.display.db_ms_per_sec,
    }
    if kind == "query":
        row.update(queryid=item.queryid, rank=item.rank, cache_type=item.cache_type,
                   sql_short=item.sql_short)
    else:
        row.update(table_id=item.table_id, ruled_out=item.ruled_out)
    return row


def _items(i: ReportItemsInput) -> dict:
    if i.kind == "query":
        page = results.list_report_queries(i.job_id, verdict=None, cache_type=None,
                                           recommended=i.recommended, limit=i.limit,
                                           offset=i.offset)
    else:
        page = results.list_report_tables(i.job_id, verdict=None, recommended=i.recommended,
                                          ruled_out=i.ruled_out, limit=i.limit, offset=i.offset)
    return {"total": page.total, "offset": page.offset,
            "items": [_item_row(x, i.kind) for x in page.items]}


def _ask(i: AskInput) -> dict:
    """The grounding pack for one item: its own fields, and how the run was judged."""
    o = results.get_report_overview(i.job_id)
    run = o.meta.model_dump(include={"assessment_mode", "degraded_reason",
                                     "interpretation_mode", "fallback_reason", "used_llm"})
    pack: dict[str, Any] = {"run": run, "bands": [b.model_dump() for b in o.scoring.bands],
                            "bands_note": o.scoring.bands_note}
    if i.kind == "workload":
        pack["report"] = _report(JobRef(job_id=i.job_id))
        return pack
    if not i.item_id:
        raise ApiError(422, "invalid_tool_input", "item_id is required for a query or table")
    if i.kind == "query":
        item = results.get_report_query(i.job_id, i.item_id)
        pack["item"] = item.model_dump(exclude={"sql", "anchor"})
        for g in o.not_cacheable:
            for nc in g.items:
                if nc.queryid == i.item_id:
                    pack["not_cacheable_group"] = {"label": g.label, "why": nc.why}
    else:
        item = results.get_report_table(i.job_id, i.item_id)
        pack["item"] = item.model_dump(exclude={"anchor"})
        reason = next((r.reason for r in o.ruled_out_tables if r.table_id == i.item_id), None)
        if reason:
            pack["ruled_out_reason"] = reason
    return pack


def _sizing(i: JobRef) -> dict:
    s = CacheSizingOutput.model_validate(results.get_sizing(i.job_id))
    return {
        "summary": SizingSummary.from_sizing(s).model_dump(),
        "node": s.node.model_dump(include={"instance_type", "shards", "replicas_per_shard",
                                           "total_nodes", "node_memory_gb", "burstable"})
        if s.node else None,
        "assumptions": s.assumptions, "warnings": s.warnings,
    }


def _guide_error(e: elasticache_skill.GuideError) -> ApiError:
    """A guide failure as the tool error the model reads."""
    return ApiError(404 if e.code == "guide_not_found" else 503, e.code, e.detail)


def _caching_router(_i: NoInput) -> dict:
    try:
        return elasticache_skill.skill_router()
    except elasticache_skill.GuideError as e:
        raise _guide_error(e) from e


def _caching_guide(i: GuideInput) -> dict:
    try:
        return elasticache_skill.read_guide(i.path)
    except elasticache_skill.GuideError as e:
        raise _guide_error(e) from e


# ---------------------------------------------------------------------------
# Saved databases and discovery. The model gets ids, names, readiness and counts: never a
# host, SSH target, key name, database user, password source or path. The browser cards
# redraw the full rows from the API, so the user still sees them.
# ---------------------------------------------------------------------------

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def _connection_values(conn: DatabaseConnection) -> list[str]:
    """The connection's values, lower case, that a label must not carry to the model.

    Args:
        conn: A saved connection.

    Returns:
        The SSH target, its user and host parts, the key name, the database role, the
        password source and the database host when it is not a loopback address.
    """
    user, _, host = conn.ssh_target.rpartition("@")
    values = [conn.ssh_target, host, user, conn.identity_file, conn.db_user,
              conn.password_file, conn.password_secret_arn,
              None if conn.cluster_endpoint.lower() in _LOOPBACK else conn.cluster_endpoint]
    return [v.lower() for v in values if v]


def model_label(label: str, conn: DatabaseConnection) -> str | None:
    """A database label as the model may see it.

    The default label names the SSH host, and a typed one may too, so a label that holds
    any connection value is withheld.

    Args:
        label: The saved label.
        conn: The saved connection.

    Returns:
        The label, or None when it holds a connection value.
    """
    low = label.lower()
    return None if any(v in low for v in _connection_values(conn)) else label


def database_for_model(v: DatabaseView) -> dict:
    """A saved database without its connection.

    Args:
        v: The database as GET /databases/{id} answers it.

    Returns:
        Its id, label (or None when withheld), database name, readiness, counts and latest
        assessments.
    """
    label = model_label(v.label, v.connection)
    return {
        "database_id": v.database_id, "label": label, "label_withheld": label is None,
        "database_name": v.connection.database_name, "discovered_from": v.discovered_from,
        "readiness": v.readiness.model_dump(), "assessments": v.assessments,
        "running": v.running,
        "latest": v.latest.model_dump() if v.latest else None,
        "latest_complete": v.latest_complete.model_dump() if v.latest_complete else None,
        "created_at": v.created_at, "updated_at": v.updated_at,
    }


def _databases(_i: NoInput) -> dict:
    """The list_databases tool: GET /databases without connections.

    Returns:
        {total, items}, each item as database_for_model() gives it.
    """
    page = databases.list_databases()
    return {"total": page.total, "items": [database_for_model(v) for v in page.items]}


def _database(i: DatabaseRef) -> dict:
    """The get_database tool: GET /databases/{id} without the connection.

    Args:
        i: The database id.

    Returns:
        The database as database_for_model() gives it.

    Raises:
        ApiError: 404 database_not_found, which run_server_tool hands to the model.
    """
    return database_for_model(databases.get_database(i.database_id))


# What a find may tell the model. Not its label, which names hosts, aliases and paths.
_FIND_KEYS = {"id", "source", "kind", "database", "version", "runs", "last_assessed_at",
              "last_verdict"}


def _discover(_i: NoInput) -> dict:
    """The discover_databases tool: the four local sources, never a host probe.

    The input takes no host, so the model can never ask for an SSH probe of a host it
    named; the user types a host in the connect form instead. The route function runs,
    so its concurrency limit (429 discovery_busy) and audit entry apply.

    Returns:
        When it ran, each find's id, source, kind, database name, version and earlier
        runs, and each source's status and count. No label, host, alias, port or path.
    """
    r = discovery.discover_databases(discovery.DiscoverRequest())
    return {
        "discovered_at": r.discovered_at,
        "servers": [s.model_dump(include=_FIND_KEYS) for s in r.servers],
        "sources": [s.model_dump(include={"source", "status", "found"}) for s in r.sources],
    }


def _shown(_i: BaseModel) -> dict:
    return {"shown": True}


def _open_report(i: OpenReportInput) -> dict:
    return not_ready(i.job_id, "report" if i.view == "html" else "analyze") or {
        "shown": True, "view": i.view}


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    kind: ToolKind
    run: Callable[[Any], dict] | None = None  # None: runs in the browser

    @property
    def executor(self) -> Literal["server", "client"]:
        return "server" if self.run is not None else "client"


TOOLS: tuple[Tool, ...] = (
    Tool("get_capabilities", "Options, defaults, limits and rules this server accepts "
         "(GET /capabilities).", NoInput, "read", lambda _i: build_capabilities().model_dump()),
    Tool("list_assessments", "Assessments, newest first (GET /assessments).", ListInput, "read",
         _jobs),
    Tool("get_status", "Where an assessment is: status, phases, warnings "
         "(GET /assessments/{id}/status). The browser shows it as a live progress card.",
         JobRef, "read", _status),
    Tool("get_summary", "Headline figures of a finished assessment "
         "(GET /assessments/{id}/summary). Returns ready=false with the status while it is "
         "still running.", JobRef, "read", _when_ready("analyze", _summary)),
    Tool("get_report", "The report overview: verdict, at a glance, workload, counts, scoring "
         "bands (GET /assessments/{id}/report-data/overview). The browser shows a compact "
         "report card. Returns ready=false with the status while it is still running.",
         JobRef, "read", _when_ready("analyze", _report)),
    Tool("list_report_items", "A page of the report's queries or tables, in report order "
         "(GET .../report-data/queries or /tables). ready=false while still running.",
         ReportItemsInput, "read", _when_ready("analyze", _items)),
    Tool("ask_report", "Everything the report records about one query, one table, or the "
         "workload, plus how the run was judged. Call before answering any why question. "
         "ready=false while still running.", AskInput, "read", _when_ready("analyze", _ask)),
    Tool("get_sizing", "Sizing & Cost of an assessment (GET /assessments/{id}/sizing). "
         "ready=false while still running.", JobRef, "read", _when_ready("size", _sizing)),
    Tool("list_databases", "The saved databases, newest first, each with its readiness, "
         "assessment count and latest assessment (GET /databases). No connection details; a "
         "label that names a host is withheld. The browser shows the list.", NoInput, "read",
         _databases),
    Tool("get_database", "One saved database: readiness with the failed and warned check "
         "names, the PostgreSQL version, its assessment count, latest and latest complete "
         "assessment (GET /databases/{id}). No connection details.", DatabaseRef, "read",
         _database),
    Tool("discover_databases", "Look for PostgreSQL servers on this machine: server "
         "processes, SSH config hosts, pg_service entries and earlier assessments "
         "(POST /databases/discover, local sources only). You get ids, sources, database "
         "names and versions; the user sees each find with a Connect button. Takes no host: "
         "to look on another host the user types it in the connect form.", NoInput, "read",
         _discover),
    Tool("list_caching_guides", "The router of the AWS ElastiCache skill: its sub-skills "
         "(requirements, setup, data modeling, GenAI, monitoring, migration), the phrases that "
         "select each, its guardrails and the shared guides. Call first for a general question "
         "about caching architectures, Valkey on ElastiCache, or adding a cache to an "
         "application. Fetched on first use.", NoInput, "read", _caching_router),
    Tool("read_caching_guide", "One guide of the AWS ElastiCache skill, by a path the router "
         "names (references/<folder>/<name>.md). Read only the guides the question needs.",
         GuideInput, "read", _caching_guide),
    Tool("present_answer", "Show a cited answer about the report. Every citation value must "
         "be copied exactly from ask_report's result.", PresentAnswerInput, "render", _shown),
    Tool("open_report", "Show a link to the full report page or the HTML report.",
         OpenReportInput, "render", _open_report),
    Tool("request_connection_details", "Ask the user for the live database connection in a "
         "form. Returns a connection_id; the details never reach you.", FormInput, "form"),
    Tool("request_collection_file", "Ask the user for a saved collection file in a form. "
         "Returns a collection_id.", FormInput, "form"),
    Tool("request_host_probe", "Ask the user to probe an SSH host in a form: they type the "
         "host and pick a key. Returns the stages (resolve, reachable, host_key, sign_in, shell, "
         "postgresql, databases) with statuses, the first failure's message and fix, the host "
         "key state, the clusters found, postgres_access (sudo, no_sudo, failed, not_checked) "
         "and the databases listed through sudo as postgres. Never the host, user or key.",
         FormInput, "form"),
    Tool("run_preflight", "Check a live connection before submitting "
         "(POST /assessments/preflight). The user confirms first.", ConnectionRef, "confirm"),
    Tool("start_assessment", "Start an assessment of a live database (connection_id) or a "
         "saved collection (collection_id) (POST /assessments or /from-collection). The user "
         "confirms first.", StartInput, "confirm"),
    Tool("cancel_assessment", "Ask a queued or running assessment to stop "
         "(POST /assessments/{id}/cancel). The user confirms first.", JobRef, "confirm"),
    Tool("rerun_assessment", "Start a new assessment on the collection one saved "
         "(POST /assessments/{id}/rerun). The user confirms first.", RerunInput, "confirm"),
    Tool("delete_assessment", "Delete an assessment and its artifacts "
         "(DELETE /assessments/{id}). The user confirms by typing its id.", JobRef, "confirm"),
    Tool("whatif_sizing", "Size a finished assessment again with other settings; saves "
         "nothing (POST /assessments/{id}/sizing). The user confirms first.", WhatIfInput,
         "confirm"),
    Tool("check_database", "Run the readiness checks on a saved database and store its "
         "readiness (POST /databases/{id}/check). Connects to its host over SSH. The user "
         "confirms first.", DatabaseRef, "confirm"),
    Tool("run_database_assessment", "Start an assessment of a saved database with run "
         "options only (POST /databases/{id}/assessments). Needs a passed check: readiness "
         "ready or warnings, or degraded with allow_degraded. The user confirms first and "
         "can change the window and the options.", RunDatabaseInput, "confirm"),
)

BY_NAME = {t.name: t for t in TOOLS}


def _inline(schema: dict) -> dict:
    """The JSON schema with $refs inlined and titles dropped, as a tool spec wants it."""
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(dict(defs[node["$ref"].rsplit("/", 1)[-1]]))
            return {k: walk(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def tool_specs() -> list[dict]:
    """The catalog as Bedrock Converse tool specs."""
    return [{"toolSpec": {"name": t.name, "description": t.description,
                          "inputSchema": {"json": _inline(t.input_model.model_json_schema())}}}
            for t in TOOLS]


def run_server_tool(tool: Tool, raw: dict) -> tuple[Literal["success", "error"], dict]:
    """Validate a server tool's input and run it.

    Returns:
        ("success", result) or ("error", {code, detail}). Never raises for a bad input or
        an API error, so the model sees what went wrong and the turn goes on.
    """
    assert tool.run is not None
    try:
        args = tool.input_model.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, p['loc']))}: {p['msg']}" for p in e.errors())
        return "error", {"code": "invalid_tool_input", "detail": problems}
    try:
        return "success", tool.run(args)
    except ApiError as e:
        return "error", {"code": e.code, "detail": str(e.detail)}
