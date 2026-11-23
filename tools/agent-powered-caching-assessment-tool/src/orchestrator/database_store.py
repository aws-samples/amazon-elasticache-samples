"""
Saved databases, behind an interface, like the job store.

`LocalDatabaseStore` keeps one JSON file per database in `<root>/_databases/`. The folder
is 0700 and the files 0600: a saved connection holds no password, but its SSH target, key
name, role and password source map the estate. Writes are atomic. An id is checked before
it reaches a path. Spec: .kiro/specs/databases (DB-1 to DB-4).
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Protocol

from src.contracts.databases import DATABASE_ID_PATTERN, DatabaseConnection, DatabaseRecord
from src.orchestrator import safe_paths

logger = logging.getLogger(__name__)

STORE_DIR = "_databases"
MAX_DATABASES = 200
_ID = re.compile(DATABASE_ID_PATTERN)


class DatabaseExists(Exception):
    """A saved database already has this connection. `database_id` is that one."""

    def __init__(self, database_id: str) -> None:
        """
        Args:
            database_id: The saved database that already has the connection.
        """
        super().__init__(database_id)
        self.database_id = database_id


class TooManyDatabases(Exception):
    """The store holds MAX_DATABASES already."""


def valid_database_id(value: str) -> bool:
    """Whether a database id is in the stored form."""
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def new_database_id() -> str:
    """A fresh id: db- and 12 hex characters."""
    return f"db-{secrets.token_hex(6)}"


def identity(conn: DatabaseConnection) -> tuple[str, str, int, str, int, str]:
    """What makes two connections the same database.

    Args:
        conn: A connection.

    Returns:
        (kind, SSH target, SSH port, endpoint, port, database name). Host names are
        compared without case; the database name keeps its case, as PostgreSQL does.
    """
    return (conn.kind, conn.ssh_target.lower(), conn.ssh_port, conn.cluster_endpoint.lower(),
            conn.port, conn.database_name)


class DatabaseStore(Protocol):
    """What the API needs from database storage."""

    def create(self, record: DatabaseRecord) -> DatabaseRecord:
        """Store a new database. Raises DatabaseExists or TooManyDatabases."""

    def get(self, database_id: str) -> DatabaseRecord | None:
        """The database, or None."""

    def save(self, record: DatabaseRecord) -> DatabaseRecord:
        """Replace a stored database. Raises DatabaseExists if its new connection is taken."""

    def delete(self, database_id: str) -> bool:
        """Remove it. False when there was none."""

    def list(self) -> list[DatabaseRecord]:
        """Every database, newest first."""


def _atomic_write(path: Path, text: str) -> None:
    """Write a file with mode 0600 so a reader sees the old content or the new."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class LocalDatabaseStore:
    """Saved databases as files next to the job folders."""

    _lock = threading.Lock()

    def __init__(self, root: str | Path) -> None:
        """
        Args:
            root: The artifact folder. `_databases/` is created inside it with mode 0700.
        """
        self.dir = Path(root) / STORE_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)

    def _path(self, database_id: str) -> Path:
        """The file of a database.

        Args:
            database_id: The id. Checked before it becomes part of a path.

        Returns:
            `<root>/_databases/<id>.json`.

        Raises:
            ValueError: the id is not in the stored form.
        """
        if not valid_database_id(database_id):
            raise ValueError("invalid database id")
        return safe_paths.child(self.dir, f"{database_id}.json")

    def _read(self, path: Path) -> DatabaseRecord | None:
        """Read one database file.

        Args:
            path: The file.

        Returns:
            The record, or None when the file cannot be read or parsed (logged by name).
        """
        try:
            return DatabaseRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("saved database %s is not readable", path.name)
            return None

    def list(self) -> list[DatabaseRecord]:
        """Every database, newest first. Unreadable files are skipped and logged."""
        out = []
        for p in self.dir.glob("db-*.json"):
            if p.is_file() and not p.is_symlink() and valid_database_id(p.stem):
                rec = self._read(p)
                if rec is not None:
                    out.append(rec)
        return sorted(out, key=lambda r: (r.created_at, r.database_id), reverse=True)

    def get(self, database_id: str) -> DatabaseRecord | None:
        """The database, or None (also for an id not in the stored form)."""
        if not valid_database_id(database_id):
            return None
        p = safe_paths.child_or_none(self.dir, f"{database_id}.json")
        return self._read(p) if p is not None and p.is_file() else None

    def _taken(self, conn: DatabaseConnection, besides: str | None) -> str | None:
        """Which other saved database has this connection.

        Args:
            conn: The connection.
            besides: A database id to skip (the one being saved), or None.

        Returns:
            The id of the database that has it, or None.
        """
        key = identity(conn)
        for rec in self.list():
            if rec.database_id != besides and identity(rec.connection) == key:
                return rec.database_id
        return None

    def create(self, record: DatabaseRecord) -> DatabaseRecord:
        """Store a new database.

        Raises:
            DatabaseExists: another saved database has this connection.
            TooManyDatabases: MAX_DATABASES are stored.
        """
        with self._lock:
            if (other := self._taken(record.connection, None)) is not None:
                raise DatabaseExists(other)
            if len(self.list()) >= MAX_DATABASES:
                raise TooManyDatabases()
            _atomic_write(self._path(record.database_id), record.model_dump_json(indent=2))
        return record

    def save(self, record: DatabaseRecord) -> DatabaseRecord:
        """Replace a stored database.

        Raises:
            DatabaseExists: another saved database has the new connection.
        """
        with self._lock:
            if (other := self._taken(record.connection, record.database_id)) is not None:
                raise DatabaseExists(other)
            _atomic_write(self._path(record.database_id), record.model_dump_json(indent=2))
        return record

    def delete(self, database_id: str) -> bool:
        """Remove a database. False when there was none."""
        if not valid_database_id(database_id):
            return False
        with self._lock:
            p = safe_paths.child_or_none(self.dir, f"{database_id}.json")
            if p is None or not p.is_file():
                return False
            p.unlink()
        return True
