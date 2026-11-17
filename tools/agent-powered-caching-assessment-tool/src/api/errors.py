"""
One error body for the whole API: {"code": "...", "detail": ...}.

`code` is a short stable word a client can switch on. `detail` is the human text. For a
validation error `detail` is still the list FastAPI produces, so nothing that read it
before breaks; `code` is added next to it.

Every route that can return an error declares it with `error_responses(...)`, so
`/openapi.json` describes the body and a generated client knows the cases. A test checks
that each operation declares what it can return.

An unexpected exception answers 500 `internal_error` with fixed text: the traceback is
logged and never sent.

Spec: .kiro/specs/ux-api (UX-30).
"""

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# The code for a status that carries no more specific one.
STATUS_CODES = {
    400: "bad_request",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "invalid_request",
    429: "too_many_requests",
    500: "internal_error",
}

_DESCRIPTIONS = {
    400: "The request could not be processed",
    403: "The request did not come from this machine (no authentication is wired)",
    404: "No such job, artifact or item",
    409: "The job is in a state that does not allow this",
    413: "The request body is larger than the server accepts",
    422: "A value is missing, out of range or not in an accepted form",
    429: "The server is at its limit for this. Retry after the time in Retry-After",
    500: "An unexpected error. Nothing internal is returned",
}


class ApiErrorBody(BaseModel):
    """What every error response carries."""

    code: str = Field(description="A short stable word to switch on, such as job_not_found")
    detail: str | list[Any] = Field(
        description="Human text. For a validation error, the list of problems.")


class ApiError(HTTPException):
    """An HTTP error with a specific code."""

    def __init__(self, status_code: int, code: str, detail: str,
                 headers: dict[str, str] | None = None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """The `responses=` entry for a route that can return these statuses."""
    return {s: {"model": ApiErrorBody, "description": _DESCRIPTIONS[s]} for s in statuses}


def body(code: str, detail: Any) -> dict[str, Any]:
    """An error body, for code that builds its own response (a middleware)."""
    return {"code": code, "detail": detail}


async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = getattr(exc, "code", None) or STATUS_CODES.get(exc.status_code, "error")
    return JSONResponse(body(code, exc.detail), status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # Where the problem is, what it is, and its kind. Not pydantic's `input`, which is the
    # offending value itself: a password, a path or a whole collection sent in the wrong
    # place would be sent straight back.
    errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")}
              for e in exc.errors()]
    return JSONResponse(body("invalid_request", jsonable_encoder(errors)), status_code=422)


async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    logger.error("Unhandled error on %s %s (request %s)", request.method, request.url.path,
                 request_id, exc_info=exc)
    return JSONResponse(body("internal_error", "Internal server error"), status_code=500,
                        headers={"X-Request-ID": request_id} if request_id else None)


def install(app: FastAPI) -> None:
    """Route every error through the one body."""
    app.add_exception_handler(StarletteHTTPException, _http_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unexpected_error)
