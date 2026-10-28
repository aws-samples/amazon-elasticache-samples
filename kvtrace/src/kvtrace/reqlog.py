"""Per-request tier log for a vLLM KV connector, without importing vLLM.

:func:`make_log_connector` builds the connector class from vLLM's base classes, so the
logic here is tested with stub classes; :mod:`kvtrace.vllm_connector` binds it to the
real vLLM ones. The connector moves no KV data. It only watches the scheduler-side
calls and writes one JSON line per finished request through :class:`RequestLogWriter`.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import stat
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional, TextIO, Tuple

from .normalize import Anonymizer, normalize_hash

log = logging.getLogger(__name__)


def connector_extra_config(vllm_config: Any) -> Dict[str, Any]:
    """Return ``vllm_config.kv_transfer_config.kv_connector_extra_config`` as a dict.

    vLLM passes this connector's own ``KVTransferConfig`` in ``vllm_config``; MultiConnector
    builds a copy of ``vllm_config`` per child whose ``kv_transfer_config`` holds the
    child's entry, so its ``kv_connector_extra_config`` is this connector's config.

    Raises:
        ValueError: the config is missing or is not a mapping. The connector must not start
            with defaults, because that would drop ``anonymize_key_file`` and log raw ids.
    """
    try:
        extra = vllm_config.kv_transfer_config.kv_connector_extra_config
    except AttributeError as exc:
        raise ValueError("KVTraceLogConnector: vllm_config has no "
                         "kv_transfer_config.kv_connector_extra_config; this vLLM version "
                         "is not supported") from exc
    if not isinstance(extra, Mapping):
        raise ValueError(f"KVTraceLogConnector: kv_connector_extra_config is "
                         f"{type(extra).__name__}, not a mapping")
    return dict(extra)


def resolve_log_path(path: str, pid: int) -> str:
    """Replace ``{pid}`` in ``path`` by ``pid``; add ``-<pid>`` before the extension if absent.

    With data parallelism each engine process has its own scheduler connector. A shared
    file would get interleaved lines and each process would rotate the other's records
    away, so a path without ``{pid}`` gets one (``requests.jsonl`` ->
    ``requests-<pid>.jsonl``) and a warning is logged.
    """
    if "{pid}" not in path:
        root, ext = os.path.splitext(path)
        resolved = f"{root}-{pid}{ext}"
        log.warning("kvtrace request log: path %r has no {pid}; writing %r so that "
                    "data-parallel engine processes do not share one file", path, resolved)
        return resolved
    return path.replace("{pid}", str(pid))

RECORD_KIND = "kvtrace.request"
STATS_KIND = "kvtrace.request_log_stats"
DEFAULT_QUEUE_SIZE = 65536
DEFAULT_MAX_PENDING = 10_000
# "{pid}" keeps the files of data-parallel engine processes apart.
DEFAULT_PATH = "/var/tmp/kvtrace/requests-{pid}.jsonl"
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3
# Seconds between repeated "hook failed" warnings; the first failure is logged at once.
HOOK_ERROR_LOG_INTERVAL = 60.0


def _open_private(path: str) -> Tuple[TextIO, int]:
    """Open ``path`` for append as an owner-only (0600) file; return it and its size."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                 0o600)
    try:
        # O_CREAT sets the mode only for a new file; tighten an older one too.
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    mode = stat.S_IMODE(os.fstat(fd).st_mode)
    if mode != 0o600:
        # fchmod failed (for example a file of another owner): records may be readable.
        log.warning("kvtrace request log: %s has mode %04o, not 0600", path, mode)
    return os.fdopen(fd, "a", encoding="utf-8"), os.fstat(fd).st_size


