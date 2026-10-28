"""Measured prompt-token split per cache tier from recorded engine metrics (``kvtrace tiers``).

Tiers: **L0** is the engine's own GPU prefix cache, **L1** the host CPU tier (LMCache
local CPU) and **L2** the remote tier (LMCache remote, e.g. Valkey). vLLM reports L0
and "external" (L1 + L2 together) per prompt token; LMCache's own counters, when the
recorder scraped them, give the L2 bytes each TP worker read, so L2 = largest bytes per
worker / KV bytes per token of one worker, and L1 = external - L2. In LMCache
multiprocess (MP) mode the MP server's lookup counters give the L1 and L2 hit keys, and
external is split in their window ratio (see :func:`_mp_l2_share`).

Counters are turned into increments between consecutive ``M`` samples of one node and
recorder instance (``rid``; see :func:`split_recorders`). An
increment is credited to the interval of the later sample, so interval edges are exact
to one sample period (``--metrics-interval``). A counter that goes down (an engine
restart) restarts from zero, as in :func:`kvtrace.metrics.aggregate_samples`. Deltas are
per series and per extra URL, and a series that appears late counts from 0 only when it
was created in the window (see :func:`kvtrace.metrics.counter_increments`).
"""

from __future__ import annotations

import csv
import io
import math
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import TOOL_NAME
from . import metrics as metrics_mod

# vLLM prompt-token sources (vllm:prompt_tokens_by_source_total{source=...}).
SRC_COMPUTE, SRC_L0, SRC_EXTERNAL = "local_compute", "local_cache_hit", "external_kv_transfer"
_PROMPT = ("vllm:prompt_tokens_total", "sglang:prompt_tokens_total")
_BY_SOURCE = "vllm:prompt_tokens_by_source_total"
_CACHED = "vllm:prompt_tokens_cached_total"
# Aliases of one counter; only the first name present is used (see costmodel).
_L0_HITS = ("vllm:prefix_cache_hits_total", "vllm:gpu_prefix_cache_hits_total")
_EXTERNAL_HITS = "vllm:external_prefix_cache_hits_total"
_SGLANG_CACHED = "sglang:cached_tokens_total"
# Finished requests: the prompt-length histogram gets one observation per request.
_REQUESTS = ("vllm:request_prompt_tokens_count", "vllm:time_to_first_token_seconds_count",
             "sglang:time_to_first_token_seconds_count")
_TTFT = ("vllm:time_to_first_token_seconds", "sglang:time_to_first_token_seconds")
_LMC_HIT = "lmcache:num_hit_tokens_total"
_LMC_REMOTE_READ = "lmcache:num_remote_read_bytes_total"
_LMC_REMOTE_WRITE = "lmcache:num_remote_write_bytes_total"
_LMC_STORED = "lmcache:num_stored_tokens_total"
_LMC_PER_WORKER = (_LMC_HIT, _LMC_REMOTE_READ, _LMC_REMOTE_WRITE, _LMC_STORED)
# LMCache multiprocess (MP) server lookup counters, one series set for all TP workers.
# The *_keys counters count keys (one per object group, KV rank and chunk) of the range
# past the engine's own prefix cache: keys L1 held and keys loaded from L2 into L1.
_MP_L1_KEYS = "lmcache_mp_lookup_hit_l1_keys_total"
_MP_L2_KEYS = "lmcache_mp_lookup_hit_l2_keys_total"
_MP_L2_LOADED = "lmcache_mp_l2_prefetch_load_completed_chunks_total"
_MP_FIELDS = ("lmcache_mp_l1_keys", "lmcache_mp_l2_keys", "lmcache_mp_l2_loaded_keys")
# L2 hit keys above loaded L2 chunks by more than this factor give a warning.
_MP_KEYS_TOLERANCE = 1.01
# Implied per-worker KV size outside [1/x, x] of the given one gives a note.
_KV_SIZE_TOLERANCE = 1.2
# LMCache's default chunk size (chunk_size in lmcache.yaml).
DEFAULT_CHUNK_TOKENS = 256
L1_L2_NOT_VALIDATED = ("L1/L2 split not validated against real LMCache output (only synthetic "
                       "LMCache counters were tested)")
L1_L2_MP_NOT_VALIDATED = ("L1/L2 split from LMCache MP lookup keys not validated against Valkey "
                          "read counts (tested on one real LMCache MP /metrics capture only)")
# Where to get LMCache's tier counters; the same text for both LMCache modes, because a
# trace without them does not tell which mode the engine ran in.
_LMCACHE_URL_HINT = ("record with --extra-metrics-url: in LMCache multiprocess mode the MP "
                     "server's HTTP /metrics (lmcache server --http-port, e.g. "
                     "http://127.0.0.1:8080/metrics), else LMCache's internal API server of "
                     "each TP worker")

TOKEN_FIELDS = ("prompt_tokens", "l0_tokens", "external_tokens", "l1_tokens", "l2_tokens",
                "computed_tokens")
SUM_FIELDS = TOKEN_FIELDS + ("requests", "lmcache_remote_read_bytes", "lmcache_remote_write_bytes",
                             "lmcache_retrieved_tokens")

Increments = Dict[str, float]


