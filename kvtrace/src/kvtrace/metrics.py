"""Minimal Prometheus text-format parser and scraper for engine metrics.

No ``prometheus_client`` dependency. Only metric families that match an
allowlist are kept, and every label except those in ``KEPT_LABELS`` is
dropped. Gauge series that collapse onto the same key are averaged (for example a
per-TP-rank hit rate). Counter series (``_total``/``_sum``/``_count``/``_bucket``) whose
dropped labels differ stay apart under a :data:`SERIES_LABEL` label, so deltas are taken
per series; :func:`counter_increments` sums the increments. Histogram ``_bucket`` series
are dropped, except for the histograms in ``HISTOGRAM_ALLOWLIST`` (TTFT), whose buckets
keep their ``le`` label. The ``*_created`` series of kept counters and histograms are kept
(per series, like the counters).
"""

from __future__ import annotations

import concurrent.futures
import http.client
import logging
import math
import re
import socket
import time
import urllib.request
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Pattern, Sequence, Tuple, Union

log = logging.getLogger(__name__)

# Known metric names, plus name-fragment patterns for other versions of the same
# metrics. Fragments are restricted to the vllm:/sglang: namespaces so that
# unrelated process metrics never pass.
DEFAULT_ALLOWLIST = (
    r"^vllm:prompt_tokens_total$",
    r"^vllm:generation_tokens_total$",
    r"^vllm:(gpu_)?prefix_cache_(queries|hits)(_total)?$",
    r"^vllm:request_prefill_time_seconds_(sum|count)$",
    r"^vllm:request_decode_time_seconds_sum$",
    r"^vllm:num_requests_(running|waiting)$",
    r"^sglang:prompt_tokens_total$",
    r"^sglang:generation_tokens_total$",
    r"^sglang:cache_hit_rate$",
    r"^sglang:cached_tokens_total$",
    r"^sglang:num_running_reqs$",
    r"^sglang:num_queue_reqs$",
    r"^(vllm|sglang):.*(prompt_tokens|generation_tokens|prefix_cache|cache_hit|prefill_time"
    r"|decode_time|num_requests|num_running|num_queue)",
    # TTFT histogram (buckets are kept through HISTOGRAM_ALLOWLIST).
    r"^(vllm|sglang):time_to_first_token_seconds_(sum|count)$",
    # LMCache 0.5.x counters (LMCache internal API server /metrics, one endpoint per
    # worker, see --extra-metrics-url): retrieve hits (L1 + L2) and remote (L2) traffic.
    r"^lmcache:(num_(retrieve_requests|requested_tokens|hit_tokens|lookup_tokens|lookup_hits"
    r"|vllm_hit_tokens|prompt_tokens|stored_tokens|remote_(read|write)_(requests|bytes))"
    r"|local_cpu_evict(_keys)?_count)_total$",
    # LMCache multiprocess (MP) server counters (the /metrics of its HTTP server, one
    # endpoint for all workers, see --extra-metrics-url): lookup hits split by tier
    # (hit = hit_l1 + hit_l2, in tokens and in keys) and the chunks loaded from L2 into L1.
    r"^lmcache_mp_(lookup_(requested|hit|hit_l1|hit_l2)_tokens|lookup_hit_l[12]_keys"
    r"|lookups_requests|l2_prefetch_load_completed_chunks)_total$",
)
# Histograms whose _bucket series are kept. One bucket set costs about 2 KB per M record.
HISTOGRAM_ALLOWLIST = (
    r"^(vllm|sglang):time_to_first_token_seconds$",
)
# vLLM >= 0.11 splits prompt tokens by ``source`` (local_compute, local_cache_hit,
# external_kv_transfer); LMCache labels every series with its ``worker_id`` (one per
# TP rank) and ``role`` (``scheduler`` or ``worker``; the scheduler's worker_id can equal
# a worker's), so per-worker counters stay apart; ``le`` is the histogram bucket bound.
KEPT_LABELS = ("engine", "model_name", "source", "worker_id", "role", "le")
COUNTER_SUFFIXES = ("_total", "_sum", "_count", "_bucket")
# Label added by :func:`scrape_many` to every series of an extra URL, so equal series of
# two URLs stay apart and counter deltas are taken per URL.
SOURCE_LABEL = "kvtrace_url"
# Label that :func:`filter_metrics` adds to counter (and ``*_created``) series that have
# labels outside KEPT_LABELS, with those labels as its value. Such series stay apart in a
# sample, so a reset of one of them (one DP rank restarts) is not read as a reset of
# their sum; :func:`counter_increments` takes deltas per series and sums the increments
# under :func:`merged_key`. Traces recorded before this label hold the summed series.
SERIES_LABEL = "kvtrace_series"
_SERIES_LABEL_RE = re.compile(r',?' + SERIES_LABEL + r'="(?:[^"\\]|\\.)*"')
# Dropped labels whose values must not get into a trace, so they are not in SERIES_LABEL
# either and their series are summed. vLLM's per-request cache_salt (on LMCache MP
# counters) isolates tenants' prefix caches; one salt per tenant would also add series
# without limit. All salts of one MP server restart together, so their sum is safe.
UNWRITTEN_LABELS = ("cache_salt",)
# Families whose series from extra URLs are merged into one key in traces recorded before
# SOURCE_LABEL existed; :func:`counter_increments` guards them when the URL set changes.
UNTAGGED_EXTRA_PREFIX = "lmcache:"

