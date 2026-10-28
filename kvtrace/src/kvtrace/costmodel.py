"""Turn simulated L2 hits into GPU time, TTFT, dollar and throughput estimates.

All formulas are documented in README.md ("Report math"). Units: seconds,
tokens, bytes, Gbit/s, USD.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .contention import DEFAULT_GPU_RTT_MS
from .metrics import split_key

SECONDS_PER_MONTH = 30 * 86400

# Saved-TTFT-per-run histogram buckets (upper bounds, seconds). A negative saving
# (the fetch takes longer than recomputing) goes to the "slower with L2" bucket.
SLOWER_BUCKET = "slower with L2"
TTFT_BUCKETS: Tuple[Tuple[str, float], ...] = (
    (SLOWER_BUCKET, 0.0), ("<10ms", 0.01), ("<50ms", 0.05), ("<100ms", 0.1), ("<250ms", 0.25),
    ("<500ms", 0.5), ("<1s", 1.0), ("<2s", 2.0), (">=2s", math.inf),
)


@dataclass
class CostInputs:
    # Prefill throughput of one replica (all its concurrent requests): GPU time and dollars.
    prefill_tokens_per_sec: Optional[float] = None
    prefill_tps_source: str = "missing"
    replica_cost_per_hour: Optional[float] = None
    l2_bandwidth_gbps: float = 100.0
    l2_rtt_ms: float = 0.5
    l2_cost_per_gb_hour: Optional[float] = None
    prefill_time_fraction: Optional[float] = None
    prefill_fraction_source: str = "missing"
    # Prefill throughput one request sees: TTFT and the fetch-or-recompute test. When
    # None, the replica rate is used (the only rate known).
    request_prefill_tokens_per_sec: Optional[float] = None
    request_tps_source: str = "missing"
    replica_cost_source: str = "missing"
    # Specific reasons for omitted figures, and input warnings, shown in the report.
    missing_notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def request_tps(inputs: CostInputs) -> Optional[float]:
    """Per-request prefill rate for TTFT math; falls back to the replica rate."""
    return inputs.request_prefill_tokens_per_sec or inputs.prefill_tokens_per_sec


def transfer_seconds(chunk_tokens: int, kv_bytes_per_token: int, bandwidth_gbps: float) -> float:
    """Transfer time of one chunk's KV bytes, without the round trip."""
    return chunk_tokens * kv_bytes_per_token * 8 / (bandwidth_gbps * 1e9)


def fetch_seconds(chunk_tokens: int, kv_bytes_per_token: int, bandwidth_gbps: float,
                  rtt_ms: float) -> float:
    """One chunk fetch: round trip plus transfer of the chunk's KV bytes."""
    return rtt_ms / 1000.0 + transfer_seconds(chunk_tokens, kv_bytes_per_token, bandwidth_gbps)


def run_fetch_seconds(chunks: int, chunk_tokens: int, kv_bytes_per_token: int,
                      bandwidth_gbps: float, rtt_ms: float) -> float:
    """One lookup run's fetch: one round trip, then all of its hit chunks in one transfer.

    This matches the contention model, which charges the round trip once per run.
    """
    return rtt_ms / 1000.0 + chunks * transfer_seconds(chunk_tokens, kv_bytes_per_token,
                                                       bandwidth_gbps)


def recompute_seconds(tokens: int, prefill_tokens_per_sec: float) -> float:
    return tokens / prefill_tokens_per_sec


def break_even_bandwidth_gbps(chunk_tokens: int, kv_bytes_per_token: int, tokens: int,
                              prefill_tokens_per_sec: float, rtt_ms: float) -> float:
    """Smallest L2 bandwidth at which fetching a chunk beats recomputing ``tokens``."""
    budget = recompute_seconds(tokens, prefill_tokens_per_sec) - rtt_ms / 1000.0
    if budget <= 0:
        return math.inf
    return chunk_tokens * kv_bytes_per_token * 8 / budget / 1e9


def throughput_gain_upper_bound(prefill_fraction: float, saved_fraction: float) -> float:
    """Amdahl-style bound: removing share ``s`` of prefill, which is share ``f`` of busy time."""
    removed = min(max(prefill_fraction, 0.0), 1.0) * min(max(saved_fraction, 0.0), 1.0)
    if removed >= 1.0:
        return math.inf
    return 1.0 / (1.0 - removed) - 1.0


def _sum_counters(counters: Dict[str, float], needle: str) -> Optional[float]:
    # Engine counters only: LMCache exports look-alike names (lmcache:num_prompt_tokens_total).
    values = [v for k, v in counters.items()
              if needle in k.split("{", 1)[0] and k.startswith(("vllm:", "sglang:"))]
    return sum(values) if values else None


# Prompt tokens served from the engine's own prefix cache, first group present wins:
# vLLM >= 0.10 (prompt_tokens_cached), older vLLM (prefix_cache_hits), SGLang. The
# names in one group are aliases of the same counter (vLLM exports the deprecated
# gpu_ name next to the new one), so only the first name present is used.
_CACHED_PROMPT_COUNTERS = (
    ("vllm:prompt_tokens_cached_total",),
    ("vllm:prefix_cache_hits_total", "vllm:gpu_prefix_cache_hits_total", "vllm:gpu_prefix_cache_hits"),
    ("sglang:cached_tokens_total",),
)


def _first_named(counters: Dict[str, float], names: Tuple[str, ...]) -> Optional[float]:
    # Sum the label series of the first alias present; never add up aliases.
    for name in names:
        values = [v for k, v in counters.items() if k.split("{", 1)[0] == name]
        if values:
            return sum(values)
    return None


def _node_totals(counters: Dict[str, float]) -> Dict[str, Optional[float]]:
    cached = None
    for names in _CACHED_PROMPT_COUNTERS:
        cached = _first_named(counters, names)
        if cached is not None:
            break
    return {
        "prompt_tokens": _sum_counters(counters, "prompt_tokens_total"),
        "cached_prompt_tokens": cached,
        "prefill_seconds": _sum_counters(counters, "prefill_time_seconds_sum"),
        "decode_seconds": _sum_counters(counters, "decode_time_seconds_sum"),
    }