class _Keys:
    """Cache of parsed series keys: ``name{a="b"}`` -> (name, labels)."""

    def __init__(self) -> None:
        self._cache: Dict[str, Tuple[str, Dict[str, str]]] = {}

    def parse(self, key: str) -> Tuple[str, Dict[str, str]]:
        hit = self._cache.get(key)
        if hit is None:
            hit = metrics_mod.split_key(key)
            self._cache[key] = hit
        return hit


def node_increments(samples: Sequence[metrics_mod.MetricSample],
                    window_start: Optional[float] = None,
                    late: Optional[Dict[str, Tuple[float, str]]] = None,
                    ) -> List[Tuple[float, Increments]]:
    """Counter increments between consecutive samples, as ``(later ts, {key: increment})``.

    See :func:`kvtrace.metrics.counter_increments` for extra URLs that miss a scrape and
    for series that appear after the first sample.
    """
    return metrics_mod.counter_increments(samples, window_start=window_start, late=late)


def _add(target: Increments, inc: Increments) -> None:
    for key, value in inc.items():
        target[key] = target.get(key, 0.0) + value


def _quantile_from_buckets(buckets: Dict[float, float], q: float) -> Optional[float]:
    """``histogram_quantile``: linear interpolation inside the bucket that holds rank q."""
    if not buckets:
        return None
    bounds = sorted(buckets)
    total = buckets[bounds[-1]]
    if total <= 0:
        return None
    rank = q * total
    lower, below = 0.0, 0.0
    for le in bounds:
        count = buckets[le]
        if count >= rank:
            if math.isinf(le):
                return lower
            if count == below:
                return le
            return lower + (le - lower) * (rank - below) / (count - below)
        lower, below = le, count
    return lower


class _NodeMethod:
    """Which counters a node's trace has, chosen once per node so every interval agrees."""

    def __init__(self, keys: Iterable[str], parser: _Keys):
        names = set()
        self.by_source = False
        for key in keys:
            name, labels = parser.parse(key)
            names.add(name)
            if name == _BY_SOURCE and "source" in labels:
                self.by_source = True
        self.names = names
        self.l0_name = next((n for n in _L0_HITS if n in names), None)
        self.has_cached = _CACHED in names
        self.sglang = _SGLANG_CACHED in names
        self.lmcache_remote = _LMC_REMOTE_READ in names
        # The non-MP counters win when both are present, so traces of that mode keep
        # their split.
        self.lmcache_mp = (not self.lmcache_remote and _MP_L1_KEYS in names
                           and _MP_L2_KEYS in names)

    def describe(self) -> Dict[str, Any]:
        if self.by_source:
            l0 = external = f"{_BY_SOURCE}{{source}}"
        elif self.l0_name and self.has_cached:
            l0, external = self.l0_name, f"{_CACHED} - {self.l0_name}"
        elif self.l0_name:
            l0 = self.l0_name
            external = _EXTERNAL_HITS if _EXTERNAL_HITS in self.names else None
        elif self.sglang:
            l0, external = _SGLANG_CACHED, None
        else:
            l0 = external = None
        if self.lmcache_remote:
            l2 = _LMC_REMOTE_READ
        elif self.lmcache_mp:
            l2 = f"external x {_MP_L2_KEYS} / ({_MP_L1_KEYS} + {_MP_L2_KEYS})"
        else:
            l2 = None
        return {"l0": l0, "external": external, "l2": l2,
                "requests": next((n for n in _REQUESTS if n in self.names), None),
                "ttft": next((n for n in _TTFT if n + "_bucket" in self.names), None)}


def _sum_named(inc: Increments, parser: _Keys, name: str) -> Optional[float]:
    values = [v for k, v in inc.items() if parser.parse(k)[0] == name]
    return sum(values) if values else None


def _per_worker_mean(values: Dict[str, float]) -> Optional[float]:
    return sum(values.values()) / len(values) if values else None


def _mp_l2_share(l1_keys: float, l2_keys: float) -> Optional[float]:
    """Share of L2 in LMCache MP mode: L2 hit keys / (L1 + L2 hit keys), None without keys.

    Args:
        l1_keys: increment of ``lmcache_mp_lookup_hit_l1_keys_total`` over the window.
        l2_keys: increment of ``lmcache_mp_lookup_hit_l2_keys_total`` over the window.

    vLLM's external tokens give the size of L1 + L2 and this share splits them. The MP
    server exports no L2 byte counter, and in LMCache PR 5556 ``lookup_hit_tokens`` and
    ``lookup_hit_l1_tokens`` include the prefix that the engine already held (L0), so a
    token ratio of L1 to L2 holds L0. The ``*_keys`` counters (one key per object group,
    KV rank and chunk, the same unit for both tiers) start at the chunk that holds the
    end of the engine's prefix, so they hold at most that one boundary chunk of L0 per
    lookup, and their ratio needs no KV size and no TP size. The ratio assumes that the
    boundary chunks have the same tier mix as the other hit chunks; on a real test run
    the keys, as tokens, were 8.2% above vLLM's external tokens. LMCache counts keys when
    a prefetch ends and vLLM counts tokens when the prompt is done, minutes later for long
    prefetches, so the share is taken over the whole window and not per interval.
    """
    keys = l1_keys + l2_keys
    return l2_keys / keys if keys > 0 else None


