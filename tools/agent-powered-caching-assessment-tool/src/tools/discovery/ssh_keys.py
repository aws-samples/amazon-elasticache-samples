"""
The SSH keys in SSH_KEY_DIR, by name and fingerprint. A key's contents are never returned.

The fingerprint comes from `ssh-keygen -l -E sha256`, run on the key's .pub file when there
is one, otherwise on the key itself; only the bit count, fingerprint and type are kept, never
the comment (which often names a user and host). Spec: .kiro/specs/ssh-host-probe.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

from src.contracts.hosts import SshKeyInfo, SshKeyList
from src.tools.database import ssh_values
from src.tools.discovery.host_probe import run_argv

MAX_KEYS = 50
SKIP = {"known_hosts", "known_hosts.old", "config", "authorized_keys"}
_LINE = re.compile(r"(\d+) (SHA256:[A-Za-z0-9+/]{43}) .*\(([A-Z0-9-]+)\)\s*$")


def list_keys(run: Callable[[list[str], int], tuple[int, str, str]] = run_argv) -> SshKeyList:
    """Every private key file in SSH_KEY_DIR.

    Args:
        run: Runs a command; replaced in tests.

    Returns:
        configured false when SSH_KEY_DIR is not set or not a folder. Otherwise one entry per
        file that is not a .pub, known_hosts or config file, with `usable` false and a
        `problem` when ssh would refuse it (readable by group or others, not a key).
    """
    configured = os.environ.get(ssh_values.KEY_DIR_ENV, "").strip()
    if not configured:
        return SshKeyList(configured=False)
    directory = Path(configured).expanduser()
    if not directory.is_dir():
        return SshKeyList(configured=False)
    items = []
    for p in sorted(directory.iterdir())[: MAX_KEYS * 3]:
        name = p.name
        if (name in SKIP or name.endswith(".pub") or p.is_symlink() or not p.is_file()
                or not ssh_values.key_name_ok(name)):
            continue
        info = SshKeyInfo(name=name, usable=True)
        if stat.S_IMODE(p.stat().st_mode) & 0o077:
            info.usable = False
            info.problem = "Readable by group or others, so ssh refuses it. Run chmod 600 on it."
        pub = p.with_name(name + ".pub")
        rc, out, _err = run(["ssh-keygen", "-l", "-E", "sha256", "-f",
                             str(pub if pub.is_file() else p)], 10)
        m = _LINE.search(out.strip()) if rc == 0 else None
        if m:
            info.fingerprint, info.key_type = m.group(2), m.group(3)
        elif info.usable:
            info.usable = False
            info.problem = "Not an SSH key that ssh-keygen can read."
        items.append(info)
        if len(items) >= MAX_KEYS:
            break
    return SshKeyList(configured=True, items=items)
