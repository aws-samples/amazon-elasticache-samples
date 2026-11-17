"""Start, find and stop the local API and web console for `apcat start|status|stop`.

Both listen on 127.0.0.1 only: the API refuses any other host (src/api/main.py,
resolve_bind) and the console's Vite config binds 127.0.0.1. This module never sets
API_HOST or passes --host. It starts each server detached, logs to $APCAT_HOME/run, and
records the pid so `apcat stop` stops only what `apcat start` started.

The API gets its SSH settings from src/cli/settings.py (environment, then config, then the
default key folder). What it was started with is recorded in run/api.settings.json, so
status() can say when the running API has other settings than the current ones.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess  # nosec B404 - fixed argv, no shell
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from src.cli import settings
from src.cli.client import ApiClient, CliError, api_base

UI_URL = "http://127.0.0.1:3000"
START_TIMEOUT_SECONDS = 45


apcat_home = settings.apcat_home


def run_dir() -> Path:
    d = apcat_home() / "run"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def repo_root() -> Path | None:
    """The checkout this package runs from (an editable install), or None."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() and (root / "src" / "api").is_dir() else None


def artifact_root() -> Path:
    """Where jobs and saved databases live. The same folder `make api` uses in a checkout."""
    configured = settings.effective()["artifact_root"]["value"]
    if configured:
        return Path(configured).resolve()
    root = repo_root()
    return (root / "artifacts") if root else apcat_home() / "artifacts"


def ui_url() -> str:
    return os.environ.get("APCAT_UI", UI_URL).rstrip("/")


