"""
Build a path from a name that came from a request, so it can never leave its folder.

WHY
---
Job ids and saved-database ids arrive in API requests and become folder and file names
under ARTIFACT_ROOT. Each id is first checked against its pattern (valid_job_id,
valid_database_id), which already rules out separators and "..". This is the second
check, in the standard form: resolve the full path and confirm it is still directly
inside the folder it belongs to. It also refuses a symlink, even one that points inside
the folder, so a planted link can never make an operation act on another entry.
"""

from __future__ import annotations

import os
from pathlib import Path


def child(root: str | Path, name: str) -> Path:
    """root/name, refused unless it resolves to an entry directly inside root.

    Args:
        root: The folder the entry must stay in.
        name: One path component from a request, already checked against its pattern.

    Returns:
        The resolved path of the entry. It need not exist.

    Raises:
        ValueError: the name would resolve outside root or to root itself, or the entry
            is a symlink.
    """
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, name))
    if not full.startswith(base + os.sep) or os.path.dirname(full) != base:
        raise ValueError("path outside its folder")
    # base has no links left, so the entry is a link exactly when resolving it changed it.
    if full != os.path.normpath(os.path.join(base, name)):
        raise ValueError("path is a symlink")
    return Path(full)


def child_or_none(root: str | Path, name: str) -> Path | None:
    """Like child(), but None instead of an error, for lookups that answer "not found"."""
    try:
        return child(root, name)
    except ValueError:
        return None
