"""
FastAPI application: caching assessment API.

Submit an assessment (or analyse a saved collection), poll its status, list and
delete jobs, and fetch the analysis, the sizing block, the HTML report, a summary,
or the whole report as JSON (report-data) for a user interface to draw. It is a
thin layer over the local orchestrator and the v2 analyzer.

SECURITY: THIS API HAS NO AUTHENTICATION. DO NOT EXPOSE IT.
------------------------------------------------------------
Its responses carry database schema names, query text (which may contain
literals) and workload figures, and POST /assessments makes the server connect
to a database it is told about. POST /assessments/preflight does the same: it makes the
server open an SSH connection to the host the caller names. A priced sizing run or
POST /assessments/{id}/sizing calls the Pricing API with the server's credentials.
Until real authentication exists:

  * The server binds to 127.0.0.1. run() refuses any other host.
  * A fail-closed gate answers 403 to every request whose peer is not loopback,
    so the app stays closed even when started some other way, for example
    `uvicorn src.api.main:app --host 0.0.0.0`. The gate is a network-origin
    check, NOT authentication: behind a reverse proxy on the same machine every
    request looks local, so never put one in front of this app.
  * CORS allows one configured origin and no credentials.

Release gate before any non-local use: wire a real verifier for the caller,
from an identity provider your environment already trusts (for example an OIDC
provider for people in a browser, or AWS IAM with SigV4 for services). Do not
write your own. Add action-level authorization on top (who may submit an
assessment against which database), then replace local_only_gate below with the
verifier.

Spec: .kiro/specs/v2-consolidation (VC-4).
"""

import asyncio
import contextlib
import ipaddress
import logging
import os
import uuid
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import TypeAdapter

from src.api import audit, common, errors, limits, retention
from src.api.routes import (
    agent,
    assessments,
    databases,
    discovery,
    hosts,
    maintenance,
    meta,
    phases,
    preflight,
    results,
    whatif,
)
from src.contracts.agent_chat import AgentEvent
from src.contracts.api_models import Health
from src.orchestrator.job_store import LocalJobStore
from src.version import __version__

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def sweep_unfinished_jobs() -> list[str]:
    """Mark jobs left queued or running as failed.

    Jobs run inside this process, so when it starts none of them is alive. Assumes
    one server process, which is how `python -m src.api.main` runs. Reads the
    artifact folder at call time.

    Returns:
        The ids of the jobs it changed.
    """
    return LocalJobStore(common.ARTIFACT_ROOT).interrupt_unfinished()


# Threads for handling requests. Jobs that run or wait hold threads of their own, on top.
REQUEST_THREADS = 40


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """At start-up, fail any job a previous run of the server left unfinished.

    Also sizes the thread pool: a job that waits for a free slot holds a thread, so the
    pool is raised by the number of jobs that can run or wait, and request handling
    keeps its own threads however many jobs are queued.
    """
    sweep_unfinished_jobs()
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = max(
        limiter.total_tokens, REQUEST_THREADS + limits.MAX_RUNNING_JOBS + limits.MAX_QUEUED_JOBS)

    # Retention is off unless JOB_RETENTION_DAYS is set. When on: once now, then on a timer.
    days, interval = retention.settings()
    sweeper = None
    if days:
        await anyio.to_thread.run_sync(retention.sweep)
        sweeper = asyncio.create_task(retention.run_forever(interval))
    try:
        yield
    finally:
        if sweeper:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper


def operation_id(route: APIRoute) -> str:
    """The route's function name, so a generated client has readable method names."""
    return route.name


# Every operation can answer these, whatever its own errors are.
COMMON_ERRORS = errors.error_responses(403, 422, 500)

app = FastAPI(
    title="Agent Powered Caching Assessment Tool",
    description=(
        "Is this PostgreSQL workload a good candidate for a cache? "
        "NO AUTHENTICATION: loopback only. Do not expose."
    ),
    version=__version__,
    lifespan=lifespan,
    generate_unique_id_function=operation_id,
)


errors.install(app)


