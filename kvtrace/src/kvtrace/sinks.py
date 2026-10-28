"""Destinations for recorded segments.

Every sink keeps the local segment file. ``s3`` also uploads each closed
segment; ``valkey`` also appends each record to a Valkey stream. Neither does
network I/O on the recorder's receive loop: both use a worker thread.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .segments import dumps, safe_name

log = logging.getLogger(__name__)

# Seconds that S3Sink.close() waits for its uploads. Shutdown (SIGTERM) must end within
# a typical termination grace period even when S3 is down.
S3_CLOSE_DEADLINE_SECONDS = 20.0


class SinkError(RuntimeError):
    pass


def require_boto3() -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise SinkError(
            "S3 support needs the 's3' extra: pip install 'kvtrace[s3]'"
        ) from exc
    return boto3


def require_valkey() -> Any:
    try:
        import valkey
    except ImportError as exc:
        raise SinkError(
            "Valkey support needs the 'valkey' extra: pip install 'kvtrace[valkey]'"
        ) from exc
    return valkey


def parse_s3_url(url: str) -> Tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise SinkError(f"not an s3://bucket/prefix URL: {url}")
    return parsed.netloc, parsed.path.strip("/")


class Sink:
    def on_record(self, record: Dict[str, Any]) -> None:
        """Called for every record written (default: nothing)."""

    def on_segment_closed(self, path: str) -> None:
        """Called after a segment file is closed and renamed."""

    def close(self) -> None:
        """Flush and release resources."""

    def stats(self) -> Dict[str, int]:
        """Delivery counters of this sink, for P records and the ``record`` output.

        Returns:
            Counter name to value; empty for a sink that only keeps local files.
        """
        return {}

    @property
    def lost(self) -> int:
        """Segments or records that this sink failed to deliver (0 = nothing lost).

        The local segment files still hold them.
        """
        return 0


class FileSink(Sink):
    """Keep segments on local disk only."""


class S3Sink(Sink):
    """Upload closed segments to ``s3://bucket/prefix/<node>/<file>`` from a worker thread.

    While recording, a failed upload is tried ``retries`` times in all, with waits that
    double from ``backoff_seconds`` up to ``max_backoff_seconds`` (1+2+4+8 = 15 s by
    default), so a short S3 outage loses nothing.

    ``close`` waits at most ``close_deadline_seconds``. From the start of ``close`` each
    remaining upload is tried once with no wait. Uploads that do not finish by the
    deadline count as failed (``s3_failed``); the local segment files keep them.
    """

    def __init__(self, url: str, node: str, client: Any = None, retries: int = 5,
                 backoff_seconds: float = 1.0, max_backoff_seconds: float = 8.0,
                 close_deadline_seconds: float = S3_CLOSE_DEADLINE_SECONDS):
        self.bucket, self.prefix = parse_s3_url(url)
        self.node = node
        self.client = client if client is not None else require_boto3().client("s3")
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.close_deadline_seconds = close_deadline_seconds
        self.uploaded = 0
        self.failed = 0
        # Set when close() starts: no more retries or backoff waits.
        self._closing = threading.Event()
        # Guards the counters and _abandoned; close() and the worker both update them.
        self._lock = threading.Lock()
        # Segments handed to the worker; minus uploaded and failed, the ones still pending.
        self._queued = 0
        # Set when close() gave up at its deadline and counted the rest as failed.
        self._abandoned = False
        self._queue: queue.Queue[Optional[str]] = queue.Queue()
        self._worker = threading.Thread(target=self._run, name="kvtrace-s3", daemon=True)
        self._worker.start()

    def key_for(self, path: str) -> str:
        parts = [p for p in (self.prefix, safe_name(self.node), os.path.basename(path)) if p]
        return "/".join(parts)

    def on_segment_closed(self, path: str) -> None:
        with self._lock:
            self._queued += 1
        self._queue.put(path)

    def upload(self, path: str) -> bool:
        """Upload one segment, retrying with backoff until ``close`` starts.

        After ``close`` starts, a failed attempt is not retried.

        Args:
            path: Local segment file.

        Returns:
            True when the segment was uploaded.
        """
        key = self.key_for(path)
        for attempt in range(1, self.retries + 1):
            try:
                self.client.upload_file(path, self.bucket, key)
                self._count(uploaded=True)
                log.info("uploaded %s to s3://%s/%s", path, self.bucket, key)
                return True
            except Exception as exc:  # boto3 raises many types; retry all of them
                log.warning("S3 upload attempt %d/%d failed: %s", attempt, self.retries, exc)
                if attempt < self.retries and not self._closing.is_set():
                    # close() ends this wait early, so shutdown does not sit out a backoff.
                    self._closing.wait(self.backoff_delay(attempt))
                if self._closing.is_set():
                    break
        self._count(uploaded=False)
        log.error("giving up on S3 upload of %s (kept locally)", path)
        return False

    def backoff_delay(self, attempt: int) -> float:
        """Seconds to wait after failed attempt ``attempt`` (1-based) before the next one."""
        return min(self.backoff_seconds * 2 ** (attempt - 1), self.max_backoff_seconds)

    def stats(self) -> Dict[str, int]:
        return {"s3_uploaded": self.uploaded, "s3_failed": self.failed}

    @property
    def lost(self) -> int:
        return self.failed

    def _run(self) -> None:
        while True:
            path = self._queue.get()
            if path is None:
                return
            self.upload(path)

    def close(self) -> None:
        """Upload the queued segments (the final one) within ``close_deadline_seconds``.

        Each segment still queued is tried once with no backoff wait. Segments that are
        not uploaded by the deadline are counted in ``s3_failed``.
        """
        self._closing.set()
        self._queue.put(None)
        self._worker.join(self.close_deadline_seconds)
        if not self._worker.is_alive():
            return
        with self._lock:
            self._abandoned = True
            pending = self._queued - self.uploaded - self.failed
            self.failed += pending
        log.error("S3 sink: close deadline of %.0f s passed; %d segment(s) not uploaded "
                  "(kept locally)", self.close_deadline_seconds, pending)

    def _count(self, uploaded: bool) -> None:
        """Count one finished upload, unless ``close`` already counted it as failed."""
        with self._lock:
            if self._abandoned:
                return
            if uploaded:
                self.uploaded += 1
            else:
                self.failed += 1


class ValkeySink(Sink):
    """XADD every record (as JSON in field ``r``) to a capped stream.

    ``on_record`` only puts the record on a bounded queue; a worker thread sends
    pipelined batches. When the queue is full (Valkey is slow or down), records
    are dropped from the stream and counted; the local segment files keep them.
    """

    def __init__(self, url: str, client: Any = None, maxlen: int = 1_000_000,
                 batch_size: int = 500, flush_seconds: float = 1.0, timeout: float = 2.0,
                 queue_size: int = 100_000):
        parsed = urlparse(url)
        if parsed.scheme not in ("valkey", "valkeys") or not parsed.hostname:
            raise SinkError(f"not a valkey://host:port/stream URL: {url}")
        self.stream = parsed.path.strip("/") or "kvtrace"
        self.maxlen = maxlen
        self.batch_size = batch_size
        self.flush_seconds = flush_seconds
        if client is None:
            valkey = require_valkey()
            # The path names the stream, not a database; credentials come from the URL.
            conn_url = parsed._replace(path="", params="", query="", fragment="").geturl()
            client = valkey.Valkey.from_url(conn_url, socket_timeout=timeout,
                                            socket_connect_timeout=timeout)
        self.client = client
        self.added = 0
        self.failed = 0
        self.dropped = 0
        self._queue: queue.Queue[Optional[str]] = queue.Queue(maxsize=queue_size)
        self._worker = threading.Thread(target=self._run, name="kvtrace-valkey", daemon=True)
        self._worker.start()

    def on_record(self, record: Dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(dumps(record))
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 10000 == 0:
                log.warning("Valkey sink queue full: %d records not streamed", self.dropped)

    def _run(self) -> None:
        batch: List[str] = []
        deadline = time.monotonic() + self.flush_seconds
        while True:
            try:
                item = self._queue.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                item = ""
            if item is None:
                self._send(batch)
                return
            if item:
                batch.append(item)
            if len(batch) >= self.batch_size or time.monotonic() >= deadline:
                self._send(batch)
                batch = []
                deadline = time.monotonic() + self.flush_seconds

    def _send(self, batch: List[str]) -> None:
        if not batch:
            return
        try:
            pipe = self.client.pipeline(transaction=False)
            for data in batch:
                pipe.xadd(self.stream, {"r": data}, maxlen=self.maxlen, approximate=True)
            pipe.execute()
            self.added += len(batch)
        except Exception as exc:  # a Valkey outage must never stop recording
            before = self.failed
            self.failed += len(batch)
            # Log the first failure and then about every 1000 lost records.
            if before == 0 or before // 1000 != self.failed // 1000:
                log.warning("Valkey XADD failed (%d records lost so far): %s", self.failed, exc)

    def stats(self) -> Dict[str, int]:
        return {"valkey_added": self.added, "valkey_failed": self.failed,
                "valkey_dropped": self.dropped}

    @property
    def lost(self) -> int:
        return self.failed + self.dropped

    def close(self) -> None:
        """Send what is queued, then close the client."""
        self._queue.put(None)
        self._worker.join()
        closer = getattr(self.client, "close", None)
        if closer:
            closer()


def make_sink(spec: Optional[str], node: str, client: Any = None) -> Sink:
    if not spec or spec == "file":
        return FileSink()
    if spec.startswith("s3://"):
        return S3Sink(spec, node, client=client)
    if spec.startswith(("valkey://", "valkeys://")):
        return ValkeySink(spec, client=client)
    raise SinkError(f"unknown sink {spec!r}: use file, s3://bucket/prefix or valkey://host:port/stream")
