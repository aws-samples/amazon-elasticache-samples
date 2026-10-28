"""Offline replay of recorded KV events through a simulated, fleet-shared L2 KV tier.

The simulator answers: "if every node had shared an L2 cache of capacity C,
how many prefill tokens would have been fetched from L2 instead of computed?"

It runs in two phases:

1. ``prepare`` walks the trace once and derives, from each node's L1 (GPU)
   behaviour, the ordered sequence of L2 operations an LMCache-style
   connector would have issued: chunk lookups for computed prefill, and (for
   the ``evict`` write policy) chunk writes when a node drops a block.
2. ``replay`` runs that operation sequence against one L2 cache per capacity.
   Cache state never feeds back into phase 1: the trace records what the
   engines really did, and we only estimate what L2 could have served.
"""

from __future__ import annotations

import gzip
import heapq
import json
import logging
import math
import random
import re
from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import TOOL_NAME
from . import contention as contention_mod
from . import metrics as metrics_mod
from .costmodel import metric_totals
from .normalize import GPU

log = logging.getLogger("kvtrace")

LOOKUP = 0
EVICT_WRITE = 1
# Emulated host tier (--host-tier-bytes-per-node): a computed chunk the node's host
# tier still holds, and a host-held prefix chunk whose L2 copy a lookup refreshes.
HOST_HIT = 2
HOST_TOUCH = 3
# Write of a chunk that a lookup missed (write policy store), at the store-event
# time. Its lookup can be earlier (--lookup-time scheduled).
STORE = 4
# --l2-lookup full-prompt: an L2 lookup of a prefix chunk that the node's GPU (L0)
# still holds. A hit saves nothing (L0 serves it) but refreshes L2; a miss ends the
# run's L2 prefix.
L0_LOOKUP = 5
# Operations a lookup run issues when it is scheduled (moved by --lookup-time scheduled).
RUN_LOOKUP_KINDS = (LOOKUP, HOST_HIT, HOST_TOUCH, L0_LOOKUP)
# Added to the kind of an op from the warm-up window (--warmup-seconds): replay
# applies it to the cache state but counts it in no result.
WARM = 8
RUN_TAIL_CAP = 1_000_000
RUN_STATE_CAP = 1_000_000
# Above this fraction of runs clamped to their first store, sim.json gets a caveat.
CLAMPED_RUN_CAVEAT_FRACTION = 0.25

# Reuse-time histogram: upper bounds in seconds (time from L2 write to hit).
REUSE_BUCKETS: Tuple[Tuple[str, float], ...] = (
    ("<1s", 1), ("<10s", 10), ("<1m", 60), ("<10m", 600), ("<1h", 3600),
    ("<6h", 21600), ("<1d", 86400), (">=1d", math.inf),
)

_SIZE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([KMGT]i?B|B)?\s*$", re.IGNORECASE)
_UNITS = {
    "B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12,
    "KIB": 2**10, "MIB": 2**20, "GIB": 2**30, "TIB": 2**40,
}


class SimulationError(ValueError):
    pass


def parse_size(text: str) -> Optional[int]:
    """Parse ``64GiB``, ``1.5TB``, ``1000`` (bytes) or ``inf`` (returns None)."""
    if text.strip().lower() in ("inf", "infinite", "unlimited"):
        return None
    match = _SIZE.match(text)
    if not match:
        raise SimulationError(f"bad capacity {text!r}: use e.g. 64GiB, 1TB or inf")
    unit = (match.group(2) or "B").upper()
    return int(float(match.group(1)) * _UNITS[unit])


def parse_capacities(text: str) -> List[Tuple[str, Optional[int]]]:
    return [(part.strip(), parse_size(part)) for part in text.split(",") if part.strip()]


# Phase 1 SimParams fields and their defaults: at these values replay is unchanged and
# sim.json does not list the field.
PHASE1_DEFAULTS: Dict[str, Any] = {
    "count_rewrites": False, "count_rewrites_source": None, "lookup_touch": "on", "admission": "all",
    "admission_ghost_chunks": None, "load_scale": 1, "tile_offset": "random", "tile_keys": "disjoint",
    "tile_seed": 0, "fetch_policies": list(contention_mod.POLICIES), "cache_node_counts": None,
    "cache_node_type": "i8ge.48xlarge", "cache_node_gb": 1237.0, "cache_node_nvme_gb": 120000.0,
    "cache_node_gbps": 180.0,
}
# Fetch policies that decide k before the fetch, from the hit prefix alone, inside replay.
STATIC_POLICY_KINDS = ("min-tokens", "max-load")
# Largest random tile offset (--tile-offset random), seconds.
TILE_OFFSET_SECONDS = 60.0
# Buckets of hits_per_written_chunk: (label, smallest hit count).
WRITE_HIT_BUCKETS: Tuple[Tuple[str, int], ...] = (("0", 0), ("1", 1), ("2-3", 2), ("4-7", 4), ("8+", 8))
STORE_FALLBACK_REFUSAL = (
    "lookup time fell back to store because no per-request prefill rate was found. Give "
    "--prefill-tokens-per-sec-per-request or record request_prefill_time metrics.")


def parse_admission(text: str) -> Tuple[str, Optional[float], bool]:
    """``(kind, T_s, absolute)`` of an ``--admission`` value.

    Values: all, second-hit, oracle-useful, ``none-after:T_s`` (trace time) or
    ``none-after:@UNIX_TS`` (absolute wall time, e.g. the logged ``t_drop``; then
    ``absolute`` is True and the value is that time).
    """
    if text in ("all", "second-hit", "oracle-useful"):
        return text, None, False
    kind, _, value = text.partition(":")
    absolute = value.startswith("@")
    if absolute:
        value = value[1:]
    if kind == "none-after" and value:
        try:
            seconds = float(value)
        except ValueError:
            seconds = math.nan
        if math.isfinite(seconds) and seconds >= 0:
            return kind, seconds, absolute
    raise SimulationError(f"bad --admission {text!r}: use all, second-hit, oracle-useful, "
                          "none-after:SECONDS or none-after:@UNIX_TS")


