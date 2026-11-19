"""A JSON client of the local API, loopback only.

The API has no authentication and answers loopback callers only, so the client refuses
any other address before it sends anything. Standard library only.
"""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_API = "http://127.0.0.1:8000"
API_PREFIX = "/api/v1"

# (method, url, body or None, timeout) -> (status, parsed JSON or text)
Transport = Callable[[str, str, Any, float], tuple[int, Any]]


class CliError(Exception):
    """A failure to report as {"error": code, "detail": ...} with exit code 1."""

    def __init__(self, code: str, detail: Any, status: int | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status
        self.extra: dict[str, Any] = {}

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.code, "detail": self.detail}
        if self.status is not None:
            out["http_status"] = self.status
        out.update(self.extra)
        return out


def is_loopback_url(url: str) -> bool:
    """True when the URL is http(s) to localhost or a loopback address."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if parts.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        return False


def api_base() -> str:
    """The API address: APCAT_API, else http://127.0.0.1:8000. Refuses non-loopback."""
    base = os.environ.get("APCAT_API", DEFAULT_API).rstrip("/")
    if not is_loopback_url(base):
        raise CliError("not_loopback",
                       "APCAT_API must be a loopback address (127.0.0.1 or localhost): the "
                       "API has no authentication and must not be reached over a network.")
    return base


def _urllib_transport(method: str, url: str, body: Any, timeout: float) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - loopback only
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())


def _parse(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


@dataclass
class ApiClient:
    """Calls /api/v1 routes and raises CliError for any error answer."""

    base: str
    transport: Transport = _urllib_transport

    @classmethod
    def from_env(cls) -> ApiClient:
        return cls(api_base())

    def call(self, method: str, path: str, body: Any = None, *,
             query: dict[str, Any] | None = None, timeout: float = 60.0,
             prefix: str = API_PREFIX) -> Any:
        url = self.base + prefix + path
        if query:
            q = {k: _qv(v) for k, v in query.items() if v is not None}
            if q:
                url += "?" + urllib.parse.urlencode(q, doseq=True)
        try:
            status, data = self.transport(method, url, body, timeout)
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            raise CliError("api_unreachable",
                           f"The API is not answering at {self.base}. Run `apcat start`.") from e
        if status >= 400:
            if isinstance(data, dict) and "code" in data:
                raise CliError(str(data["code"]), data.get("detail"), status)
            raise CliError("http_error", data, status)
        return data

    def get(self, path: str, **kw: Any) -> Any:
        return self.call("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.call("POST", path, body, **kw)

    def health(self, timeout: float = 2.0) -> dict[str, Any] | None:
        """GET /health, or None when nothing (or not this tool's API) answers."""
        try:
            data = self.call("GET", "/health", timeout=timeout, prefix="")
        except CliError:
            return None
        return data if isinstance(data, dict) and data.get("service") == "apcat" else None


def _qv(v: Any) -> Any:
    if isinstance(v, bool):
        return "true" if v else "false"
    return v