class RequestLogWriter:
    """Append JSON lines from a writer thread; :meth:`submit` never blocks.

    A full queue drops the record and counts it. The file is flushed when the queue is
    empty, so a record reaches the file within about one ``flush_interval``.

    The file is created with mode 0600. When a line would make it larger than
    ``max_bytes``, the writer thread renames it to ``PATH.1`` (older ones move to
    ``PATH.2`` ... ``PATH.<backup_count>``, the oldest is deleted) and starts a new file.
    ``max_bytes`` 0 disables rotation.
    """

    def __init__(self, path: str, queue_size: int = DEFAULT_QUEUE_SIZE, flush_interval: float = 1.0,
                 max_bytes: int = DEFAULT_MAX_BYTES, backup_count: int = DEFAULT_BACKUP_COUNT):
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        if max_bytes < 0 or backup_count < 0:
            raise ValueError("max_bytes and backup_count must be >= 0")
        # Fail at engine start with a clear message, not at the first finished request.
        if os.path.isdir(path):
            raise ValueError(f"kvtrace request log: path {path!r} is a directory, not a file")
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
            fh, size = _open_private(path)
        except OSError as exc:
            raise ValueError(f"kvtrace request log: cannot open {path!r} for append: {exc}; "
                             "create its directory and check write permission") from exc
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.rotations = 0
        self._size = size
        self._lock = threading.Lock()
        self._dropped = 0
        self.written = 0
        self._q: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue(maxsize=queue_size)
        self._flush_interval = flush_interval
        self._fh = fh
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="kvtrace-reqlog", daemon=True)
        self._thread.start()

    @property
    def dropped(self) -> int:
        return self._dropped

    def _drop(self) -> None:
        with self._lock:
            self._dropped += 1

    def submit(self, record: Dict[str, Any]) -> bool:
        if self._closed:
            self._drop()
            return False
        try:
            self._q.put_nowait(record)
            return True
        except queue.Full:
            self._drop()
            return False

    def _flush(self) -> None:
        try:
            self._fh.flush()
        except (OSError, ValueError) as exc:
            log.warning("kvtrace request log: flush failed: %s", exc)

    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=self._flush_interval)
            except queue.Empty:
                self._flush()
                continue
            if item is None:
                break
            try:
                line = json.dumps(item, separators=(",", ":")) + "\n"
                nbytes = len(line.encode("utf-8"))
                if self.max_bytes and self._size and self._size + nbytes > self.max_bytes:
                    self._rotate()
                self._fh.write(line)
                self._size += nbytes
                self.written += 1
            except (OSError, TypeError, ValueError) as exc:
                self._drop()
                log.warning("kvtrace request log: write failed: %s", exc)
            if self._q.empty():
                self._flush()
        self._flush()

    def _rotate(self) -> None:
        """Move PATH to PATH.1 (and PATH.N to PATH.N+1) and open a new PATH. Writer thread only."""
        self._fh.close()
        try:
            if self.backup_count:
                for n in range(self.backup_count - 1, 0, -1):
                    older = f"{self.path}.{n}"
                    if os.path.exists(older):
                        os.replace(older, f"{self.path}.{n + 1}")
                os.replace(self.path, f"{self.path}.1")
            else:
                os.unlink(self.path)
            self.rotations += 1
        except OSError as exc:
            # Keep logging to the same file rather than stop; it grows past max_bytes.
            log.warning("kvtrace request log: rotation failed: %s", exc)
        self._fh, self._size = _open_private(self.path)

    def close(self, timeout: float = 5.0, extra_stats: Optional[Dict[str, Any]] = None) -> None:
        """Stop the writer after it wrote what is queued, then append a stats line."""
        if self._closed:
            return
        self._closed = True
        try:
            self._q.put(None, timeout=timeout)
        except queue.Full:
            log.warning("kvtrace request log: queue still full at close; records lost")
        self._thread.join(timeout)
        if self._thread.is_alive():
            # The writer thread still owns the file; closing it here would race its writes.
            log.warning("kvtrace request log: writer still busy at close; no stats line written")
            return
        try:
            self._fh.write(json.dumps({"kind": STATS_KIND, "ts": time.time(),
                                       "written": self.written, "dropped": self.dropped,
                                       "rotations": self.rotations,
                                       **(extra_stats or {})}) + "\n")
            self._fh.close()
        except OSError as exc:
            log.warning("kvtrace request log: close failed: %s", exc)


INT_BLOCK_HASHES_ENV = "VLLM_KV_EVENTS_USE_INT_BLOCK_HASHES"


