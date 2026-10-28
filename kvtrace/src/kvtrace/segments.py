"""Trace segment files: gzip-compressed JSONL, written atomically.

A segment lives at ``DIR/<node>/<node>-<startts>-<seq>.kvt.jsonl.gz``. It is
written as ``<name>.part`` and renamed when closed, so readers never see a
partial file under the final name. The first line is an ``H`` header record.

The writer flushes the open ``.part`` with ``Z_SYNC_FLUSH`` on a timer and holds
an exclusive ``flock`` on it. A recorder that is killed (SIGKILL, OOM) leaves an
orphaned ``.part`` that is readable up to the last flush; readers accept it, skip
a ``.part`` that a live writer still locks, and mark the cut with a ``G`` record.

A recorder claims its node directory with a lock file (``SegmentWriter.claim``), so a
second live recorder with the same node id is refused. The claim also closes each
orphaned ``.part`` under its final name, with the same ``G`` marker at the cut, and
reports it to the sink like any closed segment. Without the directory lock (no
``flock``) orphans stay as ``.part``.
"""

from __future__ import annotations

import errno
import gzip
import io
import json
import logging
import os
import re
import tempfile
import time
import zlib
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from . import TOOL_NAME

try:
    import fcntl
except ImportError:  # not POSIX: no live-writer lock, every .part counts as orphaned
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

SEGMENT_SUFFIX = ".kvt.jsonl.gz"
PART_SUFFIX = SEGMENT_SUFFIX + ".part"
FORMAT_VERSION = 1
DEFAULT_FLUSH_SECONDS = 10.0
# Held by the recorder that owns a node directory, for its whole run.
LOCK_NAME = ".kvtrace-writer.lock"
# A reader's LOCK_SH probe holds a new .part only briefly; wait this long before a held
# lock counts as another writer.
_LOCK_RETRY_SECONDS = 1.0
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class NodeInUseError(ValueError):
    """Another live recorder writes the same node directory (same ``--node-id`` and ``--out``)."""


def _would_block(exc: OSError) -> bool:
    return exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN)


def safe_name(name: str) -> str:
    return _SAFE.sub("_", name) or "node"


def dumps(record: Dict[str, Any]) -> str:
    return json.dumps(record, separators=(",", ":"), sort_keys=False)


