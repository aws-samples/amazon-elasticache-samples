"""
SSH host aliases from ~/.ssh/config, so the user can pick a database host.

Reads Host blocks only. Patterns (*, ?, !) and Match blocks are skipped, Include lines are not
followed, and IdentityFile paths are never returned. No host is contacted.
Spec: .kiro/specs/database-discovery (DISC-1, DISC-2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from src.tools.database import ssh_values

MAX_FILE_BYTES = 256 * 1024
MAX_HOSTS = 200
_HOSTNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,254}")
_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
_KEYWORD_LINE = re.compile(r"([A-Za-z]+)(?:\s*=\s*|\s+)(.*)")


@dataclass
class SshHost:
    """One plain Host alias with the values the tool could use."""

    alias: str
    hostname: str | None = None
    user: str | None = None
    port: int | None = None


@dataclass
class SshConfigResult:
    """The aliases found, and notes about what was skipped."""

    hosts: list[SshHost] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def default_path() -> Path:
    """The user's ssh config file."""
    return Path.home() / ".ssh" / "config"


def parse_ssh_config(text: str) -> SshConfigResult:
    """Read Host blocks from ssh config text.

    Args:
        text: The file's content.

    Returns:
        Plain aliases (no wildcard, no negation) whose name is a valid SSH target, in file
        order, with HostName, User and Port when set and well formed.
    """
    result = SshConfigResult()
    by_alias: dict[str, SshHost] = {}
    current: list[SshHost] = []
    skipped_patterns = included = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # "Keyword value" or "Keyword=value"; only the separator after the keyword counts.
        m = _KEYWORD_LINE.fullmatch(line)
        if not m:
            continue
        key, value = m.group(1).lower(), m.group(2).strip()
        if key == "match":
            current = []
            continue
        if key == "include":
            included += 1
            continue
        if key == "host":
            current = []
            for alias in value.split():
                if any(c in alias for c in "*?!"):
                    skipped_patterns += 1
                    continue
                if not ssh_values.target_ok(alias) or alias.startswith("-"):
                    continue
                # An alias in several blocks is one host; like ssh, the first value wins.
                if alias in by_alias:
                    current.append(by_alias[alias])
                elif len(result.hosts) < MAX_HOSTS:
                    h = SshHost(alias=alias)
                    by_alias[alias] = h
                    result.hosts.append(h)
                    current.append(h)
            continue
        for h in current:
            if key == "hostname" and h.hostname is None and _HOSTNAME.fullmatch(value):
                h.hostname = value
            elif key == "user" and h.user is None and _USER.fullmatch(value):
                h.user = value
            elif key == "port" and h.port is None and value.isdigit() and 1 <= int(value) <= 65535:
                h.port = int(value)
    if skipped_patterns:
        result.notes.append(f"Skipped {skipped_patterns} wildcard or negated Host pattern(s).")
    if included:
        result.notes.append(f"Did not follow {included} Include line(s).")
    if len(result.hosts) >= MAX_HOSTS:
        result.notes.append(f"Listed the first {MAX_HOSTS} hosts only.")
    return result


def read_ssh_config(path: Path | None = None) -> SshConfigResult | None:
    """Read and parse the ssh config file.

    Args:
        path: The file. Defaults to ~/.ssh/config.

    Returns:
        The parsed hosts, or None when the file does not exist.

    Raises:
        OSError: the file exists but cannot be read.
        ValueError: the file is larger than MAX_FILE_BYTES or is not text.
    """
    p = path or default_path()
    if not p.is_file():
        return None
    if p.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("the ssh config file is too large to read")
    data = p.read_bytes()
    try:
        return parse_ssh_config(data.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise ValueError("the ssh config file is not text") from e