def derive(inc: Increments, method: _NodeMethod, parser: _Keys,
           worker_kv_bytes_per_token: Optional[int],
           mp_l2_share: Optional[float] = None) -> Dict[str, Any]:
    """Tier token counts from the summed increments of one interval (or a whole window).

    ``mp_l2_share`` is the node's L2 share over the window in LMCache MP mode (see
    :func:`_mp_l2_share`); without it, MP-mode L1 and L2 are None.
    """
    by_source: Dict[str, float] = {}
    buckets: Dict[float, float] = {}
    ttft_sum = 0.0
    per_worker: Dict[str, Dict[str, float]] = {name: {} for name in _LMC_PER_WORKER}
    for key, value in inc.items():
        name, labels = parser.parse(key)
        if name == _BY_SOURCE and "source" in labels:
            by_source[labels["source"]] = by_source.get(labels["source"], 0.0) + value
        elif name.endswith("time_to_first_token_seconds_bucket") and "le" in labels:
            le = float(labels["le"])
            buckets[le] = buckets.get(le, 0.0) + value
        elif name.endswith("time_to_first_token_seconds_sum"):
            ttft_sum += value
        elif name in per_worker and labels.get("role", "worker") == "worker":
            # LMCache's scheduler exports the same families with role="scheduler" (and a
            # worker_id that can equal a worker's); only TP workers retrieve and store KV.
            # Traces recorded before the role label was kept have no role.
            per = per_worker[name]
            worker = labels.get("worker_id", "")
            per[worker] = per.get(worker, 0.0) + value
    lmc_hits, lmc_read = per_worker[_LMC_HIT], per_worker[_LMC_REMOTE_READ]
    prompt = next((v for v in (_sum_named(inc, parser, n) for n in _PROMPT) if v is not None), None)
    l0 = external = computed = None
    if method.by_source:
        l0, external = by_source.get(SRC_L0, 0.0), by_source.get(SRC_EXTERNAL, 0.0)
        computed = by_source.get(SRC_COMPUTE, 0.0)
    elif method.l0_name:
        l0 = _sum_named(inc, parser, method.l0_name) or 0.0
        if method.has_cached:
            # prompt_tokens_cached = L0 + external, counted when the prompt is done, the same
            # moment as by_source; prefix_cache_hits counts at lookup, so the split can be
            # off by the hits of requests still in prefill (0.02% of external on a test run).
            external = max(0.0, (_sum_named(inc, parser, _CACHED) or 0.0) - l0)
        elif _EXTERNAL_HITS in method.names:
            external = _sum_named(inc, parser, _EXTERNAL_HITS) or 0.0
    elif method.sglang:
        l0 = _sum_named(inc, parser, _SGLANG_CACHED) or 0.0
    if computed is None and prompt is not None and l0 is not None:
        computed = max(0.0, prompt - l0 - (external or 0.0))
    remote_read = sum(lmc_read.values()) if method.lmcache_remote else None
    l2 = l1 = None
    mp: Dict[str, Optional[float]] = {}
    if method.lmcache_mp:
        mp = {"lmcache_mp_l1_keys": _sum_named(inc, parser, _MP_L1_KEYS) or 0.0,
              "lmcache_mp_l2_keys": _sum_named(inc, parser, _MP_L2_KEYS) or 0.0,
              "lmcache_mp_l2_loaded_keys": _sum_named(inc, parser, _MP_L2_LOADED)}
        if external is not None and mp_l2_share is not None:
            l2 = external * mp_l2_share
            l1 = external - l2
    if lmc_read and worker_kv_bytes_per_token:
        # Every TP worker reads its own copy of the chunk slice it holds. A worker's slice is
        # not 1/TP of the model when KV heads are copied (TP > KV heads) or not split (MLA),
        # so tokens = bytes per worker / bytes per token of one worker. The maximum, not the
        # mean: with MLA, LMCache reads on rank 0 only (save_only_first_rank) and the other
        # workers export 0.
        l2 = max(lmc_read.values()) / worker_kv_bytes_per_token
        if external is not None:
            l1 = max(0.0, external - l2)
    requests = next((v for v in (_sum_named(inc, parser, n) for n in _REQUESTS) if v is not None),
                    None)
    lmc_write, lmc_stored = per_worker[_LMC_REMOTE_WRITE], per_worker[_LMC_STORED]
    # LMCache stores a new chunk to every backend, so on the worker that writes most, remote
    # write bytes / stored tokens is the KV size one worker keeps per token.
    writer = max(lmc_write, key=lambda w: lmc_write[w], default=None)
    implied = (lmc_write[writer] / lmc_stored[writer]
               if writer is not None and lmc_write[writer] > 0 and lmc_stored.get(writer, 0) > 0
               else None)
    row: Dict[str, Any] = {
        "prompt_tokens": prompt, "l0_tokens": l0, "external_tokens": external,
        "l1_tokens": l1, "l2_tokens": l2, "computed_tokens": computed, "requests": requests,
        "lmcache_remote_read_bytes": remote_read,
        "lmcache_remote_write_bytes": (sum(lmc_write.values()) if method.lmcache_remote and lmc_write
                                       else None),
        # Each TP worker retrieves the same tokens, so the per-worker mean is the token count.
        "lmcache_retrieved_tokens": _per_worker_mean(lmc_hits),
        "lmcache_implied_worker_kv_bytes_per_token": implied,
        "_buckets": buckets, "_ttft_sum": ttft_sum,
    }
    # Only in MP mode, so the rows of other traces keep their fields.
    row.update(mp)
    return row