def _openapi_with_json_errors() -> dict:
    """The API document, with every error response described as JSON.

    FastAPI describes a declared error in the route's own media type, so the HTML
    report route would claim to return its errors as text/html. They are JSON.
    """
    schema = FastAPI.openapi(app)
    # The chat route streams AgentEvents; FastAPI does not add a model it never returns.
    events = TypeAdapter(AgentEvent).json_schema(ref_template="#/components/schemas/{model}")
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for name, definition in events.pop("$defs", {}).items():
        components.setdefault(name, definition)
    components["AgentEvent"] = events
    for ops in schema["paths"].values():
        for op in ops.values():
            stream = op["responses"].get("200", {}).get("content", {}).get("text/event-stream")
            if stream:
                stream["schema"] = {"$ref": "#/components/schemas/AgentEvent"}
    for ops in schema["paths"].values():
        for op in ops.values():
            for status, resp in op["responses"].items():
                content = resp.get("content")
                if status.isdigit() and int(status) >= 400 and content:
                    resp["content"] = {"application/json": next(iter(content.values()))}
    return schema


app.openapi = _openapi_with_json_errors  # type: ignore[method-assign]

# Innermost, so it counts the bytes the application reads and the error it raises reaches
# FastAPI directly (between the gate and the app, Starlette would turn it into a 400). The
# gate and CORS are outside it, so a 413 still carries the CORS headers a browser needs.
app.add_middleware(limits.BodyLimitMiddleware)


def is_loopback(host: str | None) -> bool:
    """Whether a peer address is the local machine.

    Args:
        host: Peer address as the server sees it, or None.

    Returns:
        True for 127.0.0.0/8 and ::1 (including an IPv4-mapped ::ffff:127.x).
        False for anything else, including a missing or unparsable address, so a
        peer the server cannot identify is refused.
    """
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


@app.middleware("http")
async def local_only_gate(request: Request, call_next):
    """Fail-closed: refuse every request that does not come from this machine.

    This stands in for authentication, which is not wired. Replace it with the
    real verifier (see the module docstring), do not loosen it.
    """
    peer = request.client.host if request.client else None
    request_id = uuid.uuid4().hex
    request.state.request_id = request_id  # for the error handlers, which run outside this
    if not is_loopback(peer):
        logger.warning("Refused a non-local request from %s", peer)
        return JSONResponse(
            status_code=403,
            content=errors.body("local_only", "This API accepts local requests only and "
                                              "has no authentication."),
            headers={"X-Request-ID": request_id},
        )
    token = audit.bind(peer, request_id)
    try:
        response = await call_next(request)
    finally:
        audit.unbind(token)
    response.headers["X-Request-ID"] = request_id
    return response


# Locked to a single configured origin rather than a wildcard.
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
    expose_headers=["X-Request-ID", "Retry-After"],
)

for _router, _tag in ((meta.router, "server"), (preflight.router, "assessments"),
                      (maintenance.router, "assessments"), (assessments.router, "assessments"),
                      (phases.router, "phases"),
                      (whatif.router, "results"), (results.router, "results"),
                      (discovery.router, "databases"), (databases.router, "databases"),
                      (hosts.router, "hosts"),
                      (agent.router, "agent")):
    app.include_router(_router, prefix="/api/v1", tags=[_tag], responses=COMMON_ERRORS)


@app.get("/health", tags=["ops"], response_model=Health, responses=COMMON_ERRORS)
def health() -> Health:
    """Liveness and the release number."""
    return Health(status="ok", service="apcat", version=__version__,
                  authentication=common.AUTHENTICATION)


def resolve_bind() -> tuple[str, int]:
    """The host and port to listen on, from API_HOST and API_PORT.

    Returns:
        (host, port). The host defaults to 127.0.0.1.

    Raises:
        SystemExit: when API_HOST is not a loopback address, or API_PORT is not a
            valid port. A non-loopback bind is refused because the API has no
            authentication.
    """
    host = os.environ.get("API_HOST", DEFAULT_HOST)
    if not is_loopback(host):
        raise SystemExit(
            f"Refusing to listen on {host!r}: this API has no authentication, so it "
            "binds to loopback only. See the module docstring of src/api/main.py."
        )
    raw_port = os.environ.get("API_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit(f"API_PORT must be a port number, got {raw_port!r}") from None
    return host, port


def run() -> None:
    """Start the API on loopback: `python -m src.api.main`."""
    import uvicorn

    host, port = resolve_bind()
    logger.info("API %s on http://%s:%d (no authentication, loopback only)",
                __version__, host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
