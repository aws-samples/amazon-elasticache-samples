"""Record KV events from one or more engine data-parallel ranks.

By default the recorder only CONNECTS: a SUB socket per rank for live events and
a DEALER socket per rank for the replay endpoint, so it adds no listening port
to the inference host. An engine whose publisher connects instead of binding
(vLLM with a non-wildcard endpoint) needs ``endpoint_mode="bind"``: the SUB
socket then binds the given loopback address and the engine connects to it.
The replay socket always connects. Because it is a subscriber, the recorder can
never block the engine: if it falls behind, the PUB socket drops messages, and
the recorder sees sequence gaps that it tries to fill from the replay buffer.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import ipaddress
import json
import logging
import queue
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import zmq

from . import metrics as metrics_mod
from .health import ProcessSampler
from .normalize import Anonymizer, Normalizer
from .segments import DEFAULT_FLUSH_SECONDS, NodeInUseError, SegmentWriter
from .sinks import FileSink, Sink
from .wire import SEQ_END_MARKER, DecodeError, decode_batch, decode_seq, encode_seq

log = logging.getLogger(__name__)

SUB_RCVHWM = 1_000_000
STATS_INTERVAL = 60.0
# Exit status of `kvtrace record` when another live recorder writes the same node
# directory; the same status as other setup errors.
EXIT_NODE_IN_USE = 2
# Exit status of `kvtrace record` when no event arrived within --require-events-within.
EXIT_NO_EVENTS = 3
# Exit status when a sink (S3, Valkey) failed to deliver segments or records; the local
# segment files still hold them.
EXIT_SINK_LOST = 4
# Exit status when writing the local trace failed (for example ENOSPC); records after
# the failure are lost. When several apply, the highest status wins.
EXIT_WRITE_FAILED = 5
ENDPOINT_MODES = ("connect", "bind")
# Metric names that show the engine is serving requests (gauges > 0, counters rising).
_ACTIVE_GAUGES = ("num_requests_running", "num_running_reqs")
_ACTIVE_COUNTERS = ("prompt_tokens_total",)


def endpoint_for_rank(endpoint: str, rank: int) -> str:
    """Each DP rank publishes on base port + rank."""
    if rank == 0:
        return endpoint
    parsed = urlparse(endpoint)
    if parsed.scheme != "tcp" or parsed.port is None:
        raise ValueError(f"--dp-ranks needs a tcp://HOST:PORT endpoint, got {endpoint}")
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"tcp://{host}:{parsed.port + rank}"


def enable_ipv6(sock: zmq.Socket, endpoint: str) -> None:
    """Enable IPv6 for an IPv6 address such as tcp://[::1]:PORT.

    Without it libzmq refuses to bind one, and a connect to one silently never
    reaches the peer.
    """
    if ":" in (urlparse(endpoint).hostname or ""):
        sock.setsockopt(zmq.IPV6, 1)


def default_node_id(endpoint: str) -> str:
    """Return the default node id ``<hostname>-<port>`` for a rank-0 endpoint.

    Two engines on one host publish on different ports, so their recorders get
    different node ids. An endpoint without a port (``ipc://``) gives the host name.

    Args:
        endpoint: The rank-0 ``--endpoint``, for example ``tcp://127.0.0.1:5557``.

    Returns:
        The node id.
    """
    host = socket.gethostname()
    try:
        port = urlparse(endpoint).port
    except ValueError:
        port = None
    return f"{host}-{port}" if port is not None else host


def anon_key_fingerprint(key: Optional[bytes]) -> Optional[str]:
    """First 16 hex digits of sha256(key), or None without a key.

    Traces made with the same anonymization key carry the same value, so a reader can
    tell whether their hashes can be compared. The value does not reveal the key.
    """
    if not key:
        return None
    return hashlib.sha256(key).hexdigest()[:16]


def check_bind_endpoint(endpoint: str) -> None:
    """Refuse a bind address other than loopback, so bind mode never listens on a network.

    ``ipc://`` paths are local by nature and are accepted. A host name such as
    ``localhost`` is resolved, and every address it resolves to must be loopback.
    """
    parsed = urlparse(endpoint)
    if parsed.scheme == "ipc":
        return
    if parsed.scheme != "tcp":
        raise ValueError(f"--endpoint-mode bind needs tcp://127.0.0.1:PORT or ipc://, got {endpoint}")
    host = parsed.hostname or ""
    refused = ValueError(f"--endpoint-mode bind only binds a loopback address (127.0.0.1 or ::1); "
                         f"got {endpoint}")
    if not host or host == "*":
        raise refused
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise ValueError(f"--endpoint-mode bind cannot resolve {host!r} in {endpoint}; "
                             f"it only binds a loopback address: {exc}") from exc
        # Drop an IPv6 zone id ("fe80::1%eth0"), which ip_address does not accept.
        addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    if not addresses or not all(a.is_loopback for a in addresses):
        raise refused


@dataclass
class RecorderConfig:
    endpoint: str
    model: str
    out_dir: str
    replay_endpoint: Optional[str] = None
    dp_ranks: int = 1
    topic: str = "kv-events"
    engine: str = "auto"
    # Empty: default_node_id(endpoint), i.e. "<hostname>-<port>".
    node_id: str = ""
    metrics_url: Optional[str] = None
    metrics_interval: float = 15.0
    # More Prometheus endpoints merged into the same M record (e.g. one LMCache internal
    # API server per TP worker). Used only with metrics_url; a failed extra is logged.
    extra_metrics_urls: Tuple[str, ...] = ()
    # Seconds to wait for all extra endpoints (scraped in parallel) per sample.
    extra_metrics_timeout: float = metrics_mod.DEFAULT_EXTRA_TIMEOUT
    rotate_seconds: float = 300.0
    rotate_bytes: int = 64 * 1024 * 1024
    anonymize_key: Optional[bytes] = None
    duration: Optional[float] = None
    replay_timeout: float = 2.0
    endpoint_mode: str = "connect"
    # Seconds without any S/R/C event before the recorder reports the problem; 0
    # disables the check. With metrics_url the window starts when metrics first show
    # requests (an engine that loads or has no traffic sends no events); else at start.
    require_events_within: float = 120.0
    # Stop with EXIT_NO_EVENTS when no event arrived in time; else only warn.
    fail_on_no_events: bool = True
    # Seconds between P (health) records; 0 writes only the final one.
    health_interval: float = 60.0
    # Seconds between sync flushes of the open segment, so a killed recorder leaves a
    # readable .part; 0 flushes only when the segment closes.
    segment_flush_seconds: float = DEFAULT_FLUSH_SECONDS

    def __post_init__(self) -> None:
        if not self.node_id:
            self.node_id = default_node_id(self.endpoint)

    def validate(self) -> None:
        if self.endpoint_mode not in ENDPOINT_MODES:
            raise ValueError(f"endpoint mode must be one of {ENDPOINT_MODES}, got {self.endpoint_mode}")
        if self.require_events_within < 0 or self.health_interval < 0:
            raise ValueError("--require-events-within and --health-interval must be >= 0")
        if self.segment_flush_seconds < 0:
            raise ValueError("--segment-flush-seconds must be >= 0")
        if self.extra_metrics_urls and not self.metrics_url:
            raise ValueError("--extra-metrics-url needs --metrics-url")
        # Each extra URL must have its own kvtrace_url label, or merge_samples sums the
        # series of two URLs and extra_ts keeps only one time. The --metrics-url keys have
        # no such label, so that URL cannot collide. The error names only the labels,
        # because the raw URLs can hold credentials.
        labels = [metrics_mod.source_label(url) for url in self.extra_metrics_urls]
        duplicates = sorted({label for label in labels if labels.count(label) > 1})
        if duplicates:
            raise ValueError("--extra-metrics-url values must differ after user info, query "
                             "and fragment are removed; duplicate labels: "
                             + ", ".join(duplicates))
        if self.extra_metrics_timeout <= 0:
            raise ValueError("--extra-metrics-timeout must be > 0")
        if self.endpoint_mode == "bind":
            if self.dp_ranks > 1 and urlparse(self.endpoint).port == 0:
                raise ValueError("--endpoint-mode bind with --dp-ranks > 1 needs a fixed port, not 0")
            for rank in range(self.dp_ranks):
                check_bind_endpoint(endpoint_for_rank(self.endpoint, rank))


@dataclass
class Stats:
    batches: int = 0
    events: Dict[str, int] = field(default_factory=lambda: {"S": 0, "R": 0, "C": 0})
    unknown_events: int = 0
    bad_events: int = 0
    decode_errors: int = 0
    gaps: int = 0
    gap_batches: int = 0
    recovered_batches: int = 0
    unrecovered_gap_batches: int = 0
    replay_requests: int = 0
    restarts: int = 0
    records: int = 0
    # Failed engine (--metrics-url) scrapes; such a sample writes no M record.
    metrics_failures: int = 0
    # Failed or late scrapes per --extra-metrics-url label (metrics.source_label).
    extra_metrics_failures: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "batches": self.batches,
            "events": dict(self.events),
            "unknown_events": self.unknown_events,
            "bad_events": self.bad_events,
            "decode_errors": self.decode_errors,
            "gaps": self.gaps,
            "gap_batches": self.gap_batches,
            "recovered_batches": self.recovered_batches,
            "unrecovered_gap_batches": self.unrecovered_gap_batches,
            "replay_requests": self.replay_requests,
            "restarts": self.restarts,
            "records": self.records,
            "metrics_failures": self.metrics_failures,
            "extra_metrics_failures": dict(self.extra_metrics_failures),
        }


def _diff(now: Dict[str, Any], before: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in now.items():
        prev = before.get(key, 0 if not isinstance(value, dict) else {})
        out[key] = _diff(value, prev) if isinstance(value, dict) else value - prev
    return out


class _Rank:
    def __init__(self, rank: int, sub: zmq.Socket, replay_endpoint: Optional[str]):
        self.rank = rank
        # Rank label written to records: the engine's data_parallel_rank once a
        # batch reports it, else the socket index.
        self.rank_id = rank
        self.sub = sub
        # Live endpoint as connected or bound (a bound port 0 resolves to the real port;
        # bind mode allows port 0 only for a single rank).
        self.endpoint = ""
        self.replay_endpoint = replay_endpoint
        self.replay: Optional[zmq.Socket] = None
        self.last_seq: Optional[int] = None


class Recorder:
    def __init__(self, config: RecorderConfig, sink: Optional[Sink] = None,
                 context: Optional[zmq.Context] = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = config
        self.sink = sink or FileSink()
        self.ctx = context or zmq.Context.instance()
        self.clock = clock
        self.stats = Stats()
        # One id per recorder process, on every H, M, P and T record: tells two
        # recorders apart that write records with one node id.
        self.rid = uuid.uuid4().hex[:12]
        self._extra_labels = [metrics_mod.source_label(u) for u in config.extra_metrics_urls]
        self.stats.extra_metrics_failures = {label: 0 for label in self._extra_labels}
        self._stop = threading.Event()
        anonymizer = Anonymizer(config.anonymize_key) if config.anonymize_key else None
        self.normalizer = Normalizer(config.node_id, config.model, anonymizer)
        self.engine = config.engine
        self._tally_mark: Dict[str, Any] = self.stats.as_dict()
        self.writer = SegmentWriter(
            config.out_dir,
            config.node_id,
            header={
                "node": config.node_id,
                "model": config.model,
                "engine": config.engine,
                "anonymized": anonymizer is not None,
                "rid": self.rid,
                # Labels only: the raw URLs can hold credentials.
                "metrics_urls": list(self._extra_labels),
                "anon_key_fp": anon_key_fingerprint(config.anonymize_key),
            },
            rotate_seconds=config.rotate_seconds,
            rotate_bytes=config.rotate_bytes,
            on_closed=self.sink.on_segment_closed,
            on_rotate=self._tally_record,
            clock=clock,
            flush_seconds=config.segment_flush_seconds,
        )
        self._ranks: List[_Rank] = []
        # (engine scrape time, sample, time each extra endpoint answered)
        self._metrics_q: queue.Queue[Tuple[float, Dict[str, float], Dict[str, float]]] = queue.Queue()
        # Scrape failures from the metrics thread: None for a failed engine scrape, else
        # the labels of the extra endpoints that failed in one sample.
        self._scrape_failures_q: queue.Queue[Optional[List[str]]] = queue.Queue()
        # Exit status for `kvtrace record`: EXIT_NO_EVENTS after a failed event check,
        # EXIT_SINK_LOST or EXIT_WRITE_FAILED after lost data; the highest one wins.
        self.exit_code = 0
        self._started: Optional[float] = None
        self._events_seen = 0
        self._last_event_at: Optional[float] = None
        self._no_events_reported = False
        # When metrics first showed requests; starts the first-event window with metrics.
        self._traffic_since: Optional[float] = None
        self._stalled = False
        # The two latest metrics samples, to tell whether the engine is serving requests.
        self._metrics_prev: Optional[Dict[str, float]] = None
        self._metrics_last: Optional[Dict[str, float]] = None
        self._process = ProcessSampler()
        self._health_mark: Optional[Tuple[float, Dict[str, Any], int]] = None

    # ---- lifecycle -------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def open(self) -> None:
        """Create the sockets now (``run`` does it otherwise); lets a caller learn bound ports."""
        if not self._ranks:
            self._open_sockets()

    @property
    def endpoints(self) -> List[str]:
        return [r.endpoint for r in self._ranks]

    def _open_sockets(self) -> None:
        self.cfg.validate()
        bind = self.cfg.endpoint_mode == "bind"
        for rank in range(self.cfg.dp_ranks):
            sub = self.ctx.socket(zmq.SUB)
            # Track the socket first so a failed connect still closes it.
            self._ranks.append(_Rank(rank, sub, None))
            sub.setsockopt(zmq.RCVHWM, SUB_RCVHWM)
            sub.setsockopt(zmq.LINGER, 0)
            sub.setsockopt(zmq.SUBSCRIBE, self.cfg.topic.encode("utf-8"))
            endpoint = endpoint_for_rank(self.cfg.endpoint, rank)
            enable_ipv6(sub, endpoint)
            if bind:
                sub.bind(endpoint)
                endpoint = sub.getsockopt(zmq.LAST_ENDPOINT).decode()
            else:
                sub.connect(endpoint)
            self._ranks[-1].endpoint = endpoint
            replay = (endpoint_for_rank(self.cfg.replay_endpoint, rank)
                      if self.cfg.replay_endpoint else None)
            self._ranks[-1].replay_endpoint = replay
            log.info("rank %d: subscribed to %s (%s, topic %r, replay %s)", rank, endpoint,
                     "bound" if bind else "connected", self.cfg.topic, replay or "disabled")

    def _close_sockets(self) -> None:
        for r in self._ranks:
            r.sub.close(linger=0)
            if r.replay is not None:
                r.replay.close(linger=0)
        self._ranks = []

    def run(self) -> Stats:
        """Record until stopped, ``duration`` ends, or the event check fails.

        At start the writer claims the node directory: it refuses a second live
        recorder with the same node id (``NodeInUseError``) and closes orphaned
        ``.part`` files of a killed recorder so the sink gets them. An ``OSError``
        while claiming or writing the trace (for example ENOSPC) stops the recording
        and sets ``exit_code`` to ``EXIT_WRITE_FAILED``; another writer that takes a
        new segment's lock during the run stops it with ``EXIT_NODE_IN_USE``; a sink
        that lost data sets ``EXIT_SINK_LOST``. The highest status wins. Shutdown
        always closes the writer and the sink.

        Returns:
            The recorder stats.

        Raises:
            NodeInUseError: at start, another live recorder writes this node directory.
        """
        metrics_thread = None
        try:
            try:
                self.writer.claim()
            except OSError:
                self._write_failed("claiming the node directory failed; nothing is recorded")
                return self.stats
            self.open()
            if self.cfg.metrics_url:
                metrics_thread = threading.Thread(target=self._metrics_loop, name="kvtrace-metrics",
                                                  daemon=True)
                metrics_thread.start()
            try:
                self._loop()
            except NodeInUseError:
                self._node_in_use("recording stopped; events after this point are lost")
            except OSError:
                self._write_failed("recording stopped; events after this point are lost")
        finally:
            self._shutdown(metrics_thread)
        return self.stats

    def _shutdown(self, metrics_thread: Optional[threading.Thread]) -> None:
        self._stop.set()
        try:
            if metrics_thread is not None:
                metrics_thread.join(timeout=self.cfg.metrics_interval + 6)
            self._guarded(self._drain_metrics, "writing the last metrics records")
            self._close_sockets()
            if self._started is not None:
                self._guarded(lambda: self._write_health(final=True),
                              "writing the final health record")
            self._guarded(self.writer.close, "closing the open segment")
        finally:
            # Runs even when a step above fails, so queued S3 uploads are not lost.
            try:
                self.sink.close()
            finally:
                self.writer.release()
                if self.sink.lost:
                    log.error("sink lost data: %s (the local segments keep it)",
                              json.dumps(self.sink.stats(), sort_keys=True))
                    self.exit_code = max(self.exit_code, EXIT_SINK_LOST)
                self.log_stats()

    def _guarded(self, step: Callable[[], Any], what: str) -> None:
        try:
            step()
        except NodeInUseError:
            self._node_in_use(f"{what} failed")
        except OSError:
            self._write_failed(f"{what} failed")

    def _node_in_use(self, what: str) -> None:
        log.exception("another writer uses %s: %s", self.writer.dir, what)
        self.exit_code = max(self.exit_code, EXIT_NODE_IN_USE)

    def _write_failed(self, what: str) -> None:
        log.exception("trace write error in %s: %s", self.writer.dir, what)
        self.exit_code = max(self.exit_code, EXIT_WRITE_FAILED)

    def _loop(self) -> None:
        poller = zmq.Poller()
        for r in self._ranks:
            poller.register(r.sub, zmq.POLLIN)
        by_socket = {r.sub: r for r in self._ranks}
        started = self._started = self.clock()
        window = self.cfg.require_events_within
        if not window:
            log.info("event watchdog: disabled (--require-events-within 0)")
        elif self.cfg.metrics_url:
            log.info("event watchdog: first event due %.0f s after metrics first show requests "
                     "(%s)", window, self.cfg.metrics_url)
        else:
            log.info("event watchdog: first event due %.0f s after start (no --metrics-url)",
                     window)
        self._health_mark = (started, self.stats.as_dict(), self.writer.total_bytes())
        next_stats = started + STATS_INTERVAL
        next_health = started + self.cfg.health_interval if self.cfg.health_interval else None
        while not self._stop.is_set():
            now = self.clock()
            if self.cfg.duration is not None and now - started >= self.cfg.duration:
                break
            for sock, _ in poller.poll(timeout=200):
                rank = by_socket[sock]
                # Drain what is queued now, but yield regularly to the stop check.
                for _ in range(1000):
                    try:
                        frames = sock.recv_multipart(zmq.NOBLOCK)
                    except zmq.Again:
                        break
                    self._on_live(rank, frames)
            self._drain_metrics()
            # Also runs the --segment-flush-seconds timer, so a quiet recorder still flushes.
            self.writer.maybe_rotate()
            self.check_events(self.clock())
            if now >= next_stats:
                self.log_stats()
                next_stats = now + STATS_INTERVAL
            if next_health is not None and now >= next_health:
                self._write_health(final=False)
                next_health = now + self.cfg.health_interval

    # ---- event watchdog --------------------------------------------------
    def _requests_active(self) -> bool:
        """True when the latest metrics show running requests or rising prompt tokens."""
        last, prev = self._metrics_last, self._metrics_prev
        if not last:
            return False
        for key, value in last.items():
            name = metrics_mod.base_name(key)
            if name.endswith(_ACTIVE_GAUGES) and value > 0:
                return True
            if prev and name.endswith(_ACTIVE_COUNTERS) and value > prev.get(key, value):
                return True
        return False

    def _warn(self, code: str, message: str, since: float, now: float, **extra: Any) -> None:
        self._emit({"t": "W", "ts": now, "node": self.cfg.node_id, "code": code,
                    "message": message, "endpoints": self.endpoints, "topic": self.cfg.topic,
                    "endpoint_mode": self.cfg.endpoint_mode, "seconds": round(now - since, 3),
                    **extra})

    def check_events(self, now: float) -> None:
        """Report a recording that never got an event, or whose events stopped.

        No S/R/C event within ``require_events_within`` seconds is an ERROR (and
        stops the recorder when ``fail_on_no_events``). The window starts when
        metrics first show requests if ``metrics_url`` is set, so a loading or idle
        engine is not a failure; without metrics it starts at the recorder start.
        Events that stop for that long later, while metrics show the engine serving
        requests, are a warning; the warning repeats only after events resumed.
        """
        window = self.cfg.require_events_within
        if not window or self._started is None:
            return
        seen = sum(self.stats.events.values())
        if seen != self._events_seen:
            self._events_seen, self._last_event_at, self._stalled = seen, now, False
            return
        if self._last_event_at is None:
            if self._no_events_reported:
                return
            if self.cfg.metrics_url:
                if self._traffic_since is None:
                    if not self._requests_active():
                        return
                    self._traffic_since = now
                    log.info("metrics show requests: first KV event due within %.0f s", window)
                since, what = self._traffic_since, "since metrics showed requests"
            else:
                since, what = self._started, "since start"
            if now - since < window:
                return
            self._no_events_reported = True
            endpoints = ", ".join(self.endpoints) or self.cfg.endpoint
            s = self.stats
            if s.batches or s.decode_errors:
                # Messages arrive, so the sockets and topic are fine; decoding is not.
                hint = (f"{s.batches} batches arrived ({s.decode_errors} decode errors, "
                        f"{s.unknown_events} unknown events) but none held a KV event. Check "
                        f"--engine ({self.engine}) and that the engine version is supported")
            else:
                hint = ("Check that the engine publishes there: vLLM binds only a wildcard "
                        "endpoint and connects to any other, so for tcp://127.0.0.1:PORT use "
                        "--endpoint-mode bind; a forwarder's XSUB (input) port does not accept "
                        "subscribers; the topic must be a prefix of the engine's topic")
            message = (f"no KV events in {now - since:.0f} s {what} on {endpoints} "
                       f"({self.cfg.endpoint_mode}, topic {self.cfg.topic!r}). {hint}")
            log.error("%s", message)
            self._warn("no_events", message, since, now, batches=s.batches,
                       decode_errors=s.decode_errors, unknown_events=s.unknown_events)
            if self.cfg.fail_on_no_events:
                self.exit_code = max(self.exit_code, EXIT_NO_EVENTS)
                self._stop.set()
        elif not self._stalled and now - self._last_event_at >= window and self._requests_active():
            self._stalled = True
            message = (f"no KV events for {now - self._last_event_at:.0f} s while metrics show "
                       f"requests; last event at {self._last_event_at:.3f}")
            log.warning("%s", message)
            self._warn("events_stalled", message, self._last_event_at, now)

    # ---- health ----------------------------------------------------------
    def health(self, final: bool = False) -> Dict[str, Any]:
        """Totals since start plus rates since the previous health record."""
        now = self.clock()
        stats = self.stats.as_dict()
        total_bytes = self.writer.total_bytes()
        mark_ts, mark_stats, mark_bytes = self._health_mark or (self._started or now, stats, total_bytes)
        span = now - mark_ts
        events = sum(stats["events"].values()) - sum(mark_stats["events"].values())
        record: Dict[str, Any] = {
            "t": "P", "ts": now, "node": self.cfg.node_id, "rid": self.rid, "final": final,
            "uptime": round(now - (self._started or now), 3), "interval": round(span, 3),
            "batches": stats["batches"], "events": stats["events"],
            "gaps": stats["gaps"], "gap_batches": stats["gap_batches"],
            "replay_requests": stats["replay_requests"],
            "recovered_batches": stats["recovered_batches"],
            "unrecovered_gap_batches": stats["unrecovered_gap_batches"],
            "decode_errors": stats["decode_errors"], "restarts": stats["restarts"],
            "events_per_s": round(events / span, 3) if span > 0 else None,
            "bytes_written": total_bytes,
            "bytes_per_s": round((total_bytes - mark_bytes) / span, 1) if span > 0 else None,
            "metrics_failures": stats["metrics_failures"],
            "extra_metrics_failures": stats["extra_metrics_failures"],
            # Sink delivery counters (S3 uploads, Valkey records); empty for file.
            "sink": self.sink.stats(),
            **self._process.sample(),
        }
        self._health_mark = (now, stats, total_bytes)
        return record

    def _write_health(self, final: bool) -> None:
        record = self.health(final=final)
        log.info("health %s", json.dumps(record, sort_keys=True))
        self._emit(record)

    # ---- records ---------------------------------------------------------
    def _emit(self, record: Dict[str, Any]) -> None:
        self.writer.write(record)
        self.sink.on_record(record)
        self.stats.records += 1

    def _tally_record(self) -> Dict[str, Any]:
        now = self.stats.as_dict()
        delta = _diff(now, self._tally_mark)
        self._tally_mark = now
        return {"t": "T", "ts": self.clock(), "node": self.cfg.node_id, "rid": self.rid, **delta}

    def _process_payload(self, rank: _Rank, payload: bytes) -> None:
        try:
            batch = decode_batch(payload, self.engine)
        except DecodeError as exc:
            self.stats.decode_errors += 1
            log.debug("rank %d: decode error: %s", rank.rank, exc)
            return
        self.stats.batches += 1
        self.stats.unknown_events += batch.unknown_events
        self.stats.bad_events += batch.bad_events
        # The batch rank wins over the socket index when the engine sends one.
        rank_id = batch.dp_rank if batch.dp_rank is not None else rank.rank
        rank.rank_id = rank_id
        bad_before = self.normalizer.bad_events
        records = self.normalizer.records(batch, rank_id)
        self.stats.bad_events += self.normalizer.bad_events - bad_before
        for rec in records:
            self.stats.events[rec["t"]] += 1
            self._emit(rec)

    def _on_live(self, rank: _Rank, frames: List[bytes]) -> None:
        if len(frames) == 3:
            seq_frame, payload = frames[1], frames[2]
        elif len(frames) == 2:
            seq_frame, payload = None, frames[1]
        else:
            self.stats.decode_errors += 1
            return
        if seq_frame is None:
            self._process_payload(rank, payload)
            return
        try:
            seq = decode_seq(seq_frame)
        except DecodeError:
            self.stats.decode_errors += 1
            return
        last = rank.last_seq
        if last is not None and seq > last + 1:
            self._fill_gap(rank, last + 1, seq - 1)
        elif last is not None and seq <= last:
            # Sequence went backwards: the publisher restarted. Accept the new stream.
            self.stats.restarts += 1
            log.warning("rank %d: sequence reset %d -> %d (publisher restart?)",
                        rank.rank, last, seq)

            def restart_marker(recovered: Dict[int, bytes]) -> None:
                # The simulator sorts by ts and forgets the rank's state at X, so X must
                # not sort after any batch of the new stream (recorder and engine clocks
                # differ). At equal ts, file order keeps X first.
                times = [self.clock()]
                for data in [*recovered.values(), payload]:
                    batch_ts = self._batch_ts(data)
                    if batch_ts is not None:
                        times.append(batch_ts)
                self._emit({"t": "X", "ts": min(times), "node": self.cfg.node_id,
                            "rank": rank.rank_id, "from_seq": last, "to_seq": seq})

            if seq > 0:
                # The new stream starts at 0: batches before the first one seen were lost.
                self._fill_gap(rank, 0, seq - 1, before_apply=restart_marker)
            else:
                restart_marker({})
        rank.last_seq = seq
        self._process_payload(rank, payload)

    def _batch_ts(self, payload: bytes) -> Optional[float]:
        try:
            return decode_batch(payload, self.engine).ts
        except DecodeError:
            return None

    # ---- gap replay ------------------------------------------------------
    def _replay_socket(self, rank: _Rank) -> zmq.Socket:
        if rank.replay is None:
            # DEALER instead of REQ: the replay ROUTER answers one request with
            # many messages, which a strict REQ socket would reject.
            sock = self.ctx.socket(zmq.DEALER)
            sock.setsockopt(zmq.LINGER, 0)
            assert rank.replay_endpoint is not None
            enable_ipv6(sock, rank.replay_endpoint)
            sock.connect(rank.replay_endpoint)
            rank.replay = sock
        return rank.replay

    def _request_replay(self, rank: _Rank, first: int, last: int) -> Dict[int, bytes]:
        got: Dict[int, bytes] = {}
        sock = self._replay_socket(rank)
        self.stats.replay_requests += 1
        sock.send_multipart([b"", encode_seq(first)])
        deadline = time.monotonic() + self.cfg.replay_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not sock.poll(timeout=int(remaining * 1000)):
                log.warning("rank %d: replay timed out", rank.rank)
                # A late answer would confuse the next request; drop the socket.
                sock.close(linger=0)
                rank.replay = None
                return got
            frames = sock.recv_multipart()
            # vLLM and SGLang answer [b"", topic, seq, payload] and end with
            # [b"", b"", END_SEQ, b""]; older publishers omit the topic. The
            # sequence and payload are always the last two frames.
            if len(frames) < 2:
                # Too short to hold a sequence and a payload: a malformed answer.
                self.stats.decode_errors += 1
                continue
            try:
                seq = decode_seq(frames[-2])
            except DecodeError:
                self.stats.decode_errors += 1
                continue
            if seq == SEQ_END_MARKER:
                return got
            if first <= seq <= last:
                got[seq] = frames[-1]

    def _fill_gap(self, rank: _Rank, first: int, last: int,
                  before_apply: Optional[Callable[[Dict[int, bytes]], None]] = None) -> None:
        missing = last - first + 1
        self.stats.gaps += 1
        self.stats.gap_batches += missing
        recovered: Dict[int, bytes] = {}
        if rank.replay_endpoint:
            recovered = self._request_replay(rank, first, last)
        if before_apply is not None:
            # Lets a restart write its X marker after the replay, before its batches.
            before_apply(recovered)
        for seq in sorted(recovered):
            self._process_payload(rank, recovered[seq])
        unrecovered = missing - len(recovered)
        self.stats.recovered_batches += len(recovered)
        self.stats.unrecovered_gap_batches += unrecovered
        log.info("rank %d: gap of %d batches (seq %d..%d), recovered %d", rank.rank, missing,
                 first, last, len(recovered))
        self._emit({"t": "G", "ts": self.clock(), "node": self.cfg.node_id, "rank": rank.rank_id,
                    "missing": missing, "recovered": len(recovered)})

    # ---- metrics ---------------------------------------------------------
    def _metrics_loop(self) -> None:
        assert self.cfg.metrics_url is not None
        extras = self.cfg.extra_metrics_urls
        pool = (concurrent.futures.ThreadPoolExecutor(
            max_workers=min(metrics_mod.MAX_EXTRA_WORKERS, len(extras)),
            thread_name_prefix="kvtrace-extra") if extras else None)
        try:
            while not self._stop.is_set():
                try:
                    sample = metrics_mod.scrape(self.cfg.metrics_url)
                    # The engine scrape sets the time; slow extra endpoints must not shift it.
                    ts = self.clock()
                    extra_ts: Dict[str, float] = {}
                    if extras:
                        extra, errors, extra_ts = metrics_mod.scrape_many(
                            extras, timeout=self.cfg.extra_metrics_timeout, executor=pool,
                            clock=self.clock)
                        metrics_mod.merge_samples(sample, extra)
                        for err in errors:
                            log.warning("extra metrics scrape failed: %s", err)
                        failed = [label for label in self._extra_labels if label not in extra_ts]
                        if failed:
                            self._scrape_failures_q.put(failed)
                    self._metrics_q.put((ts, sample, extra_ts))
                except Exception as exc:  # network errors must never stop recording
                    log.warning("metrics scrape failed: %s", exc)
                    self._scrape_failures_q.put(None)
                self._stop.wait(self.cfg.metrics_interval)
        finally:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)

    def _drain_metrics(self) -> None:
        # Counted here, on the loop thread, which owns the stats.
        while True:
            try:
                failed = self._scrape_failures_q.get_nowait()
            except queue.Empty:
                break
            if failed is None:
                self.stats.metrics_failures += 1
                continue
            for label in failed:
                self.stats.extra_metrics_failures[label] = (
                    self.stats.extra_metrics_failures.get(label, 0) + 1)
        while True:
            try:
                ts, sample, extra_ts = self._metrics_q.get_nowait()
            except queue.Empty:
                return
            self._metrics_prev, self._metrics_last = self._metrics_last, sample
            if self.engine == "auto":
                detected = metrics_mod.detect_engine(sample)
                if detected:
                    self.engine = detected
                    # Later segments carry the engine in their header. The open
                    # segment's header is already compressed out, so an E record
                    # names the engine for it (the simulator honors both).
                    self.writer.header["engine"] = detected
                    log.info("detected engine %s from metrics", detected)
                    self._emit({"t": "E", "ts": ts, "node": self.cfg.node_id,
                                "engine": detected, "source": "metrics"})
            record = {"t": "M", "ts": ts, "node": self.cfg.node_id, "rid": self.rid,
                      "metrics": sample}
            if extra_ts:
                # When each extra endpoint answered; ts is the engine scrape.
                record["extra_ts"] = extra_ts
            self._emit(record)

    def log_stats(self) -> None:
        s = self.stats
        log.info(
            "batches=%d events=%s unknown=%d bad=%d decode_errors=%d gaps=%d "
            "unrecovered=%d restarts=%d bytes_written=%d",
            s.batches, s.events, s.unknown_events, s.bad_events, s.decode_errors, s.gaps,
            s.unrecovered_gap_batches, s.restarts, self.writer.bytes_written,
        )