def metric_totals(sim: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Fleet totals of the metrics the cost model uses, summed over nodes.

    Each node's cached-prompt counter is picked from that node's own metrics, so a
    mixed vLLM and SGLang fleet subtracts every engine's prefix-cache hits.
    """
    metrics = sim.get("metrics", {})
    per_node = metrics.get("per_node")
    if per_node:
        node_counters = [agg.get("counters", {}) for agg in per_node.values()]
    else:
        # Older sim.json without a per-node breakdown: treat the fleet as one node.
        node_counters = [metrics.get("fleet", {}).get("counters", {})]
    totals: Dict[str, Optional[float]] = {}
    for counters in node_counters:
        for key, value in _node_totals(counters).items():
            if value is not None:
                totals[key] = (totals.get(key) or 0.0) + value
            else:
                totals.setdefault(key, None)
    return totals


def estimate_prefill_tps(sim: Dict[str, Any]) -> Optional[float]:
    """Computed prompt tokens / prefill time, summed over nodes (an estimate).

    Prompt tokens served from the engine's prefix cache cost no prefill compute,
    so they are subtracted when the engine reports them. The prefill time is summed
    per request, so this is the rate one request sees, not the replica's rate.
    """
    totals = metric_totals(sim)
    prompt, seconds = totals["prompt_tokens"], totals["prefill_seconds"]
    if not prompt or not seconds:
        return None
    computed = prompt - (totals["cached_prompt_tokens"] or 0.0)
    return computed / seconds if computed > 0 else None


# Running-request gauges, first name present wins (one series per engine = one replica).
_RUNNING_GAUGES = ("vllm:num_requests_running", "sglang:num_running_reqs")


def _node_gauges(sim: Dict[str, Any]) -> List[Dict[str, float]]:
    metrics = sim.get("metrics", {})
    per_node = metrics.get("per_node")
    if per_node:
        # The time-weighted mean first; a series without one (an older sim.json) keeps
        # the mean of its scrapes.
        return [{**agg.get("gauges", {}), **agg.get("gauges_time_weighted", {})}
                for agg in per_node.values()]
    fleet = metrics.get("fleet", {})
    return [fleet.get("gauges_mean_per_node") or fleet.get("gauges") or {}]


def mean_running_requests(sim: Dict[str, Any]) -> Optional[float]:
    """Mean number of running requests per replica, from the running-request gauge.

    Each node's gauge series (one per engine label) is its time-weighted mean over the
    window (``gauges_time_weighted``). An older sim.json without it gives the mean of
    the scrapes, which is the time-weighted mean only at a fixed scrape interval.
    The result is the mean over all replicas; None when no node reports the gauge.
    """
    values: List[float] = []
    for gauges in _node_gauges(sim):
        for name in _RUNNING_GAUGES:
            found = [v for k, v in gauges.items() if k.split("{", 1)[0] == name]
            if found:
                values.extend(found)
                break
    if not values:
        return None
    mean = sum(values) / len(values)
    return mean if mean > 0 else None


def resolve_prefill_rates(inputs: CostInputs, sim: Dict[str, Any],
                          replica_tps: Optional[float]) -> None:
    """Fill the per-request and per-replica prefill rates of ``inputs``.

    The per-request rate (TTFT) comes from metrics. The replica rate (GPU time,
    dollars) is ``replica_tps`` when given, else the per-request rate times the mean
    number of running requests; without that gauge it stays missing, with a reason.
    """
    req = estimate_prefill_tps(sim)
    if req:
        inputs.request_prefill_tokens_per_sec = req
        inputs.request_tps_source = "estimated from metrics"
    if replica_tps:
        inputs.prefill_tokens_per_sec, inputs.prefill_tps_source = replica_tps, "input"
        if not req:
            inputs.request_tps_source = "replica rate (no per-request estimate)"
        return
    if not req:
        return
    running = mean_running_requests(sim)
    if running:
        inputs.prefill_tokens_per_sec = req * running
        inputs.prefill_tps_source = (f"estimated: per-request rate x {running:.2f} mean "
                                     "running requests")
    else:
        inputs.missing_notes.append(
            "prefill tokens/s per replica: pass --prefill-tokens-per-sec. The trace has no "
            "vllm:num_requests_running gauge to scale the per-request estimate, so GPU time "
            "and dollar figures are omitted")


def trace_gpus_per_replica(sim: Dict[str, Any]) -> Optional[int]:
    """Tensor-parallel size seen in the trace, or None.

    LMCache labels its worker series with one ``worker_id`` per TP rank; the
    ``role="scheduler"`` series are not workers. Every node must show the same count.
    Pipeline parallelism is not visible, so the value counts TP ranks only.
    """
    per_node = sim.get("metrics", {}).get("per_node") or {}
    sizes = set()
    for agg in per_node.values():
        workers = set()
        for key in agg.get("counters", {}):
            name, labels = split_key(key)
            if name.startswith("lmcache:") and "worker_id" in labels and \
                    labels.get("role", "worker") == "worker":
                workers.add(labels["worker_id"])
        if workers:
            sizes.add(len(workers))
    return sizes.pop() if len(sizes) == 1 else None


def resolve_replica_cost(inputs: CostInputs, sim: Dict[str, Any],
                         gpu_cost_per_hour: Optional[float], gpus_per_replica: Optional[int],
                         replica_cost_per_hour: Optional[float]) -> None:
    """Fill the replica cost of ``inputs``. There is no default price."""
    traced = trace_gpus_per_replica(sim)
    if gpus_per_replica is not None and traced is not None and gpus_per_replica != traced:
        inputs.warnings.append(
            f"--gpus-per-replica {gpus_per_replica} does not agree with the trace, which shows "
            f"{traced} tensor-parallel ranks per replica. The report uses {gpus_per_replica}.")
    if replica_cost_per_hour is not None:
        inputs.replica_cost_per_hour, inputs.replica_cost_source = replica_cost_per_hour, "input"
        return
    if gpu_cost_per_hour is None:
        return
    gpus, source = gpus_per_replica, "--gpus-per-replica"
    if gpus is None and traced is not None:
        gpus, source = traced, "TP size from the trace"
    if gpus is None:
        inputs.missing_notes.append(
            "--gpus-per-replica: the trace does not show the TP size, so the replica cost and "
            "dollar figures are omitted")
        return
    inputs.replica_cost_per_hour = gpu_cost_per_hour * gpus
    inputs.replica_cost_source = f"${gpu_cost_per_hour:,.2f} per GPU-hour x {gpus} GPUs ({source})"


def estimate_prefill_fraction(sim: Dict[str, Any]) -> Optional[float]:
    totals = metric_totals(sim)
    prefill, decode = totals["prefill_seconds"], totals["decode_seconds"]
    if prefill and decode is not None and prefill + decode > 0:
        return prefill / (prefill + decode)
    return None


def _ttft_bucket(seconds: float) -> str:
    for label, bound in TTFT_BUCKETS:
        if seconds < bound:
            return label
    return TTFT_BUCKETS[-1][0]


def capacity_savings(cap: Dict[str, Any], chunk_tokens: int, kv_bytes_per_token: Optional[int],
                     duration_seconds: float, computed_tokens: int,
                     inputs: CostInputs) -> Dict[str, Any]:
    """Savings for one simulated capacity. Fields are None when an input is missing.

    TTFT figures and the fetch-or-recompute test use the per-request rate. GPU time
    and dollars use the replica rate and are omitted (``gpu_available`` False) without it.
    """
    out: Dict[str, Any] = {"label": cap["label"], "saved_tokens": cap["saved_tokens"]}
    tps = request_tps(inputs)
    replica_tps = inputs.prefill_tokens_per_sec
    hist: List[List[int]] = cap.get("hit_tokens_histogram", [])
    if not tps or not kv_bytes_per_token:
        out["available"] = False
        return out
    fetch = fetch_seconds(chunk_tokens, kv_bytes_per_token, inputs.l2_bandwidth_gbps,
                          inputs.l2_rtt_ms)
    ttft_saved = 0.0
    beneficial = non_beneficial = beneficial_tokens = 0
    for tokens, count in hist:
        recompute = recompute_seconds(tokens, tps)
        # Only fetches faster than recomputing would be used by a sensible connector.
        # Each hit chunk pays one RTT here, but run_fetch_seconds pays one RTT per run;
        # the report states this difference as a caveat.
        if fetch < recompute:
            beneficial += count
            beneficial_tokens += count * tokens  # the GPU no longer computes these tokens
            ttft_saved += count * (recompute - fetch)  # the request still waits for the fetch
        else:
            non_beneficial += count

    run_buckets = {label: 0 for label, _ in TTFT_BUCKETS}
    run_total = 0.0
    run_count = 0
    for tokens, chunks, count in cap.get("runs_histogram", []):
        if not chunks:
            continue
        # Signed: a negative value means the run is slower with L2.
        saved = recompute_seconds(tokens, tps) - run_fetch_seconds(
            chunks, chunk_tokens, kv_bytes_per_token, inputs.l2_bandwidth_gbps, inputs.l2_rtt_ms)
        run_buckets[_ttft_bucket(saved)] += count
        run_total += saved * count
        run_count += count

    # GPU time: the GPU is free while a fetch is in flight, so fetch time is not subtracted.
    gpu_saved = beneficial_tokens / replica_tps if replica_tps else None
    hours = duration_seconds / 3600.0 if duration_seconds > 0 else 0.0
    out.update({
        "available": True,
        "gpu_available": gpu_saved is not None,
        "fetch_seconds_per_chunk": fetch,
        "beneficial_hits": beneficial,
        "non_beneficial_hits": non_beneficial,
        "beneficial_saved_tokens": beneficial_tokens,
        "gpu_replica_seconds_saved": gpu_saved,
        "ttft_seconds_saved": ttft_saved,
        "mean_ttft_saved_per_hit_chunk": ttft_saved / beneficial if beneficial else 0.0,
        "mean_ttft_saved_per_run": run_total / run_count if run_count else 0.0,
        "runs_with_hits": run_count,
        "runs_slower_with_l2": run_buckets[SLOWER_BUCKET],
        "ttft_saved_per_run_histogram": run_buckets,
        "prefill_saved_fraction_tokens": beneficial_tokens / computed_tokens if computed_tokens else 0.0,
    })

    if gpu_saved is not None and inputs.replica_cost_per_hour is not None:
        saved_usd = gpu_saved / 3600.0 * inputs.replica_cost_per_hour
        out["saved_usd_window"] = saved_usd
        out["saved_usd_per_hour"] = saved_usd / hours if hours else None
        out["saved_usd_per_month"] = (saved_usd * SECONDS_PER_MONTH / duration_seconds
                                      if duration_seconds > 0 else None)
        # Size the L2 at its configured capacity; the unbounded ceiling uses its peak use.
        size_bytes = cap["capacity_bytes"]
        if size_bytes is None:
            size_bytes = cap.get("peak_used_bytes")
        size_gb = (size_bytes or 0) / 1e9
        out["l2_size_gb"] = size_gb
        if size_gb and out["saved_usd_per_hour"] is not None:
            out["break_even_usd_per_gb_hour"] = out["saved_usd_per_hour"] / size_gb
        if inputs.l2_cost_per_gb_hour is not None and hours:
            l2_cost = size_gb * inputs.l2_cost_per_gb_hour * hours
            out["l2_cost_usd_window"] = l2_cost
            out["net_usd_window"] = saved_usd - l2_cost
            out["net_usd_per_month"] = (saved_usd - l2_cost) * SECONDS_PER_MONTH / duration_seconds
    return out


def headroom(savings: Dict[str, Any], sim: Dict[str, Any], inputs: CostInputs,
             computed_tokens: int) -> Dict[str, Any]:
    """Throughput gain upper bound for one capacity.

    ``s`` is the share of prefill work removed: beneficial saved tokens / computed
    tokens (both in tokens). Non-beneficial hits are not fetched, so they save nothing.
    Without a prefill rate the beneficial hits are unknown, so no bound is given.
    """
    f = inputs.prefill_time_fraction
    if f is None or not savings.get("available"):
        return {"available": False}
    s = savings["beneficial_saved_tokens"] / computed_tokens if computed_tokens else 0.0
    return {"available": True, "prefill_fraction": f, "saved_fraction": min(s, 1.0),
            "saved_fraction_source": "beneficial saved tokens / computed tokens",
            "throughput_gain_upper_bound": throughput_gain_upper_bound(f, s)}


def recommended_capacity(capacities: List[Dict[str, Any]], threshold: float = 0.9,
                         saved: Optional[Dict[str, float]] = None) -> Optional[str]:
    """Smallest capacity that reaches ``threshold`` of the ceiling's saved tokens (the knee).

    ``saved`` maps a capacity label to its beneficial saved tokens; when given it
    replaces each capacity's ``saved_tokens``, so hits not worth fetching do not count.
    """
    if not capacities:
        return None

    def tokens(cap: Dict[str, Any]) -> float:
        return saved.get(cap["label"], 0.0) if saved is not None else cap["saved_tokens"]

    ceiling = max(tokens(c) for c in capacities)
    if ceiling <= 0:
        return None
    finite = [c for c in capacities if c["capacity_bytes"] is not None]
    for cap in sorted(finite, key=lambda c: c["capacity_bytes"]):
        if tokens(cap) >= threshold * ceiling:
            return cap["label"]
    return next((c["label"] for c in capacities if c["capacity_bytes"] is None), None)


# Phase 1 break-even model (README "Priority sections"). The functions below use only
# GPU inputs and sim.json counters; the fitted TTFT knobs (write_stall_ms_per_gb,
# l2_rtt_ms) never enter them.

DEFAULT_BETAS: Tuple[float, ...] = (0.2, 0.4, 0.6)
LOAD_MODES = ("blocking", "async")
# A stored TTFT rtt above this is a fitted value, not a network round trip.
FITTED_RTT_WARN_MS = 5.0
GPU_MAX_STALL_SHARE = 0.95
HOURS_PER_MONTH = 720
P_SOURCE_INPUT = "input"
P_SOURCE_BUSY = "from measured busy share"
P_SOURCE_ESTIMATED = "estimated, biased high"
LOAD_MODE_REFUSAL = (
    "GPU cost needs --load-mode blocking|async. There is no default: LMCache's default "
    "(enable_async_loading False) is blocking, but the load mode of this trace is not known.")
P1_SWEEP_REFUSAL = ("P1 break-even needs simulate --contention-sweep "
                    "write_stall_ms_per_gb=0,10,25,50,100,200")
Y_LABEL = "fitted fixed cost per fetch, not network RTT"


@dataclass
class GpuInputs:
    """Report inputs of the GPU cost and break-even sections (all GPU-side, none fitted)."""
    load_mode: Optional[str] = None
    # Report override of the stored r_g; None uses inputs.contention.gpu_rtt_ms.
    gpu_rtt_ms: Optional[float] = None
    gpu_overlap: float = 0.0
    gpu_load_gb_per_sec: float = math.inf
    gpus_per_replica: Optional[int] = None
    gpu_stall_ms_per_gb: float = 0.0
    trace_utilization: Optional[float] = None
    tpot_change: Optional[float] = None
    cache_node_ec2_usd_per_hour: Optional[float] = None
    cache_node_margin: Optional[float] = None
    betas: Tuple[float, ...] = DEFAULT_BETAS
    # Opt-in cost line items (None = off, the report is as before).
    # Store cost, one of two forms: a fitted TTFT cost per stored token (µs/token), or
    # block hold = stored bytes / store rate, priced as held share of the GPU KV cache.
    store_us_per_token: Optional[float] = None
    store_gb_per_sec: Optional[float] = None
    gpu_kv_capacity_gb: Optional[float] = None
    # Decode gain: TPOT ON - OFF in ms (negative = ON faster) and the decode batch size B
    # (None = the mean running-request gauge of the trace).
    tpot_delta_ms: Optional[float] = None
    decode_batch: Optional[float] = None
    # GPU servers per cache node for the per-N table.
    gpu_servers_per_node: Optional[Tuple[int, ...]] = None
    # Source label of each opt-in input: input, calibrated or default.
    sources: Dict[str, str] = field(default_factory=dict)
    # calibrate.calibration_quality of the report's --calibration file, for weak-fit warnings.
    calibration_quality: Optional[Dict[str, Any]] = None

    def line_items_wanted(self) -> bool:
        """True when an opt-in cost line-item input is given."""
        return any(v is not None for v in (self.store_us_per_token, self.store_gb_per_sec,
                                           self.gpu_kv_capacity_gb, self.tpot_delta_ms,
                                           self.gpu_servers_per_node))


def stored_rtts(sim: Dict[str, Any]) -> Dict[str, Any]:
    """``r_g`` and the TTFT ``rtt`` stored by ``simulate`` (``inputs.contention``).

    An older sim.json without ``gpu_rtt_ms`` gives the 0.5 ms default with source
    ``default``. ``l2_rtt_ms`` is None without ``--contention on``.
    """
    cp = sim["inputs"].get("contention") or {}
    gpu = cp.get("gpu_rtt_ms")
    return {"gpu_rtt_ms": DEFAULT_GPU_RTT_MS if gpu is None else float(gpu),
            "gpu_rtt_ms_source": cp.get("gpu_rtt_ms_source") or "default",
            "l2_rtt_ms": cp.get("l2_rtt_ms")}


def wall_replica_seconds(sim: Dict[str, Any]) -> float:
    """Fleet wall time ``N * D`` in replica-s; with load tiles, ``T_l * N * D`` (not ``D'``)."""
    trace = sim["trace"]
    tiles = trace.get("load_scale")
    if tiles:
        return float(tiles["wall_replica_seconds"])
    return len(trace["nodes"]) * float(trace["window"]["duration_seconds"] or 0.0)


def policy_saved_tokens(policy: Dict[str, Any]) -> int:
    """Prefill tokens a policy removes: fetched hit tokens plus host-enabled tokens."""
    return int(policy["fetched_tokens"]) + int(policy["host_enabled_tokens"])


def gpu_cost(policy: Dict[str, Any], gpu_rtt_ms: float, read_gbps: float, load_mode: str,
             omega: float = 0.0, load_gb_per_sec_per_replica: float = math.inf,
             stall_ms_per_gb: float = 0.0, busy_seconds_per_node: float = 0.0,
             store_seconds: Optional[float] = None) -> Dict[str, float]:
    """GPU cost ``L_g`` of one policy in replica-s: load wait + copy + GPU write stall.

    Load wait = ``fetching_runs * r_g + read_gb * 8 / W``; ``async`` multiplies it by
    ``1 - omega``. The queue wait is not in it (the transfer term already counts the
    time a run waits behind earlier runs of the same replica); ``total_upper`` adds
    ``queue_wait_sum``. Copy = ``read_gb / H``. Stall = sum over engine nodes of
    ``g / (1 - g) * u * D`` with ``g = X_g / 1000 * write GB/s`` of that node, capped
    at 0.95. ``store_seconds`` (see :func:`store_cost`), when given, is added as ``store``.

    Raises:
        ValueError: ``load_mode`` is not ``blocking`` or ``async``.
    """
    if load_mode not in LOAD_MODES:
        raise ValueError(f"load mode must be one of {LOAD_MODES}, not {load_mode!r}")
    read_gb = float(policy["read_gb"])
    wait = blocking = int(policy["fetching_runs"]) * gpu_rtt_ms / 1000.0 + read_gb * 8 / read_gbps
    queue = float(policy["queue_wait_sum"])
    if load_mode == "async":
        wait *= 1.0 - omega
        queue *= 1.0 - omega
    copy = read_gb / load_gb_per_sec_per_replica if load_gb_per_sec_per_replica > 0 else math.inf
    stall = 0.0
    if stall_ms_per_gb:
        for node in policy["per_node"].values():
            g = min(stall_ms_per_gb / 1000.0 * node["write_bytes_per_sec"] / 1e9, GPU_MAX_STALL_SHARE)
            stall += g / (1.0 - g) * busy_seconds_per_node
    total = wait + copy + stall
    out = {"load_wait": wait, "load_wait_blocking": blocking, "copy": copy, "stall": stall,
           "total": total,
           "queue_wait": queue, "total_upper": total + queue}
    if store_seconds is not None:
        out["store"] = store_seconds
        out["total"] += store_seconds
        out["total_upper"] += store_seconds
    return out


def store_cost(stored_tokens: float, runs: int, kv_bytes_per_token: Optional[int],
               gpu: GpuInputs) -> Optional[Dict[str, Any]]:
    """Store overhead in replica-s for ``stored_tokens`` written by ``runs`` runs, or None.

    fitted (``store_us_per_token`` X_s): ``stored_tokens * X_s``. Each request's measured
    TTFT add is counted as replica time, as the blocking load wait is, so it is an upper
    bound when the GPU serves other requests meanwhile.

    block hold (``store_gb_per_sec`` S, ``gpu_kv_capacity_gb`` K): vLLM holds a request's
    blocks until its store is done, ``b / S`` for ``b`` stored GB. Held GB-s =
    ``sum(b_i^2) / S``; with only the total ``B`` known it is taken at the mean run,
    ``B^2 / (runs * S)``, a lower bound (Cauchy-Schwarz). Held GB-s / K is the replica time
    the held share of the KV cache stands for, if KV capacity limits concurrency.

    Raises:
        ValueError: both forms are given, or block hold lacks the KV size.
    """
    fitted = gpu.store_us_per_token is not None
    hold = gpu.store_gb_per_sec is not None or gpu.gpu_kv_capacity_gb is not None
    if fitted and hold:
        raise ValueError("give --store-us-per-token or --store-gb-per-sec with "
                         "--gpu-kv-capacity-gb, not both")
    if fitted:
        return {"seconds": stored_tokens * gpu.store_us_per_token / 1e6, "method": "fitted",
                "label": "fitted TTFT cost per stored token, counted as replica time (upper bound)",
                "stored_tokens": stored_tokens}
    if not hold:
        return None
    if gpu.store_gb_per_sec is None or gpu.gpu_kv_capacity_gb is None:
        raise ValueError("--store-gb-per-sec and --gpu-kv-capacity-gb go together")
    if not kv_bytes_per_token:
        raise ValueError("block-hold store cost needs the KV size in sim.json")
    stored_gb = stored_tokens * kv_bytes_per_token / 1e9
    held = stored_gb * stored_gb / (max(runs, 1) * gpu.store_gb_per_sec)
    return {"seconds": held / gpu.gpu_kv_capacity_gb, "method": "block-hold",
            "label": "held KV share x time, at the mean run size (lower bound of the hold)",
            "stored_tokens": stored_tokens, "held_gb_seconds": held}


# Generation-token counters, first group present wins per node (as for cached tokens).
_GENERATION_COUNTERS = (("vllm:generation_tokens_total",), ("sglang:generation_tokens_total",))


def generation_tokens(sim: Dict[str, Any]) -> Optional[float]:
    """Output tokens of the trace window, summed over nodes; None without the counter."""
    metrics = sim.get("metrics", {})
    per_node = metrics.get("per_node")
    node_counters = ([agg.get("counters", {}) for agg in per_node.values()] if per_node
                     else [metrics.get("fleet", {}).get("counters", {})])
    total: Optional[float] = None
    for counters in node_counters:
        for names in _GENERATION_COUNTERS:
            value = _first_named(counters, names)
            if value is not None:
                total = (total or 0.0) + value
                break
    return total


def decode_saved(output_tokens: float, tpot_delta_ms: float, batch: float) -> float:
    """Decode replica-s saved by a TPOT change, ``output_tokens * -dTPOT / B``.

    One decode step makes one token for each of the ``B`` running requests, so a
    replica spends ``TPOT / B`` per output token. At a fixed batch, a per-request TPOT
    change ``dTPOT`` (ON - OFF, negative = faster) then changes decode replica time by
    ``output_tokens * dTPOT / B``; the decode throughput gain is ``-dTPOT / TPOT_off``.
    Positive = saved.
    """
    return output_tokens * -tpot_delta_ms / 1000.0 / batch


SCALING_ASSUMPTION = "independent-replica, no cross-server reuse"


def server_scaling(per_server_usd_per_hour: float, per_server_excl_decode: Optional[float],
                   cache_nodes: int, node_usd_per_hour: Optional[float], servers: Sequence[int],
                   limits: Dict[str, Any]) -> Dict[str, Any]:
    """Net $/h per count ``N`` of GPU servers that share one L2 of ``cache_nodes`` nodes.

    Each server sees an independent copy of the workload with the same hit share
    (:data:`SCALING_ASSUMPTION`), so GPU $ saved = ``N * per server`` and the L2 costs
    ``n * Q_n``. ``N*`` is the smallest listed ``N`` with net >= 0; ``n_star_min`` =
    ``ceil(n * Q_n / per server)`` is the same bound without the list. ``limits``
    (per-server read and write Gbit/s, peak L2 bytes, the cache NIC Gbit/s per node and
    capacity bytes) flags each ``N`` whose bandwidth or working set the L2 cannot hold,
    since the model does not scale the hit share down.
    """
    node_cost = None if node_usd_per_hour is None else cache_nodes * node_usd_per_hour
    rows = []
    for n in servers:
        flags = []
        nic = limits.get("cache_node_gbps")
        for name in ("read", "write"):
            rate = limits.get(f"{name}_gbps_per_server")
            if nic is None:
                continue
            if rate is not None and n * rate > cache_nodes * nic:
                flags.append(f"{name} {n * rate:,.1f} Gbit/s above the {cache_nodes} x {nic:g} "
                             "Gbit/s cache NIC")
        peak, cap = limits.get("peak_bytes"), limits.get("capacity_bytes")
        if peak and cap and n * peak > cap:
            flags.append(f"working set {n * peak / 1e12:,.2f} TB above capacity "
                         f"{cap / 1e12:,.2f} TB: the hit share would fall (not modelled)")
        saved = n * per_server_usd_per_hour
        row = {"servers": n, "gpu_usd_per_hour_saved": saved, "cache_usd_per_hour": node_cost,
               "net_usd_per_hour": None if node_cost is None else saved - node_cost,
               "flags": flags}
        if per_server_excl_decode is not None:
            row["net_usd_per_hour_excl_decode"] = (None if node_cost is None
                                                   else n * per_server_excl_decode - node_cost)
        rows.append(row)

    def first(key: str) -> Optional[int]:
        return next((r["servers"] for r in rows if r.get(key) is not None and r[key] >= 0), None)

    def bound(per: Optional[float]) -> Optional[int]:
        if node_cost is None or per is None or per <= 0:
            return None
        return max(1, math.ceil(node_cost / per))

    out = {"assumption": SCALING_ASSUMPTION, "rows": rows, "n_star": first("net_usd_per_hour"),
           "n_star_min": bound(per_server_usd_per_hour),
           "nic_modelled": limits.get("cache_node_gbps") is not None}
    if per_server_excl_decode is not None:
        out["n_star_excl_decode"] = first("net_usd_per_hour_excl_decode")
        out["n_star_min_excl_decode"] = bound(per_server_excl_decode)
    return out


def break_even(saved_tokens: float, computed_tokens: float, gpu_cost_seconds: float,
               wall_seconds: float, engine_nodes: int, cache_nodes: Optional[int],
               betas: Sequence[float] = DEFAULT_BETAS,
               utilization: Optional[float] = None) -> Dict[str, Any]:
    """P2 and P3 break-evens of one policy; none needs ``P`` or a price.

    ``s_pol = saved / computed``. ``P* = saved / L_g``: ``S_net >= 0`` only if ``P < P*``
    (None when ``L_g`` is 0: every ``P`` pays). ``beta* = L_g / (s_pol * N * D)``.
    ``Q*(beta) = (s_pol * beta * N - L_g / D) / n``, freed replicas per cache node, i.e.
    the break-even node price in replica-hours per node-hour. Without ``cache_nodes``
    ``n`` = 1 (the whole L2). With ``utilization``, ``beta`` values above ``u`` are
    dropped, because the ``P`` check refuses them.
    """
    s_pol = saved_tokens / computed_tokens if computed_tokens else 0.0
    window = wall_seconds / engine_nodes if engine_nodes else 0.0
    p_star = saved_tokens / gpu_cost_seconds if gpu_cost_seconds > 0 else None
    beta_star = (gpu_cost_seconds / (s_pol * wall_seconds)
                 if s_pol > 0 and wall_seconds > 0 else None)
    n = cache_nodes or 1
    q_star: Dict[str, Optional[float]] = {}
    for beta in betas:
        if utilization is not None and beta > utilization:
            continue
        q_star[f"{beta:g}"] = ((s_pol * beta * engine_nodes - gpu_cost_seconds / window) / n
                               if window > 0 else None)
    return {"s_pol": s_pol, "p_star": p_star, "beta_star": beta_star, "q_star": q_star,
            "q_star_per": "cache node" if cache_nodes else "L2 (no --cache-node-counts)"}


def omega_star(saved_tokens: float, replica_tps: float, load_wait_blocking: float,
               other_cost: float) -> Dict[str, Any]:
    """The overlap ``omega`` in [0, 1] at which ``S_net`` = 0 under async loading.

    ``S_net(omega) = saved / P - (1 - omega) * load_wait - other`` is linear in
    ``omega``. When it has one sign for all of [0, 1], ``omega`` is None and
    ``direction`` says which.
    """
    gain = saved_tokens / replica_tps - other_cost
    at0, at1 = gain - load_wait_blocking, gain
    if at0 >= 0:
        return {"omega": None, "direction": "S_net >= 0 at all omega"}
    if at1 < 0:
        return {"omega": None, "direction": "S_net < 0 at all omega"}
    return {"omega": 1.0 - gain / load_wait_blocking, "direction": "S_net >= 0 above"}


def p90_change(policy: Dict[str, Any]) -> Dict[str, Any]:
    """P1 headline statistic of one contention policy entry.

    With OFF TTFT (``ttft_change``), the change of p90 TTFT over all requests; else
    the p90 of per-run delta over the runs with hits, with its label.
    """
    if "ttft_change" in policy:
        return {"statistic": "ttft_p90_change", "label": "change of p90 TTFT",
                "value": policy["ttft_change"]["p90"]}
    return {"statistic": "ttft_delta_p90_hit_runs",
            "label": "p90 of per-run TTFT change, runs with hits",
            "value": policy["ttft_delta_hit_runs"]["p90"]}


def tpot_test(gpu_cost_seconds: float, wall_seconds: float, s_pol: float, tpot_change: float,
              utilization: Optional[float] = None,
              measured_beta: Optional[float] = None) -> Dict[str, Any]:
    """Reject GPU costs too large for the measured TPOT change ``dT``.

    The bound ``L_g / (u * N * D) <= dT + s_pol * beta / u`` holds only for
    ``beta >= (L_g / (N * D) - u * dT) / s_pol``. The input set is rejected when this
    limit is above ``u`` (the ``P`` check allows ``beta <= u``) or above a measured
    ``beta``. ``u`` is 1 when not given.
    """
    u = 1.0 if utilization is None else utilization
    share = gpu_cost_seconds / wall_seconds if wall_seconds > 0 else math.inf
    limit = (share - u * tpot_change) / s_pol if s_pol > 0 else math.inf
    reasons = []
    if limit > u:
        reasons.append(f"beta limit {limit:.3f} is above u = {u:g}")
    if measured_beta is not None and limit > measured_beta:
        reasons.append(f"beta limit {limit:.3f} is above the measured beta {measured_beta:.3f}")
    return {"beta_limit": limit, "u": u, "u_source": "input" if utilization is not None else "assumed 1",
            "gpu_cost_share": share, "rejected": bool(reasons), "reasons": reasons}


def replica_rate_check(computed_tokens: float, replica_tps: float, wall_seconds: float,
                       utilization: Optional[float] = None) -> Dict[str, Any]:
    """Busy-time ``P`` check: prefill time ``computed / P`` must fit in ``u * N * D``.

    ``ratio`` > 1 means the rate is too low to be a replica rate (``P`` is likely a
    per-request rate), so GPU time and dollar figures from it are not physical.
    """
    budget = wall_seconds * (1.0 if utilization is None else utilization)
    ratio = computed_tokens / replica_tps / budget if replica_tps > 0 and budget > 0 else math.inf
    return {"ratio": ratio, "ok": ratio <= 1.0, "floor_tokens_per_sec":
            computed_tokens / budget if budget > 0 else None}


def node_price(ec2_usd_per_hour: Optional[float], margin: Optional[float]) -> Optional[float]:
    """Cache node price ``Q_n = E_n * (1 + m)`` in $/node-hour; None without both inputs."""
    if ec2_usd_per_hour is None or margin is None:
        return None
    return ec2_usd_per_hour * (1.0 + margin)


def phase1_replica_rate(inputs: CostInputs, gpu: GpuInputs, computed_tokens: float,
                        wall_seconds: float) -> Dict[str, Any]:
    """``P`` for the priority sections, in this order, with its source.

    1. ``--prefill-tokens-per-sec`` ("input").
    2. ``--trace-utilization`` u with an input ``--prefill-time-fraction`` f:
       ``P = computed / (u * f * N * D)`` ("from measured busy share").
    3. The gauge estimate ``p * R`` ("estimated, biased high"), labeled.
    ``trusted`` is False for the estimate. ``rate`` is None when nothing is known.
    """
    if inputs.prefill_tokens_per_sec and inputs.prefill_tps_source == "input":
        return {"rate": inputs.prefill_tokens_per_sec, "source": P_SOURCE_INPUT, "trusted": True}
    u, f = gpu.trace_utilization, inputs.prefill_time_fraction
    if u and f and inputs.prefill_fraction_source == "input" and wall_seconds > 0:
        return {"rate": computed_tokens / (u * f * wall_seconds), "source": P_SOURCE_BUSY,
                "trusted": True}
    if inputs.prefill_tokens_per_sec:
        return {"rate": inputs.prefill_tokens_per_sec, "source": P_SOURCE_ESTIMATED, "trusted": False}
    return {"rate": None, "source": "missing", "trusted": False}


def _policy_ids(policies: Dict[str, Any], load_mode: Optional[str]) -> List[str]:
    # lookup-timeout acts only with async loading in LMCache, so its rows need --load-mode async.
    return [pid for pid in policies
            if not (pid.startswith("lookup-timeout:") and load_mode != "async")]


def cost_rows(sim: Dict[str, Any], inputs: CostInputs, gpu: GpuInputs) -> Dict[str, Any]:
    """Per capacity and policy: GPU cost, break-evens, TPOT test, net GPU time and spend.

    Needs a sim.json made with ``--contention on`` and a phase 1 option (the policy
    entries need ``fetching_runs``). ``available`` is False with ``reason`` otherwise.
    Without ``gpu.load_mode`` the GPU figures are refused (``gpu_refusal``), but the
    P1 figures and write metrics still need no GPU input.
    """
    rtts = stored_rtts(sim)
    r_g = rtts["gpu_rtt_ms"] if gpu.gpu_rtt_ms is None else gpu.gpu_rtt_ms
    out: Dict[str, Any] = {"gpu_rtt_ms": r_g, "stored_gpu_rtt_ms": rtts["gpu_rtt_ms"],
                           "gpu_rtt_ms_source": ("report input" if gpu.gpu_rtt_ms is not None
                                                 else rtts["gpu_rtt_ms_source"]),
                           "l2_rtt_ms": rtts["l2_rtt_ms"], "load_mode": gpu.load_mode,
                           "warnings": [], "rows": []}
    warnings: List[str] = out["warnings"]
    if rtts["l2_rtt_ms"] is not None and rtts["l2_rtt_ms"] > FITTED_RTT_WARN_MS and \
            rtts["gpu_rtt_ms_source"] == "default":
        warnings.append(
            f"the stored TTFT rtt (l2_rtt_ms = {rtts['l2_rtt_ms']:g} ms) is above "
            f"{FITTED_RTT_WARN_MS:g} ms, so it is a fitted value, not a network round trip. It does "
            f"not enter GPU cost: r_g = {rtts['gpu_rtt_ms']:g} ms (default, not measured). Give "
            "simulate --gpu-rtt-ms or report --gpu-rtt-ms to set r_g.")
    if gpu.gpu_rtt_ms is not None and gpu.gpu_rtt_ms != rtts["gpu_rtt_ms"]:
        warnings.append(
            f"report --gpu-rtt-ms {gpu.gpu_rtt_ms:g} differs from the stored r_g "
            f"{rtts['gpu_rtt_ms']:g} ms. GPU cost uses {gpu.gpu_rtt_ms:g} ms; X_t* is only at the "
            f"stored {rtts['gpu_rtt_ms']:g} ms.")
    if gpu.gpu_rtt_ms is None and rtts["gpu_rtt_ms_source"] == "calibrated" and \
            rtts["gpu_rtt_ms"] > FITTED_RTT_WARN_MS:
        warnings.append(
            f"r_g = {rtts['gpu_rtt_ms']:g} ms is calibrated from the fixed cost of one L2 load "
            "(t_fixed_s, idle engine), not a network round trip. It holds one lookup, so with the "
            "measured lookup delay a hit run pays part of the lookup twice in TTFT.")
    cp = sim["inputs"].get("contention")
    caps = sim["capacities"]
    if not cp or not caps or "contention" not in caps[0]:
        out.update(available=False, reason="needs simulate --contention on")
        return out
    if "fetching_runs" not in caps[0]["contention"]["policies"]["lmcache"]:
        out.update(available=False, reason=(
            "needs a sim.json from simulate --contention on with a phase 1 option "
            "(for example --gpu-rtt-ms 0.5 or --contention-sweep)"))
        return out
    out["available"] = True
    if gpu.load_mode is None:
        out["gpu_refusal"] = LOAD_MODE_REFUSAL
    computed = float(sim["trace"]["tokens"]["computed"])
    wall = wall_replica_seconds(sim)
    nodes = len(sim["trace"]["nodes"])
    busy_per_node = (wall / nodes if nodes else 0.0) * (gpu.trace_utilization or 1.0)
    h = gpu.gpu_load_gb_per_sec * (gpu.gpus_per_replica or 1)
    rate = phase1_replica_rate(inputs, gpu, computed, wall)
    out.update(computed_tokens=computed, wall_replica_seconds=wall, engine_nodes=nodes,
               replica_rate=rate, copy_gb_per_sec_per_replica=h)
    if rate["rate"]:
        out["rate_check"] = replica_rate_check(computed, rate["rate"], wall, gpu.trace_utilization)
        if rate["source"] == P_SOURCE_ESTIMATED:
            warnings.append(f"P = {rate['rate']:,.0f} tokens per replica-s is the gauge estimate "
                            "(per-request rate x mean running requests), biased high; the priority "
                            "sections give no GPU-s or $ figure from it.")
        if not out["rate_check"]["ok"]:
            warnings.append(
                f"P check failed: computed tokens / P = {out['rate_check']['ratio']:.2f} x the busy "
                f"wall time, so P = {rate['rate']:,.0f} cannot be a replica rate (floor "
                f"{out['rate_check']['floor_tokens_per_sec']:,.0f} tokens per replica-s). Net GPU "
                "time and $ figures are omitted; dollar figures in the capacity table that use "
                "this P are not physical.")
    q_n = node_price(gpu.cache_node_ec2_usd_per_hour, gpu.cache_node_margin)
    out["node_price_usd_per_hour"] = q_n
    c_rep = inputs.replica_cost_per_hour
    rate_ok = bool(rate["rate"]) and rate["trusted"] and out["rate_check"]["ok"]
    if rate["rate"] and not rate_ok:
        why = (f"P check failed (P = {rate['rate']:,.0f})" if not out["rate_check"]["ok"]
               else f"P = {rate['rate']:,.0f} is the gauge estimate (estimated, biased high)")
        out["net_omitted"] = (f"{why}, so S_net, phi, spend and net $ are omitted. Give "
                              "--prefill-tokens-per-sec, or --trace-utilization with "
                              "--prefill-time-fraction, for a trusted P. L_g, P*, beta* and Q* "
                              "do not use P, so they stay.")
    measured_beta = computed / (rate["rate"] * wall) if rate_ok and wall > 0 else None
    kvb = sim["inputs"]["params"].get("kv_bytes_per_token")
    extras = gpu.line_items_wanted() and gpu.load_mode is not None
    if gpu.line_items_wanted() and gpu.load_mode is None:
        out["line_items_omitted"] = f"cost line items: {LOAD_MODE_REFUSAL}"
    decode: Optional[Dict[str, Any]] = None
    if extras:
        decode = _decode_input(sim, gpu)
        out["decode"] = decode
        if not (rate_ok and c_rep is not None):
            out["line_items_omitted"] = (
                "cost line items and the per-N table need a trusted P that passes the P check "
                "and a replica price (--gpu-cost-per-hour or --replica-cost-per-hour)")
        warnings.extend(_weak_calibration_warnings(sim, gpu))
    for cap in caps:
        n = cap.get("cache_nodes")
        policies = cap["contention"]["policies"]
        for pid in _policy_ids(policies, gpu.load_mode):
            pol = policies[pid]
            saved = policy_saved_tokens(pol)
            row: Dict[str, Any] = {"capacity": cap["label"], "cache_nodes": n, "policy": pid,
                                   "saved_tokens": saved, "recency": pol.get("recency"),
                                   "p1": p90_change(pol)}
            if pid != "ideal" and "ideal" in policies:
                ideal = p90_change(policies["ideal"])["value"]
                value = row["p1"]["value"]
                row["oracle_gap_p90"] = (None if value is None or ideal is None else value - ideal)
            row["runs_slower"], row["runs_faster"] = pol["runs_slower"], pol["runs_faster"]
            if gpu.load_mode is not None:
                # LMCache stores every chunk it computes, so stored = computed - saved.
                store = (store_cost(computed - saved, int(cap["contention"]["runs"]), kvb, gpu)
                         if extras else None)
                cost = gpu_cost(pol, r_g, float(cp["l2_read_gbps_per_node"]), gpu.load_mode,
                                gpu.gpu_overlap, h, gpu.gpu_stall_ms_per_gb, busy_per_node,
                                None if store is None else store["seconds"])
                if store is not None:
                    row["store_cost"] = store
                row["gpu_cost"] = cost
                row["break_even"] = break_even(saved, computed, cost["total"], wall, nodes, n,
                                               gpu.betas, gpu.trace_utilization)
                row["break_even_upper"] = break_even(saved, computed, cost["total_upper"], wall,
                                                     nodes, n, gpu.betas, gpu.trace_utilization)
                if gpu.tpot_change is not None:
                    row["tpot_test"] = tpot_test(cost["total"], wall, row["break_even"]["s_pol"],
                                                 gpu.tpot_change, gpu.trace_utilization, measured_beta)
                if c_rep is not None:
                    q_star = row["break_even"]["q_star"]
                    row["break_even_node_usd_per_hour"] = {
                        b: (None if q is None else q * c_rep) for b, q in q_star.items()}
                    if cap["capacity_bytes"]:
                        gb = cap["capacity_bytes"] / 1e9
                        row["break_even_usd_per_gb_hour"] = {
                            b: (None if q is None else q * c_rep * (n or 1) / gb)
                            for b, q in q_star.items()}
                if rate_ok:
                    _add_net(row, cost, saved, rate, c_rep, gpu, wall, nodes, n, q_n)
                    if extras and c_rep is not None:
                        _add_line_items(row, cap, pol, cost, saved, rate["rate"], c_rep, wall,
                                        nodes, q_n, decode, gpu, sim)
            out["rows"].append(row)
    return out


STORE_FIT_MIN_R2 = 0.1
LOOKUP_MIN_SAMPLES = 30


def weak_calibration_warnings(quality: Dict[str, Any], store_applied: bool,
                              lookup_applied: bool, where: str) -> List[str]:
    """Warnings for applied calibration values with a weak fit or few samples.

    ``store_applied`` / ``lookup_applied`` say whether that value came from the calibration.
    A store fit with r^2 below :data:`STORE_FIT_MIN_R2`, or a lookup median from fewer than
    :data:`LOOKUP_MIN_SAMPLES` requests, gets a warning.
    """
    out = []
    r, n = quality.get("store_r"), quality.get("store_n")
    if store_applied and r is not None and r * r < STORE_FIT_MIN_R2:
        out.append(f"the calibrated store cost ({where}) is from a fit with r = {r:.3f} (r^2 "
                   f"{r * r:.3f}, n {n}): it explains little of the per-request TTFT spread")
    if lookup_applied:
        for kind in ("hit", "miss"):
            count = quality.get(f"lookup_{kind}_n")
            if count is not None and count < LOOKUP_MIN_SAMPLES:
                out.append(f"the calibrated lookup delay, {kind} ({where}), is a median of only "
                           f"{count} requests")
    return out


def _weak_calibration_warnings(sim: Dict[str, Any], gpu: GpuInputs) -> List[str]:
    rec = sim["inputs"].get("calibration") or {}
    out: List[str] = []
    if rec.get("quality"):
        vals = rec.get("values", {})

        def cal(key: str) -> bool:
            return (vals.get(key) or {}).get("source") == "calibrated"

        out += weak_calibration_warnings(rec["quality"], cal("store_us_per_token"),
                                         cal("l2_lookup_ms_hit") or cal("l2_lookup_ms_miss"),
                                         "simulate")
    if gpu.calibration_quality:
        out += weak_calibration_warnings(gpu.calibration_quality,
                                         gpu.sources.get("store_us_per_token") == "calibrated",
                                         False, "report")
    return out


def _decode_input(sim: Dict[str, Any], gpu: GpuInputs) -> Dict[str, Any]:
    """Decode saved (replica-s) of the trace window from ``gpu.tpot_delta_ms``, with sources."""
    if gpu.tpot_delta_ms is None:
        return {"available": False, "reason": "no TPOT delta (--tpot-delta-ms or --calibration)"}
    tokens = generation_tokens(sim)
    batch, batch_source = gpu.decode_batch, "input"
    if batch is None:
        batch, batch_source = mean_running_requests(sim), (
            "estimated: mean running-request gauge (also counts requests in prefill)")
    if not tokens or not batch:
        return {"available": False,
                "reason": "decode gain needs the generation-token counter in the trace and "
                          "--decode-batch (or the running-request gauge)"}
    return {"available": True, "output_tokens": tokens, "tpot_delta_ms": gpu.tpot_delta_ms,
            "batch": batch, "batch_source": batch_source,
            "seconds": decode_saved(tokens, gpu.tpot_delta_ms, batch),
            "note": "measured at the ON run's configuration, so the same for every row; part of "
                    "the TPOT gain can come from less prefill interleaved with decode, which the "
                    "prefill line already counts"}


def _scaling_limits(sim: Dict[str, Any], cap: Dict[str, Any], pol: Dict[str, Any],
                    wall: float, nodes: int) -> Dict[str, Any]:
    # Per-server L2 traffic and size; the NIC limit only when the sim modelled cache nodes.
    write = sum(nd["write_bytes_per_sec"] for nd in pol["per_node"].values())
    cn = sim["inputs"].get("cache_node") or {}
    return {"read_gbps_per_server": float(pol["read_gb"]) * 8 / wall if wall > 0 else None,
            "write_gbps_per_server": write * 8 / 1e9 / nodes if nodes else None,
            "peak_bytes": (cap.get("peak_used_bytes") or 0) / nodes if nodes else None,
            "capacity_bytes": cap.get("capacity_bytes"),
            "cache_node_gbps": cn.get("gbps") if cap.get("cache_nodes") else None}


def _add_line_items(row: Dict[str, Any], cap: Dict[str, Any], pol: Dict[str, Any],
                    cost: Dict[str, float], saved: float, replica_tps: float, c_rep: float,
                    wall: float, nodes: int, q_n: Optional[float],
                    decode: Optional[Dict[str, Any]], gpu: GpuInputs, sim: Dict[str, Any]) -> None:
    # Replica-s per wall replica-s is the share of one server, so x C_rep gives $/h per server.
    def usd(seconds: Optional[float]) -> Optional[float]:
        return None if seconds is None or wall <= 0 else seconds / wall * c_rep

    dec = decode["seconds"] if decode and decode.get("available") else None
    load_rtt = cost["load_wait"] + cost["copy"] + cost["stall"]
    items = {"prefill_saved": saved / replica_tps, "decode_saved": dec,
             "lookup_rtt_overhead": load_rtt, "store_overhead": cost.get("store", 0.0)}
    per_hour = {k: usd(v) for k, v in items.items()}
    excl = per_hour["prefill_saved"] - per_hour["lookup_rtt_overhead"] - per_hour["store_overhead"]
    net = excl + (per_hour["decode_saved"] or 0.0)
    n = cap.get("cache_nodes") or 1
    row["line_items"] = {"replica_seconds": items, "usd_per_hour_per_server": per_hour,
                         "net_usd_per_hour_per_server": net,
                         "net_usd_per_hour_per_server_excl_decode": excl,
                         "cache_nodes": n,
                         "cache_usd_per_hour": None if q_n is None else n * q_n}
    if gpu.gpu_servers_per_node:
        scaling = server_scaling(net, excl if dec is not None else None, n, q_n,
                                 gpu.gpu_servers_per_node, _scaling_limits(sim, cap, pol, wall, nodes))
        if nodes > 1:
            scaling["assumption"] += (f"; the per-server value is the mean over the {nodes} recorded "
                                      "servers simulated on one shared L2, so reuse among them is "
                                      "included")
        row["server_scaling"] = scaling


def _add_net(row: Dict[str, Any], cost: Dict[str, float], saved: float, rate: Dict[str, Any],
             c_rep: Optional[float], gpu: GpuInputs, wall: float, nodes: int, n: Optional[int],
             q_n: Optional[float]) -> None:
    # Net GPU time saved and its two exclusive spend modes; only for a trusted P that passed the check.
    s_net = saved / rate["rate"] - cost["total"]
    u = gpu.trace_utilization or 1.0
    phi = s_net / (u * wall) if wall > 0 else None
    window = wall / nodes if nodes else 0.0
    row["s_net"] = s_net
    row["s_net_source"] = rate["source"]
    row["phi"] = phi
    same_fleet = phi / (1.0 - phi) if phi is not None and phi < 1.0 else None
    shrink = (s_net / window * HOURS_PER_MONTH * c_rep
              if c_rep is not None and window > 0 else None)
    # Freed GPU time is spent once: either more throughput or a smaller fleet, never both.
    row["spend"] = {"exclusive": True,
                    "same_fleet_throughput_gain": same_fleet,
                    "same_fleet_label": "compute-bound, SLO not binding",
                    "shrink_fleet_gpu_usd_per_month": shrink}
    if shrink is not None and q_n is not None and n:
        row["net_usd_per_month"] = shrink - n * q_n * HOURS_PER_MONTH
    if gpu.load_mode == "async":
        row["omega_star"] = omega_star(saved, rate["rate"], cost["load_wait_blocking"],
                                       cost["copy"] + cost["stall"])