def _finish(row: Dict[str, Any], t_start: float, t_end: float) -> Dict[str, Any]:
    """Add shares, rates and TTFT quantiles; drop the private bucket fields."""
    buckets = row.pop("_buckets", {})
    ttft_sum = row.pop("_ttft_sum", 0.0)
    out = {"t_start": t_start, "t_end": t_end, **row}
    prompt = row.get("prompt_tokens")
    for name in ("l0", "external", "l1", "l2", "computed"):
        value = row.get(f"{name}_tokens")
        out[f"{name}_pct"] = (100.0 * value / prompt) if prompt and value is not None else None
    duration = t_end - t_start
    out["requests_per_s"] = (row["requests"] / duration
                             if row.get("requests") is not None and duration > 0 else None)
    out["ttft_p50_s"] = _quantile_from_buckets(buckets, 0.5)
    out["ttft_p90_s"] = _quantile_from_buckets(buckets, 0.9)
    count = buckets.get(math.inf, 0.0)
    out["ttft_mean_s"] = ttft_sum / count if count else None
    return out


def _sum_rows(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    total: Dict[str, Any] = {}
    for field in SUM_FIELDS:
        values = [r[field] for r in rows if r.get(field) is not None]
        total[field] = sum(values) if values else None
    for field in _MP_FIELDS:
        if any(field in r for r in rows):
            values = [r[field] for r in rows if r.get(field) is not None]
            total[field] = sum(values) if values else None
    buckets: Dict[float, float] = {}
    for r in rows:
        for le, v in r.get("_buckets", {}).items():
            buckets[le] = buckets.get(le, 0.0) + v
    total["_buckets"] = buckets
    total["_ttft_sum"] = sum(r.get("_ttft_sum", 0.0) for r in rows)
    return total


def _late_notes(node: str, late: Dict[str, Tuple[float, str]]) -> List[str]:
    """Notes for counter series that first appeared after the node's first sample."""
    notes = []
    for how, text in (("first_value", "counted from their first value, so their window is "
                                      "shorter (endpoint not scraped before and no *_created "
                                      "time in the window)"),
                      ("created", "counted from 0 (their *_created time is in the window)")):
        keys = sorted(k for k, (_, h) in late.items() if h == how)
        if keys:
            first = min(late[k][0] for k in keys)
            more = f" and {len(keys) - 3} more" if len(keys) > 3 else ""
            notes.append(f"{node}: {len(keys)} counter series started late (first at "
                         f"{_fmt_ts(first)} UTC) and are {text}: {', '.join(keys[:3])}{more}")
    return notes


def split_recorders(samples: Dict[Tuple[str, Optional[str]], List[metrics_mod.MetricSample]],
                    ) -> Tuple[Dict[str, List[metrics_mod.MetricSample]], List[str]]:
    """Join or split the sample series of each ``(node, rid)`` into report nodes.

    ``rid`` is the recorder instance id of the ``M`` records (None in old traces). When the
    time ranges of a node's recorders do not overlap (a recorder restart), their samples
    are one series in time order, as before ``rid`` existed: the last sample before the
    restart is the baseline of the first one after it, so no increment is lost, and an
    engine restart in between is a counter reset (see
    :func:`kvtrace.metrics.counter_increments`). When two of them overlap, two recorders
    used the same node id, and diffs between their samples are meaningless: each one
    becomes its own report node ``<node>@<rid>`` and a warning note names the node.

    Returns ``{report node: samples}`` and the notes.
    """
    by_node: Dict[str, Dict[Optional[str], List[metrics_mod.MetricSample]]] = {}
    for (node, rid), series in samples.items():
        by_node.setdefault(node, {})[rid] = series
    out: Dict[str, List[metrics_mod.MetricSample]] = {}
    notes: List[str] = []
    for node, by_rid in by_node.items():
        spans = sorted(((min(s[0] for s in series), max(s[0] for s in series), rid)
                        for rid, series in by_rid.items() if series), key=lambda sp: sp[:2])
        overlap = any(spans[i][0] <= max(sp[1] for sp in spans[:i]) for i in range(1, len(spans)))
        if not overlap:
            out[node] = [s for _, _, rid in spans for s in by_rid[rid]]
            continue
        names = []
        for _, _, rid in spans:
            name = f"{node}@{rid if rid is not None else 'no-rid'}"
            out[name] = by_rid[rid]
            names.append(name)
        notes.append(f"WARNING {node}: {len(spans)} recorders wrote metrics with this node id at "
                     f"the same time; their counters are kept apart as nodes {', '.join(names)}. "
                     "Give each recorder its own --node-id")
    return out, notes


# A configured extra URL that answered in fewer samples than this share gives a warning.
_URL_COVERAGE_MIN = 0.5


def url_coverage_notes(node: str, samples: Sequence[metrics_mod.MetricSample],
                       start: Optional[float] = None, end: Optional[float] = None) -> List[str]:
    """Warnings for extra metrics URLs that answered in few of a node's samples.

    A URL is known from the ``kvtrace_url`` labels of the samples and from ``extra_ts``.
    It answered in a sample when it is in that sample's ``extra_ts`` or labels one of
    its keys. A URL that rarely answers (a wrong LMCache socket path, for example) makes
    its counters, and so L2, low. Only samples in ``[start, end]`` count. A URL that never
    answered has no label in the trace and cannot be found here.
    """
    total = 0
    answered: Dict[str, int] = {}
    sources: Dict[str, Optional[str]] = {}
    for row in samples:
        ts = row[0]
        if (start is not None and ts < start) or (end is not None and ts > end):
            continue
        total += 1
        urls = set(row[2] or ()) if len(row) > 2 else set()  # type: ignore[misc]
        for key in row[1]:
            if key not in sources:
                sources[key] = metrics_mod.source_url(key)
            url = sources[key]
            if url is not None:
                urls.add(url)
        for url in urls:
            answered[url] = answered.get(url, 0) + 1
    notes = []
    for url in sorted(answered):
        n = answered[url]
        if n < _URL_COVERAGE_MIN * total:
            notes.append(f"WARNING {node}: extra metrics URL {url} answered in {n} of {total} "
                         f"samples ({100.0 * n / total:.0f}%); its counters (LMCache L2) can be "
                         "low. Check the URL and the recorder log")
    return notes


def _overlap_bound(total: Dict[str, Any], chunk_tokens: int) -> Optional[float]:
    """Upper bound of L0 tokens counted as L2: one chunk per request, at most all of L2.

    LMCache counts the bytes of every chunk it fetches in full, so the part of the boundary
    chunk that vLLM already had in L0 also counts as L2 (and is taken from L1). The number
    of requests that hit L2 is not known, so all finished requests are used.
    """
    l2, requests = total.get("l2_tokens"), total.get("requests")
    if l2 is None or requests is None:
        return None
    return min(l2, requests * chunk_tokens)


def _overlap_note(name: str, total: Dict[str, Any], chunk_tokens: int) -> Optional[str]:
    bound = total.get("l2_chunk_overlap_bound_tokens")
    if bound is None:
        return None
    prompt = total.get("prompt_tokens")
    share = f" ({100.0 * bound / prompt:.2f} points of prompt tokens)" if prompt else ""
    return (f"{name}: L2 can include up to one LMCache chunk ({chunk_tokens} tokens) per request "
            f"that hits L2, because LMCache counts each fetched chunk in full and the L0 part "
            f"of the boundary chunk then counts as L2 (and L1 is low by the same amount). "
            f"Upper bound with {total['requests']:,.0f} finished requests: {bound:,.0f} "
            f"tokens{share}. Suspected from the LMCache 0.5.5 source; not measured")


def measure_tiers(records: Iterable[Dict[str, Any]], interval: float = 60.0,
                  start: Optional[float] = None, end: Optional[float] = None,
                  worker_kv_bytes_per_token: Optional[int] = None,
                  sources: Optional[List[str]] = None,
                  worker_kv_source: Optional[str] = None,
                  chunk_tokens: int = DEFAULT_CHUNK_TOKENS) -> Dict[str, Any]:
    """Per-interval and total tier split for every node and the fleet.

    ``worker_kv_source`` says where ``worker_kv_bytes_per_token`` came from (for the note).
    ``chunk_tokens`` is LMCache's chunk size, for the bound in ``l2_chunk_overlap_bound_tokens``.
    """
    if interval <= 0:
        raise ValueError("--interval must be > 0")
    if chunk_tokens <= 0:
        raise ValueError("--chunk-tokens must be > 0")
    samples: Dict[Tuple[str, Optional[str]], List[metrics_mod.MetricSample]] = {}
    for rec in records:
        if rec.get("t") != "M" or not isinstance(rec.get("ts"), (int, float)):
            continue
        # One series per recorder instance (rid): two recorders with the same node id, or
        # one recorder before and after a restart, must not be diffed against each other.
        rid = rec.get("rid")
        # The extra URLs that answered (extra_ts) tell a partial scrape from a reset.
        samples.setdefault((str(rec.get("node", "")), None if rid is None else str(rid)), []).append(
            (float(rec["ts"]), rec.get("metrics") or {}, tuple(rec.get("extra_ts") or ())))
    by_node, recorder_notes = split_recorders(samples)
    parser = _Keys()
    per_node: Dict[str, List[Tuple[float, Increments]]] = {}
    late_by_node: Dict[str, Dict[str, Tuple[float, str]]] = {}
    for node, node_samples in by_node.items():
        # A sample before ``start`` is kept as the baseline of the first increment; the
        # first sample of a node has no increment.
        late: Dict[str, Tuple[float, str]] = {}
        incs = [(ts, inc) for ts, inc in node_increments(node_samples, window_start=start, late=late)
                if inc and (start is None or ts >= start) and (end is None or ts <= end)]
        if incs:
            per_node[node] = incs
            late_by_node[node] = {k: v for k, v in late.items()
                                  if (start is None or v[0] >= start) and (end is None or v[0] <= end)}
    notes: List[str] = list(recorder_notes)
    result: Dict[str, Any] = {
        "tool": TOOL_NAME, "format": "kvtrace-tiers/1",
        "inputs": {"sources": sources or [], "interval_s": interval, "start": start, "end": end,
                   "worker_kv_bytes_per_token": worker_kv_bytes_per_token,
                   "worker_kv_source": worker_kv_source, "chunk_tokens": chunk_tokens},
        "l1_l2_validated": False,
        "nodes": {}, "fleet": None, "notes": notes,
    }
    if worker_kv_bytes_per_token:
        notes.append(f"L2 tokens use {worker_kv_bytes_per_token:,} KV bytes per token per TP worker "
                     f"(from {worker_kv_source or 'the caller'}); a wrong value scales L2 by the "
                     "same factor (fp8 KV is half of bf16)")
    if not per_node:
        notes.append("no metrics (M) records in the window; record with --metrics-url")
        return result
    first_ts = {node: max(start or -math.inf, min(s[0] for s in by_node[node]))
                for node in per_node}
    t0 = start if start is not None else min(first_ts.values())
    t_last = max(incs[-1][0] for incs in per_node.values())
    fleet_intervals: Dict[int, List[Dict[str, Any]]] = {}
    fleet_totals: List[Dict[str, Any]] = []
    mp_nodes = set()
    for node in sorted(per_node):
        incs = per_node[node]
        method = _NodeMethod({k for _, inc in incs for k in inc}, parser)
        desc = method.describe()
        by_idx: Dict[int, Increments] = {}
        total: Increments = {}
        for ts, inc in incs:
            _add(by_idx.setdefault(int((ts - t0) // interval), {}), inc)
            _add(total, inc)
        intervals = []
        node_t0, node_t1 = first_ts[node], incs[-1][0]
        # One share for the whole window: LMCache counts keys when a prefetch ends and
        # vLLM counts external when the prompt is done, often in another interval.
        share = (_mp_l2_share(_sum_named(total, parser, _MP_L1_KEYS) or 0.0,
                              _sum_named(total, parser, _MP_L2_KEYS) or 0.0)
                 if method.lmcache_mp else None)
        if method.lmcache_mp:
            mp_nodes.add(node)
        for idx in sorted(by_idx):
            row = derive(by_idx[idx], method, parser, worker_kv_bytes_per_token, share)
            fleet_intervals.setdefault(idx, []).append(row)
            lo = max(t0 + idx * interval, node_t0)
            hi = min(t0 + (idx + 1) * interval, node_t1)
            intervals.append(_finish(dict(row, _buckets=dict(row["_buckets"])), lo, hi))
        total_row = derive(total, method, parser, worker_kv_bytes_per_token, share)
        fleet_totals.append(total_row)
        result["nodes"][node] = {"counters": desc, "samples": len(incs) + 1,
                                 "total": _finish(dict(total_row, _buckets=dict(total_row["_buckets"])),
                                                  node_t0, node_t1),
                                 "intervals": intervals}
        notes.extend(_late_notes(node, late_by_node.get(node, {})))
        notes.extend(url_coverage_notes(node, by_node[node], start=start, end=end))
        if desc["l0"] is None:
            notes.append(f"{node}: no prefix-cache counters; L0 unknown")
        elif not method.by_source:
            notes.append(f"{node}: no prompt_tokens_by_source{{source}} series (recorded by an older "
                         f"kvtrace or vLLM); L0/external from {desc['l0']} and {desc['external']}")
        if method.lmcache_remote and not worker_kv_bytes_per_token:
            notes.append(f"{node}: LMCache remote-read bytes present; give "
                         "--worker-kv-bytes-per-token (or --model-config/--preset with --tp) "
                         "for the L1/L2 split")
        elif method.lmcache_remote:
            l2, got = total_row["l2_tokens"], total_row["lmcache_retrieved_tokens"]
            if l2 is not None and got is not None and l2 > got * 1.01:
                notes.append(f"{node}: L2 tokens {l2:,.0f} exceed LMCache retrieved tokens "
                             f"{got:,.0f}; check --worker-kv-bytes-per-token / --tp")
            implied = total_row["lmcache_implied_worker_kv_bytes_per_token"]
            if implied is not None and not (1 / _KV_SIZE_TOLERANCE
                                            <= implied / worker_kv_bytes_per_token
                                            <= _KV_SIZE_TOLERANCE):
                notes.append(f"WARNING {node}: LMCache remote write bytes / stored tokens imply "
                             f"{implied:,.0f} KV bytes per token per worker, not the "
                             f"{worker_kv_bytes_per_token:,} used; L2 may be off by "
                             f"{implied / worker_kv_bytes_per_token:.2f}x (check the KV dtype "
                             "and --tp)")
        elif method.lmcache_mp:
            l2_keys, loaded = total_row["lmcache_mp_l2_keys"], total_row["lmcache_mp_l2_loaded_keys"]
            if share is None and total_row["external_tokens"]:
                notes.append(f"{node}: LMCache MP counted no L1 or L2 hit keys in the window; "
                             "L1 and L2 are reported together as external")
            # LMCache counts an L2 hit key only for a chunk that its prefetch loaded, so
            # more hit keys than loaded chunks means the counters disagree; a failed load
            # shortens the hit prefix and does not show here.
            elif loaded is None:
                if l2_keys:
                    notes.append(f"{node}: no {_MP_L2_LOADED} series in the trace (no L2 load "
                                 "yet, or another LMCache version); L2 hit keys not checked "
                                 "against loaded chunks")
            elif l2_keys > loaded * _MP_KEYS_TOLERANCE:
                notes.append(f"WARNING {node}: LMCache MP counted {l2_keys:,.0f} L2 hit keys but "
                             f"{loaded:,.0f} chunks loaded from L2 ({_MP_L2_LOADED}); the "
                             "counters disagree (scrape timing or another LMCache version), so "
                             "check the L1/L2 split")
        elif desc["external"] is not None:
            answered = sorted({url for row in by_node[node] if len(row) > 2
                               and (start is None or row[0] >= start)
                               and (end is None or row[0] <= end)
                               for url in (row[2] or ())})  # type: ignore[misc]
            if answered:
                # The URL was given and answered; the recording kvtrace dropped its series
                # (before lmcache_mp_* was in the allowlist) or the URL is not LMCache.
                hint = (f"the extra metrics URL {', '.join(answered)} answered but gave no "
                        "LMCache tier counters; record again with this kvtrace, or check "
                        f"that the URL is LMCache's ({_LMCACHE_URL_HINT})")
            else:
                hint = _LMCACHE_URL_HINT
            notes.append(f"{node}: no lmcache:num_remote_read_bytes or lmcache_mp_lookup_hit_*_keys; "
                         f"L1 and L2 are reported together as external ({hint})")
    rows = []
    for idx in sorted(fleet_intervals):
        rows.append(_finish(_sum_rows(fleet_intervals[idx]), t0 + idx * interval,
                            min(t0 + (idx + 1) * interval, t_last)))
    fleet_start = min(first_ts.values())
    result["fleet"] = {"nodes": sorted(per_node), "t0": t0,
                       "total": _finish(_sum_rows(fleet_totals), fleet_start, t_last),
                       "intervals": rows}
    fleet_total = result["fleet"]["total"]
    for name, n in result["nodes"].items():
        # MP-mode L2 is a share of vLLM's external tokens, so it cannot hold the L0 part of
        # a chunk that LMCache reads in full.
        n["total"]["l2_chunk_overlap_bound_tokens"] = (
            None if name in mp_nodes else _overlap_bound(n["total"], chunk_tokens))
    if not mp_nodes:
        fleet_total["l2_chunk_overlap_bound_tokens"] = _overlap_bound(fleet_total, chunk_tokens)
    else:
        # Only the nodes whose L2 comes from LMCache bytes can include L0 tokens.
        bounds = [n["total"]["l2_chunk_overlap_bound_tokens"] for name, n in result["nodes"].items()
                  if name not in mp_nodes and n["total"]["l2_chunk_overlap_bound_tokens"] is not None]
        fleet_total["l2_chunk_overlap_bound_tokens"] = sum(bounds) if bounds else None
        result["l1_l2_mp_nodes"] = sorted(mp_nodes)
    if fleet_total.get("l2_tokens") is not None:
        split_nodes = {name for name, n in result["nodes"].items()
                       if n["total"].get("l2_tokens") is not None}
        if split_nodes & mp_nodes:
            notes.append(L1_L2_MP_NOT_VALIDATED + "; see README 'kvtrace tiers' limits")
        if split_nodes - mp_nodes:
            notes.append(L1_L2_NOT_VALIDATED + "; see README 'kvtrace tiers' limits")
        note = _overlap_note("fleet", fleet_total, chunk_tokens)
        if note:
            notes.append(note)
    return result


# ---- simulated values next to measured ones ---------------------------------
def compare_with_sim(result: Dict[str, Any], sim: Dict[str, Any],
                     capacity: Optional[str] = None) -> Dict[str, Any]:
    """Put a ``kvtrace simulate`` capacity row next to the measured fleet totals.

    The simulator's host tier is L1 and its saved tokens are L2. Its L2 hits per minute
    are added to the fleet intervals as ``sim_l2_tokens``, minute 0 at the measured start.
    """
    caps = sim.get("capacities") or []
    if not caps:
        raise ValueError("sim.json has no capacities")
    cap = caps[0] if capacity is None else next((c for c in caps if c.get("label") == capacity), None)
    if cap is None:
        raise ValueError(f"capacity {capacity!r} not in sim.json "
                         f"({', '.join(str(c.get('label')) for c in caps)})")
    fleet = result.get("fleet") or {}
    total = fleet.get("total") or {}
    prompt = total.get("prompt_tokens")
    sim_l1 = (cap.get("host_tier") or {}).get("hit_tokens")
    sim_l2 = cap.get("saved_tokens")
    sim_ext = None if sim_l1 is None and sim_l2 is None else (sim_l1 or 0) + (sim_l2 or 0)

    def pct(v: Optional[float]) -> Optional[float]:
        return 100.0 * v / prompt if prompt and v is not None else None

    window = (sim.get("trace") or {}).get("window") or {}
    out = {
        "capacity": cap.get("label"),
        "sim_window": {"start": window.get("start"), "end": window.get("end")},
        "measured_window": {"start": total.get("t_start"), "end": total.get("t_end")},
        "simulated": {"l1_tokens": sim_l1, "l2_tokens": sim_l2, "external_tokens": sim_ext,
                      "l1_pct": pct(sim_l1), "l2_pct": pct(sim_l2), "external_pct": pct(sim_ext)},
        "measured": {k: total.get(k) for k in ("l1_tokens", "l2_tokens", "external_tokens",
                                               "l1_pct", "l2_pct", "external_pct")},
        "note": "percentages use the measured prompt tokens; check that both windows match",
    }
    series = cap.get("hit_tokens_per_minute") or []
    intervals = fleet.get("intervals") or []
    if series and intervals:
        # A prediction usually comes from another run (simulate the OFF run, measure the ON
        # run), so minute i of the simulation is placed i minutes after the measured start.
        # fleet.t0 is the origin of the interval index in measure_tiers; the first row can
        # start one or more intervals after it (no increment yet in interval 0).
        t0 = fleet.get("t0")
        if t0 is None:
            t0 = result["inputs"].get("start")
        if t0 is None:
            t0 = intervals[0]["t_start"]
        step = result["inputs"]["interval_s"]
        by_idx: Dict[int, float] = {}
        for i, tokens in enumerate(series):
            idx = int((60 * i) // step)
            by_idx[idx] = by_idx.get(idx, 0.0) + tokens
        for row in intervals:
            row["sim_l2_tokens"] = by_idx.get(int(round((row["t_start"] - t0) / step)), 0.0)
    return out


# ---- rendering ---------------------------------------------------------------
def _fmt_ts(ts: Optional[float]) -> str:
    return "-" if ts is None else time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))


def _fmt_num(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:,.0f}"


def _fmt_pct(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.2f}%"


def _fmt_s(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.3f}"


def _fmt_rate(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.2f}"


_TABLE_HEAD = ["start (UTC)", "node", "prompt tokens", "L0", "external (L1+L2)", "L1", "L2",
               "computed", "req/s", "TTFT p50 s", "TTFT p90 s"]


def _table_row(row: Dict[str, Any], node: str) -> List[str]:
    return [_fmt_ts(row["t_start"]), node, _fmt_num(row.get("prompt_tokens")),
            _fmt_pct(row.get("l0_pct")), _fmt_pct(row.get("external_pct")),
            _fmt_pct(row.get("l1_pct")), _fmt_pct(row.get("l2_pct")),
            _fmt_pct(row.get("computed_pct")), _fmt_rate(row.get("requests_per_s")),
            _fmt_s(row.get("ttft_p50_s")), _fmt_s(row.get("ttft_p90_s"))]


def _md(rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def render_markdown(result: Dict[str, Any]) -> str:
    fleet = result.get("fleet")
    mp_nodes = set(result.get("l1_l2_mp_nodes") or ())
    caveats = []
    if mp_nodes:
        caveats.append(f"**{L1_L2_MP_NOT_VALIDATED}.** On {', '.join(sorted(mp_nodes))}, L1 and L2 "
                       "split vLLM's external tokens by LMCache's L1 and L2 hit keys; see Notes.")
    if not mp_nodes or set(result.get("nodes") or ()) - mp_nodes:
        caveats.append(f"**{L1_L2_NOT_VALIDATED}.** L2 can also include up to one LMCache chunk "
                       f"({result['inputs'].get('chunk_tokens') or DEFAULT_CHUNK_TOKENS} tokens) "
                       "per request that hits L2; see Notes.")
    out = ["# Measured tier split", "",
           f"Interval {result['inputs']['interval_s']:g} s. L0 = GPU prefix cache, L1 = host CPU "
           "tier, L2 = remote tier; shares are of prompt tokens.", ""]
    for caveat in caveats:
        out += [caveat, ""]
    if not fleet:
        return "\n".join(out + [f"- {n}" for n in result["notes"]]) + "\n"
    totals = [_TABLE_HEAD] + [_table_row(n["total"], name) for name, n in result["nodes"].items()]
    if len(result["nodes"]) > 1:
        totals.append(_table_row(fleet["total"], "fleet"))
    out += ["## Totals", "", _md(totals), ""]
    totals_tokens = [["node", "prompt", "L0", "external", "L1", "L2", "computed", "requests"]]
    for name, n in list(result["nodes"].items()) + [("fleet", fleet)]:
        t = n["total"]
        totals_tokens.append([name] + [_fmt_num(t.get(f)) for f in TOKEN_FIELDS + ("requests",)])
    out += [_md(totals_tokens), ""]
    cmp_ = result.get("compare")
    if cmp_:
        rows = [["", "L1 tokens", "L2 tokens", "external tokens", "L1", "L2", "external"]]
        for label in ("simulated", "measured"):
            d = cmp_[label]
            rows.append([label] + [_fmt_num(d.get(k)) for k in ("l1_tokens", "l2_tokens",
                                                                 "external_tokens")]
                        + [_fmt_pct(d.get(k)) for k in ("l1_pct", "l2_pct", "external_pct")])
        out += [f"## Simulated vs measured (capacity {cmp_['capacity']})", "", _md(rows), "",
                cmp_["note"], ""]
    head = _TABLE_HEAD + (["sim L2 tokens"] if any("sim_l2_tokens" in r for r in fleet["intervals"])
                          else [])
    rows = [head]
    for r in fleet["intervals"]:
        rows.append(_table_row(r, "fleet") + ([_fmt_num(r.get("sim_l2_tokens"))]
                                              if len(head) > len(_TABLE_HEAD) else []))
    out += ["## Fleet per interval", "", _md(rows), ""]
    req = result.get("requests")
    if req:
        from .reqtiers import render_markdown as render_requests

        out += [render_requests(req), ""]
    if result["notes"]:
        out += ["## Notes", ""] + [f"- {n}" for n in result["notes"]] + [""]
    return "\n".join(out)


CSV_FIELDS = ["node", "t_start", "t_end", *SUM_FIELDS, "l0_pct", "external_pct", "l1_pct",
              "l2_pct", "computed_pct", "requests_per_s", "ttft_p50_s", "ttft_p90_s", "ttft_mean_s",
              "sim_l2_tokens"]


def render_csv(result: Dict[str, Any]) -> str:
    """One row per node and interval, plus fleet rows (node ``fleet``)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for name, n in result.get("nodes", {}).items():
        for row in n["intervals"]:
            writer.writerow({"node": name, **row})
    for row in (result.get("fleet") or {}).get("intervals", []):
        writer.writerow({"node": "fleet", **row})
    return buf.getvalue()