def ui_running(url: str | None = None, timeout: float = 2.0) -> bool:
    """True when the console answers and forwards /health to this tool's API."""
    try:
        with urllib.request.urlopen((url or ui_url()) + "/health", timeout=timeout) as r:  # nosec B310
            return b'"apcat"' in r.read()
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid(name: str) -> int | None:
    p = run_dir() / f"{name}.pid"
    try:
        pid = int(p.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def _spawn(name: str, argv: list[str], cwd: Path, env: dict[str, str]) -> int:
    log = run_dir() / f"{name}.log"
    with open(log, "ab") as out:
        proc = subprocess.Popen(  # nosec B603 - fixed argv, no shell  # nosemgrep: dangerous-subprocess-use-audit
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
            stderr=subprocess.STDOUT, start_new_session=True)
    (run_dir() / f"{name}.pid").write_text(str(proc.pid))
    return proc.pid


def _wait(check: Any, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.5)
    return False


def _started_settings() -> dict[str, str] | None:
    try:
        return json.loads((run_dir() / "api.settings.json").read_text())
    except (OSError, ValueError):
        return None


def warnings(client: ApiClient, api_running: bool) -> list[str]:
    """Things that would make the agent flow fail later, each with the fix."""
    out: list[str] = []
    wanted = settings.api_env()
    if api_running:
        started = _started_settings() if _read_pid("api") is not None else None
        if started is not None and started != wanted:
            out.append("The running API was started with other SSH settings than the current "
                       "ones. Run `apcat start --restart`.")
        elif started is None and wanted.get("SSH_KEY_DIR"):
            try:
                keys = client.get("/ssh-keys")
            except CliError:
                keys = None
            if keys is not None and not keys.get("configured"):
                out.append("The running API was not started by apcat and has no SSH key folder, "
                           f"so keys in {wanted['SSH_KEY_DIR']} cannot be used. Stop it and "
                           "run `apcat start`.")
    from src.cli import agents  # late import: agents imports nothing from here
    for item in agents.status(["claude"])["agents"]:
        if item["state"] == "outdated":
            out.append(f"The installed Claude skill is older than this apcat ({item['path']}). "
                       "Run `apcat agent install`.")
            break
    return out


def status(client: ApiClient | None = None) -> dict[str, Any]:
    """Whether the API and console answer, where, with what settings, and any warnings."""
    client = client or ApiClient.from_env()
    health = client.health()
    ui_ok = ui_running()
    return {
        "api": {"url": client.base, "running": health is not None,
                "version": (health or {}).get("version"), "started_by_apcat": _read_pid("api") is not None},
        "ui": {"url": ui_url(), "running": ui_ok, "started_by_apcat": _read_pid("ui") is not None},
        "artifact_root": str(artifact_root()),
        "settings": settings.effective(),
        "logs": str(run_dir()),
        "warnings": warnings(client, health is not None),
    }


def start(with_ui: bool = True, restart: bool = False) -> dict[str, Any]:
    """Start whatever is not running yet, wait for it, and report like status().

    With restart, first stop the API (and console) that apcat started, so they pick up
    the current settings. A server apcat did not start is never stopped.
    """
    base = api_base()
    client = ApiClient(base)
    notes: list[str] = []
    if restart:
        stopped = stop(("api",))["stopped"]  # the console only forwards; it keeps running
        if stopped:
            _wait(lambda: client.health() is None, 15)
        elif client.health() is not None:
            notes.append("The running API was not started by apcat, so it was not restarted.")
    if client.health() is None:
        port = base.rsplit(":", 1)[-1].split("/")[0]
        env = {k: v for k, v in os.environ.items() if k != "API_HOST"}  # loopback default only
        wanted = settings.api_env()
        env.update(wanted)
        env["ARTIFACT_ROOT"] = str(artifact_root())
        if port.isdigit():
            env["API_PORT"] = port
        cwd = repo_root() or apcat_home()
        _spawn("api", [sys.executable, "-m", "src.api.main"], cwd, env)
        (run_dir() / "api.settings.json").write_text(json.dumps(wanted))
        if not _wait(lambda: client.health() is not None, START_TIMEOUT_SECONDS):
            notes.append(f"The API did not answer within {START_TIMEOUT_SECONDS}s. "
                         f"See {run_dir() / 'api.log'}.")
    if with_ui and not ui_running():
        reason = _ui_unavailable()
        if reason:
            notes.append(reason)
        else:
            root = repo_root()
            assert root is not None  # nosec B101 - _ui_unavailable checked it
            env = dict(os.environ)
            env["APCAT_API"] = base
            npm = shutil.which("npm") or "npm"
            port = ui_url().rsplit(":", 1)[-1]
            argv = [npm, "run", "dev", "--", "--strictPort"] + (["--port", port] if port.isdigit() else [])
            _spawn("ui", argv, root / "ui", env)
            if not _wait(ui_running, START_TIMEOUT_SECONDS):
                notes.append(f"The web console did not answer at {ui_url()} within "
                             f"{START_TIMEOUT_SECONDS}s (is the port taken?). See {run_dir() / 'ui.log'}.")
    out = status(client)
    out["notes"] = notes
    return out


def _ui_unavailable() -> str | None:
    """Why the console cannot be started here, or None when it can."""
    root = repo_root()
    if root is None or not (root / "ui" / "package.json").is_file():
        return ("The web console is not in this install (it runs from a checkout of the "
                "repository). Reports are still served by the API.")
    if not shutil.which("npm"):
        return "The web console needs Node.js 20 or later (npm was not found)."
    if not (root / "ui" / "node_modules").is_dir():
        return f"The web console's dependencies are not installed: run `npm ci` in {root / 'ui'}."
    return None


def stop(names: tuple[str, ...] = ("ui", "api")) -> dict[str, Any]:
    """Stop the servers `apcat start` started. Leaves any other server alone."""
    stopped = []
    for name in names:
        pid = _read_pid(name)
        if pid is not None:
            try:
                os.killpg(pid, signal.SIGTERM)
                stopped.append(name)
            except OSError:
                pass
        (run_dir() / f"{name}.pid").unlink(missing_ok=True)
    return {"stopped": stopped}
