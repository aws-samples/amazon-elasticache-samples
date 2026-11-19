"""
The shape of every value that reaches an `ssh` argument or a remote shell command.

A live job names a host, a user, a key and a password file. Those end up on the local `ssh`
command line and in a command run on the database host, so each is checked with a full match
(not `$`, which also accepts a trailing newline) before anything runs. A target cannot start
with a dash, so it cannot be read as an option. A key is a file name inside a directory the
operator chose, never a path.

Messages name the field and never echo the value.

Spec: .kiro/specs/ssh-collection (SSH-2 to SSH-5).
"""

import os
import re
import stat
from pathlib import Path

# The host part may start with ":" (an IPv6 address such as ::1) or contain "_" (a name from
# ssh_config). It can never start with "-", so it cannot be read as an option.
TARGET = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9._-]{0,63}@)?[A-Za-z0-9_:][A-Za-z0-9._:-]{0,253}")
DB_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_$-]{0,62}")
KEY_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
_PASSWORD_FILE = re.compile(r"/[A-Za-z0-9_./-]{1,255}")

KNOWN_HOSTS_ENV = "SSH_KNOWN_HOSTS"
KEY_DIR_ENV = "SSH_KEY_DIR"


def target_ok(value: str) -> bool:
    """Whether `value` is a host, user@host, or a name from ssh_config."""
    return bool(TARGET.fullmatch(value))


def db_user_ok(value: str) -> bool:
    """Whether `value` is a PostgreSQL role name in a safe form."""
    return bool(DB_USER.fullmatch(value))


def key_name_ok(value: str) -> bool:
    """Whether `value` is a plain file name (no path, no leading dot or dash)."""
    return bool(KEY_NAME.fullmatch(value)) and not value.startswith(".")


def password_file_ok(value: str) -> bool:
    """Whether `value` is an absolute path in a safe form, with no `..` segment."""
    return bool(_PASSWORD_FILE.fullmatch(value)) and ".." not in value.split("/")


def known_hosts_path() -> Path:
    """Where the known_hosts file is, whether or not it exists yet.

    Returns:
        `SSH_KNOWN_HOSTS` when set, otherwise `~/.ssh/known_hosts`.
    """
    configured = os.environ.get(KNOWN_HOSTS_ENV, "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".ssh" / "known_hosts"


def known_hosts_file() -> Path:
    """The known_hosts file ssh must check the host key against.

    Returns:
        `SSH_KNOWN_HOSTS` when set, otherwise `~/.ssh/known_hosts`.

    Raises:
        FileNotFoundError: the file does not exist. ssh would otherwise refuse every host
            with a message that does not say why.
    """
    path = known_hosts_path()
    if not path.is_file():
        raise FileNotFoundError("the known_hosts file does not exist")
    return path


def resolve_key(name: str) -> Path:
    """The private key a request named, from the operator's key directory.

    Args:
        name: A file name inside `SSH_KEY_DIR`.

    Returns:
        The key's path.

    Raises:
        ValueError: the name is not a plain file name, `SSH_KEY_DIR` is not set, the key is
            not in that directory, or it can be read by group or others.
        FileNotFoundError: there is no such key.
    """
    if not key_name_ok(name):
        raise ValueError("identity_file must be a plain file name")
    configured = os.environ.get(KEY_DIR_ENV, "").strip()
    if not configured:
        raise ValueError(f"{KEY_DIR_ENV} is not set, so no key can be chosen by name")
    directory = Path(configured).expanduser().resolve()
    path = (directory / name).resolve()
    if path.parent != directory:
        raise ValueError("identity_file is not inside the key directory")
    if not path.is_file():
        raise FileNotFoundError("the key file does not exist")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("the key file can be read by group or others")
    return path
