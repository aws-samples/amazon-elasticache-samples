"""Per-node L2 link and write-cost model (``kvtrace simulate --contention on``).

The basic cost model scores every L2 hit alone: ``fetch = rtt + bytes / bw``.
That ignores two costs a busy fleet pays:

1. **Link contention.** Each node reaches L2 over its own network link. Reads
   (hit chunks) and writes (chunk saves) queue on that link, so a hit can wait
   behind other transfers.
2. **Write cost.** Saving chunks can block the request (``blocking`` writes),
   and a constant save stream can slow the whole host (``write_stall_ms_per_gb``)
   or add a fixed delay to every request on the node (``write_load_ms_per_gbs``).

This module replays the L2 operations of one capacity (the ``timeline`` that
:func:`kvtrace.simulator.replay` collects) through one FIFO fluid queue per node
and link, and returns per-run TTFT deltas against a fleet without L2 for two
fetch policies. With ``cache_nodes``, L2 is also ``cache_nodes`` servers, each
with its own send and receive link; a chunk key maps to one server by hash.

- ``ideal``: fetch the best prefix of the run's hit chunks, or nothing when no
  prefix is faster than recomputing it (kvtrace's "skip non-beneficial hits").
- ``lmcache``: always fetch every hit chunk (LMCache does not skip slow hits).

All outputs are model estimates, not measurements. Formulas are in README.md
("Bandwidth and write-cost model").
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Set, Tuple

# Timeline event kinds, collected by replay(): a lookup hit, a lookup miss, and
# an L2 write of a chunk that was not yet in L2 (store-on-miss or evict-write).
HIT = 0
MISS = 1
WRITE = 2
# Host-tier tokens a run can use only because its earlier chunks came from L2: no
# fetch, but recomputed without L2.
HOST_ENABLED = 3

POLICIES = ("ideal", "lmcache")
# Display ids of the internal policy ids. Flags accept both forms.
DISPLAY_IDS = {"ideal": "oracle", "lmcache": "fetch-all"}
# Policy kinds with a value after the colon: min-tokens:T_tok|auto, max-load:M (tokens),
# lookup-timeout:T_ms. min-tokens and max-load run inside replay (static policies).
VALUE_POLICY_KINDS = ("min-tokens", "max-load", "lookup-timeout")
# Default of simulate --gpu-rtt-ms: one network round trip, not measured.
DEFAULT_GPU_RTT_MS = 0.5
# Bisection steps stop when the bracket is this narrow (ms/GB for X_t, ms for rtt).
BISECT_MS = 1.0
# The rtt bisection for Y stops searching above this (ms).
MAX_RTT_SEARCH_MS = 1e7
# Trailing window for the per-node write rate that drives the host stall and the write load.
STALL_WINDOW_SECONDS = 60.0
# A stall share above this would mean the host does nothing but write.
MAX_STALL_SHARE = 0.95

# (kind, ts, node_id, run_id, tokens, chunk_end_offset[, chunk key id]). The key id
# maps a chunk to a cache node (cache_nodes); without it the chunk maps by (run, end).
Event = Tuple[int, ...]


class ContentionError(ValueError):
    pass


def normalize_policy(text: str) -> str:
    """Internal id of a fetch policy given as a display id (oracle, fetch-all) or internal id."""
    text = text.strip()
    by_display = {v: k for k, v in DISPLAY_IDS.items()}
    pid = by_display.get(text, text)
    parse_policy(pid)
    return pid


def display_policy(pid: str) -> str:
    """Display id of an internal policy id (ideal -> oracle, lmcache -> fetch-all)."""
    return DISPLAY_IDS.get(pid, pid)


def parse_policy(pid: str) -> Tuple[str, Optional[float]]:
    """``(kind, value)`` of an internal policy id; ``min-tokens:auto`` gives value None."""
    if pid in POLICIES:
        return pid, None
    kind, sep, raw = pid.partition(":")
    if kind in VALUE_POLICY_KINDS and sep:
        if kind == "min-tokens" and raw == "auto":
            return kind, None
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if math.isfinite(value) and value >= 0:
            return kind, value
    raise ContentionError(f"bad fetch policy {pid!r}: use fetch-all (lmcache), oracle (ideal), "
                          "min-tokens:TOKENS|auto, max-load:TOKENS or lookup-timeout:MS")


@dataclass
class ContentionParams:
    prefill_tokens_per_sec: float
    l2_read_gbps_per_node: float = 100.0
    l2_write_gbps_per_node: float = 100.0
    l2_link_shared: bool = False
    l2_rtt_ms: float = 0.5
    l2_lookup_ms: float = 0.0
    l2_write_mode: str = "async"
    write_stall_ms_per_gb: float = 0.0
    write_load_ms_per_gbs: float = 0.0
    prefill_model: str = "linear"
    prefill_quadratic_tokens: float = 32768.0
    # Server side of L2: this many cache nodes, each with a NIC of cache_node_gbps per
    # direction. None = no server-side limit (only the client links).
    cache_nodes: Optional[int] = None
    cache_node_gbps: Optional[float] = None
    # Physical fixed cost per fetching run for the GPU cost (r_g). Stored only; the TTFT
    # model uses l2_rtt_ms. None = not given (sim.json leaves both fields out).
    gpu_rtt_ms: Optional[float] = None
    gpu_rtt_ms_source: Optional[str] = None
    # Measured per-run TTFT adds (kvtrace calibrate). Each is a fixed delay, not a queue:
    # the measured values already hold the queueing of a loaded engine. The hit value
    # applies to a run whose lookup finds L2 hits, the miss value to every other run.
    # None = not given (sim.json leaves the field out), which adds nothing.
    l2_lookup_ms_hit: Optional[float] = None
    l2_lookup_ms_miss: Optional[float] = None
    # TTFT cost per token a run computes (and so stores to L2) with L2 on, µs/token.
    store_us_per_token: Optional[float] = None

    def validate(self) -> None:
        numbers = (self.prefill_tokens_per_sec, self.l2_read_gbps_per_node, self.l2_write_gbps_per_node,
                   self.l2_rtt_ms, self.l2_lookup_ms, self.write_stall_ms_per_gb,
                   self.write_load_ms_per_gbs, self.prefill_quadratic_tokens)
        if not all(math.isfinite(x) for x in numbers):
            raise ContentionError("contention model inputs must be finite numbers")
        if not self.prefill_tokens_per_sec or self.prefill_tokens_per_sec <= 0:
            raise ContentionError("--contention on needs --prefill-tokens-per-sec > 0")
        if self.l2_read_gbps_per_node <= 0 or self.l2_write_gbps_per_node <= 0:
            raise ContentionError("--l2-read-gbps-per-node and --l2-write-gbps-per-node must be > 0")
        if self.l2_rtt_ms < 0 or self.l2_lookup_ms < 0 or self.write_stall_ms_per_gb < 0 \
                or self.write_load_ms_per_gbs < 0:
            raise ContentionError("--l2-rtt-ms, --l2-lookup-ms, --write-stall-ms-per-gb and "
                                  "--write-load-ms-per-gbs must be >= 0")
        if self.l2_write_mode not in ("async", "blocking"):
            raise ContentionError("--l2-write-mode must be async or blocking")
        if self.prefill_model not in ("linear", "quadratic"):
            raise ContentionError("--prefill-model must be linear or quadratic")
        if self.prefill_quadratic_tokens <= 0:
            raise ContentionError("--prefill-quadratic-tokens must be > 0")
        if (self.cache_nodes is None) != (self.cache_node_gbps is None):
            raise ContentionError("--cache-nodes and --cache-node-gbps go together")
        if self.cache_nodes is not None and self.cache_nodes < 1:
            raise ContentionError("--cache-nodes must be >= 1")
        if self.cache_node_gbps is not None and not (math.isfinite(self.cache_node_gbps)
                                                     and self.cache_node_gbps > 0):
            raise ContentionError("--cache-node-gbps must be > 0")
        if self.gpu_rtt_ms is not None and not (math.isfinite(self.gpu_rtt_ms) and self.gpu_rtt_ms >= 0):
            raise ContentionError("--gpu-rtt-ms must be >= 0")
        for name in ("l2_lookup_ms_hit", "l2_lookup_ms_miss", "store_us_per_token"):
            value = getattr(self, name)
            # Measured ON - OFF deltas can be below 0, so only finiteness is checked.
            if value is not None and not math.isfinite(value):
                raise ContentionError(f"--{name.replace('_', '-')} must be a finite number")
        if self.l2_lookup_ms and (self.l2_lookup_ms_hit is not None or self.l2_lookup_ms_miss is not None):
            # Both model the same lookup gate; the measured values already hold its queueing.
            raise ContentionError("--l2-lookup-ms and the measured lookup delay (--l2-lookup-ms-hit, "
                                  "--l2-lookup-ms-miss or the lookup values of --calibration) both "
                                  "model the lookup gate; give one of them. A lookup-timeout policy "
                                  "needs --l2-lookup-ms, so it cannot run with measured lookup values")

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        if self.cache_nodes is None:
            # Keep the inputs of a run without cache nodes as they were.
            del out["cache_nodes"], out["cache_node_gbps"]
        if self.gpu_rtt_ms is None:
            del out["gpu_rtt_ms"], out["gpu_rtt_ms_source"]
        for name in ("l2_lookup_ms_hit", "l2_lookup_ms_miss", "store_us_per_token"):
            if getattr(self, name) is None:
                del out[name]
        return out


class Link:
    """One FIFO fluid queue: transfers run one after another at ``gbps``."""

    def __init__(self, gbps: float) -> None:
        self.bytes_per_sec = gbps * 1e9 / 8
        self.free_at = -math.inf
        self.busy_seconds = 0.0
        self.bytes = 0
        self.waits: List[float] = []

    def enqueue(self, ts: float, nbytes: int) -> Tuple[float, float]:
        """Queue ``nbytes`` at ``ts``; returns (start, done) times."""
        self.bytes += nbytes
        return self.enqueue_seconds(ts, nbytes / self.bytes_per_sec)

    def enqueue_seconds(self, ts: float, duration: float) -> Tuple[float, float]:
        """Queue a job of ``duration`` seconds at ``ts``; returns (start, done) times."""
        start = max(ts, self.free_at)
        done = start + duration
        self.free_at = done
        self.busy_seconds += duration
        self.waits.append(start - ts)
        return start, done


class _Node:
    def __init__(self, cp: ContentionParams) -> None:
        self.read = Link(cp.l2_read_gbps_per_node)
        self.write = self.read if cp.l2_link_shared else Link(cp.l2_write_gbps_per_node)
        # L2 lookups run one at a time on the node's scheduler (a synchronous
        # existence check before admission), so they queue behind each other.
        self.lookups = Link(1.0)
        self.recent_writes: Deque[Tuple[float, int]] = deque()
        self.recent_bytes = 0
        self.read_bytes = 0
        self.write_bytes = 0

    def write_rate(self, ts: float) -> float:
        """Bytes per second the node wrote over the trailing window that ends at ``ts``."""
        while self.recent_writes and self.recent_writes[0][0] < ts - STALL_WINDOW_SECONDS:
            self.recent_bytes -= self.recent_writes.popleft()[1]
        return self.recent_bytes / STALL_WINDOW_SECONDS

    def stall_share(self, ts: float, stall_s_per_byte: float) -> float:
        # Share of host time lost to writes: stall seconds per byte times the
        # node's write rate over the trailing window.
        return min(MAX_STALL_SHARE, self.write_rate(ts) * stall_s_per_byte)

    def note_write(self, ts: float, nbytes: int) -> None:
        self.recent_writes.append((ts, nbytes))
        self.recent_bytes += nbytes
        self.write_bytes += nbytes


class _CacheNode:
    """One L2 server: a send link (hits it serves) and a receive link (writes)."""

    def __init__(self, gbps: float) -> None:
        self.read = Link(gbps)
        self.write = Link(gbps)


def _cache_node_of(key: int, run_id: int, end: int, n: int) -> int:
    if key < 0:
        key = hash((run_id, end)) & 0xFFFFFFFF
    # Multiplicative hash: key ids are given in first-seen order, so spread them.
    return ((key * 0x9E3779B1) & 0xFFFFFFFF) % n


class _Run:
    __slots__ = ("node", "ts", "hits", "tokens", "enabled", "write_ts", "writes", "hit_servers",
                 "write_servers")

    def __init__(self, node: int, ts: float) -> None:
        self.node = node
        self.ts = ts
        self.hits: List[Tuple[int, int]] = []  # (tokens, chunk end offset), prefix order
        self.tokens: List[Tuple[int, int]] = []  # every chunk the run computes without L2
        self.enabled: List[Tuple[int, int]] = []  # host chunks usable only after all hits
        self.write_ts: Optional[float] = None
        self.writes = 0
        self.hit_servers: List[int] = []  # cache node of each hit chunk (cache_nodes only)
        self.write_servers: List[int] = []  # cache node of each written chunk


def chunk_seconds(tokens: int, end: int, cp: ContentionParams, rate0: float) -> float:
    """Prefill time of the ``tokens`` computed tokens that end at offset ``end``.

    linear: ``tokens / P``. quadratic: a token at depth ``d`` costs
    ``(1 + d / D) / P0``, so tokens ``[a, b)`` cost ``((b - a) + (b^2 - a^2) / (2 D)) / P0``.
    """
    if cp.prefill_model == "linear":
        return tokens / rate0
    b = float(end)
    a = b - tokens
    return ((b - a) + (b * b - a * a) / (2.0 * cp.prefill_quadratic_tokens)) / rate0


def fit_quadratic_rate(events: Sequence[Event], cp: ContentionParams) -> float:
    """Depth-0 rate ``P0`` that makes the trace's lookup tokens cost what ``P`` predicts.

    ``P0 = P * (1 + mean_depth / D)``, with the token-weighted mean depth of
    every computed lookup token in the trace.
    """
    tokens = 0
    depth_sum = 0.0
    for kind, _ts, _node, _run, tok, end, *_key in events:
        if kind == WRITE or tok <= 0:
            continue
        a = end - tok
        tokens += tok
        depth_sum += tok * (a + end) / 2.0
    if not tokens:
        return cp.prefill_tokens_per_sec
    return cp.prefill_tokens_per_sec * (1.0 + depth_sum / tokens / cp.prefill_quadratic_tokens)


def _quantile(values: Sequence[float], q: float, presorted: bool = False) -> Optional[float]:
    """Upper nearest-rank quantile: element ``int(q * n)`` of the sorted values."""
    if not values:
        return None
    s = values if presorted else sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def _link_stats(link: Link, duration: float) -> Dict[str, float]:
    return {"utilization": link.busy_seconds / duration,
            "queue_wait_mean": (sum(link.waits) / len(link.waits)) if link.waits else 0.0,
            "queue_wait_p99": _quantile(link.waits, 0.99) or 0.0,
            "jobs": len(link.waits)}


def _dist(values: Sequence[float]) -> Dict[str, Optional[float]]:
    s = sorted(values)
    return {"p50": _quantile(s, 0.5, True), "p90": _quantile(s, 0.9, True),
            "p99": _quantile(s, 0.99, True), "mean": sum(s) / len(s) if s else None}


def evaluate(events: Sequence[Event], chunk_bytes: int, nodes: Sequence[str],
             duration_seconds: float, cp: ContentionParams,
             keep_runs: bool = False, policies: Sequence[str] = POLICIES,
             extended: bool = False, store_ts: Optional[Sequence[float]] = None,
             hit_runs: Optional[Set[int]] = None,
             prefill_rate_depth0: Optional[float] = None) -> Dict[str, Any]:
    """TTFT deltas, link use and write cost for one capacity's timeline.

    A run's delta is its TTFT with L2 minus its TTFT without L2:
    ``lookup check + fetch - recompute(fetched chunks) + blocking write time + stall + write load``, where
    ``fetch = rtt + queue wait + transfer`` of the fetched chunks. Negative = faster.
    With the measured inputs of ``cp`` (``l2_lookup_ms_hit``, ``l2_lookup_ms_miss``,
    ``store_us_per_token``), each run also gets the hit or miss lookup delay, by whether its
    lookup found hits, plus ``store_us_per_token`` times the tokens it still computes.

    ``policies`` are internal ids: ``ideal``, ``lmcache`` and ``lookup-timeout:T_ms``
    (fetch-all, but no hits when the lookup check takes longer than ``T_ms``; the run
    then waits ``T_ms``). Each policy has its own links. ``extended`` adds, per policy,
    ``fetching_runs``, ``read_gb``, ``queue_wait_sum``, ``fetched_tokens``,
    ``host_enabled_tokens`` and ``ttft_delta_hit_runs``, and per run row the fetched
    chunks of every policy, ``has_hits`` and ``trailing_write_gb_per_s``. ``store_ts``
    (by run id) adds the run's first store-event time to each run row. ``hit_runs`` sets
    which runs count as "runs with hits" (default: runs with a HIT event); give the
    fetch-all set for the timeline of a static policy, whose unloaded hits are MISS events.
    ``prefill_rate_depth0`` fixes the quadratic ``P0`` (default: fitted to ``events``), so
    the timelines of different policies use one rate.
    """
    cp.validate()
    policies = tuple(policies)
    kinds = {p: parse_policy(p) for p in policies}
    for pid, (kind, _value) in kinds.items():
        if kind in ("min-tokens", "max-load"):
            raise ContentionError(f"{pid} is applied in replay, not in the contention model")
        if kind == "lookup-timeout" and not cp.l2_lookup_ms:
            raise ContentionError(f"{pid} needs --l2-lookup-ms > 0")
    rate0 = cp.prefill_tokens_per_sec
    if cp.prefill_model == "quadratic":
        rate0 = prefill_rate_depth0 or fit_quadratic_rate(events, cp)
    rtt = cp.l2_rtt_ms / 1000.0
    stall_s_per_byte = cp.write_stall_ms_per_gb / 1000.0 / 1e9
    # Seconds added to every run per byte/s the node writes (ms per GB/s -> s per B/s).
    load_s_per_bps = cp.write_load_ms_per_gbs / 1000.0 / 1e9
    lookup_hit_s = (cp.l2_lookup_ms_hit or 0.0) / 1000.0
    lookup_miss_s = (cp.l2_lookup_ms_miss or 0.0) / 1000.0
    store_s_per_token = (cp.store_us_per_token or 0.0) / 1e6
    measured_lookup = bool(lookup_hit_s or lookup_miss_s)

    # Group events into runs. A run's reads go out at its first lookup (hits are a
    # prefix of the run), its store-on-miss writes at its first write. Evict-writes
    # are their own jobs.
    n_servers = cp.cache_nodes or 0
    runs: Dict[int, _Run] = {}
    # (ts, seq, kind, run id, or (node id, cache node) for an evict-write)
    jobs: List[Tuple[float, int, int, Any]] = []
    seq = 0
    for kind, ts, node, run_id, tok, end, *key in events:
        server = _cache_node_of(key[0] if key else -1, run_id, end, n_servers) if n_servers else 0
        if kind == WRITE and run_id < 0:
            jobs.append((ts, seq, WRITE, (node, server)))
            seq += 1
            continue
        run = runs.get(run_id)
        if run is None:
            run = runs[run_id] = _Run(node, ts)
            jobs.append((ts, seq, HIT, run_id))
            seq += 1
        if kind == WRITE:
            if run.write_ts is None:
                run.write_ts = ts
                jobs.append((ts, seq, -WRITE, run_id))
                seq += 1
            run.writes += 1
            run.write_servers.append(server)
            continue
        run.tokens.append((tok, end))
        if kind == HIT:
            run.hits.append((tok, end))
            run.hit_servers.append(server)
        elif kind == HOST_ENABLED:
            run.enabled.append((tok, end))
    jobs.sort(key=lambda j: (j[0], j[1]))

    worlds = {p: [_Node(cp) for _ in nodes] for p in policies}
    servers = {p: [_CacheNode(cp.cache_node_gbps or 0.0) for _ in range(n_servers)]
               for p in policies}
    deltas: Dict[str, Dict[int, float]] = {p: {} for p in policies}
    fetched: Dict[str, Dict[int, int]] = {p: {} for p in policies}
    slow_hits = {p: 0 for p in policies}
    hit_chunks = 0
    write_delay: Dict[str, Dict[int, float]] = {p: {} for p in policies}
    write_load: Dict[int, float] = {}  # both worlds see the same writes
    # Extended outputs per policy: queue wait of fetching runs, fetched and host-enabled
    # tokens, lookup timeouts; per run: trailing write rate (the same in every world).
    queue_wait: Dict[str, float] = {p: 0.0 for p in policies}
    fetched_tokens: Dict[str, int] = {p: 0 for p in policies}
    enabled_tokens: Dict[str, int] = {p: 0 for p in policies}
    timeouts: Dict[str, int] = {p: 0 for p in policies}
    run_write_rate: Dict[int, float] = {}

    def run_delta(policy: str, run_id: int, run: _Run, ts: float) -> None:
        kind, value = kinds[policy]
        node = worlds[policy][run.node]
        hits = run.hits
        lookup = 0.0
        timed_out = False
        if cp.l2_lookup_ms:
            # Every run checks L2 first, hit or miss; fetches start when the check is done.
            _start, checked = node.lookups.enqueue_seconds(ts, cp.l2_lookup_ms / 1000.0)
            lookup, ts = checked - ts, checked
            if kind == "lookup-timeout":
                assert value is not None
                limit = value / 1000.0
                if lookup > limit:
                    # LMCache gives up after lookup_timeout_ms and serves the run with no hits.
                    timed_out = True
                    ts -= lookup - limit
                    lookup = limit
        recompute = [chunk_seconds(t, e, cp, rate0) for t, e in hits]
        start = max(ts, node.read.free_at)
        wait = start - ts
        per_chunk = chunk_bytes / node.read.bytes_per_sec
        world_servers = servers[policy]
        # fetch[k-1] = queue wait + transfer of the first k hit chunks. With cache nodes,
        # the client link and each cache node's send link work in parallel, so the
        # fetch ends when the slowest of them is done.
        fetch: List[float] = []
        server_chunks: Dict[int, int] = {}
        server_done = 0.0
        for k in range(1, len(hits) + 1):
            done = wait + k * per_chunk
            if world_servers:
                sid = run.hit_servers[k - 1]
                link = world_servers[sid].read
                server_chunks[sid] = server_chunks.get(sid, 0) + 1
                server_done = max(server_done, max(0.0, link.free_at - ts)
                                  + server_chunks[sid] * chunk_bytes / link.bytes_per_sec)
                done = max(done, server_done)
            fetch.append(done)
        stall = node.stall_share(ts, stall_s_per_byte)
        if extended:
            run_write_rate[run_id] = node.write_rate(ts)
        stall_factor = stall / (1.0 - stall)
        all_compute = sum(chunk_seconds(t, e, cp, rate0) for t, e in run.tokens)
        # Host chunks enabled by L2 hits get credit only when every hit is fetched
        # (a lower bound for the ideal policy when one sits between two hits).
        enabled = sum(chunk_seconds(t, e, cp, rate0) for t, e in run.enabled)
        # delta(k) for fetching the first k hit chunks:
        #   rtt + wait + k * transfer - recompute(first k) + stall on what is still computed
        #   (+ store cost of the tokens still computed, with store_us_per_token), so the
        #   oracle picks k with the store term in.
        fetch_all = kind != "ideal" and not timed_out
        best_k, best = 0, stall_factor * all_compute
        store_left = 0.0
        if store_s_per_token:
            store_left = store_s_per_token * sum(t for t, _e in run.tokens)
            best += store_left
        if fetch_all:
            best_k = len(hits)
        saved = 0.0
        for k in range(1, len(hits) + 1):
            saved += recompute[k - 1]
            if k == len(hits):
                saved += enabled
            value_k = rtt + fetch[k - 1] - saved + stall_factor * (all_compute - saved)
            if store_s_per_token:
                store_left -= store_s_per_token * hits[k - 1][0]
                if k == len(hits):
                    store_left -= store_s_per_token * sum(t for t, _e in run.enabled)
                value_k += store_left
            if fetch_all and k == len(hits):
                best = value_k
            elif kind == "ideal" and value_k < best:
                best_k, best = k, value_k
        if best_k:
            node.read.enqueue(ts, best_k * chunk_bytes)
            node.read_bytes += best_k * chunk_bytes
            if world_servers:
                per_server: Dict[int, int] = {}
                for sid in run.hit_servers[:best_k]:
                    per_server[sid] = per_server.get(sid, 0) + 1
                for sid, count in per_server.items():
                    world_servers[sid].read.enqueue(ts, count * chunk_bytes)
            if extended:
                queue_wait[policy] += fetch[best_k - 1] - best_k * per_chunk
                fetched_tokens[policy] += sum(t for t, _e in hits[:best_k])
                if best_k == len(hits):
                    enabled_tokens[policy] += sum(t for t, _e in run.enabled)
        if timed_out:
            timeouts[policy] += 1
        if hits and rtt + fetch[-1] >= sum(recompute):
            # Fetching the run's hits (one round trip, queue wait, transfer) is
            # slower than recomputing them.
            slow_hits[policy] += len(hits)
        # Write load: a fixed delay for every run on the node, hit or miss, in
        # proportion to the node's current write rate (client and NIC work that
        # the serving process does for the save stream).
        load = 0.0
        if load_s_per_bps:
            load = write_load[run_id] = node.write_rate(ts) * load_s_per_bps
        measured = 0.0
        if measured_lookup:
            # Measured lookup gate: the hit value only when the run is served with L2 hits
            # (a lookup-timeout run that timed out is served with none). An oracle run that
            # fetches nothing still found hits, so it pays the hit value.
            measured = lookup_hit_s if hits and not timed_out else lookup_miss_s
        deltas[policy][run_id] = best + lookup + load + measured
        fetched[policy][run_id] = best_k

    for ts, _seq, kind, ref in jobs:
        if kind == WRITE:
            # Evict-write: no request waits for it; it only uses the link.
            node_id, sid = ref
            for policy in policies:
                node = worlds[policy][node_id]
                node.write.enqueue(ts, chunk_bytes)
                node.note_write(ts, chunk_bytes)
                if n_servers:
                    servers[policy][sid].write.enqueue(ts, chunk_bytes)
            continue
        run = runs[ref]
        if kind == -WRITE:
            for policy in policies:
                node = worlds[policy][run.node]
                _start, done = node.write.enqueue(ts, run.writes * chunk_bytes)
                node.note_write(ts, run.writes * chunk_bytes)
                if n_servers:
                    per_server: Dict[int, int] = {}
                    for sid in run.write_servers:
                        per_server[sid] = per_server.get(sid, 0) + 1
                    for sid, count in per_server.items():
                        # The save is done when the slowest cache node has its part.
                        done = max(done, servers[policy][sid].write.enqueue(ts, count * chunk_bytes)[1])
                if cp.l2_write_mode == "blocking":
                    # The first token waits until the save of this run is done. The
                    # run's lookup job always sorts first (lower ts or seq), so its
                    # delta exists.
                    deltas[policy][ref] += done - ts
                    write_delay[policy][ref] = done - ts
            continue
        # A run without hits still pays the host stall (and, later, blocking writes).
        hit_chunks += len(run.hits)
        for policy in policies:
            run_delta(policy, ref, run, ts)

    duration = duration_seconds if duration_seconds > 0 else 1.0
    out: Dict[str, Any] = {
        "model": "fifo-fluid-per-node" + ("-and-cache-node" if n_servers else ""),
        "prefill_rate_depth0": rate0,
        "runs": len(runs),
        "hit_chunks": hit_chunks,
        "policies": {},
    }
    for policy in policies:
        values = list(deltas[policy].values())
        world = worlds[policy]
        per_node = {}
        for name, node in zip(nodes, world):
            links = ({"shared": node.read} if cp.l2_link_shared
                     else {"read": node.read, "write": node.write})
            if cp.l2_lookup_ms:
                links["lookup"] = node.lookups
            per_node[name] = {
                "read_bytes_per_sec": node.read_bytes / duration,
                "write_bytes_per_sec": node.write_bytes / duration,
                "link": {lk: {"utilization": link.busy_seconds / duration,
                              "queue_wait_mean": (sum(link.waits) / len(link.waits)) if link.waits else 0.0,
                              "queue_wait_p99": _quantile(link.waits, 0.99) or 0.0,
                              "jobs": len(link.waits)}
                         for lk, link in links.items()},
            }
        out["policies"][policy] = {
            "ttft_delta": _dist(values),
            "runs_slower": sum(1 for v in values if v > 0),
            "runs_faster": sum(1 for v in values if v < 0),
            "fetched_chunks": sum(fetched[policy].values()),
            "hits_slower_than_recompute": slow_hits[policy],
            "hits_slower_fraction": slow_hits[policy] / hit_chunks if hit_chunks else 0.0,
            "per_node": per_node,
        }
        if n_servers:
            out["policies"][policy]["per_cache_node"] = {
                f"cache-{i}": {
                    "read_bytes_per_sec": server.read.bytes / duration,
                    "write_bytes_per_sec": server.write.bytes / duration,
                    "link": {lk: _link_stats(link, duration)
                             for lk, link in (("read", server.read), ("write", server.write))},
                }
                for i, server in enumerate(servers[policy])
            }
        if extended:
            pol = out["policies"][policy]
            pol["fetching_runs"] = sum(1 for k in fetched[policy].values() if k)
            pol["read_gb"] = pol["fetched_chunks"] * chunk_bytes / 1e9
            pol["queue_wait_sum"] = queue_wait[policy]
            pol["fetched_tokens"] = fetched_tokens[policy]
            pol["host_enabled_tokens"] = enabled_tokens[policy]
            pol["ttft_delta_hit_runs"] = _dist([deltas[policy][rid] for rid, run in runs.items()
                                                if (rid in hit_runs if hit_runs is not None else run.hits)
                                                and rid in deltas[policy]])
            if kinds[policy][0] == "lookup-timeout":
                pol["lookup_timeouts"] = timeouts[policy]
    if keep_runs:
        rows = []
        for rid, run in sorted(runs.items(), key=lambda kv: (kv[1].ts, kv[0])):
            row: Dict[str, Any] = {"run": rid, "node": nodes[run.node], "ts": run.ts,
                                   "hit_tokens": sum(t for t, _ in run.hits),
                                   "lookup_tokens": sum(t for t, _ in run.tokens)}
            for policy in policies:
                row[policy] = deltas[policy].get(rid, 0.0)
            for policy in policies:
                if policy != "lmcache" or extended:
                    row[f"{policy}_fetched_chunks"] = fetched[policy].get(rid, 0)
            for policy in policies:
                row[f"{policy}_write_delay"] = write_delay[policy].get(rid, 0.0)
            if extended:
                row["has_hits"] = rid in hit_runs if hit_runs is not None else bool(run.hits)
                row["trailing_write_gb_per_s"] = run_write_rate.get(rid, 0.0) / 1e9
            if store_ts is not None:
                row["store_ts"] = store_ts[rid]
            rows.append(row)
        out["run_deltas"] = rows
        if load_s_per_bps:
            for row in out["run_deltas"]:
                row["write_load"] = write_load.get(row["run"], 0.0)
    return out


def auto_min_tokens(cp: ContentionParams, chunk_tokens: int, chunk_bytes: int,
                    fetch_all: Dict[str, Any]) -> Dict[str, Any]:
    """``T_tok`` of ``min-tokens:auto``: the linear-model token count at which fetch-all wins.

    ``T_tok = c * ceil(rtt / ((1 + stall_factor) * c / p - B * 8 / W))``, with ``rtt`` =
    ``l2_rtt_ms``, ``p`` = ``prefill_tokens_per_sec``, ``W`` = the read link, and
    ``stall_factor`` from the mean engine-node write rate of the fetch-all policy in
    ``fetch_all`` (an :func:`evaluate` output). ``min_tokens`` is None when fetching a
    chunk is never faster than computing it (the policy then never fetches).
    """
    per_node = fetch_all["policies"]["lmcache"]["per_node"]
    rate = sum(n["write_bytes_per_sec"] for n in per_node.values()) / len(per_node) if per_node else 0.0
    share = min(MAX_STALL_SHARE, rate * cp.write_stall_ms_per_gb / 1000.0 / 1e9)
    stall_factor = share / (1.0 - share)
    transfer = chunk_bytes * 8 / (cp.l2_read_gbps_per_node * 1e9)
    gain = (1.0 + stall_factor) * chunk_tokens / cp.prefill_tokens_per_sec - transfer
    tokens = None
    if gain > 0:
        tokens = chunk_tokens * math.ceil(cp.l2_rtt_ms / 1000.0 / gain)
    return {"min_tokens": tokens, "stall_factor": stall_factor, "mean_write_bytes_per_sec": rate,
            "l2_rtt_ms": cp.l2_rtt_ms, "gain_per_chunk_seconds": gain, "model": "linear"}


def ttft_change(off: Sequence[float], on: Sequence[float]) -> Dict[str, Optional[float]]:
    """``quantile(ON) - quantile(OFF)`` over all requests, at p50, p90 and p99 (seconds).

    This is the "change of p90 TTFT" that OFF/ON run pairs measure, not the p90 of the
    per-run deltas.
    """
    a, b = sorted(off), sorted(on)
    out: Dict[str, Optional[float]] = {}
    for name, q in (("p50", 0.5), ("p90", 0.9), ("p99", 0.99)):
        qa, qb = _quantile(a, q, True), _quantile(b, q, True)
        out[name] = None if qa is None or qb is None else qb - qa
    return out


@dataclass
class OffJoin:
    """OFF TTFT per request and the runs of each request (``simulate --off-ttft``)."""

    off: List[float]
    runs: List[List[int]]
    stats: Dict[str, Any]

    def on(self, delta: Dict[int, float], rtt_s: float = 0.0,
           hit: Optional[Dict[int, bool]] = None) -> List[float]:
        """ON TTFT per request: OFF + the deltas of its runs (+ ``rtt_s`` per fetching run)."""
        out = []
        for off, rids in zip(self.off, self.runs):
            extra = sum(delta.get(r, 0.0) for r in rids)
            if hit is not None and rtt_s:
                extra += rtt_s * sum(1 for r in rids if hit.get(r))
            out.append(off + extra)
        return out


def join_off_ttft(run_rows: Sequence[Dict[str, Any]], requests: Sequence[Tuple[str, float, float]]
                  ) -> OffJoin:
    """Join OFF requests ``(node, ts, ttft_s)`` to run rows on (node, ``store_ts`` to 1 µs).

    The synth index gives each request one time, send + OFF TTFT, which is also the first
    store-event time of its run. A request without a run gets delta 0.
    """
    by_key: Dict[Tuple[str, int], List[int]] = {}
    for row in run_rows:
        by_key.setdefault((row["node"], round(row["store_ts"] * 1e6)), []).append(row["run"])
    runs = [by_key.get((node, round(ts * 1e6)), []) for node, ts, _ttft in requests]
    joined = {r for rids in runs for r in rids}
    stats = {"requests": len(requests), "requests_with_run": sum(1 for rids in runs if rids),
             "runs": len(run_rows), "runs_joined": len(joined),
             "run_join_rate": len(joined) / len(run_rows) if run_rows else None}
    return OffJoin([ttft for _n, _t, ttft in requests], runs, stats)


def _headline(delta: Dict[int, float], hit: Dict[int, bool], rtt_s: float,
              join: Optional[OffJoin]) -> Optional[float]:
    """Headline statistic at ``rtt_s``: change of p90 TTFT with a join, else the p90 of the
    per-run delta over runs with hits. Below 0 means fetch-all is faster."""
    if join is not None:
        return ttft_change(join.off, join.on(delta, rtt_s, hit))["p90"]
    return _quantile([d + rtt_s for r, d in delta.items() if hit.get(r)], 0.9)


def _bisect_stall(stat: Callable[[float], Optional[float]], grid: Sequence[float],
                  x_max: Optional[float]) -> Dict[str, Any]:
    """Largest ``X_t`` (ms/GB) where ``stat`` < 0, to :data:`BISECT_MS`; ``stat`` does not
    decrease in ``X_t``."""
    if stat(0.0) is None:
        return {"ms_per_gb": None, "direction": "no runs with hits"}
    if stat(0.0) >= 0:  # type: ignore[operator]
        return {"ms_per_gb": None, "direction": "slower at all X_t"}
    points = sorted({0.0, *grid})
    faster = [x for x in points if stat(x) < 0]  # type: ignore[operator]
    lo = max(faster)
    slower = [x for x in points if x > lo]
    hi: Optional[float] = min(slower) if slower else None
    if hi is None:
        if x_max is None:
            return {"ms_per_gb": None, "direction": "faster at all X_t (no writes)"}
        x = lo
        while hi is None:
            x = min(x_max, max(2.0 * x, BISECT_MS))
            if stat(x) >= 0:  # type: ignore[operator]
                hi = x
            elif x >= x_max:
                return {"ms_per_gb": None, "direction": "faster at the stall cap (0.95)",
                        "bracket": [x_max, None]}
            else:
                lo = x
    while hi - lo > BISECT_MS:
        mid = (lo + hi) / 2.0
        if stat(mid) < 0:  # type: ignore[operator]
            lo = mid
        else:
            hi = mid
    return {"ms_per_gb": lo, "direction": "fetch-all faster below", "bracket": [lo, hi]}


def _break_even_rtt(delta: Dict[int, float], hit: Dict[int, bool],
                    join: Optional[OffJoin]) -> Dict[str, Any]:
    """``Y``: the largest ``rtt`` (ms) at which fetch-all lowers the headline statistic."""
    if join is None:
        p90 = _quantile([d for r, d in delta.items() if hit.get(r)], 0.9)
        if p90 is None:
            return {"ms": None, "direction": "no runs with hits"}
        if p90 >= 0:
            return {"ms": None, "direction": "slower at all rtt"}
        return {"ms": -p90 * 1000.0, "direction": "fetch-all faster below"}

    def stat(ms: float) -> float:
        value = _headline(delta, hit, ms / 1000.0, join)
        return math.inf if value is None else value

    if stat(0.0) >= 0:
        return {"ms": None, "direction": "slower at all rtt"}
    lo, hi = 0.0, BISECT_MS
    while stat(hi) < 0:
        lo, hi = hi, 2.0 * hi
        if hi > MAX_RTT_SEARCH_MS:
            return {"ms": None, "direction": "faster at all rtt"}
    while hi - lo > BISECT_MS:
        mid = (lo + hi) / 2.0
        if stat(mid) < 0:
            lo = mid
        else:
            hi = mid
    return {"ms": lo, "direction": "fetch-all faster below"}


def stall_sweep(events: Sequence[Event], chunk_bytes: int, nodes: Sequence[str],
                duration_seconds: float, cp: ContentionParams, grid: Sequence[float],
                gpu_rtt_ms: float, join: Optional[OffJoin] = None,
                rtt_grid: Optional[Sequence[float]] = None,
                policies: Sequence[str] = POLICIES) -> Dict[str, Any]:
    """P1 break-even of fetch-all on one timeline (``simulate --contention-sweep``).

    One :func:`evaluate` call per ``X_t`` value at ``rtt`` = 0 gives each run's delta and
    hit flag. Under fetch-all, ``rtt`` adds 1:1 to the delta of each fetching run and
    changes nothing else, so the statistic at any ``rtt`` needs no new call. ``X_t*`` is
    found at ``rtt`` = ``gpu_rtt_ms`` and at ``cp.l2_rtt_ms``: the grid is evaluated,
    then bisected to 1 ms/GB; above the grid, ``X_t`` doubles up to
    ``X_max = 950 / w_min`` (every run with a nonzero trailing write rate is then at the
    0.95 stall cap). ``Y`` (break-even ``rtt``) is given per grid point. ``rtt_grid``
    adds rows of every policy at each (``X_t``, ``rtt``) pair, because ``rtt`` changes
    the choice of ``ideal``.
    """
    memo: Dict[float, Tuple[Dict[int, float], Dict[int, bool]]] = {}
    rates: List[float] = []

    def at(x: float) -> Tuple[Dict[int, float], Dict[int, bool]]:
        if x not in memo:
            out = evaluate(events, chunk_bytes, nodes, duration_seconds,
                           replace(cp, l2_rtt_ms=0.0, write_stall_ms_per_gb=x),
                           keep_runs=True, policies=("lmcache",), extended=True)
            rows = out["run_deltas"]
            memo[x] = ({row["run"]: row["lmcache"] for row in rows},
                       {row["run"]: row["lmcache_fetched_chunks"] > 0 for row in rows})
            if not rates:
                rates.extend(row["trailing_write_gb_per_s"] for row in rows)
        return memo[x]

    at(0.0)
    nonzero = [w for w in rates if w > 0]
    w_min = min(nonzero) if nonzero else None
    x_max = MAX_STALL_SHARE * 1000.0 / w_min if w_min else None

    def stat_at(rtt_ms: float) -> Callable[[float], Optional[float]]:
        return lambda x: _headline(*at(x), rtt_ms / 1000.0, join)

    star = {"at_gpu_rtt": _bisect_stall(stat_at(gpu_rtt_ms), grid, x_max),
            "at_l2_rtt": _bisect_stall(stat_at(cp.l2_rtt_ms), grid, x_max)}
    star["at_gpu_rtt"]["rtt_ms"] = gpu_rtt_ms
    star["at_l2_rtt"]["rtt_ms"] = cp.l2_rtt_ms
    points = []
    for x in grid:
        delta, hit = at(x)
        points.append({"write_stall_ms_per_gb": x,
                       "headline_at_gpu_rtt_s": _headline(delta, hit, gpu_rtt_ms / 1000.0, join),
                       "headline_at_l2_rtt_s": _headline(delta, hit, cp.l2_rtt_ms / 1000.0, join),
                       "break_even_rtt": _break_even_rtt(delta, hit, join)})
    out: Dict[str, Any] = {
        "knob": "write_stall_ms_per_gb", "grid": list(grid),
        "statistic": "ttft_p90_change" if join is not None else "ttft_delta_p90_hit_runs",
        "gpu_rtt_ms": gpu_rtt_ms, "l2_rtt_ms": cp.l2_rtt_ms,
        "w_min_gb_per_s": w_min, "x_max_ms_per_gb": x_max,
        "evaluations": len(memo), "points": points, "x_t_star": star,
    }
    if rtt_grid:
        rows = []
        for x in grid:
            for rtt_ms in rtt_grid:
                res = evaluate(events, chunk_bytes, nodes, duration_seconds,
                               replace(cp, l2_rtt_ms=rtt_ms, write_stall_ms_per_gb=x),
                               keep_runs=join is not None, policies=policies, extended=True)
                pols = {}
                for pid, pol in res["policies"].items():
                    entry = {"ttft_delta": pol["ttft_delta"],
                             "ttft_delta_hit_runs": pol["ttft_delta_hit_runs"],
                             "fetched_chunks": pol["fetched_chunks"], "fetching_runs": pol["fetching_runs"]}
                    if join is not None:
                        delta = {row["run"]: row[pid] for row in res["run_deltas"]}
                        entry["ttft_change"] = ttft_change(join.off, join.on(delta))
                    pols[pid] = entry
                rows.append({"write_stall_ms_per_gb": x, "l2_rtt_ms": rtt_ms, "policies": pols})
        out["rtt_points"] = rows
    return out