_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+(?P<value>\S+)(?:\s+\S+)?\s*$"
)
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')


def compile_allowlist(patterns: Iterable[str]) -> List[Pattern[str]]:
    return [re.compile(p) for p in patterns]


def _parse_value(text: str) -> Optional[float]:
    try:
        value = float(text)
    except ValueError:
        return None
    return None if math.isnan(value) else value


def parse_prometheus_text(text: str) -> List[Tuple[str, Dict[str, str], float]]:
    """Return ``(name, labels, value)`` for every sample line; bad lines are skipped."""
    samples = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        value = _parse_value(match.group("value"))
        if value is None:
            continue
        labels = {}
        if match.group("labels"):
            labels = {k: v.replace('\\"', '"').replace("\\\\", "\\")
                      for k, v in _LABEL.findall(match.group("labels"))}
        samples.append((match.group("name"), labels, value))
    return samples


def series_key(name: str, labels: Dict[str, str]) -> str:
    kept = [(k, labels[k]) for k in KEPT_LABELS if k in labels]
    if not kept:
        return name
    return name + "{" + ",".join(f'{k}="{v}"' for k, v in kept) + "}"


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def counter_series_key(name: str, labels: Dict[str, str]) -> str:
    """:func:`series_key` plus the :data:`SERIES_LABEL` of the labels it drops.

    Series that differ only in dropped labels get different keys, so counter deltas can
    be taken per series. Without dropped labels, the key equals :func:`series_key`.
    Labels in :data:`UNWRITTEN_LABELS` are not written, so series that differ only in
    them get the same key and are summed.
    """
    key = series_key(name, labels)
    # An empty label value is the same as no label in Prometheus (LMCache exports
    # served_model_name=""), so it does not tell series apart.
    dropped = sorted((k, v) for k, v in labels.items()
                     if k not in KEPT_LABELS and k not in UNWRITTEN_LABELS and v)
    if not dropped:
        return key
    value = _escape_label_value(",".join(f"{k}={v}" for k, v in dropped))
    label = f'{SERIES_LABEL}="{value}"'
    return key[:-1] + "," + label + "}" if key.endswith("}") else key + "{" + label + "}"


def merged_key(key: str) -> str:
    """``key`` without its :data:`SERIES_LABEL`: the key that series are summed under."""
    if SERIES_LABEL not in key:
        return key
    name, _, rest = key.partition("{")
    body = _SERIES_LABEL_RE.sub("", rest[:-1] if rest.endswith("}") else rest).lstrip(",")
    return name + "{" + body + "}" if body else name


def base_name(key: str) -> str:
    return key.split("{", 1)[0]


def split_key(key: str) -> Tuple[str, Dict[str, str]]:
    """Inverse of :func:`series_key`: ``name{a="b"}`` -> ``("name", {"a": "b"})``."""
    name, _, rest = key.partition("{")
    return name, dict(_LABEL.findall(rest)) if rest else {}


def is_counter(key: str) -> bool:
    return base_name(key).endswith(COUNTER_SUFFIXES)


def is_created(key: str) -> bool:
    return base_name(key).endswith("_created")


def created_key(key: str) -> Optional[str]:
    """Key of the Prometheus ``*_created`` series of a counter key, or None."""
    name, sep, rest = key.partition("{")
    for suffix in COUNTER_SUFFIXES:
        if name.endswith(suffix):
            break
    else:
        return None
    name = name[:-len(suffix)] + "_created"
    if suffix == "_bucket" and rest:
        # A histogram has one _created series without the bucket label.
        labels = ",".join(f'{k}="{v}"' for k, v in _LABEL.findall(rest) if k != "le")
        return f"{name}{{{labels}}}" if labels else name
    return name + sep + rest