def read_off_ttft(path: str) -> List[Tuple[str, float, float]]:
    """``(node, ts, ttft_s)`` per request from a ``synth_from_mooncake.py --index`` file.

    Phase 1 joins requests to runs on (node, first store time), which only the synth
    index gives: each row has ``node``, ``ts`` (send + OFF TTFT) and ``ttft_s``, and no
    ``t_send``. Driver rows (``requests.jsonl``) have no node and no store time, so they
    are refused, as is an index made without the OFF driver log (``ttft_s`` null).
    """
    opener = gzip.open if path.endswith(".gz") else open
    rows: List[Tuple[str, float, float]] = []
    with opener(path, "rt", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if "t_send" in row or not all(k in row for k in ("node", "ts", "ttft_s")):
                raise SimulationError(
                    f"{path}:{n}: --off-ttft accepts only the synth_from_mooncake.py --index file "
                    "(keys node, ts, ttft_s and no t_send); driver request rows have no node and no "
                    "store time")
            if row["ttft_s"] is None:
                raise SimulationError(f"{path}:{n}: no OFF TTFT; make the index with "
                                      "synth_from_mooncake.py --requests OFF_DRIVER_LOG --index")
            rows.append((str(row["node"]), float(row["ts"]), float(row["ttft_s"])))
    if not rows:
        raise SimulationError(f"{path}: no requests")
    return rows


def cache_node_capacities(counts: Sequence[int], node_type: str, node_gb: float
                          ) -> List[Tuple[str, Optional[int]]]:
    """One capacity per cache node count ``n``: ``n * node_gb`` GB (usable DRAM per node)."""
    return [(f"{n} x {node_type}", int(round(n * node_gb * 1e9))) for n in counts]


def _bucket_hits(hits: int) -> str:
    label = WRITE_HIT_BUCKETS[0][0]
    for name, low in WRITE_HIT_BUCKETS:
        if hits >= low:
            label = name
    return label


@dataclass
class StaticPolicy:
    """A fetch policy that replay applies to each run's external hit prefix.

    ``min_tokens``: fetch every hit when the run's external hit tokens (host tier plus L2)
    are at least this many, else load none (None = never fetch). ``max_chunks``: load at
    most this many external hit chunks in prefix order (None = no cap).
    """

    pid: str
    kind: str
    min_tokens: Optional[float] = None
    max_chunks: Optional[int] = None

    def allowed(self, ext_tokens: Sequence[int]) -> float:
        """How many external hit chunks of the run, in prefix order, are loaded."""
        if self.kind == "min-tokens":
            if self.min_tokens is None:
                return 0
            return math.inf if sum(ext_tokens) >= self.min_tokens else 0
        return math.inf if self.max_chunks is None else self.max_chunks


def static_policy(pid: str, chunk_tokens: int, min_tokens: Optional[float] = None) -> StaticPolicy:
    """Build the :class:`StaticPolicy` of an internal id. ``min_tokens`` resolves ``min-tokens:auto``."""
    kind, value = contention_mod.parse_policy(pid)
    if kind == "min-tokens":
        return StaticPolicy(pid, kind, min_tokens=min_tokens if value is None else value)
    if kind != "max-load":
        raise SimulationError(f"{pid} is not a static fetch policy")
    assert value is not None
    # LMCache max_tokens_per_load 0 means no cap.
    return StaticPolicy(pid, kind, max_chunks=int(value) // chunk_tokens if value else None)


@dataclass
class SimParams:
    chunk_tokens: int = 256
    capacities: List[Tuple[str, Optional[int]]] = field(
        default_factory=lambda: parse_capacities("64GiB,256GiB,1TiB,4TiB,16TiB,inf"))
    kv_bytes_per_token: Optional[int] = None
    policy: str = "lru"
    ttl_seconds: Optional[float] = None
    write_policy: str = "store"
    model: Optional[str] = None
    start: Optional[float] = None
    end: Optional[float] = None
    # Events in [start - warmup_seconds, start) build the cache state but are not
    # counted in any result.
    warmup_seconds: float = 0.0
    # L2 space model: bytes each key costs beyond its chunk (metadata, allocator
    # rounding) and the share of the capacity that can hold keys.
    l2_per_key_overhead_bytes: int = 0
    l2_usable_fraction: float = 1.0
    # Two BlockStored events form one lookup run (chunked prefill) only when the
    # second starts at the first one's last block AND arrives within this window.
    run_continue_seconds: float = 1.0
    # A block whose parent depth is unknown (it was cached before recording began)
    # anchors a chunk grid of its own at that parent. Off: such chains are unaligned.
    anchor_unknown_parents: bool = True
    # vLLM stores each block that decoding fills as a one-block event that extends
    # the request's previous event: count those tokens as decode, not prefill.
    # auto = only on nodes identified as vLLM by a segment header, an engine (E)
    # record or vllm: metric names (SGLang emits one event per radix node, which
    # can also be a single page).
    decode_blocks: str = "auto"
    # Max gap between a request's previous event and a one-block continuation for
    # it to count as decode. Decode fills one block every block_size / (per-request
    # decode tokens/s), e.g. 16 tokens at 5 tok/s = 3.2 s, which is far longer than
    # the chunked-prefill window; 10 s covers decode down to 1.6 tok/s at 16 tokens.
    decode_window_seconds: float = 10.0
    # Per-node host tier that publishes no events (LMCache local CPU behind vLLM):
    # an LRU of full chunks in front of L2. vLLM reports its loads as GPU stores, so
    # without this they look like computed prefill and become L2 lookups.
    host_tier_bytes_per_node: Optional[int] = None
    # Host-served prefix chunks refresh their L2 copy (like a lookup-time TOUCH).
    host_tier_touch_l2: bool = False
    # When a lookup run looks up L2. scheduled: when the engine scheduled the request,
    # i.e. the run's last store event minus its computed tokens / per-request prefill
    # rate, not later than its first store event. store: at the store event (the earlier
    # model). New L2 writes are always visible at the store event.
    lookup_time: str = "scheduled"
    # Which chunks a lookup run looks up in L2. computed: only the chunks it computes.
    # full-prompt: from chunk 0 of the prompt, as LMCache does, so the L0-held prefix
    # is looked up too and its first L2 miss ends the run's L2 hits.
    l2_lookup: str = "computed"
    # Per-request prefill rate for lookup_time=scheduled; None = from recorded metrics.
    prefill_tokens_per_sec_per_request: Optional[float] = None
    # Phase 1 fields. sim.json leaves out each one at its default (PHASE1_DEFAULTS).
    # A write of a chunk that L2 already holds counts as a write and a WRITE event.
    count_rewrites: bool = False
    # "input" or "auto" (turned on by a fetch policy or admission policy).
    count_rewrites_source: Optional[str] = None
    # on: a hit check refreshes every hit (the L2 cache). off: only fetched hits (stock Valkey).
    lookup_touch: str = "on"
    # all | second-hit | oracle-useful | none-after:T_s
    admission: str = "all"
    # Ghost set size of --admission second-hit; None = the capacity in chunks.
    admission_ghost_chunks: Optional[int] = None
    # Load scale: T_l copies (tiles) of the trace on their own engine nodes.
    load_scale: int = 1
    tile_offset: str = "random"
    tile_keys: str = "disjoint"
    tile_seed: int = 0
    # Fetch policies as internal ids (ideal, lmcache, min-tokens:T, max-load:M,
    # lookup-timeout:T_ms). Static policies (min-tokens, max-load) run inside replay.
    fetch_policies: List[str] = field(default_factory=lambda: list(contention_mod.POLICIES))
    # Capacity axis in cache nodes (replaces capacities): n * cache_node_gb GB of DRAM each.
    cache_node_counts: Optional[List[int]] = None
    cache_node_type: str = "i8ge.48xlarge"
    cache_node_gb: float = 1237.0
    cache_node_nvme_gb: float = 120000.0
    cache_node_gbps: float = 180.0

    def phase1(self) -> bool:
        """True when any phase 1 replay option differs from its default."""
        return any(getattr(self, key) != value for key, value in PHASE1_DEFAULTS.items())

    def static_policies(self) -> List[str]:
        return [p for p in self.fetch_policies if p.split(":", 1)[0] in STATIC_POLICY_KINDS]

    def validate(self) -> None:
        if self.chunk_tokens <= 0:
            raise SimulationError("--chunk-tokens must be positive")
        if self.policy not in ("lru", "ttl"):
            raise SimulationError("--policy must be lru or ttl")
        if self.policy == "ttl" and not self.ttl_seconds:
            raise SimulationError("--policy ttl needs --ttl-seconds")
        if self.write_policy not in ("store", "evict"):
            raise SimulationError("--write-policy must be store or evict")
        if self.decode_blocks not in ("auto", "on", "off"):
            raise SimulationError("--decode-blocks must be auto, on or off")
        if self.run_continue_seconds < 0 or self.decode_window_seconds < 0:
            raise SimulationError("--run-continue-seconds and --decode-window-seconds must be >= 0")
        if not self.capacities:
            raise SimulationError("no capacities given")
        if not self.warmup_seconds >= 0:
            raise SimulationError("--warmup-seconds must be >= 0")
        if self.l2_per_key_overhead_bytes < 0:
            raise SimulationError("--l2-per-key-overhead-bytes must be >= 0")
        if not 0 < self.l2_usable_fraction <= 1:
            raise SimulationError("--l2-usable-fraction must be > 0 and <= 1")
        if self.warmup_seconds and self.start is None:
            raise SimulationError("--warmup-seconds needs --start")
        if self.l2_lookup not in ("computed", "full-prompt"):
            raise SimulationError("--l2-lookup must be computed or full-prompt")
        if self.lookup_time not in ("scheduled", "store"):
            raise SimulationError("--lookup-time must be scheduled or store")
        if self.prefill_tokens_per_sec_per_request is not None and \
                not self.prefill_tokens_per_sec_per_request > 0:
            raise SimulationError("--prefill-tokens-per-sec-per-request must be > 0")
        if self.host_tier_bytes_per_node is not None:
            if self.host_tier_bytes_per_node <= 0:
                raise SimulationError("--host-tier-bytes-per-node must be positive")
            if not self.kv_bytes_per_token:
                raise SimulationError("--host-tier-bytes-per-node needs the KV size "
                                      "(--kv-bytes-per-token, --model-config or --preset)")
            if self.host_tier_bytes_per_node < self.chunk_tokens * self.kv_bytes_per_token:
                raise SimulationError("--host-tier-bytes-per-node is smaller than one chunk "
                                      f"({self.chunk_tokens * self.kv_bytes_per_token} bytes)")
        elif self.host_tier_touch_l2:
            raise SimulationError("--host-tier-touch-l2 needs --host-tier-bytes-per-node")
        finite = any(b is not None for _, b in self.capacities)
        if finite and not self.kv_bytes_per_token:
            raise SimulationError(
                "finite capacities need KV size: pass --kv-bytes-per-token, --model-config or --preset")
        if self.lookup_touch not in ("on", "off"):
            raise SimulationError("--lookup-touch must be on or off")
        parse_admission(self.admission)
        if self.admission_ghost_chunks is not None and self.admission_ghost_chunks < 1:
            raise SimulationError("--admission-ghost-chunks must be >= 1")
        if self.load_scale < 1:
            raise SimulationError("--load-scale must be >= 1")
        if self.tile_offset not in ("none", "random"):
            raise SimulationError("--tile-offset must be none or random")
        if self.tile_keys not in ("disjoint", "shared"):
            raise SimulationError("--tile-keys must be disjoint or shared")
        for pid in self.fetch_policies:
            contention_mod.parse_policy(pid)
        if self.cache_node_counts is not None:
            if not self.cache_node_counts or any(n < 1 for n in self.cache_node_counts):
                raise SimulationError("--cache-node-counts must be a list of counts >= 1")
            if len(self.cache_node_counts) != len(self.capacities):
                raise SimulationError("cache_node_counts needs one capacity per count "
                                      "(build them with cache_node_capacities)")
            if not (self.cache_node_gb > 0 and self.cache_node_gbps > 0 and self.cache_node_nvme_gb >= 0):
                raise SimulationError("--cache-node-gb and --cache-node-gbps must be > 0 and "
                                      "--cache-node-nvme-gb >= 0")


@dataclass
class Prepared:
    """Phase-1 output: compact L2 operations plus trace facts."""

    # (kind, ts, key_id, node_id, run_id, computed_tokens, chunk end offset)
    ops: List[Tuple[int, float, int, int, int, int, int]] = field(default_factory=list)
    key_model: List[int] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    nodes: List[str] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    # Per node: (ts, metrics, extra URLs that answered) of each M record.
    metric_samples: Dict[str, List[metrics_mod.MetricSample]] = field(default_factory=dict)
    # How lookup times were set (sim.json "lookup_time").
    lookup_time: Dict[str, Any] = field(default_factory=dict)
    # Model limits that apply to this simulation (sim.json "caveats").
    caveats: List[str] = field(default_factory=list)
    # Per lookup run (by run id): time of its first store event (run "store_ts").
    run_store_ts: List[float] = field(default_factory=list)
    # Earliest segment header "started" time (trace time 0 of --admission none-after).
    trace_epoch: Optional[float] = None


class _Interner:
    def __init__(self) -> None:
        self.ids: Dict[Any, int] = {}
        self.values: List[Any] = []

    def __call__(self, value: Any) -> int:
        idx = self.ids.get(value)
        if idx is None:
            idx = len(self.values)
            self.ids[value] = idx
            self.values.append(value)
        return idx


def sort_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    indexed = list(records)
    order = sorted(range(len(indexed)), key=lambda i: (
        indexed[i].get("ts", 0.0), str(indexed[i].get("node", "")), indexed[i].get("rank", 0) or 0, i))
    return [indexed[i] for i in order]


def node_engines(records: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Engine per node, from ``H`` headers, ``E`` engine records and metric names.

    A header or ``E`` record that names a concrete engine wins. A recorder run
    with ``--engine auto`` writes ``auto`` in the first header, so otherwise the
    engine comes from the node's metric names (``vllm:`` or ``sglang:``).
    """
    declared: Dict[str, str] = {}
    from_metrics: Dict[str, str] = {}
    for rec in records:
        kind = rec.get("t")
        node = str(rec.get("node", ""))
        if kind in ("H", "E"):
            engine = rec.get("engine")
            if engine in ("vllm", "sglang"):
                declared[node] = engine
        elif kind == "M" and node not in from_metrics:
            detected = metrics_mod.detect_engine(rec.get("metrics") or {})
            if detected:
                from_metrics[node] = detected
    return {**from_metrics, **declared}


def prepare(records: Iterable[Dict[str, Any]], params: SimParams) -> Prepared:
    params.validate()
    chunk = params.chunk_tokens
    prep = Prepared()
    models = _Interner()
    nodes = _Interner()
    keys = _Interner()

    # Per (node, rank): block hash -> set of media holding it (GPU and host tiers).
    local: Dict[Tuple[str, int], Dict[str, set]] = {}
    # Per (node, rank): chunk-boundary block hash -> L2 key id (write-policy evict only).
    boundary: Dict[Tuple[str, int], Dict[str, int]] = {}
    # Token offset where each block ends, keyed by (model, ns, hash). The block
    # hash is chained, so it identifies its whole prefix and the offset is global.
    end_tok: Dict[Tuple[str, str, str], int] = {}
    # (node, rank, last block hash) -> (run_id, pending computed tokens, last ts, reset epoch).
    run_tail: OrderedDict[Tuple[str, int, str], Tuple[int, int, float, int]] = OrderedDict()
    next_run = 0
    # Lookup runs that have computed a block (their host-tier prefix is over).
    computed_runs: OrderedDict[int, None] = OrderedDict()
    # Per lookup run: ts of its first and last store events, and its computed tokens
    # (for the scheduled lookup time).
    run_first_ts: List[float] = []
    run_last_ts: List[float] = []
    run_computed: List[int] = []
    store_writes = params.write_policy == "store"
    # (node, rank) -> count of cache resets (C) and publisher restarts (X) so far.
    epoch: Dict[Tuple[str, int], int] = {}

    counts: Counter = Counter()
    counted_tokens: Counter = Counter()
    # Token tallies of warm-up records: kept apart, never reported.
    warm_tokens: Counter = Counter()
    tokens = counted_tokens
    # Records before this time only build state (--warmup-seconds); None = no warm-up.
    count_from = params.start if params.warmup_seconds else None
    # Ops are appended in time order, so warm-up ops are the first n_warm_ops.
    n_warm_ops: Optional[int] = None
    block_sizes: set = set()
    bad_block_sizes: Counter = Counter()
    media_seen: set = set()
    ranks_seen: set = set()
    model_names: set = set()
    t_min, t_max = math.inf, -math.inf
    tallies: Counter = Counter()
    gap_missing = gap_recovered = restarts = 0
    removed_unknown = 0
    anchors = 0
    # (model, ns, hash) of blocks on a grid anchored at an unknown parent, and the
    # computed tokens placed on such grids.
    anchored_keys: set = set()
    anchored_tokens = 0
    host_on = params.host_tier_bytes_per_node is not None
    full_prompt = params.l2_lookup == "full-prompt"
    # Chunk chains (blk_chunk, chunk_parent) are needed by the host tier and full-prompt.
    track_chunks = host_on or full_prompt
    host_cap = (params.host_tier_bytes_per_node // (chunk * params.kv_bytes_per_token)
                if host_on and params.kv_bytes_per_token else 0)
    # Emulated host tier per node, shared by the node's ranks (its capacity is per
    # node): chunk key ids, least recent first.
    host: Dict[str, OrderedDict[int, None]] = {}
    # Last chunk key at or before each block, and each chunk key's previous chunk
    # key (-1 at the root): the request's whole cached prefix, for host recency.
    blk_chunk: Dict[Tuple[str, str, str], int] = {}
    chunk_parent: Dict[int, int] = {}
    host_touches = 0
    def retire_tail(entry: Tuple[int, int, float, int]) -> None:
        # Computed tokens that never reached a chunk boundary: LMCache does not
        # store partial chunks, so these can never be served by L2.
        tokens["tail"] += entry[1]

    ordered = sort_records(records)
    # Pre-pass over the whole trace (not only the window): the engine of a node is
    # fixed, and metrics that identify it can arrive after its first events.
    # Nodes identified as vLLM get decode-block detection (decode_blocks=auto).
    vllm_nodes = {node for node, engine in node_engines(ordered).items() if engine == "vllm"}

    for rec in ordered:
        kind = rec.get("t")
        ts = float(rec.get("ts") or 0.0)
        if kind in ("H", "E"):
            if kind == "H" and isinstance(rec.get("started"), (int, float)):
                started = float(rec["started"])
                prep.trace_epoch = started if prep.trace_epoch is None else min(prep.trace_epoch, started)
            continue
        if params.start is not None and ts < params.start - params.warmup_seconds:
            continue
        if params.end is not None and ts > params.end:
            continue
        if params.model and rec.get("model") not in (None, params.model):
            continue
        warm = count_from is not None and ts < count_from
        if warm and kind not in ("S", "R", "C", "X"):
            # Metrics, tallies and diagnostics of the warm-up window are not results.
            continue
        if not warm and n_warm_ops is None:
            n_warm_ops = len(prep.ops)
        tokens = warm_tokens if warm else counted_tokens
        node = str(rec.get("node", ""))
        if not warm:
            counts[kind] += 1
        if kind == "M":
            # The extra URLs that answered (extra_ts) tell a partial scrape from a reset,
            # as in `kvtrace tiers`.
            prep.metric_samples.setdefault(node, []).append(
                (ts, rec.get("metrics") or {}, tuple(rec.get("extra_ts") or ())))
            continue
        if kind == "T":
            for key in ("batches", "decode_errors", "unknown_events", "bad_events"):
                tallies[key] += int(rec.get(key) or 0)
            continue
        if kind == "G":
            gap_missing += int(rec.get("missing") or 0)
            gap_recovered += int(rec.get("recovered") or 0)
            continue
        if kind in ("P", "W"):
            # Recorder health and warnings: diagnostics, not cache events.
            continue
        if kind not in ("S", "R", "C", "X"):
            counts["unknown_record"] += 1
            continue

        if not warm:
            t_min, t_max = min(t_min, ts), max(t_max, ts)
        rank = int(rec.get("rank") or 0)
        nr = (node, rank)
        loc = local.setdefault(nr, {})

        if kind in ("C", "X"):
            # The engine reset its cache (C), or the publisher restarted (X), which
            # means a new engine process with an empty cache: forget this rank's state.
            if kind == "X" and not warm:
                restarts += 1
            loc.clear()
            boundary.pop(nr, None)
            # The host tier is shared by the node's ranks; a reset of any rank
            # clears it (the safe choice: no host hit survives a reset).
            host.pop(node, None)
            epoch[nr] = epoch.get(nr, 0) + 1
            continue
        medium = rec.get("medium") or GPU
        if not warm:
            ranks_seen.add(f"{node}:{rank}")
            media_seen.add(medium)
        hashes: List[str] = rec.get("h") or []

        if kind == "R":
            bmap = boundary.get(nr)
            for h in hashes:
                held = loc.get(h)
                if not held:
                    removed_unknown += 0 if warm else 1
                    continue
                held.discard(medium)
                if held:
                    continue
                # The node no longer holds this block in any tier.
                del loc[h]
                if bmap is not None:
                    key_id = bmap.pop(h, None)
                    if key_id is not None:
                        # write-policy evict: the chunk reaches L2 only when the node
                        # drops it entirely (a GPU->host offload keeps it local).
                        prep.ops.append((EVICT_WRITE, ts, key_id, nodes(node), -1, 0, 0))
            continue

        # kind == "S"
        model = str(rec.get("model") or "")
        model_names.add(model)
        ns = str(rec.get("ns") or "")
        bs = int(rec.get("bs") or 1)
        if bs <= 0 or chunk % bs:
            # This block size cannot sit on the chunk grid (e.g. a partial block
            # from an engine fork). Skip the event and report it as data quality.
            bad_block_sizes[bs] += 1
            tokens["bad_block_size"] += len(hashes) * max(bs, 0)
            continue
        block_sizes.add(bs)
        parent = rec.get("p")
        parent_end: Optional[int] = 0 if parent is None else end_tok.get((model, ns, parent))
        anchored = parent is not None and (model, ns, parent) in anchored_keys
        if parent_end is None and params.anchor_unknown_parents:
            # The parent's chained hash fixes its prefix, so every chain through it
            # sees the same grid: chunk keys stay consistent across requests and
            # nodes, only shifted from the absolute grid by the unknown depth.
            parent_end = 0
            end_tok[(model, ns, parent)] = 0
            anchored_keys.add((model, ns, parent))
            anchored = True
            anchors += 0 if warm else 1
        tokens["stored"] += len(hashes) * bs

        if medium != GPU:
            # Store to a host/disk tier = a tier move (offload), never prefill compute.
            for h in hashes:
                loc.setdefault(h, set()).add(medium)
                if parent_end is not None:
                    parent_end += bs
                    end_tok[(model, ns, h)] = parent_end
                    if anchored:
                        anchored_keys.add((model, ns, h))
            tokens["tier_move"] += len(hashes) * bs
            continue

        # GPU store. Continue a lookup run if this event extends the previous event's
        # last block on the same rank shortly after it (chunked prefill).
        run_id = -1
        pending = 0
        decode = False
        if parent is not None:
            tail = run_tail.pop((node, rank, parent), None)
            if tail is not None:
                # A run never continues across a cache reset or publisher restart.
                fresh = tail[3] == epoch.get(nr, 0)
                gap = ts - tail[2]
                detect = (params.decode_blocks == "on" or
                          (params.decode_blocks == "auto" and node in vllm_nodes))
                if fresh and detect and len(hashes) == 1 and gap <= params.decode_window_seconds:
                    # One block filled by decoding: generated tokens end the
                    # prefill run, whose partial chunk becomes tail. Decode has its
                    # own window because blocks fill at the per-request decode rate.
                    decode = True
                    retire_tail(tail)
                elif fresh and gap <= params.run_continue_seconds:
                    if tail[0] >= 0:
                        run_id, pending = tail[0], tail[1]
                    else:
                        # A prefill that extends a decoded block starts a new run.
                        retire_tail(tail)
                else:
                    retire_tail(tail)
        if run_id < 0 and not decode:
            run_id = next_run
            next_run += 1
            run_first_ts.append(ts)
            run_last_ts.append(ts)
            run_computed.append(0)
            if host_on and parent is not None:
                host_touches += (0 if warm else 1) * _refresh_host_prefix(
                    host.setdefault(node, OrderedDict()), blk_chunk.get((model, ns, parent), -1),
                    chunk_parent, prep.ops if params.host_tier_touch_l2 else None,
                    ts, nodes(node), run_id)
            if full_prompt and parent is not None:
                _lookup_l0_prefix(prep.ops, blk_chunk.get((model, ns, parent), -1), chunk_parent,
                                  host.get(node) if host_on else None, ts, nodes(node), run_id)
        if run_id >= 0:
            # A chunked prefill stores across several events; the last one ends the prefill.
            run_last_ts[run_id] = max(run_last_ts[run_id], ts)

        model_id = models(model)
        node_id = nodes(node)
        bmap = boundary.setdefault(nr, {}) if params.write_policy == "evict" else None
        cur_chunk = blk_chunk.get((model, ns, parent), -1) if track_chunks and parent is not None else -1
        host_new: List[int] = []
        for h in hashes:
            end = None if parent_end is None else parent_end + bs
            if end is not None:
                end_tok[(model, ns, h)] = end
                if anchored:
                    anchored_keys.add((model, ns, h))
            held = loc.get(h)
            if held and GPU in held:
                tokens["duplicate"] += bs
            elif held and (decode or run_id not in computed_runs):
                # Present in this node's host tier: a load-back, not compute and not
                # an L2 lookup (the node serves it from its own host memory).
                tokens["loadback"] += bs
            elif decode:
                # Generated by decoding: no prefill compute, no L2 lookup (LMCache
                # saves prefill KV by default). It still extends the prefix grid.
                tokens["decode"] += bs
            else:
                # Not anywhere on this node: an L1 miss that cost prefill compute.
                # A host tier serves a prefix only, so a block it holds behind a
                # block this run computed is recomputed too, not loaded back.
                tokens["computed"] += bs
                run_computed[run_id] += bs
                computed_runs[run_id] = None
                if len(computed_runs) > RUN_STATE_CAP:
                    computed_runs.popitem(last=False)
                if end is None:
                    # Parent depth unknown (recording began mid-stream), so the
                    # block cannot be placed on a chunk grid.
                    tokens["unaligned"] += bs
                else:
                    pending += bs
                    if anchored:
                        # On a grid shifted by the anchor's unknown depth: chunk
                        # boundaries differ from the engine's absolute grid.
                        anchored_tokens += 0 if warm else bs
            if end is not None and end % chunk == 0:
                # This block closes a chunk. The chained boundary hash identifies the
                # whole prefix, so (model, ns, hash) is the L2 key.
                key_id = keys((model_id, ns, h))
                if key_id == len(prep.key_model):
                    prep.key_model.append(model_id)
                if track_chunks:
                    chunk_parent.setdefault(key_id, cur_chunk)
                    cur_chunk = key_id
                if pending and host_on:
                    held_host = host.setdefault(node, OrderedDict())
                    if key_id in held_host:
                        # The node's own host tier serves it: vLLM reports the load
                        # as a GPU store, but it is neither compute nor an L2 lookup.
                        held_host.move_to_end(key_id)
                        prep.ops.append((HOST_HIT, ts, key_id, node_id, run_id, pending, end))
                        tokens["computed"] -= pending
                        run_computed[run_id] -= pending
                        tokens["host_hit"] += pending
                    else:
                        prep.ops.append((LOOKUP, ts, key_id, node_id, run_id, pending, end))
                        if store_writes:
                            prep.ops.append((STORE, ts, key_id, node_id, run_id, 0, end))
                        # Stored after the event's lookups: LMCache looks up the whole
                        # prompt first, so a new chunk cannot evict a later hit.
                        host_new.append(key_id)
                    pending = 0
                elif pending:
                    prep.ops.append((LOOKUP, ts, key_id, node_id, run_id, pending, end))
                    if store_writes:
                        prep.ops.append((STORE, ts, key_id, node_id, run_id, 0, end))
                    pending = 0
                if bmap is not None and not decode:
                    # A chunk that decode closes holds generated tokens, which LMCache
                    # does not save: `store` retires them as tail, so `evict` must not
                    # write the chunk when the node drops it either.
                    bmap[h] = key_id
            loc.setdefault(h, set()).add(GPU)
            if track_chunks:
                blk_chunk[(model, ns, h)] = cur_chunk
            parent_end = end
        if host_new:
            held_host = host.setdefault(node, OrderedDict())
            for key_id in host_new:
                held_host[key_id] = None
            while len(held_host) > host_cap:
                held_host.popitem(last=False)
        if hashes:
            run_tail[(node, rank, hashes[-1])] = (run_id, pending, ts, epoch.get(nr, 0))
            if len(run_tail) > RUN_TAIL_CAP:
                retire_tail(run_tail.popitem(last=False)[1])

    tokens = counted_tokens
    for entry in run_tail.values():
        retire_tail(entry)
    if bad_block_sizes and not block_sizes:
        # No event fits the chunk grid: the chunk size is wrong for this trace.
        raise SimulationError(
            f"--chunk-tokens {chunk} is not a multiple of any block size in the trace "
            f"({', '.join(str(b) for b in sorted(bad_block_sizes))})")

    prep.models = list(models.values)
    prep.nodes = list(nodes.values)
    warm_runs = 0
    if count_from is not None:
        if n_warm_ops is None:
            n_warm_ops = len(prep.ops)
        prep.ops[:n_warm_ops] = [(op[0] + WARM,) + op[1:] for op in prep.ops[:n_warm_ops]]
        # The rule of replay(): a run with a lookup is counted when any of its lookups is
        # not a warm-up op, so a chunked run that crosses --start is counted. A run with
        # no lookup is warm when it started in the warm-up window.
        counted = {op[4] for op in prep.ops[n_warm_ops:] if op[0] == LOOKUP}
        looked_up = {op[4] for op in prep.ops[:n_warm_ops] if op[0] == LOOKUP + WARM}
        warm_runs = sum(1 for run_id, t in enumerate(run_first_ts)
                        if run_id not in counted and (run_id in looked_up or t < count_from))
    _set_lookup_times(prep, params, run_first_ts, run_last_ts, run_computed)
    prep.run_store_ts = run_first_ts
    has_events = t_min <= t_max
    prep.summary = {
        "window": {
            "start": t_min if has_events else None,
            "end": t_max if has_events else None,
            "duration_seconds": (t_max - t_min) if has_events else 0.0,
        },
        "nodes": sorted({r.split(":")[0] for r in ranks_seen}),
        "ranks": sorted(ranks_seen),
        "models": sorted(model_names),
        "record_counts": dict(counts),
        "block_sizes": sorted(block_sizes),
        "skipped_block_sizes": {str(k): v for k, v in sorted(bad_block_sizes.items())},
        "media": sorted(media_seen),
        "gaps": {"missing_batches": gap_missing, "recovered_batches": gap_recovered,
                 "unrecovered_batches": gap_missing - gap_recovered, "restarts": restarts},
        "decode": dict(tallies),
        "removed_unknown_blocks": removed_unknown,
        "anchored_unknown_parents": anchors,
        "anchored_tokens": anchored_tokens,
        "tokens": {k: tokens[k] for k in (
            "stored", "computed", "loadback", "tier_move", "duplicate", "decode", "unaligned",
            "tail", "bad_block_size")},
        "l2_lookups": sum(1 for op in prep.ops if op[0] == LOOKUP),
        "lookup_runs": next_run - warm_runs,
    }
    if count_from is not None:
        prep.summary["warmup"] = {
            "seconds": params.warmup_seconds, "from": count_from - params.warmup_seconds,
            "ops": n_warm_ops, "lookup_runs": warm_runs,
            "tokens": {k: warm_tokens[k] for k in ("stored", "computed", "decode")}}
    if host_on:
        prep.summary["host_tier"] = {
            "chunks_per_node": host_cap, "hit_tokens": tokens["host_hit"],
            "hit_chunks": sum(1 for op in prep.ops if op[0] == HOST_HIT),
            "prefix_chunks_refreshed": host_touches,
            "touch_l2": params.host_tier_touch_l2,
            # The trace also has real host-tier events: both models then run, and
            # the emulated tier holds chunks the trace already shows off GPU.
            "explicit_host_media": any(m != GPU for m in media_seen),
        }
    return prep


def recorded_prefill_rate(metric_samples: Dict[str, List[metrics_mod.MetricSample]]
                          ) -> Optional[float]:
    """Per-request prefill rate from metrics: computed prompt tokens / summed prefill time.

    The engine's request prefill time histogram sums one prefill time per request, so
    the ratio is the rate of one request, not of the replica. Prompt tokens served
    from the engine's prefix cache are subtracted when the engine reports them.
    """
    if not metric_samples:
        return None
    totals = metric_totals({"metrics": {"per_node": {
        node: metrics_mod.aggregate_samples(samples) for node, samples in metric_samples.items()}}})
    prompt, seconds = totals.get("prompt_tokens"), totals.get("prefill_seconds")
    if not prompt or not seconds:
        return None
    computed = prompt - (totals.get("cached_prompt_tokens") or 0.0)
    return computed / seconds if computed > 0 else None


def _set_lookup_times(prep: Prepared, params: SimParams, run_first_ts: List[float],
                      run_last_ts: List[float], run_computed: List[int]) -> None:
    """Move each run's lookups to its scheduling time and sort the ops by time.

    A run's store events come after the engine computed its prefill, but LMCache
    looks up L2 when the request is scheduled. The store events of one run span its
    chunked prefill, so the prefill ends at the last one. The scheduling time is the
    last store time minus the run's computed tokens / per-request prefill rate, and
    never later than the first store time. New L2 writes (STORE, EVICT_WRITE) stay at
    their store-event time.
    """
    rate, source = params.prefill_tokens_per_sec_per_request, "input"
    if rate is None and params.lookup_time == "scheduled":
        rate, source = recorded_prefill_rate(prep.metric_samples), "metrics"
    info: Dict[str, Any] = {"requested": params.lookup_time, "model": params.lookup_time,
                            "prefill_tokens_per_sec_per_request": None, "rate_source": None}
    if params.lookup_time == "scheduled" and not rate:
        info["model"] = "store"
        msg = ("lookup time = store-event time: no per-request prefill rate (give "
               "--prefill-tokens-per-sec-per-request or record request_prefill_time metrics), "
               "so a lookup sees writes of requests that were still in prefill")
        prep.caveats.append(msg)
        log.warning("%s", msg)
    prep.lookup_time = info
    if info["model"] != "scheduled":
        return
    assert rate
    info["prefill_tokens_per_sec_per_request"] = rate
    info["rate_source"] = source
    # The lookup comes before the run's first store: a rate estimate that is too high
    # cannot put it after a store of its own run.
    lookup_ts = [min(first, last - tok / rate)
                 for first, last, tok in zip(run_first_ts, run_last_ts, run_computed)]
    shift = [first - t for first, t in zip(run_first_ts, lookup_ts)]
    info["mean_shift_seconds"] = sum(shift) / len(shift) if shift else 0.0
    # A run whose prefill estimate starts after its first store is clamped to that store.
    # Decode blocks that extend a run (nodes without decode detection) cause this.
    clamped = sum(1 for first, last, tok in zip(run_first_ts, run_last_ts, run_computed)
                  if last - tok / rate > first)
    info["clamped_run_fraction"] = clamped / len(run_first_ts) if run_first_ts else 0.0
    if info["clamped_run_fraction"] > CLAMPED_RUN_CAVEAT_FRACTION:
        prep.caveats.append(
            f"lookup time: {info['clamped_run_fraction']:.0%} of runs end later than their "
            "computed tokens explain, so their lookup is set to the first store event. "
            "Decode blocks that extend runs can cause this on nodes without decode detection; "
            "try --decode-blocks on")
    ops = []
    for op in prep.ops:
        run_id = op[4]
        if op[0] % WARM in RUN_LOOKUP_KINDS and run_id >= 0:
            op = (op[0], lookup_ts[run_id]) + op[2:]
        ops.append(op)
    prep.ops = ops
    # Stable sort: a run's ops share one time, so they keep their prefix order. A
    # lookup can now come before a store that has an earlier store-event time.
    prep.ops.sort(key=lambda op: op[1])


def _prefix_chain(last_chunk: int, chunk_parent: Dict[int, int]) -> List[int]:
    """Chunk keys of a cached prefix, from the root to ``last_chunk``."""
    chain: List[int] = []
    seen = set()
    while last_chunk >= 0 and last_chunk not in seen:
        seen.add(last_chunk)
        chain.append(last_chunk)
        last_chunk = chunk_parent.get(last_chunk, -1)
    chain.reverse()
    return chain


def _lookup_l0_prefix(ops: list, last_chunk: int, chunk_parent: Dict[int, int],
                      host_held: Optional[OrderedDict[int, None]], ts: float, node_id: int,
                      run_id: int) -> None:
    """Add an L2 lookup for each chunk of the prefix that the run's GPU still holds.

    LMCache looks up the whole prompt from chunk 0, also the part vLLM holds on GPU.
    A chunk that the node's host tier holds is served from there without L2.
    """
    for key_id in _prefix_chain(last_chunk, chunk_parent):
        if host_held is not None and key_id in host_held:
            continue
        ops.append((L0_LOOKUP, ts, key_id, node_id, run_id, 0, 0))


def _refresh_host_prefix(held: OrderedDict[int, None], last_chunk: int,
                         chunk_parent: Dict[int, int], ops: Optional[list], ts: float,
                         node_id: int, run_id: int) -> int:
    """Refresh the host-tier recency of a request's already cached prefix.

    LMCache looks up the whole prompt, including the part vLLM still holds on GPU,
    so the host copies of that prefix stay recent. The walk goes from the root and
    stops at the first chunk the host tier no longer holds (lookups are prefix-only).
    With ``ops``, each refreshed chunk also refreshes its L2 copy.
    """
    n = 0
    for key_id in _prefix_chain(last_chunk, chunk_parent):
        if key_id not in held:
            break
        held.move_to_end(key_id)
        n += 1
        if ops is not None:
            ops.append((HOST_TOUCH, ts, key_id, node_id, run_id, 0, 0))
    return n


def _reuse_bucket(seconds: float) -> str:
    for label, bound in REUSE_BUCKETS:
        if seconds < bound:
            return label
    return REUSE_BUCKETS[-1][0]


def tile_trace(prep: Prepared, params: SimParams) -> None:
    """Copy the L2 operations ``params.load_scale`` times (load tiles), in place.

    Tile ``t`` runs on its own engine nodes (``name#t``) and its own run ids. With
    ``tile_keys`` disjoint it also has its own chunk keys; with shared, tiles reuse each
    other's chunks. Tile 0 keeps the recorded times. With ``tile_offset`` random, every
    other tile is shifted by a seeded random time in [0, 60 s), without wrap-around, so
    the order inside a tile does not change and the window grows by the largest shift.
    """
    tiles = params.load_scale
    if tiles <= 1:
        return
    rng = random.Random(params.tile_seed)
    offsets = [0.0] + [rng.random() * TILE_OFFSET_SECONDS if params.tile_offset == "random" else 0.0
                       for _ in range(tiles - 1)]
    n_nodes, n_keys, n_runs = len(prep.nodes), len(prep.key_model), len(prep.run_store_ts)
    disjoint = params.tile_keys == "disjoint"
    ops: List[Tuple[int, float, int, int, int, int, int]] = []
    for t, off in enumerate(offsets):
        key_shift = t * n_keys if disjoint else 0
        node_shift, run_shift = t * n_nodes, t * n_runs
        ops.extend((kind, ts + off, key + key_shift, node + node_shift,
                    run + run_shift if run >= 0 else run, tok, end)
                   for kind, ts, key, node, run, tok, end in prep.ops)
    # Stable: equal times keep tile order, and each tile keeps its own order.
    ops.sort(key=lambda op: op[1])
    prep.ops = ops
    names = list(prep.nodes)
    prep.nodes = names + [f"{name}#{t}" for t in range(1, tiles) for name in names]
    if disjoint:
        prep.key_model = prep.key_model * tiles
    prep.run_store_ts = [ts + off for off in offsets for ts in prep.run_store_ts]
    summary = prep.summary
    window = summary["window"]
    span = max(offsets)
    busy = float(window["duration_seconds"] or 0.0)
    if window["end"] is not None:
        window["end"] += span
    window["duration_seconds"] = busy + span
    summary["nodes"] = sorted(prep.nodes)
    summary["ranks"] = sorted(summary["ranks"] + [
        f"{r.split(':')[0]}#{t}:{r.split(':', 1)[1]}" for t in range(1, tiles) for r in summary["ranks"]])
    summary["tokens"] = {k: v * tiles for k, v in summary["tokens"].items()}
    summary["l2_lookups"] *= tiles
    summary["lookup_runs"] *= tiles
    summary["load_scale"] = {
        "tiles": tiles, "tile_keys": params.tile_keys, "tile_offset": params.tile_offset,
        "seed": params.tile_seed, "offsets_seconds": offsets, "engine_nodes": len(prep.nodes),
        # Each tile is busy for the recorded window only: beta, Q*, P* and the P check use
        # tiles * recorded engine nodes * busy_seconds_per_tile as the wall time.
        "busy_seconds_per_tile": busy,
        "wall_replica_seconds": tiles * n_nodes * busy,
    }
    prep.caveats.append(
        f"load scale: {tiles} tiles of the trace ({params.tile_keys} keys, offset {params.tile_offset}); "
        "trace.tokens and lookup counts sum all tiles, but recorded metrics are of the recorded nodes only")
    if params.tile_offset == "none":
        prep.caveats.append("load scale with --tile-offset none: the write bursts of all tiles align, so "
                            "cache-node queue values are correlated bursts, an upper bound")
    if not disjoint:
        prep.caveats.append("load scale with --tile-keys shared: tiles reuse each other's chunks, an "
                            "optimistic case for hits")


def split_lookup_runs(ops: Sequence[Tuple[int, ...]]) -> int:
    """Runs whose lookup operations are not one block: an op of another run lies between two.

    A static fetch policy decides from the run's whole hit prefix at its first lookup. In
    ``scheduled`` mode the lookups of a run share one time and stay together, so this is
    0 unless two runs got the same lookup time.
    """
    last: Dict[int, Tuple[int, int]] = {}
    n_ops: Counter = Counter()
    split = set()
    for i, op in enumerate(ops):
        run = op[4]
        if run < 0:
            continue
        if op[0] % WARM in RUN_LOOKUP_KINDS:
            prev = last.get(run)
            if prev is not None and i - prev[0] - 1 != n_ops[run] - prev[1]:
                split.add(run)
            n_ops[run] += 1
            last[run] = (i, n_ops[run])
        else:
            n_ops[run] += 1
    return len(split)


def replay(prep: Prepared, capacity_bytes: Optional[int], params: SimParams,
           timeline: Optional[List[contention_mod.Event]] = None,
           policy: Optional[StaticPolicy] = None, admit_ops: Optional[set] = None,
           useful_out: Optional[set] = None, track: bool = False) -> Dict[str, Any]:
    """Run the L2 operation sequence against one cache of the given capacity.

    With ``timeline``, every hit, miss and new L2 write is appended to it, in
    trace order, for the bandwidth and write-cost model (:mod:`kvtrace.contention`).

    Phase 1 options (all off by default, and then the result is unchanged):

    - ``policy``: a static fetch policy. At a run's first lookup, the run's external hit
      prefix (host-tier and L2 hits) is found without a refresh, and the policy sets how
      many of those chunks are loaded. An unloaded L2 hit saves nothing, goes to the
      timeline as a MISS, and is refreshed only with ``lookup_touch`` on.
    - ``params.count_rewrites``, ``params.admission``: rewrites and write admission.
      ``oracle-useful`` admits only the writes (op indices) in ``admit_ops``; without
      ``admit_ops`` it admits every write. ``useful_out`` collects the op index of each
      write that got a fetched hit.
    - ``track``: add write metrics (dead writes, hits per written chunk) to the result.
    """
    chunk_bytes = params.chunk_tokens * params.kv_bytes_per_token if params.kv_bytes_per_token else None
    cap_chunks = None
    if capacity_bytes is not None:
        assert chunk_bytes
        # floor(capacity * usable / (chunk_bytes + overhead)), in integers where possible.
        usable = (capacity_bytes if params.l2_usable_fraction == 1.0
                  else math.floor(capacity_bytes * params.l2_usable_fraction))
        cap_chunks = usable // (chunk_bytes + params.l2_per_key_overhead_bytes)
    ttl = params.ttl_seconds if params.policy == "ttl" else None
    store_on_miss = params.write_policy == "store"
    count_rewrites = params.count_rewrites
    touch = params.lookup_touch == "on"
    adm_kind, adm_seconds, adm_absolute = parse_admission(params.admission)
    if adm_kind == "oracle-useful" and admit_ops is None:
        adm_kind = "all"
    cutoff = None
    epoch_source = None
    if adm_kind == "none-after":
        assert adm_seconds is not None
        if adm_absolute:
            epoch, epoch_source = 0.0, "absolute"
        elif prep.trace_epoch is not None:
            # synth_from_mooncake writes --epoch (trace time 0) as the header start. For a
            # recorder trace this is the recorder start, so give an absolute time instead.
            epoch, epoch_source = prep.trace_epoch, "trace header started"
        else:
            epoch, epoch_source = float(prep.summary["window"]["start"] or 0.0), "window start"
        cutoff = epoch + adm_seconds
    # --admission second-hit: keys that missed once and were not written, least recent first.
    ghost: OrderedDict[int, None] = OrderedDict()
    ghost_cap = params.admission_ghost_chunks or cap_chunks
    # Write metrics: written chunks by hit-count bucket, and dead ones by how they ended.
    write_hits: Counter = Counter()
    dead: Counter = Counter()
    dead_writes = 0
    # Static policy: lookup op indices of each run, loaded-chunk allowance per run, and
    # external hit chunks loaded so far.
    run_ops: Dict[int, List[int]] = {}
    allow: Dict[int, float] = {}
    ext_used: Counter = Counter()
    # max-load: host-tier chunks after the cap are recomputed and SET at the run's first
    # store event, which has no STORE op of its own: (ts, seq, key, node, run, op index).
    deferred: List[Tuple[float, int, int, int, int, int]] = []
    if policy is not None:
        for i, op in enumerate(prep.ops):
            if op[4] >= 0 and op[0] % WARM in RUN_LOOKUP_KINDS:
                run_ops.setdefault(op[4], []).append(i)

    # key_id -> [write_ts, writer_node_id, last_access_ts, fetched hits, counted write,
    # writes (with rewrites), op index of the write]; order = least recent first.
    cache: OrderedDict[int, List[Any]] = OrderedDict()
    # run_id -> True while every earlier chunk in the run hit (prefix semantics).
    run_state: OrderedDict[int, bool] = OrderedDict()
    run_hits: Dict[int, List[int]] = {}
    # (run_id, key_id) of missed lookups whose STORE op is still to come, and why the
    # chunk is written: "miss", "prefix_blocked" (L2 held it) or "max_load_cap".
    pending_stores: Dict[Tuple[int, int], str] = {}

    start = prep.summary["window"]["start"] or 0.0
    minutes = int(prep.summary["window"]["duration_seconds"] // 60) + 1
    series = [0] * minutes
    reuse = Counter()
    hit_tokens_hist: Counter = Counter()
    per_model: Dict[int, Counter] = {}
    r = counted = Counter()
    # Warm-up ops change the cache but count here, in no result.
    warm_r: Counter = Counter()
    warm_models: Dict[int, Counter] = {}
    timeline_out = timeline
    # Runs with a lookup outside the warm-up window.
    counted_runs: set = set()
    # run_id -> [tokens, chunks] of the hits outside the warm-up window.
    counted_run_hits: Dict[int, List[int]] = {}
    counting = False
    peak = 0

    def retire(entry: List[Any], how: str) -> None:
        nonlocal dead_writes
        if track and entry[4]:
            write_hits[_bucket_hits(entry[3])] += 1
            if not entry[3]:
                dead[how] += 1
                dead_writes += entry[5]

    def admitted(new: bool, key_id: int, ts: float, op_idx: int) -> bool:
        if adm_kind == "none-after":
            # No SET at all after the cutoff: no new chunk and no refresh by a rewrite.
            assert cutoff is not None
            return ts <= cutoff
        if not new:
            return True
        if adm_kind == "second-hit":
            if key_id in ghost:
                del ghost[key_id]
                return True
            ghost[key_id] = None
            while ghost_cap is not None and len(ghost) > ghost_cap:
                ghost.popitem(last=False)
            return False
        assert admit_ops is not None
        return op_idx in admit_ops

    def insert(key_id: int, ts: float, node_id: int, run_id: int, op_idx: int, cause: str) -> None:
        nonlocal peak
        entry = cache.get(key_id)
        if adm_kind != "all" and not admitted(entry is None, key_id, ts, op_idx):
            r["admission_dropped_writes"] += 1
            return
        if entry is None:
            cache[key_id] = [ts, node_id, ts, 0, r is counted, 1, op_idx]
            r["writes"] += 1
            if timeline is not None:
                timeline.append((contention_mod.WRITE, ts, node_id, run_id, 0, 0, key_id))
        else:
            # The chunk is already in L2 (a prefix-blocked lookup or a repeated
            # evict-write): refresh recency, but keep the original writer and
            # write time for the same/cross-node split and the reuse histogram.
            entry[2] = ts
            cache.move_to_end(key_id)
            if count_rewrites:
                # LMCache sends an unconditional SET without an existence check.
                r["writes"] += 1
                r["rewrites"] += 1
                r["rewrites_" + ("written_by_other_run" if cause == "miss" else cause)] += 1
                entry[5] += 1
                if timeline is not None:
                    timeline.append((contention_mod.WRITE, ts, node_id, run_id, 0, 0, key_id))
        while cap_chunks is not None and len(cache) > cap_chunks:
            retire(cache.popitem(last=False)[1], "evicted")
            r["evictions"] += 1
        if r is counted:
            # Not a warm-up op.
            peak = max(peak, len(cache))

    def external_hit_prefix(run_id: int) -> List[int]:
        """Tokens of the run's external hit chunks (host tier and L2), without a refresh."""
        tokens: List[int] = []
        for idx in run_ops.get(run_id, ()):
            kind, _ts, key_id, _node, _run, tok, _end = prep.ops[idx]
            kind %= WARM
            if kind == L0_LOOKUP:
                if key_id not in cache:
                    break
            elif kind == HOST_HIT:
                tokens.append(tok)
            elif kind == LOOKUP:
                if key_id not in cache:
                    break
                tokens.append(tok)
        return tokens

    for op_idx, (kind, ts, key_id, node_id, run_id, tok, end) in enumerate(prep.ops):
        warm = kind >= WARM
        if warm:
            kind -= WARM
        # insert() and the counters below use the current r and timeline.
        r = warm_r if warm else counted
        timeline = None if warm else timeline_out
        while deferred and deferred[0][0] <= ts:
            d_ts, _seq, d_key, d_node, d_run, d_idx = heapq.heappop(deferred)
            insert(d_key, d_ts, d_node, d_run, d_idx, "max_load_cap")
        if warm_r and not warm and not counting:
            # First counted op: the warm-up state is the starting point of peak use.
            counting = True
            peak = max(peak, len(cache))
        if ttl is not None:
            # The front of the LRU order is also the oldest access, so expiry can
            # stop at the first entry that is still fresh.
            while cache:
                oldest_key, oldest = next(iter(cache.items()))
                if ts - oldest[2] <= ttl:
                    break
                del cache[oldest_key]
                retire(oldest, "evicted")
                r["expirations"] += 1

        if kind == EVICT_WRITE:
            insert(key_id, ts, node_id, -1, op_idx, "evict_write")
            continue
        if kind == STORE:
            # The chunk this run computed becomes visible in L2 at its store event.
            cause = pending_stores.pop((run_id, key_id), None)
            if cause is not None:
                insert(key_id, ts, node_id, run_id, op_idx, cause)
            continue
        if policy is not None and run_id >= 0 and run_id not in allow:
            # First lookup op of the run: the policy decides from the whole hit prefix.
            allow[run_id] = policy.allowed(external_hit_prefix(run_id))
        if kind == L0_LOOKUP:
            # A prefix chunk that L0 serves: L2 is checked, but a hit saves nothing.
            r["l0_prefix_lookups"] += 1
            if run_state.get(run_id, True):
                entry = cache.get(key_id)
                if entry is not None:
                    entry[2] = ts
                    cache.move_to_end(key_id)
                    r["l0_prefix_hits"] += 1
                else:
                    # LMCache stops at the first L2 miss: later chunks are recomputed.
                    r["l0_prefix_misses"] += 1
                    run_state[run_id] = False
            continue
        if kind == HOST_TOUCH:
            entry = cache.get(key_id)
            if entry is not None:
                entry[2] = ts
                cache.move_to_end(key_id)
                r["host_touch_refreshed"] += 1
            continue
        if kind == HOST_HIT:
            if run_state.get(run_id, True) and policy is not None and ext_used[run_id] >= allow[run_id]:
                # The policy loads no more external chunks of this run: recomputed.
                r["policy_skipped_host_tokens"] += tok
                if timeline is not None:
                    timeline.append((contention_mod.MISS, ts, node_id, run_id, tok, end, key_id))
                if store_on_miss and policy.kind == "max-load":
                    heapq.heappush(deferred, (max(ts, prep.run_store_ts[run_id]), op_idx, key_id,
                                              node_id, run_id, op_idx))
                continue
            if run_state.get(run_id, True):
                if policy is not None:
                    ext_used[run_id] += 1
                r["host_hit_tokens"] += tok
                if run_hits.get(run_id, (0, 0))[1] > 0:
                    # An earlier chunk of this run came from L2: without that hit the
                    # prefix would be broken here, so L2 also saves these tokens.
                    r["host_l2_enabled_tokens"] += tok
                    if timeline is not None:
                        timeline.append((contention_mod.HOST_ENABLED, ts, node_id, run_id, tok, end, key_id))
                entry = cache.get(key_id)
                if params.host_tier_touch_l2 and entry is not None:
                    entry[2] = ts
                    cache.move_to_end(key_id)
                    r["host_touch_refreshed"] += 1
            else:
                # An earlier chunk of the run missed: the prefix is broken, so the
                # host copy cannot be used and these tokens are recomputed.
                r["host_prefix_blocked_tokens"] += tok
                if timeline is not None:
                    timeline.append((contention_mod.MISS, ts, node_id, run_id, tok, end, key_id))
            continue

        r["lookups"] += 1
        r["lookup_tokens"] += tok
        model_stats = (warm_models if warm else per_model).setdefault(prep.key_model[key_id], Counter())
        if not warm:
            counted_runs.add(run_id)
        model_stats["lookups"] += 1
        model_stats["lookup_tokens"] += tok
        still_hitting = run_state.pop(run_id, True)
        entry = cache.get(key_id)
        unloaded = False
        if still_hitting and entry is not None and policy is not None:
            if ext_used[run_id] < allow[run_id]:
                ext_used[run_id] += 1
            else:
                unloaded = True
        if unloaded:
            # An L2 hit that the policy does not fetch: computed, so it saves nothing.
            assert policy is not None and entry is not None
            r["policy_unfetched_hits"] += 1
            r["policy_unfetched_tokens"] += tok
            if touch:
                # The L2 cache: the hit check itself refreshes the key.
                entry[2] = ts
                cache.move_to_end(key_id)
            run_hits.setdefault(run_id, [0, 0])
            if timeline is not None:
                timeline.append((contention_mod.MISS, ts, node_id, run_id, tok, end, key_id))
            if store_on_miss and policy.kind == "max-load":
                # LMCache skips only the capped count, so it writes the chunks after the cap again.
                pending_stores[(run_id, key_id)] = "max_load_cap"
        elif still_hitting and entry is not None:
            r["hits"] += 1
            r["saved_tokens"] += tok
            model_stats["hits"] += 1
            model_stats["saved_tokens"] += tok
            entry[3] += 1
            if useful_out is not None:
                useful_out.add(entry[6])
            side = "same_node" if int(entry[1]) == node_id else "cross_node"
            r[f"{side}_hits"] += 1
            r[f"{side}_hit_tokens"] += tok
            if not warm:
                reuse[_reuse_bucket(ts - entry[0])] += 1
                hit_tokens_hist[tok] += 1
                idx = int((ts - start) // 60)
                if 0 <= idx < minutes:
                    series[idx] += tok
            stats = run_hits.setdefault(run_id, [0, 0])
            stats[0] += tok
            stats[1] += 1
            if not warm:
                stats = counted_run_hits.setdefault(run_id, [0, 0])
                stats[0] += tok
                stats[1] += 1
            entry[2] = ts
            cache.move_to_end(key_id)
            if timeline is not None:
                timeline.append((contention_mod.HIT, ts, node_id, run_id, tok, end, key_id))
        else:
            # LMCache retrieves only the longest cached prefix: after the first miss
            # in a run, later chunks are recomputed even if L2 holds them.
            if entry is not None:
                r["prefix_blocked"] += 1
            still_hitting = False
            run_hits.setdefault(run_id, [0, 0])
            if timeline is not None:
                timeline.append((contention_mod.MISS, ts, node_id, run_id, tok, end, key_id))
            if store_on_miss:
                pending_stores[(run_id, key_id)] = "miss" if entry is None else "prefix_blocked"
        run_state[run_id] = still_hitting
        if len(run_state) > RUN_STATE_CAP:
            run_state.popitem(last=False)

    r = counted
    timeline = timeline_out
    while deferred:
        d_ts, _seq, d_key, d_node, d_run, d_idx = heapq.heappop(deferred)
        insert(d_key, d_ts, d_node, d_run, d_idx, "max_load_cap")
    lookups = r["lookups"]
    if warm_r:
        # Only the runs with a lookup in the counted window, with their counted hits:
        # warm-up hits of a run that crosses --start are not counted.
        run_hits = {rid: counted_run_hits.get(rid, [0, 0]) for rid in run_hits
                    if rid in counted_runs}
    runs = Counter((tok, n) for tok, n in run_hits.values())
    host_stats: Dict[str, Any] = {}
    if params.l2_lookup == "full-prompt":
        host_stats["l0_prefix"] = {"lookups": r["l0_prefix_lookups"], "hits": r["l0_prefix_hits"],
                                   "misses": r["l0_prefix_misses"]}
    if params.host_tier_bytes_per_node is not None:
        host_stats["host_tier"] = {
            "hit_tokens": r["host_hit_tokens"],
            "prefix_blocked_tokens": r["host_prefix_blocked_tokens"],
            "l2_enabled_tokens": r["host_l2_enabled_tokens"],
            "l2_refreshed": r["host_touch_refreshed"],
        }
    extra: Dict[str, Any] = {}
    if track:
        for entry in cache.values():
            retire(entry, "at_end")
        read_chunks = r["hits"]
        extra["fetched_chunks"] = read_chunks
        extra["fetching_runs"] = sum(1 for _tok, n in run_hits.values() if n > 0)
        extra["write_metrics"] = {
            "written_chunks": sum(write_hits.values()),
            "dead_writes": {"evicted": dead["evicted"], "at_end": dead["at_end"],
                            "total": dead["evicted"] + dead["at_end"]},
            "dead_write_gb_share": dead_writes / r["writes"] if r["writes"] else 0.0,
            "hits_per_written_chunk": {label: write_hits[label] for label, _low in WRITE_HIT_BUCKETS},
            "write_gb": r["writes"] * chunk_bytes / 1e9 if chunk_bytes else None,
            "read_gb": read_chunks * chunk_bytes / 1e9 if chunk_bytes else None,
            "write_read_ratio": r["writes"] / read_chunks if read_chunks else None,
        }
        if count_rewrites:
            extra["rewrites"] = r["rewrites"]
            extra["rewrites_by_cause"] = {cause: r["rewrites_" + cause] for cause in (
                "prefix_blocked", "written_by_other_run", "evict_write", "max_load_cap")}
        if params.admission != "all":
            adm: Dict[str, Any] = {"policy": params.admission,
                                   "dropped_writes": r["admission_dropped_writes"]}
            if cutoff is not None:
                epoch = None if adm_absolute else cutoff - (adm_seconds or 0.0)
                adm.update(cutoff_ts=cutoff, epoch=epoch, epoch_source=epoch_source)
            if adm_kind == "second-hit":
                adm["ghost_chunks"] = ghost_cap
            extra["admission"] = adm
        if policy is not None:
            extra["fetch_policy"] = {
                "id": policy.pid, "min_tokens": policy.min_tokens, "max_chunks": policy.max_chunks,
                "unfetched_hits": r["policy_unfetched_hits"],
                "unfetched_hit_tokens": r["policy_unfetched_tokens"],
                "skipped_host_tokens": r["policy_skipped_host_tokens"],
            }
    return {
        "lookups": lookups,
        "lookup_tokens": r["lookup_tokens"],
        "hits": r["hits"],
        "saved_tokens": r["saved_tokens"],
        "hit_rate": r["hits"] / lookups if lookups else 0.0,
        "token_hit_rate": r["saved_tokens"] / r["lookup_tokens"] if r["lookup_tokens"] else 0.0,
        "prefix_blocked": r["prefix_blocked"],
        "same_node_hits": r["same_node_hits"],
        "cross_node_hits": r["cross_node_hits"],
        "same_node_hit_tokens": r["same_node_hit_tokens"],
        "cross_node_hit_tokens": r["cross_node_hit_tokens"],
        "writes": r["writes"],
        "evictions": r["evictions"],
        "expirations": r["expirations"],
        "capacity_chunks": cap_chunks,
        "peak_entries": peak,
        "peak_used_bytes": peak * chunk_bytes if chunk_bytes else None,
        "reuse_histogram": {label: reuse.get(label, 0) for label, _ in REUSE_BUCKETS},
        "hit_tokens_histogram": sorted([tok, n] for tok, n in hit_tokens_hist.items()),
        "runs_histogram": sorted([tok, n, c] for (tok, n), c in runs.items()),
        "lookup_runs": len(run_hits),
        "per_model": {
            prep.models[m]: {
                "lookups": s["lookups"], "hits": s["hits"],
                "lookup_tokens": s["lookup_tokens"], "saved_tokens": s["saved_tokens"],
                "token_hit_rate": s["saved_tokens"] / s["lookup_tokens"] if s["lookup_tokens"] else 0.0,
            }
            for m, s in per_model.items()
        },
        "hit_tokens_per_minute": series,
        **host_stats,
        **extra,
    }


def time_weighted_gauges(samples: Sequence[metrics_mod.MetricSample]) -> Dict[str, float]:
    """Time-weighted mean of each gauge series of one node.

    The mean is the area under the straight lines between a series' samples (trapezoid
    rule), divided by the time from its first to its last sample. Thus a sample after a
    long scrape gap weighs more than a sample in a dense run of scrapes. A series with
    one sample, or with all samples at one time, gets the plain mean of its samples.
    Counters and ``*_created`` series are not gauges and are left out, as in
    :func:`kvtrace.metrics.aggregate_samples`.
    """
    series: Dict[str, List[Tuple[float, float]]] = {}
    for sample in sorted(samples, key=lambda s: s[0]):
        ts, metrics = sample[0], sample[1]
        for key, value in metrics.items():
            if metrics_mod.is_counter(key) or metrics_mod.is_created(key):
                continue
            series.setdefault(key, []).append((ts, value))
    out: Dict[str, float] = {}
    for key, points in series.items():
        span = points[-1][0] - points[0][0]
        if span <= 0:
            out[key] = sum(v for _t, v in points) / len(points)
            continue
        area = sum((t1 - t0) * (v0 + v1) / 2 for (t0, v0), (t1, v1) in zip(points, points[1:]))
        out[key] = area / span
    return out


def aggregate_metrics(prep: Prepared) -> Dict[str, Any]:
    per_node = {node: metrics_mod.aggregate_samples(samples)
                for node, samples in sorted(prep.metric_samples.items())}
    for node, agg in per_node.items():
        # The sample mean ("gauges") is biased toward dense scrapes when the scrape
        # interval is uneven (slow or missed scrapes); this mean is not.
        agg["gauges_time_weighted"] = time_weighted_gauges(prep.metric_samples[node])
    fleet_counters: Counter = Counter()
    gauge_sum: Counter = Counter()
    gauge_n: Counter = Counter()
    for agg in per_node.values():
        fleet_counters.update(agg["counters"])
        gauge_sum.update(agg["gauges"])
        gauge_n.update(dict.fromkeys(agg["gauges"], 1))
    # Counters add up over the fleet; gauges (rates, queue depths) are averaged per node.
    return {"per_node": per_node,
            "fleet": {"counters": dict(fleet_counters),
                      "gauges_mean_per_node": {k: gauge_sum[k] / gauge_n[k] for k in gauge_sum}}}


def simulate(records: Iterable[Dict[str, Any]], params: SimParams,
             sources: Optional[List[str]] = None,
             contention: Optional[contention_mod.ContentionParams] = None,
             keep_run_deltas: bool = False,
             sweep: Optional[Dict[str, List[float]]] = None,
             off_ttft: Optional[List[Tuple[str, float, float]]] = None) -> Dict[str, Any]:
    """Simulate every capacity. ``contention`` adds the bandwidth and write-cost
    model per capacity; without it the output is the same as before the model existed.

    Phase 1 inputs (each adds sim.json keys only when given): the fetch policies,
    admission, rewrites, lookup touch, load tiles and cache node counts of ``params``;
    ``sweep`` (``{"write_stall_ms_per_gb": [...], "l2_rtt_ms": [...]}``, needs
    ``contention``) for the P1 break-even; ``off_ttft`` (``(node, ts, ttft_s)`` per OFF
    request, from the synth index) for the change of p90 TTFT.
    """
    static = params.static_policies()
    model_policies = [p for p in params.fetch_policies if p not in static]
    if contention is not None:
        contention.validate()
        if not params.kv_bytes_per_token:
            raise SimulationError("--contention on needs the KV size (--kv-bytes-per-token, "
                                  "--model-config or --preset)")
    else:
        needs = [p for p in params.fetch_policies
                 if p == "min-tokens:auto" or p.startswith("lookup-timeout:")]
        if needs or sweep or off_ttft is not None:
            raise SimulationError(f"{', '.join(needs) or '--contention-sweep / --off-ttft'} "
                                  "needs --contention on")
    if sweep is not None:
        unknown = set(sweep) - {"write_stall_ms_per_gb", "l2_rtt_ms"}
        if unknown or not sweep.get("write_stall_ms_per_gb"):
            raise SimulationError("--contention-sweep needs write_stall_ms_per_gb=LIST (optional "
                                  "l2_rtt_ms=LIST)")
    if off_ttft is not None and params.load_scale > 1:
        raise SimulationError("--off-ttft joins the recorded nodes only; it cannot be used with "
                              "--load-scale")
    other_policy = any(p not in contention_mod.POLICIES for p in params.fetch_policies)
    if not params.count_rewrites and (other_policy or params.admission != "all"):
        # Fetch-all and the other policies then get the same write scoring.
        params = replace(params, count_rewrites=True, count_rewrites_source="auto")
    elif params.count_rewrites and params.count_rewrites_source is None:
        params = replace(params, count_rewrites_source="input")
    extended = (params.phase1() or sweep is not None or off_ttft is not None
                or (contention is not None and contention.gpu_rtt_ms is not None))
    prep = prepare(records, params)
    tile_trace(prep, params)
    caveats = list(prep.caveats)
    if params.l2_per_key_overhead_bytes == 0 and params.l2_usable_fraction == 1.0:
        caveats.append("L2 is one global LRU of whole chunks with no allocator, segment or "
                       "drain-buffer overhead: every byte of capacity holds chunk data. Set "
                       "--l2-per-key-overhead-bytes and --l2-usable-fraction to model them")
    policy_check: Dict[str, Any] = {}
    if static:
        if prep.lookup_time["model"] != "scheduled":
            if params.lookup_time == "scheduled":
                raise SimulationError(STORE_FALLBACK_REFUSAL)
            raise SimulationError(f"{', '.join(static)} need --lookup-time scheduled")
        policy_check["split_lookup_runs"] = split_lookup_runs(prep.ops)
        if policy_check["split_lookup_runs"]:
            caveats.append(f"static fetch policies: {policy_check['split_lookup_runs']} runs have an op of "
                           "another run between their lookups, so the policy decided from an "
                           "approximate hit prefix")
    lookup_timeouts = [p for p in model_policies if p.startswith("lookup-timeout:")]
    if params.lookup_touch == "off":
        if contention is not None:
            # ideal is always evaluated, also when the list does not name it.
            after_replay = ["ideal"] + lookup_timeouts
            caveats.append(f"--lookup-touch off: {', '.join(after_replay)} pick their hits after replay, "
                           "so their rows are an approximate upper bound, recency not modeled")
        if params.l2_lookup == "full-prompt":
            caveats.append("--lookup-touch off with --l2-lookup full-prompt: L0 prefix lookups still "
                           "refresh L2 (a no-touch L0 lookup is not modeled)")
    adm_kind, _adm_seconds, _adm_absolute = parse_admission(params.admission)
    if adm_kind == "oracle-useful":
        caveats.append("--admission oracle-useful: fewer writes change eviction, so the result is an "
                       "approximate upper bound")
    if params.count_rewrites_source == "auto":
        caveats.append("count-rewrites: on (auto), because a fetch policy or admission policy other than "
                       "the defaults was given; fetch-all rows count rewrites too")
    if lookup_timeouts and contention is not None and not contention.l2_lookup_ms:
        caveats.append(f"{', '.join(lookup_timeouts)}: not computed, because --l2-lookup-ms is 0 "
                       "(LMCache applies lookup_timeout_ms only to an async lookup check)")
        lookup_timeouts = []
    eval_policies = tuple(contention_mod.POLICIES) + tuple(lookup_timeouts)
    gpu_rtt_ms = None
    if contention is not None and (sweep is not None or contention.gpu_rtt_ms is not None):
        if contention.gpu_rtt_ms is None:
            contention = replace(contention, gpu_rtt_ms=contention_mod.DEFAULT_GPU_RTT_MS,
                                 gpu_rtt_ms_source="default")
        gpu_rtt_ms = contention.gpu_rtt_ms
    chunk_bytes = params.chunk_tokens * params.kv_bytes_per_token if params.kv_bytes_per_token else 0
    duration = float(prep.summary["window"]["duration_seconds"] or 0.0)
    need_runs = keep_run_deltas or off_ttft is not None
    counts = params.cache_node_counts or [None] * len(params.capacities)

    def run_replay(cap: Optional[int], policy: Optional[StaticPolicy]
                   ) -> Tuple[Dict[str, Any], Optional[List[contention_mod.Event]]]:
        admit = None
        if adm_kind == "oracle-useful":
            # First pass: every write, to mark the writes that get a hit before eviction.
            admit = set()
            replay(prep, cap, params, None, policy=policy, useful_out=admit)
        timeline: Optional[List[contention_mod.Event]] = [] if contention is not None else None
        res = replay(prep, cap, params, timeline, policy=policy, admit_ops=admit, track=extended)
        return res, timeline

    results = []
    for (label, cap), n_cache in zip(params.capacities, counts):
        res, timeline = run_replay(cap, None)
        res["label"] = label
        res["capacity_bytes"] = cap
        if n_cache is not None:
            res["cache_nodes"] = n_cache
        cp = contention
        if cp is not None and n_cache is not None:
            cp = replace(cp, cache_nodes=n_cache, cache_node_gbps=params.cache_node_gbps)
        rows: Dict[str, List[Dict[str, Any]]] = {}
        con: Optional[Dict[str, Any]] = None
        if cp is not None and timeline is not None:
            con = res["contention"] = contention_mod.evaluate(
                timeline, chunk_bytes, prep.nodes, duration, cp,
                keep_runs=need_runs, policies=eval_policies if extended else contention_mod.POLICIES,
                extended=extended, store_ts=prep.run_store_ts if off_ttft is not None else None)
            if need_runs:
                rows[""] = con["run_deltas"]
            if extended:
                for pid in eval_policies:
                    con["policies"][pid]["recency"] = (
                        "approximate upper bound, recency not modeled"
                        if params.lookup_touch == "off" and pid != "lmcache" else "exact")
        if static:
            res["fetch_policies"] = {}
        for pid in static:
            auto = None
            if pid == "min-tokens:auto":
                assert cp is not None and con is not None
                auto = contention_mod.auto_min_tokens(cp, params.chunk_tokens, chunk_bytes, con)
            sp = static_policy(pid, params.chunk_tokens, min_tokens=auto["min_tokens"] if auto else None)
            sp_res, sp_timeline = run_replay(cap, sp)
            summary = {k: v for k, v in sp_res.items() if k not in (
                "reuse_histogram", "hit_tokens_histogram", "runs_histogram", "hit_tokens_per_minute",
                "per_model", "capacity_chunks", "peak_entries", "peak_used_bytes")}
            if auto is not None:
                summary["min_tokens_auto"] = auto
            res["fetch_policies"][pid] = summary
            if cp is not None and con is not None and sp_timeline is not None:
                assert timeline is not None
                sp_con = contention_mod.evaluate(
                    sp_timeline, chunk_bytes, prep.nodes, duration, cp, keep_runs=need_runs,
                    policies=("lmcache",), extended=True,
                    store_ts=prep.run_store_ts if off_ttft is not None else None,
                    hit_runs={ev[3] for ev in timeline if ev[0] == contention_mod.HIT},
                    prefill_rate_depth0=con["prefill_rate_depth0"])
                pol = con["policies"][pid] = sp_con["policies"]["lmcache"]
                pol["recency"] = "exact"
                if need_runs:
                    rows[pid] = sp_con["run_deltas"]
        if con is not None and off_ttft is not None:
            join = contention_mod.join_off_ttft(rows[""], off_ttft)
            con["off_ttft_join"] = join.stats
            for pid, pol in con["policies"].items():
                key = "lmcache" if pid in static else pid
                delta = {row["run"]: row[key] for row in rows[pid if pid in static else ""]}
                pol["ttft_change"] = contention_mod.ttft_change(join.off, join.on(delta))
        else:
            join = None
        if con is not None and sweep is not None and timeline is not None:
            assert cp is not None and gpu_rtt_ms is not None
            con["sweep"] = contention_mod.stall_sweep(
                timeline, chunk_bytes, prep.nodes, duration, cp, sweep["write_stall_ms_per_gb"],
                gpu_rtt_ms, join=join, rtt_grid=sweep.get("l2_rtt_ms"), policies=eval_policies)
        if con is not None:
            if keep_run_deltas:
                by_run = {row["run"]: row for row in rows[""]}
                for pid in static:
                    for row in rows.get(pid, ()):
                        base = by_run.get(row["run"])
                        if base is not None:
                            base[pid] = row["lmcache"]
                            base[f"{pid}_fetched_chunks"] = row["lmcache_fetched_chunks"]
            elif off_ttft is not None:
                del con["run_deltas"]
        results.append(res)
    param_dict = asdict(params)
    param_dict["capacities"] = [{"label": label, "bytes": cap} for label, cap in params.capacities]
    if params.host_tier_bytes_per_node is None:
        # Keep sim.json byte-identical to earlier versions when the host tier is off.
        del param_dict["host_tier_bytes_per_node"], param_dict["host_tier_touch_l2"]
    for key, default in PHASE1_DEFAULTS.items():
        keep_node_shape = key.startswith("cache_node_") and params.cache_node_counts is not None
        if param_dict[key] == default and not keep_node_shape:
            del param_dict[key]
    inputs: Dict[str, Any] = {"sources": sources or [], "params": param_dict}
    if contention is not None:
        inputs["contention"] = contention.as_dict()
    if params.cache_node_counts is not None:
        inputs["cache_node"] = {
            "type": params.cache_node_type, "usable_dram_gb": params.cache_node_gb,
            "nvme_gb": params.cache_node_nvme_gb, "gbps": params.cache_node_gbps,
            "counts": list(params.cache_node_counts),
            # Phase 1 models DRAM capacity only; the NVMe size is a label.
            "capacity_model": "dram-only"}
    if sweep is not None:
        inputs["contention_sweep"] = {k: list(v) for k, v in sweep.items()}
    if off_ttft is not None:
        inputs["off_ttft"] = {"format": "synth-index", "requests": len(off_ttft)}
    out = {
        "tool": TOOL_NAME,
        "format": "kvtrace-sim/1",
        "inputs": inputs,
        "trace": prep.summary,
        "lookup_time": prep.lookup_time,
        "caveats": caveats,
        "capacities": results,
        "metrics": aggregate_metrics(prep),
    }
    if policy_check:
        out["fetch_policy_check"] = policy_check
    return out