def _int_block_hashes_from_env() -> bool:
    """Read vLLM's switch for int block hashes in KV events, with vLLM's default (1)."""
    try:
        return bool(int(os.environ.get(INT_BLOCK_HASHES_ENV, "1")))
    except ValueError:
        return True


def _unwrap_hash(value: Any) -> Any:
    inner = getattr(value, "hash_value", None)
    return inner if inner is not None else value


def _event_hash(value: Any, int_block_hashes: bool) -> Any:
    """Convert a request block hash to the value vLLM puts in its KV events.

    The same rule as vLLM's ``maybe_convert_block_hash``: with
    ``VLLM_KV_EVENTS_USE_INT_BLOCK_HASHES`` set (the default) a bytes hash becomes the int
    of its low 64 bits (big-endian), else the bytes are kept. Ints pass unchanged.
    """
    value = _unwrap_hash(value)
    if int_block_hashes and isinstance(value, (bytes, bytearray)):
        return int.from_bytes(bytes(value), byteorder="big") & ((1 << 64) - 1)
    return value


def _hash_hex(value: Any, int_block_hashes: bool = True) -> Optional[str]:
    """Canonical hex of a block hash, equal to the recorder's raw hash of the KV event."""
    if value is None:
        return None
    try:
        return normalize_hash(_event_hash(value, int_block_hashes))
    except TypeError:
        return None


class RequestTracker:
    """Scheduler-side state: first lookup -> first schedule -> finish, one record per request.

    vLLM calls ``get_num_new_matched_tokens`` again for a request that stays waiting, so
    only the first lookup time is kept and the L0 hit is updated to the latest value.
    """

    def __init__(self, emit: Callable[[Dict[str, Any]], Any], node: Optional[str] = None,
                 max_pending: int = DEFAULT_MAX_PENDING, clock: Callable[[], float] = time.time,
                 anonymizer: Optional[Anonymizer] = None,
                 int_block_hashes: Optional[bool] = None):
        self._emit = emit
        # With a key, req_id and last_block_hash are written as the recorder's HMAC.
        self._anonymizer = anonymizer
        # None follows VLLM_KV_EVENTS_USE_INT_BLOCK_HASHES, so the hash matches the events.
        self._int_block_hashes = (_int_block_hashes_from_env() if int_block_hashes is None
                                  else int_block_hashes)
        self._node = node
        self._max_pending = max_pending
        self._clock = clock
        self._pending: Dict[str, Dict[str, Any]] = {}
        # vLLM's PrefillStats object of each pending request (set in place at first
        # schedule); the Request itself is not kept.
        self._prefill_stats: Dict[str, Any] = {}
        self.evicted = 0
        self.lookups = 0

    def on_lookup(self, request: Any, num_computed_tokens: int) -> None:
        self.lookups += 1
        req_id = request.request_id
        rec = self._pending.get(req_id)
        if rec is None:
            if len(self._pending) >= self._max_pending:
                # Requests that never finish (should not happen) must not grow memory.
                old = next(iter(self._pending))
                self._pending.pop(old)
                self._prefill_stats.pop(old, None)
                self.evicted += 1
            rec = {"kind": RECORD_KIND, "req_id": self._anon_id(req_id), "t_lookup": self._clock(),
                   "t_arrival": getattr(request, "arrival_time", None),
                   "prompt_tokens": getattr(request, "num_prompt_tokens", None), "lookups": 0}
            if self._node:
                rec["node"] = self._node
            self._pending[req_id] = rec
            stats = getattr(request, "prefill_stats", None)
            if stats is not None:
                self._prefill_stats[req_id] = stats
        rec["lookups"] += 1
        if "t_sched" not in rec:
            # vLLM passes the block-aligned local hit (L0) of this lookup.
            rec["l0_tokens"] = int(num_computed_tokens)
            hashes = getattr(request, "block_hashes", None) or []
            rec["blocks"] = len(hashes)
            rec["last_block_hash"] = self._block_hash(hashes[-1]) if hashes else None

    def _anon_id(self, req_id: Any) -> Any:
        if self._anonymizer is None or req_id is None:
            return req_id
        return self._anonymizer(str(req_id))

    def _block_hash(self, value: Any) -> Optional[str]:
        # The canonical hex of the KV-event form of the hash, as the recorder writes it.
        hexed = _hash_hex(value, self._int_block_hashes)
        if self._anonymizer is None or hexed is None:
            return hexed
        # Keyed like the recorder's trace hashes.
        return self._anonymizer(hexed)

    def on_schedule(self, scheduler_output: Any) -> None:
        now = None
        for new in getattr(scheduler_output, "scheduled_new_reqs", None) or []:
            req_id = getattr(new, "req_id", None)
            rec = self._pending.get(req_id)
            if rec is None or "t_sched" in rec:
                continue
            now = now if now is not None else self._clock()
            computed = int(getattr(new, "num_computed_tokens", 0) or 0)
            rec["t_sched"] = now
            rec["computed_at_sched"] = computed
            # vLLM >= 0.30 records the exact split at first schedule (num_prompt_tokens is 0
            # until then). It differs from the lookup value when vLLM keeps a sub-block local
            # tail: the lookup saw only the block-aligned part.
            stats = self._prefill_stats.pop(req_id, None)
            if stats is not None and getattr(stats, "num_prompt_tokens", 0):
                rec["l0_tokens"] = int(stats.num_local_cached_tokens)
                rec["external_tokens"] = int(stats.num_external_cached_tokens)
                rec["split"] = "prefill_stats"
            else:
                # num_computed_tokens at first schedule = L0 + external tokens loaded.
                rec["external_tokens"] = max(0, computed - rec.get("l0_tokens", 0))
                rec["split"] = "computed_minus_lookup"

    def on_finish(self, request: Any) -> None:
        rec = self._pending.pop(request.request_id, None)
        self._prefill_stats.pop(request.request_id, None)
        if rec is None:
            return
        rec["t_finish"] = self._clock()
        status = getattr(request, "status", None)
        rec["status"] = getattr(status, "name", None) if status is not None else None
        rec["num_preemptions"] = getattr(request, "num_preemptions", None)
        self._emit(rec)

    @property
    def pending(self) -> int:
        return len(self._pending)