def source_url(key: str) -> Optional[str]:
    """The extra URL that :func:`tag_source` wrote into ``key``, or None."""
    if SOURCE_LABEL not in key:
        return None
    raw = dict(_LABEL.findall(key.partition("{")[2])).get(SOURCE_LABEL)
    return None if raw is None else raw.replace('\\"', '"').replace("\\\\", "\\")


def source_label(url: str) -> str:
    """The ``kvtrace_url`` value of ``url``: no user info, query string or fragment.

    Credentials and tokens must not reach a trace. The rest of the URL is kept as
    written, so the label of one endpoint stays the same across scrapes and versions.
    """
    url = url.partition("#")[0].partition("?")[0]
    scheme, sep, rest = url.partition("://")
    if sep:
        authority, slash, path = rest.partition("/")
        url = scheme + sep + authority.rpartition("@")[2] + slash + path
    return url


def tag_source(sample: Dict[str, float], url: str) -> Dict[str, float]:
    """Add the label ``kvtrace_url="<url>"`` to every key of ``sample``.

    The label holds :func:`source_label` of ``url``."""
    url = source_label(url)
    label = SOURCE_LABEL + '="' + url.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return {(key[:-1] + "," + label + "}" if key.endswith("}") else key + "{" + label + "}"): value
            for key, value in sample.items()}


def filter_metrics(text: str, allowlist: Optional[List[Pattern[str]]] = None,
                   histograms: Optional[List[Pattern[str]]] = None) -> Dict[str, float]:
    patterns = allowlist if allowlist is not None else compile_allowlist(DEFAULT_ALLOWLIST)
    hist_patterns = histograms if histograms is not None else compile_allowlist(HISTOGRAM_ALLOWLIST)
    sums: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    created: Dict[str, float] = {}
    for name, labels, value in parse_prometheus_text(text):
        if name.endswith("_created"):
            # Keep the creation time of kept counters and histograms: it tells whether a
            # series that appears late started inside the window (see counter_increments).
            family = name[:-len("_created")]
            if (any(p.search(family) for p in hist_patterns)
                    or any(p.search(family + s) for s in ("_total", "_sum", "_count")
                           for p in patterns)):
                key = counter_series_key(name, labels)
                # Series that collapse onto one key: the earliest creation time.
                created[key] = min(value, created.get(key, value))
            continue
        if name.endswith("_bucket"):
            family = name[:-len("_bucket")]
            if not any(p.search(family) for p in hist_patterns):
                continue
        elif not any(p.search(name) for p in patterns):
            continue
        key = (counter_series_key(name, labels) if name.endswith(COUNTER_SUFFIXES)
               else series_key(name, labels))
        sums[key] = sums.get(key, 0.0) + value
        counts[key] = counts.get(key, 0) + 1
    out = {k: (v if is_counter(k) else v / counts[k]) for k, v in sums.items()}
    out.update(created)
    return out


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._unix_path)
        self.sock = sock


def _fetch(url: str, timeout: float) -> str:
    # ``unix:/path/to.sock`` (optionally ``unix:/path/to.sock:/metrics``) reads a Unix
    # socket, e.g. LMCache's internal API server with internal_api_server_socket_path_prefix,
    # so no TCP listener is needed on the engine host. The last ":/" starts the HTTP path.
    if url.startswith("unix:"):
        head, sep, tail = url[len("unix:"):].rpartition(":/")
        path, http_path = (head, "/" + tail) if sep else (tail, "/metrics")
        conn = _UnixHTTPConnection(path, timeout)
        try:
            conn.request("GET", http_path)
            resp = conn.getresponse()
            if resp.status != 200:
                raise OSError(f"{url}: HTTP {resp.status}")
            return resp.read().decode("utf-8", "replace")
        finally:
            conn.close()
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - operator-supplied URL
        return resp.read().decode("utf-8", "replace")


def scrape(url: str, allowlist: Optional[List[Pattern[str]]] = None,
           timeout: float = 5.0) -> Dict[str, float]:
    return filter_metrics(_fetch(url, timeout), allowlist)


def merge_samples(target: Dict[str, float], extra: Dict[str, float]) -> None:
    """Add ``extra`` into ``target``: counters with the same key add up, gauges are replaced."""
    for key, value in extra.items():
        if key in target and is_counter(key):
            target[key] += value
        else:
            target[key] = value


DEFAULT_EXTRA_TIMEOUT = 1.0
# Enough for the scheduler plus 8 TP workers of one LMCache engine, plus spare.
MAX_EXTRA_WORKERS = 16