class SegmentWriter:
    """Write records into rotating segments and report each closed segment."""

    def __init__(
        self,
        out_dir: str,
        node: str,
        header: Dict[str, Any],
        rotate_seconds: float = 300.0,
        rotate_bytes: int = 64 * 1024 * 1024,
        on_closed: Optional[Callable[[str], None]] = None,
        on_rotate: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        clock: Callable[[], float] = time.time,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
    ):
        self.dir = os.path.join(out_dir, safe_name(node))
        os.makedirs(self.dir, exist_ok=True)
        self.node = node
        self.header = header
        self.rotate_seconds = rotate_seconds
        self.rotate_bytes = rotate_bytes
        self.on_closed = on_closed
        # Called just before a segment closes; can return a final record (tallies).
        self.on_rotate = on_rotate
        self.clock = clock
        # Seconds between sync flushes of the open segment; 0 disables the timer.
        self.flush_seconds = flush_seconds
        self.seq = 0
        self.bytes_written = 0
        self.closed_paths: List[str] = []
        self._raw: Optional[io.BufferedWriter] = None
        self._gz: Optional[gzip.GzipFile] = None
        self._path = ""
        self._opened_at = 0.0
        self._flushed_at = 0.0
        self._lock_fd: Optional[int] = None

    def claim(self) -> List[str]:
        """Take the node directory for this writer and close what a stopped writer left.

        Locks the directory lock file for the life of the writer (until ``release``).
        Each orphaned ``.part`` (left by a killed recorder) is closed with
        ``finalize_orphan``, added to ``closed_paths`` and reported to ``on_closed``, so
        a sink uploads it. When the directory cannot be locked (no ``flock`` on the file
        system), orphans stay as ``.part``: a live writer's ``.part`` cannot be told
        from an orphan then, and readers already read an orphaned ``.part``.

        Returns:
            The final paths of the orphaned segments, in name order.

        Raises:
            NodeInUseError: another live writer holds the directory lock or a ``.part``
                in the directory. Two recorders with one node id would merge their
                records into one node.
            OSError: the lock file cannot be opened, or an orphan cannot be closed.
        """
        in_use = NodeInUseError(
            f"another live recorder writes {self.dir}; give each recorder its own "
            f"--node-id (node id {self.node!r})")
        if fcntl is not None and self._lock_fd is None:
            fd = os.open(os.path.join(self.dir, LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                if _would_block(exc):
                    raise in_use from exc
                log.warning("%s: cannot lock the node directory (%s); a second recorder with "
                            "the same node id is not detected", self.dir, exc)
            else:
                self._lock_fd = fd
        parts = sorted(os.path.join(self.dir, f) for f in os.listdir(self.dir)
                       if f.endswith(PART_SUFFIX))
        # A recorder of an older version takes no directory lock, only the .part lock.
        if any(_is_live_part(p) for p in parts):
            self.release()
            raise in_use
        if self._lock_fd is None:
            if parts:
                log.warning("%s: no directory lock, so %d .part file(s) stay as .part (they "
                            "may belong to a live recorder); readers read them as orphaned",
                            self.dir, len(parts))
            return []
        finalized: List[str] = []
        for part in parts:
            final = finalize_orphan(part)
            if final is not None:
                finalized.append(final)
                self.closed_paths.append(final)
                self._report_closed(final)
        return finalized

    def release(self) -> None:
        """Drop the directory lock that ``claim`` took (a no-op without one)."""
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def _open(self) -> None:
        self._opened_at = self._flushed_at = self.clock()
        while True:
            name = f"{safe_name(self.node)}-{int(self._opened_at * 1000)}-{self.seq:05d}{SEGMENT_SUFFIX}"
            self.seq += 1
            path = os.path.join(self.dir, name)
            # O_EXCL: never truncate the .part of another writer that picked the same
            # name; a closed segment of that name is not replaced either. The next seq
            # makes the name unique.
            if os.path.exists(path):
                log.warning("%s exists; use the next segment number", path)
                continue
            try:
                fd = os.open(path + ".part", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                log.warning("%s.part exists; use the next segment number", path)
                continue
            break
        self._path = path
        self._raw = os.fdopen(fd, "wb")
        if fcntl is not None:
            # Readers skip a .part that is still locked: its writer is alive.
            self._lock_part()
        self._gz = gzip.GzipFile(fileobj=self._raw, mode="wb")
        header = {"t": "H", "version": FORMAT_VERSION, "tool": TOOL_NAME}
        header.update(self.header)
        header["started"] = self._opened_at
        self._write_line(header)

    def _lock_part(self) -> None:
        """Lock the new ``.part`` so readers skip it while this writer is alive."""
        assert self._raw is not None
        deadline = time.monotonic() + _LOCK_RETRY_SECONDS
        while True:
            try:
                fcntl.flock(self._raw.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if not _would_block(exc):
                    # Some file systems (NFS without lockd, FUSE) have no flock. Write
                    # anyway; a reader then treats the open .part as orphaned.
                    log.warning("%s.part: cannot lock (%s); write it without a lock",
                                self._path, exc)
                    return
                if time.monotonic() >= deadline:
                    self._raw.close()
                    self._raw = None
                    raise NodeInUseError(
                        f"{self._path}.part: another process holds its lock; give each "
                        f"recorder its own --node-id (node id {self.node!r})") from exc
            # A reader's LOCK_SH probe holds the lock only for a moment.
            time.sleep(0.01)

    def _write_line(self, record: Dict[str, Any]) -> None:
        assert self._gz is not None
        data = (dumps(record) + "\n").encode("utf-8")
        self._gz.write(data)

    def write(self, record: Dict[str, Any]) -> None:
        # Rotate first so a record never extends a segment that is already too old.
        self.maybe_rotate()
        if self._gz is None:
            self._open()
        self._write_line(record)
        self.maybe_rotate()

    def maybe_rotate(self) -> None:
        if self._gz is None:
            return
        assert self._raw is not None
        too_old = self.clock() - self._opened_at >= self.rotate_seconds
        # raw.tell() is the compressed size flushed so far; close enough for rotation.
        too_big = self._raw.tell() >= self.rotate_bytes
        if too_old or too_big:
            self.close()
        else:
            self.maybe_flush()

    def maybe_flush(self) -> None:
        """Sync-flush the open segment when ``flush_seconds`` passed since the last flush.

        ``Z_SYNC_FLUSH`` ends the deflate data on a byte boundary, so a reader can
        decompress every line written before the flush even if the writer dies.
        """
        if self._gz is None or not self.flush_seconds:
            return
        now = self.clock()
        if now - self._flushed_at >= self.flush_seconds:
            self.flush()
            self._flushed_at = now

    def flush(self) -> None:
        if self._gz is not None:
            self._gz.flush(zlib.Z_SYNC_FLUSH)

    def total_bytes(self) -> int:
        """Compressed bytes in closed segments plus what the open segment has flushed."""
        return self.bytes_written + (self._raw.tell() if self._raw is not None else 0)

    def close(self) -> Optional[str]:
        if self._gz is None:
            return None
        assert self._raw is not None
        if self.on_rotate:
            final = self.on_rotate()
            if final:
                self._write_line(final)
        self._gz.close()
        self.bytes_written += self._raw.tell()
        self._raw.close()
        os.replace(self._path + ".part", self._path)
        path = self._path
        self._gz = None
        self._raw = None
        self.closed_paths.append(path)
        self._report_closed(path)
        return path

    def _report_closed(self, path: str) -> None:
        if self.on_closed:
            try:
                self.on_closed(path)
            except Exception:
                log.exception("segment sink failed for %s", path)


def finalize_orphan(path: str) -> Optional[str]:
    """Close the orphaned ``.part`` of a stopped writer under its final segment name.

    Every complete line up to the last flush is kept as written. When the file was cut
    (no gzip end, or a partial last line), the ``G`` record that ``iter_records`` adds
    for an orphaned ``.part`` is appended (``missing`` 0, ``truncated`` names the
    ``.part``), so the hole stays visible after the rename and in S3. No ``T`` record is
    added: the tallies of the lost part are not known.

    Args:
        path: An orphaned ``.part`` file that no live writer locks.

    Returns:
        The final path, or None when no line of the file is readable; such a file
        is left in place.

    Raises:
        OSError: the new file cannot be written or renamed.
    """
    final = path[: -len(".part")]
    if os.path.exists(final):
        # A previous finalize renamed its copy and stopped before it removed the .part.
        log.warning("%s: already closed as %s; remove the leftover", path, final)
        os.unlink(path)
        return final
    lines: List[bytes] = []
    header: Dict[str, Any] = {}
    last: Dict[str, Any] = {}
    truncated = False
    try:
        with open(path, "rb") as raw, gzip.GzipFile(fileobj=raw, mode="rb") as gz:
            for line in gz:
                if not line.endswith(b"\n"):
                    truncated = True
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    rec = None
                if not isinstance(rec, dict):
                    log.warning("%s: drop a malformed line", path)
                    continue
                if rec.get("t") == "H":
                    header = rec
                if isinstance(rec.get("ts"), (int, float)):
                    last = rec
                lines.append(line)
    except (EOFError, zlib.error, gzip.BadGzipFile) as exc:
        log.info("%s: ends after its last flush (%s)", path, exc)
        truncated = True
    if not lines:
        log.warning("%s: no readable record; leave it in place", path)
        return None
    if truncated:
        gap: Dict[str, Any] = {"t": "G", "ts": last.get("ts", header.get("started", 0.0)),
                               "node": last.get("node", header.get("node"))}
        if last.get("rank") is not None:
            gap["rank"] = last["rank"]
        gap.update({"missing": 0, "recovered": 0, "truncated": os.path.basename(path)})
        lines.append((dumps(gap) + "\n").encode("utf-8"))
    tmp = final + ".tmp"
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
            gz.writelines(lines)
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(tmp, final)
    os.unlink(path)
    log.warning("%s: closed the orphaned segment of a stopped recorder as %s%s", path, final,
                " (cut after its last flush)" if truncated else "")
    return final


def _is_live_part(path: str) -> bool:
    """True when a writer still holds the lock on this ``.part`` file."""
    if fcntl is None:
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as exc:
        # Only "would block" means another process holds the lock. Other errors
        # (ENOLCK, EOPNOTSUPP) mean no lock can exist: read the file.
        return exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN)
    finally:
        # Closing the descriptor also drops the probe lock.
        os.close(fd)
    return False


def iter_segment_files(paths: Iterable[str]) -> List[str]:
    """Expand files and directories into a sorted list of segment files.

    Closed segments and orphaned ``.part`` files (left by a killed recorder) are
    returned. A ``.part`` that a live recorder still writes is skipped.
    """
    found: List[str] = []
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                found.extend(os.path.join(root, f) for f in files
                             if f.endswith(SEGMENT_SUFFIX) or f.endswith(PART_SUFFIX))
        elif os.path.isfile(path):
            found.append(path)
        else:
            raise FileNotFoundError(path)
    kept: List[str] = []
    for path in sorted(set(found)):
        if path.endswith(PART_SUFFIX) and _is_live_part(path):
            log.info("%s: skip open segment of a running recorder", path)
            continue
        kept.append(path)
    return kept


def download_s3_prefix(url: str, target: str, client: Any = None) -> int:
    """Download every segment under ``s3://bucket/prefix`` into ``target``.

    The prefix matches whole path components (``traces`` does not match
    ``traces2/``), and keys that would resolve outside ``target`` are skipped.
    """
    from .sinks import parse_s3_url, require_boto3

    bucket, prefix = parse_s3_url(url)
    if client is None:
        client = require_boto3().client("s3")
    root = os.path.realpath(target)
    list_prefix = prefix + "/" if prefix else ""
    count = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(SEGMENT_SUFFIX):
                continue
            dest = os.path.realpath(os.path.join(root, *key[len(list_prefix):].split("/")))
            if not dest.startswith(root + os.sep):
                log.warning("skip S3 key outside the download directory: %s", key)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            client.download_file(bucket, key, dest)
            count += 1
    return count


class TracePaths:
    """Context manager: resolve local paths and ``s3://`` prefixes to segment files.

    S3 data is downloaded into a temporary directory that is deleted on exit.
    """

    def __init__(self, paths: Iterable[str], client: Any = None):
        self.paths = list(paths)
        self.client = client
        self._tmp: Optional[tempfile.TemporaryDirectory] = None

    def __enter__(self) -> List[str]:
        local: List[str] = []
        try:
            for path in self.paths:
                if path.startswith("s3://"):
                    if self._tmp is None:
                        self._tmp = tempfile.TemporaryDirectory(prefix="kvtrace-s3-")
                    target = os.path.join(self._tmp.name, str(len(local)))
                    os.makedirs(target)
                    download_s3_prefix(path, target, self.client)
                    local.append(target)
                else:
                    local.append(path)
            return iter_segment_files(local)
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *exc: Any) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


def _open_segment(path: str) -> Optional[io.BufferedReader]:
    """Open a segment file for reading.

    A ``.part`` that is gone was renamed by its recorder after the listing: open the
    final name instead. Return None when neither name exists.
    """
    try:
        return open(path, "rb")
    except FileNotFoundError:
        if not path.endswith(PART_SUFFIX):
            raise
    try:
        return open(path[: -len(".part")], "rb")
    except FileNotFoundError:
        return None


def iter_records(files: Iterable[str], errors: Optional[Dict[str, int]] = None
                 ) -> Iterator[Dict[str, Any]]:
    """Yield records from segment files. The header's model is copied onto
    records that lack it. Malformed lines are skipped and a corrupt or truncated
    file stops at the damage; both are counted in ``errors`` when given.

    An orphaned ``.part`` file is read up to its truncated end. Its cut is
    counted in ``errors["truncated_parts"]`` and marked with a ``G`` record
    (``missing`` 0 because the lost count is unknown, ``truncated`` names the file).
    A ``.part`` that its recorder renamed after the listing is read under its final
    name, with no ``G`` record."""
    for path in files:
        header: Dict[str, Any] = {}
        is_part = path.endswith(PART_SUFFIX)
        last: Dict[str, Any] = {}
        try:
            raw = _open_segment(path)
        except OSError as exc:
            log.warning("%s: stop reading damaged segment: %s", path, exc)
            if errors is not None:
                errors["damaged_files"] = errors.get("damaged_files", 0) + 1
            continue
        if raw is None:
            log.info("%s: segment is gone; skip it", path)
            continue
        if raw.name != path:
            # The recorder closed the segment after the listing: it is complete now.
            path, is_part = raw.name, False
        if is_part:
            log.warning("%s: read orphaned segment of a stopped recorder up to its last flush",
                        path)
        try:
            with raw, gzip.open(raw, "rt", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        rec = None
                    if not isinstance(rec, dict):
                        log.warning("%s:%d: skip malformed line", path, lineno)
                        if errors is not None:
                            errors["bad_lines"] = errors.get("bad_lines", 0) + 1
                        continue
                    if rec.get("t") == "H":
                        header = rec
                    elif "model" not in rec and "model" in header:
                        rec["model"] = header["model"]
                    if isinstance(rec.get("ts"), (int, float)):
                        last = rec
                    yield rec
        except (OSError, EOFError, zlib.error) as exc:
            if not is_part:
                log.warning("%s: stop reading damaged segment: %s", path, exc)
                if errors is not None:
                    errors["damaged_files"] = errors.get("damaged_files", 0) + 1
                continue
            log.warning("%s: orphaned segment ends here (%s); records after its last flush "
                        "are lost", path, exc)
            if errors is not None:
                errors["truncated_parts"] = errors.get("truncated_parts", 0) + 1
            # Put the hole at the last readable record of this file.
            ts = last.get("ts", header.get("started", 0.0))
            yield {"t": "G", "ts": ts, "node": last.get("node", header.get("node")),
                   "rank": last.get("rank"), "missing": 0, "recovered": 0,
                   "truncated": os.path.basename(path)}