class HookErrors:
    """Count exceptions from scheduler-side hooks and log them without flooding the log.

    The connector is an observer inside vLLM's EngineCore loop: an exception that left a
    hook would stop the engine, so every hook calls :meth:`run` and goes on after a failure.
    """

    def __init__(self, interval: float = HOOK_ERROR_LOG_INTERVAL,
                 clock: Callable[[], float] = time.monotonic):
        self.count = 0
        self._interval = interval
        self._clock = clock
        self._next_log = -math.inf
        self._since_log = 0

    def run(self, hook: str, fn: Callable[..., Any], *args: Any) -> None:
        try:
            fn(*args)
        except Exception as exc:  # a tracker bug must never stop the engine
            self.count += 1
            self._since_log += 1
            now = self._clock()
            if now >= self._next_log:
                log.warning("kvtrace request log: %s failed (%d errors, %d since the last "
                            "warning); request records may be missing: %r", hook, self.count,
                            self._since_log, exc, exc_info=self._next_log == -math.inf)
                self._next_log = now + self._interval
                self._since_log = 0


def make_log_connector(base: type, metadata_base: type, scheduler_role: Any,
                       hma_base: Optional[type] = None) -> type:
    """Build the log-only connector class on top of vLLM's ``KVConnectorBase_V1``.

    Config (``kv_connector_extra_config`` of this connector): ``path`` (JSON-lines file;
    ``{pid}`` is replaced by the process id, and a path without ``{pid}`` gets ``-<pid>``
    before its extension, see :func:`resolve_log_path`; default :data:`DEFAULT_PATH`,
    for example ``/var/tmp/kvtrace/requests-{pid}.jsonl``), ``queue_size``,
    ``node``, ``max_pending``, ``anonymize_key_file`` (HMAC key file of ``kvtrace record
    --anonymize-key-file``), ``max_bytes`` and ``backup_count`` (rotation).

    ``build_connector_meta`` returns a plain ``metadata_base()``: vLLM pickles the scheduler
    output to send it to the TP workers, and a class defined in this function cannot be
    pickled. vLLM's ``KVConnectorMetadata`` has no abstract methods.
    """

    bases: Tuple[type, ...] = (base,) + ((hma_base,) if hma_base is not None else ())

    class KVTraceLogConnector(*bases):  # type: ignore[misc]
        def __init__(self, vllm_config: Any, role: Any, kv_cache_config: Any = None):
            super().__init__(vllm_config=vllm_config, role=role, kv_cache_config=kv_cache_config)
            self._writer: Optional[RequestLogWriter] = None
            self._tracker: Optional[RequestTracker] = None
            self._errors = HookErrors()
            if role != scheduler_role:
                return
            # The public constructor argument, not the base class's private copy of it.
            extra = connector_extra_config(vllm_config)
            path = extra.get("path") or DEFAULT_PATH
            # One scheduler per engine process: with data parallelism, "{pid}" keeps files apart.
            path = resolve_log_path(str(path), os.getpid())
            key_file = extra.get("anonymize_key_file")
            try:
                # A key that is set but empty is an error too, never a raw log.
                anonymizer = Anonymizer.from_file(str(key_file)) if key_file is not None else None
            except (OSError, ValueError) as exc:
                raise ValueError(f"KVTraceLogConnector: cannot use anonymize_key_file "
                                 f"{key_file!r}: {exc}") from exc
            self._writer = RequestLogWriter(
                path, int(extra.get("queue_size", DEFAULT_QUEUE_SIZE)),
                max_bytes=int(extra.get("max_bytes", DEFAULT_MAX_BYTES)),
                backup_count=int(extra.get("backup_count", DEFAULT_BACKUP_COUNT)))
            self._tracker = RequestTracker(self._writer.submit, node=extra.get("node"),
                                           max_pending=int(extra.get("max_pending", DEFAULT_MAX_PENDING)),
                                           anonymizer=anonymizer)
            log.info("kvtrace request log: writing %s (%s)", path,
                     "anonymized" if anonymizer is not None else "raw request ids and hashes")

        # A best-effort observer: never asks vLLM to keep blocks for delivery.
        @property
        def requires_kv_delivery(self) -> bool:
            return False

        # ---- worker side: nothing to load or save ----
        def start_load_kv(self, forward_context: Any, **kwargs: Any) -> None:
            return None

        def wait_for_layer_load(self, layer_name: str) -> None:
            return None

        def save_kv_layer(self, layer_name: str, kv_layer: Any, attn_metadata: Any,
                          **kwargs: Any) -> None:
            return None

        def wait_for_save(self) -> None:
            return None

        # ---- scheduler side: every hook goes through self._errors.run ----
        def get_num_new_matched_tokens(self, request: Any, num_computed_tokens: int
                                       ) -> Tuple[Optional[int], bool]:
            if self._tracker is not None:
                self._errors.run("get_num_new_matched_tokens", self._tracker.on_lookup, request,
                                 num_computed_tokens)
            return 0, False

        def update_state_after_alloc(self, request: Any, blocks: Any, num_external_tokens: int) -> None:
            return None

        def build_connector_meta(self, scheduler_output: Any) -> Any:
            if self._tracker is not None:
                self._errors.run("build_connector_meta", self._tracker.on_schedule, scheduler_output)
            return metadata_base()

        def request_finished(self, request: Any, block_ids: Any) -> Tuple[bool, Optional[Dict[str, Any]]]:
            if self._tracker is not None:
                self._errors.run("request_finished", self._tracker.on_finish, request)
            return False, None

        def request_finished_all_groups(self, request: Any, block_ids: Any
                                        ) -> Tuple[bool, Optional[Dict[str, Any]]]:
            return self.request_finished(request, block_ids)

        def shutdown(self) -> None:
            if self._writer is not None:
                writer, self._writer = self._writer, None
                tracker = self._tracker
                stats = {"hook_errors": self._errors.count,
                         "evicted": tracker.evicted if tracker is not None else 0}
                self._errors.run("shutdown", writer.close, 5.0, stats)
            parent = getattr(super(), "shutdown", None)
            if parent is not None:
                parent()

    return KVTraceLogConnector