def scrape_many(urls: Sequence[str], timeout: float = DEFAULT_EXTRA_TIMEOUT,
                executor: Optional[concurrent.futures.Executor] = None,
                clock: Callable[[], float] = time.time,
                ) -> Tuple[Dict[str, float], List[str], Dict[str, float]]:
    """Scrape every URL in parallel into one sample.

    Returns the sample, one error text per failed URL, and the time each URL's scrape
    finished. A URL that has not answered ``timeout`` seconds after the start counts as
    failed, so slow endpoints cannot delay the caller much more than ``timeout``. Any
    exception from one URL (a bad endpoint, a parser bug) drops only that URL.

    Every key gets the label ``kvtrace_url`` (:data:`SOURCE_LABEL`) with its URL. Two URLs
    can export the same series (for example LMCache series without ``worker_id``); a sum
    of both would drop when one URL misses a scrape and look like a counter reset.
    """
    sample: Dict[str, float] = {}
    errors: List[str] = []
    times: Dict[str, float] = {}
    if not urls:
        return sample, errors, times
    own = executor is None
    pool = executor or concurrent.futures.ThreadPoolExecutor(
        max_workers=min(MAX_EXTRA_WORKERS, len(urls)), thread_name_prefix="kvtrace-extra")

    def one(url: str) -> Tuple[Dict[str, float], float]:
        return scrape(url, timeout=timeout), clock()

    try:
        futures = [pool.submit(one, url) for url in urls]
        # A small margin over the socket timeout for connect plus parse.
        concurrent.futures.wait(futures, timeout=timeout + 0.25)
        for url, future in zip(urls, futures):
            if not future.done():
                future.cancel()
                errors.append(f"{url}: no answer within {timeout:g} s")
                continue
            try:
                got, ts = future.result()
            except Exception as exc:  # one bad endpoint must not drop the others
                errors.append(f"{url}: {str(exc) or repr(exc)}")
                continue
            merge_samples(sample, tag_source(got, url))
            # The same value as the label, so counter_increments can match the two.
            times[source_label(url)] = ts
    finally:
        if own:
            pool.shutdown(wait=False, cancel_futures=True)
    return sample, errors, times


def detect_engine(metric_keys: Iterable[str]) -> Optional[str]:
    for key in metric_keys:
        if key.startswith("vllm:"):
            return "vllm"
        if key.startswith("sglang:"):
            return "sglang"
    return None


def _delta(prev: float, value: float, prev_created: Optional[float] = None,
           created: Optional[float] = None) -> float:
    # A changed *_created time is a new series (an engine or worker restart), even when
    # the counter has already climbed past its old value. Without *_created (prometheus
    # multiprocess mode, old traces), a counter that goes down restarts from zero.
    if prev_created is not None and created is not None and created != prev_created:
        return value
    return value - prev if value >= prev else value


MetricSample = Union[Tuple[float, Dict[str, float]],
                     Tuple[float, Dict[str, float], Optional[Iterable[str]]]]


