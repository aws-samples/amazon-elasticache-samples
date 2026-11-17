"""
PostgreSQL service entries from ~/.pg_service.conf (or $PGSERVICEFILE).

Keeps host, port, dbname and user. A password key is never returned; its presence is noted.
~/.pgpass is never read. Spec: .kiro/specs/database-discovery (DISC-1, DISC-2).
"""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILE_BYTES = 256 * 1024
MAX_SERVICES = 200
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
_HOST = re.compile(r"[A-Za-z0-9/][A-Za-z0-9.:/_-]{0,254}")
_DATABASE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}")


@dataclass
class PgService:
    """One service entry, without any password."""

    name: str
    host: str | None = None
    port: int | None = None
    dbname: str | None = None
    user: str | None = None
    has_password: bool = False


@dataclass
class PgServiceResult:
    """The services found, and notes."""

    services: list[PgService] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def default_path() -> Path:
    """$PGSERVICEFILE when set, else ~/.pg_service.conf."""
    env = os.environ.get("PGSERVICEFILE")
    return Path(env) if env else Path.home() / ".pg_service.conf"


def parse_pg_service(text: str) -> PgServiceResult:
    """Read service sections.

    Args:
        text: The file's content.

    Returns:
        Services with well-formed values only; anything else is left out.

    Raises:
        configparser.Error: the text is not an INI file.
    """
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.read_string(text)
    result = PgServiceResult()
    with_password = 0
    for section in cp.sections()[:MAX_SERVICES]:
        if not _NAME.fullmatch(section):
            continue
        s = cp[section]
        svc = PgService(name=section)
        host = s.get("host", "").strip()
        if host and _HOST.fullmatch(host):
            svc.host = host
        port = s.get("port", "").strip()
        if port.isdigit() and 1 <= int(port) <= 65535:
            svc.port = int(port)
        db = s.get("dbname", "").strip()
        if db and _DATABASE.fullmatch(db):
            svc.dbname = db
        user = s.get("user", "").strip()
        if user and _NAME.fullmatch(user):
            svc.user = user
        if "password" in s:
            svc.has_password = True
            with_password += 1
        result.services.append(svc)
    if with_password:
        result.notes.append(f"{with_password} service(s) keep a password in the file. "
                            "The tool does not read it.")
    return result


def read_pg_service(path: Path | None = None) -> PgServiceResult | None:
    """Read and parse the service file.

    Args:
        path: The file. Defaults to default_path().

    Returns:
        The services, or None when the file does not exist.

    Raises:
        OSError: the file exists but cannot be read.
        ValueError: the file is too large, not text, or not an INI file.
    """
    p = path or default_path()
    if not p.is_file():
        return None
    if p.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("the service file is too large to read")
    try:
        return parse_pg_service(p.read_bytes().decode("utf-8"))
    except UnicodeDecodeError as e:
        raise ValueError("the service file is not text") from e
    except configparser.Error as e:
        raise ValueError("the service file is not in the pg_service.conf format") from e
