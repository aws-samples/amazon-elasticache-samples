"""
Limits on load: request size, jobs running at once, jobs waiting.

All three come from the environment and are read once, when the server starts. A bad
value stops the server with a message rather than falling back, so a typo in a limit is
noticed. GET /capabilities publishes them.

    MAX_REQUEST_BYTES   largest request body, default 64 MiB
    MAX_RUNNING_JOBS    jobs that run at once, default 4
    MAX_QUEUED_JOBS     jobs that wait for a free slot, default 20

A job that would take the total (running plus waiting) past the two job limits is
refused with 429 `queue_full` and no job is created. Jobs run in the server's own
threads; a waiting job holds one, so start-up raises the thread pool by the number of
jobs that can wait or run (see lifespan in main.py), and request handling is never
starved by them.

Spec: .kiro/specs/ux-api (UX-32 to UX-34).
"""

import logging
import os
import threading

from fastapi.responses import JSONResponse

from src.api.errors import ApiError, body
from src.orchestrator.job_store import JobStore

logger = logging.getLogger(__name__)


def env_int(name: str, default: int, minimum: int = 1) -> int:
    """A whole number from the environment.

    Args:
        name: The variable.
        default: Used when it is unset or empty.
        minimum: The smallest accepted value.

    Returns:
        The value.

    Raises:
        SystemExit: when the variable is set to something that is not a whole number of
            at least `minimum`.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        if value < minimum:
            raise ValueError
    except ValueError:
        raise SystemExit(f"{name} must be a whole number of at least {minimum}, got {raw!r}") \
            from None
    return value


MAX_REQUEST_BYTES = env_int("MAX_REQUEST_BYTES", 64 * 1024 * 1024, minimum=1024)
MAX_RUNNING_JOBS = env_int("MAX_RUNNING_JOBS", 4)
MAX_QUEUED_JOBS = env_int("MAX_QUEUED_JOBS", 20, minimum=0)
RETRY_AFTER_SECONDS = 30

# Jobs that run, one slot each. A job waits on this before it starts.
running_slots = threading.BoundedSemaphore(MAX_RUNNING_JOBS)
# The count of unfinished jobs and the creation of the next one are one step.
admission_lock = threading.Lock()


def admit(store: JobStore) -> None:
    """Refuse a background job when the server already holds as many as it will.

    Call with `admission_lock` held, and create the job before releasing it, so two
    submits cannot both take the last place.

    Args:
        store: Where the jobs are.

    Raises:
        ApiError: 429 `queue_full`, with Retry-After.
    """
    capacity = MAX_RUNNING_JOBS + MAX_QUEUED_JOBS
    if store.count_unfinished() >= capacity:
        raise ApiError(
            429, "queue_full",
            f"The server is running or holding {capacity} jobs, its limit. "
            "Try again shortly.",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)})


class BodyLimitMiddleware:
    """Refuse a request body larger than MAX_REQUEST_BYTES, with 413.

    A Content-Length over the limit is refused before anything is read. Otherwise the
    bytes are counted as they arrive, so a chunked body, or a Content-Length that
    understates the body, stops at the limit too. The limit is read on each request.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = MAX_REQUEST_BYTES
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    too_big = int(value) > limit
                except ValueError:
                    too_big = False  # a malformed length is the server's to reject
                if too_big:
                    await _refuse(limit)(scope, receive, send)
                    return
        received = 0

        async def counted_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _too_large(limit)
            return message

        await self.app(scope, counted_receive, send)


def _too_large(limit: int) -> ApiError:
    return ApiError(413, "payload_too_large",
                    f"The request body is larger than the {limit} bytes this server accepts.")


def _refuse(limit: int) -> JSONResponse:
    return JSONResponse(body("payload_too_large", _too_large(limit).detail), status_code=413)