def counter_increments(samples: Sequence[MetricSample], window_start: Optional[float] = None,
                       late: Optional[Dict[str, Tuple[float, str]]] = None,
                       ) -> List[Tuple[float, Dict[str, float]]]:
    """Counter increments between consecutive samples of one node, ``(later ts, {key: inc})``.

    A sample is ``(ts, metrics)`` or ``(ts, metrics, answered)``, where ``answered`` holds
    the extra URLs that answered for that sample (the keys of ``extra_ts`` in an ``M``
    record). Deltas are taken per key, so per extra URL for keys with ``kvtrace_url``; a
    URL that misses a scrape leaves its keys out, and their last value stays the baseline.
    Keys that differ only in :data:`SERIES_LABEL` get their own deltas, and the increments
    are summed under :func:`merged_key` (the key in the output and in ``late``).

    A counter whose ``*_created`` time changed between two samples is a new series (a
    restart), so its increment is its new value. Without ``*_created``, a counter that goes
    down restarts from zero.

    A key that first appears after the first sample (``late``, if given, gets
    ``key -> (ts, how)``):

    - ``"born"``: its endpoint answered in the previous sample without it, so it was created
      between the two samples (vLLM creates a labelled series at its first increment). It
      counts from 0.
    - ``"created"``: its endpoint did not answer before, and its ``*_created`` time is at
      or after ``window_start`` (default: the first sample). It counts from 0.
    - ``"first_value"``: otherwise. Its first value is its baseline, so its window is
      shorter than the node's.

    Traces recorded before ``kvtrace_url`` existed hold one summed key for equal series of
    two extra URLs. For such ``lmcache:`` keys, when ``answered`` differs from the set at
    the key's baseline: a smaller set is a partial scrape (no increment, the baseline is
    kept; if the same smaller set comes again, it becomes the baseline); any other change
    starts a new baseline with no increment. That loses at most one sample period per
    change, instead of counting the drop as a reset. Samples without ``answered`` get no
    such guard.
    """
    rows = sorted(samples, key=lambda s: s[0])
    out: List[Tuple[float, Dict[str, float]]] = []
    last: Dict[str, Tuple[float, Optional[FrozenSet[str]], Optional[float]]] = {}
    pending: Dict[str, Tuple[float, FrozenSet[str], Optional[float]]] = {}
    kinds: Dict[str, Tuple[Optional[str], bool, Optional[str], str]] = {}
    prev_answered: Optional[FrozenSet[str]] = None
    threshold = window_start if window_start is not None else (rows[0][0] if rows else 0.0)
    for i, row in enumerate(rows):
        ts, metrics = row[0], row[1]
        raw = row[2] if len(row) > 2 else None  # type: ignore[misc]
        answered = frozenset(raw) if raw is not None else None
        inc: Dict[str, float] = {}
        for key, value in metrics.items():
            if not is_counter(key):
                continue
            kind = kinds.get(key)
            if kind is None:
                src = source_url(key)
                kind = (src, src is None and key.startswith(UNTAGGED_EXTRA_PREFIX),
                        created_key(key), merged_key(key))
                kinds[key] = kind
            src, guarded, ckey, out_key = kind
            created = metrics.get(ckey) if ckey else None
            if key not in last:
                if i > 0:
                    if src is not None:
                        seen_before = prev_answered is not None and src in prev_answered
                    elif guarded:
                        seen_before = (prev_answered is not None and answered is not None
                                       and answered <= prev_answered)
                    else:
                        seen_before = True  # engine scrape: present in every M record
                    if seen_before:
                        how = "born"
                    elif created is not None and created >= threshold:
                        how = "created"
                    else:
                        how = "first_value"
                    if how != "first_value":
                        inc[out_key] = inc.get(out_key, 0.0) + value
                    # Series summed under one key: a late one counted from its first
                    # value shortens the window of the sum, so that note wins.
                    if late is not None and (out_key not in late or how == "first_value"):
                        late[out_key] = (ts, how)
                last[key] = (value, answered, created)
                continue
            prev, prev_set, prev_created = last[key]
            if guarded and answered is not None and prev_set is not None and answered != prev_set:
                if answered < prev_set:
                    held = pending.get(key)
                    if held is not None and held[1] == answered:
                        # The smaller URL set came again: a URL is gone, not late.
                        inc[out_key] = (inc.get(out_key, 0.0)
                                        + _delta(held[0], value, held[2], created))
                        last[key] = (value, answered, created)
                        del pending[key]
                    else:
                        pending[key] = (value, answered, created)
                    continue
                last[key] = (value, answered, created)
                pending.pop(key, None)
                continue
            inc[out_key] = inc.get(out_key, 0.0) + _delta(prev, value, prev_created, created)
            last[key] = (value, answered, created)
            pending.pop(key, None)
        out.append((ts, inc))
        prev_answered = answered
    return out


def aggregate_samples(samples: Sequence[MetricSample]) -> Dict[str, object]:
    """Reduce time-ordered samples from one node to counter deltas and gauge averages.

    Counter resets (a changed ``*_created`` time, or else a value decrease) are treated as
    a restart from zero. Counter deltas
    come from :func:`counter_increments` (per key, and per extra URL for tagged keys).
    Counter keys are :func:`merged_key` keys.
    A sample may carry the extra URLs that answered, as in :func:`counter_increments`;
    then the partial-scrape guard applies to untagged ``lmcache:`` keys of old traces.
    ``*_created`` series are not gauges and are left out.
    """
    samples = sorted(samples, key=lambda s: s[0])
    counters: Dict[str, float] = {}
    gauges_sum: Dict[str, float] = {}
    gauges_n: Dict[str, int] = {}
    for sample in samples:
        for key, value in sample[1].items():
            if is_counter(key):
                counters.setdefault(merged_key(key), 0.0)
            elif not is_created(key):
                gauges_sum[key] = gauges_sum.get(key, 0.0) + value
                gauges_n[key] = gauges_n.get(key, 0) + 1
    for _ts, inc in counter_increments(samples):
        for key, value in inc.items():
            counters[key] += value
    return {
        "samples": len(samples),
        "window": [samples[0][0], samples[-1][0]] if samples else None,
        "counters": counters,
        "gauges": {k: gauges_sum[k] / gauges_n[k] for k in gauges_sum},
    }
