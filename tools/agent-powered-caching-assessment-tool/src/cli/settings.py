"""`apcat config`: settings the CLI passes to the API it starts.

The API reads its SSH key folder and known_hosts file from its environment when it starts
(SSH_KEY_DIR, SSH_KNOWN_HOSTS). A coding agent cannot know those, so they live here, in
$APCAT_HOME/config.json (owner-only), and `apcat start` sets them on the API it launches.
An environment variable set in the shell still wins.

Defaults: the key folder is $APCAT_HOME/keys when it exists, so a key put there (for
example by scripts/local-pg) works with no setting. known_hosts stays ssh's own
(~/.ssh/known_hosts) unless set; `apcat trust` adds a host key to it after the user
confirms the fingerprint.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.cli.client import CliError

# setting name -> environment variable it becomes for the API
SETTINGS = {
    "ssh_key_dir": "SSH_KEY_DIR",
    "ssh_known_hosts": "SSH_KNOWN_HOSTS",
    "artifact_root": "ARTIFACT_ROOT",
}


def apcat_home() -> Path:
    return Path(os.environ.get("APCAT_HOME") or Path.home() / ".apcat")


def config_path() -> Path:
    return apcat_home() / "config.json"


def load() -> dict[str, str]:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise CliError("invalid_config", f"{path} is not valid JSON. Fix or delete it.") from e
    return {k: str(v) for k, v in data.items() if k in SETTINGS} if isinstance(data, dict) else {}


def _save(data: dict[str, str]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def set_value(name: str, value: str) -> dict[str, Any]:
    if name not in SETTINGS:
        raise CliError("invalid_request", f"Settings are {', '.join(SETTINGS)}")
    resolved = str(Path(value).expanduser().resolve())
    data = load()
    data[name] = resolved
    _save(data)
    return show()


def unset(name: str) -> dict[str, Any]:
    data = load()
    data.pop(name, None)
    _save(data)
    return show()


def default_key_dir() -> Path:
    return apcat_home() / "keys"


def effective() -> dict[str, dict[str, str | None]]:
    """Each setting's value and where it comes from: env, config, default or none."""
    data = load()
    out: dict[str, dict[str, str | None]] = {}
    for name, env in SETTINGS.items():
        if os.environ.get(env):
            out[name] = {"value": os.environ[env], "source": "environment"}
        elif name in data:
            out[name] = {"value": data[name], "source": "config"}
        elif name == "ssh_key_dir" and default_key_dir().is_dir():
            out[name] = {"value": str(default_key_dir()), "source": "default"}
        else:
            out[name] = {"value": None, "source": "none"}
    return out


def api_env() -> dict[str, str]:
    """The variables `apcat start` sets on the API (artifact_root is handled by servers)."""
    return {SETTINGS[k]: v["value"] for k, v in effective().items()
            if v["value"] and k != "artifact_root"}


def show() -> dict[str, Any]:
    return {"config_file": str(config_path()), "settings": effective()}
