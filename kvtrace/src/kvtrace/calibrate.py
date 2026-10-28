"""Measured model inputs from an OFF/ON run pair and a break-even chain (``kvtrace calibrate``).

The inputs are:

- ``breakeven.json`` of a chain run: ``t_fixed_s`` (fixed cost of one L2 load, seconds)
  and ``BW_req_B_s.median`` (per-request load bandwidth, bytes/s), measured on an idle engine.
- One OFF and one ON run directory, each with the driver's ``requests.jsonl`` and the
  connector's ``connector/requests-*.jsonl`` (``kvtrace.vllm_connector`` output).

The output is a calibration JSON. ``values`` holds the inputs that ``simulate`` and
``report`` take with ``--calibration``; each value has its method and sample size.
``diagnostics`` holds figures that are reported but never applied. Formulas are in
README.md ("Calibration").
"""

from __future__ import annotations

import glob
import hashlib
import json
import math
import os
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import TOOL_NAME
from .contention import ttft_change

CALIBRATION_KIND = "kvtrace.calibration"
CALIBRATION_VERSION = 1
DEFAULT_QUEUE_THRESHOLD_MS = 50.0
DEFAULT_JOIN_TOLERANCE_S = 0.2

# Calibration values that each command applies. Both rtt inputs come from one measured
# fixed cost per load; the read bandwidth comes from the same chain fit, so the pair
# stays consistent.
SIMULATE_KEYS = ("l2_rtt_ms", "gpu_rtt_ms", "l2_read_gbps_per_node", "l2_lookup_ms_hit",
                 "l2_lookup_ms_miss", "store_us_per_token")
REPORT_KEYS = ("store_us_per_token", "tpot_change", "tpot_delta_ms")

SOURCE_INPUT = "input"
SOURCE_CALIBRATED = "calibrated"
SOURCE_DEFAULT = "default"


class CalibrationError(ValueError):
    """A calibration input is missing or not usable."""


def sha256_of(path: str) -> str:
    """Hex SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def connector_files(run_dir: str) -> List[str]:
    """``connector/requests-*.jsonl`` files of a run directory; ``.part`` files are skipped."""
    files = sorted(glob.glob(os.path.join(run_dir, "connector", "requests-*.jsonl")))
    return [f for f in files if not f.endswith(".part")]


def load_run(run_dir: str, tolerance_s: float = DEFAULT_JOIN_TOLERANCE_S
             ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Driver rows of ``run_dir`` with their connector row under ``"connector"``.

    A driver row joins the connector row with the same ``prompt_tokens`` whose
    ``t_arrival`` is nearest its ``t_send``, within ``tolerance_s``; each connector row
    joins once. Only rows with ``status`` ok and a ``ttft_s`` are kept.

    Returns:
        ``(rows, provenance)``; provenance lists each file with its SHA-256 and row count.

    Raises:
        CalibrationError: ``requests.jsonl`` or the connector files are missing.
    """
    req_path = os.path.join(run_dir, "requests.jsonl")
    if not os.path.isfile(req_path):
        raise CalibrationError(f"{run_dir}: no requests.jsonl")
    conn_paths = connector_files(run_dir)
    if not conn_paths:
        raise CalibrationError(f"{run_dir}: no connector/requests-*.jsonl")
    rows = [r for r in _read_jsonl(req_path)
            if r.get("status", "ok") == "ok" and r.get("ttft_s") is not None]
    by_tokens: Dict[Any, List[Dict[str, Any]]] = {}
    files = [{"path": req_path, "sha256": sha256_of(req_path), "rows": len(rows)}]
    for path in conn_paths:
        conn = _read_jsonl(path)
        files.append({"path": path, "sha256": sha256_of(path), "rows": len(conn)})
        for c in conn:
            if c.get("t_arrival") is not None and c.get("t_lookup") is not None:
                by_tokens.setdefault(c.get("prompt_tokens"), []).append(c)
    used = set()
    joined = 0
    for row in sorted(rows, key=lambda r: r["t_send"]):
        best: Optional[Tuple[float, int, Dict[str, Any]]] = None
        for i, c in enumerate(by_tokens.get(row.get("prompt_tokens"), [])):
            key = (row.get("prompt_tokens"), i)
            if key in used:
                continue
            dt = abs(c["t_arrival"] - row["t_send"])
            if dt <= tolerance_s and (best is None or dt < best[0]):
                best = (dt, i, c)
        if best is not None:
            used.add((row.get("prompt_tokens"), best[1]))
            row["connector"] = best[2]
            joined += 1
    return rows, {"dir": run_dir, "files": files, "requests": len(rows),
                  "connector_rows": sum(f["rows"] for f in files[1:]),
                  "requests_with_connector": joined}


def pair_runs(off: Sequence[Dict[str, Any]], on: Sequence[Dict[str, Any]],
              tolerance_s: float = DEFAULT_JOIN_TOLERANCE_S
              ) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any]]], str]:
    """Pairs of the same request in the OFF and the ON run, and the join method.

    The key is ``(session, line, pass)`` when every row has it. Otherwise a row pairs the
    row with the same ``prompt_tokens`` whose send time after its run's first send is
    nearest, within ``tolerance_s``.
    """
    keys = ("session", "line", "pass")
    if all(all(r.get(k) is not None for k in keys) for r in list(off) + list(on)):
        by_key = {tuple(r[k] for k in keys): r for r in on}
        pairs = [(r, by_key[tuple(r[k] for k in keys)]) for r in off
                 if tuple(r[k] for k in keys) in by_key]
        return pairs, "session/line/pass"
    t0_off = min((r["t_send"] for r in off), default=0.0)
    t0_on = min((r["t_send"] for r in on), default=0.0)
    by_tokens: Dict[Any, List[Dict[str, Any]]] = {}
    for r in on:
        by_tokens.setdefault(r.get("prompt_tokens"), []).append(r)
    used = set()
    pairs = []
    for r in off:
        rel = r["t_send"] - t0_off
        best = None
        for o in by_tokens.get(r.get("prompt_tokens"), []):
            if id(o) in used:
                continue
            dt = abs(o["t_send"] - t0_on - rel)
            if dt <= tolerance_s and (best is None or dt < best[0]):
                best = (dt, o)
        if best is not None:
            used.add(id(best[1]))
            pairs.append((r, best[1]))
    return pairs, f"prompt_tokens + relative t_send within {tolerance_s:g} s"


def _summary(values: Sequence[float], scale: float = 1.0) -> Dict[str, Any]:
    if not values:
        return {"median": None, "mean": None, "n": 0}
    return {"median": statistics.median(values) * scale, "mean": statistics.mean(values) * scale,
            "n": len(values)}


def ols(xs: Sequence[float], ys: Sequence[float]) -> Dict[str, Optional[float]]:
    """Least squares ``y = slope * x + intercept`` with Pearson ``r``; None values when undefined."""
    n = len(xs)
    if n < 2:
        return {"slope": None, "intercept": None, "r": None, "n": n}
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return {"slope": None, "intercept": None, "r": None, "n": n}
    slope = sxy / sxx
    r = sxy / math.sqrt(sxx * syy) if syy > 0 else None
    return {"slope": slope, "intercept": my - slope * mx, "r": r, "n": n}


def _lookup_wait(row: Dict[str, Any]) -> float:
    c = row["connector"]
    return c["t_lookup"] - c["t_arrival"]


def _stored_tokens(row: Dict[str, Any]) -> int:
    # Tokens the ON request computes, and so stores: prompt minus what was loaded or cached.
    c = row["connector"]
    return int(c["prompt_tokens"]) - int(c.get("computed_at_sched") or 0)


def l2_tokens(off: Dict[str, Any], on: Dict[str, Any]) -> int:
    """Tokens the ON request loads from L2: ON minus OFF ``external_tokens``.

    ``external_tokens`` counts every token LMCache supplies, CPU tier and L2 together, and
    the OFF run keeps the CPU tier, so only the increase over OFF is from L2.
    """
    return max(0, int(on["connector"].get("external_tokens") or 0)
               - int(off["connector"].get("external_tokens") or 0))


def fit_pair(pairs: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]], kv_bytes_per_token: int,
             queue_threshold_ms: float = DEFAULT_QUEUE_THRESHOLD_MS) -> Dict[str, Any]:
    """Fits of one OFF/ON pair: lookup delay, store cost, TPOT change and load bandwidth.

    Only pairs with a connector row on both sides are used; an L2 hit is a pair with
    :func:`l2_tokens` > 0. Lookup delay of a request = its ON ``t_lookup - t_arrival``
    minus its OFF one, over requests whose OFF wait is below ``queue_threshold_ms`` (not
    queued in OFF). Store cost = OLS slope of per-request ON - OFF TTFT on ON computed
    tokens over pairs without an L2 hit: on a hit pair the loaded tokens lower both the
    computed tokens and the TTFT, which the slope would take as store cost (``store_all``
    is that biased fit over all pairs, for reference). Load bandwidth under load =
    ``l2_tokens * kv_bytes_per_token / (t_sched - t_lookup)`` of hit requests (diagnostic).
    """
    both = [(o, n) for o, n in pairs if "connector" in o and "connector" in n]
    limit = queue_threshold_ms / 1000.0
    hit_d, miss_d = [], []
    for o, n in both:
        if _lookup_wait(o) >= limit:
            continue
        d = _lookup_wait(n) - _lookup_wait(o)
        (hit_d if l2_tokens(o, n) > 0 else miss_d).append(d)
    no_hit = [(o, n) for o, n in both if l2_tokens(o, n) == 0]
    store = ols([float(_stored_tokens(n)) for _o, n in no_hit],
                [n["ttft_s"] - o["ttft_s"] for o, n in no_hit])
    store_all = ols([float(_stored_tokens(n)) for _o, n in both],
                    [n["ttft_s"] - o["ttft_s"] for o, n in both])
    hits = [(o, n) for o, n in both if l2_tokens(o, n) > 0]
    bw, sizes, times = [], [], []
    for o, n in hits:
        c = n["connector"]
        t = c.get("t_sched", c["t_lookup"]) - c["t_lookup"]
        nbytes = l2_tokens(o, n) * kv_bytes_per_token
        sizes.append(float(nbytes))
        times.append(t)
        if t > 0:
            bw.append(nbytes * 8 / t / 1e9)
    bw_fit = ols(sizes, times)
    tp = [(o["tpot_s"], n["tpot_s"]) for o, n in both
          if o.get("tpot_s") is not None and n.get("tpot_s") is not None]
    tpot_off = statistics.median([a for a, _ in tp]) if tp else None
    tpot_on = statistics.median([b for _, b in tp]) if tp else None
    ttft_off = [o["ttft_s"] for o, _ in pairs]
    ttft_on = [n["ttft_s"] for _, n in pairs]
    deltas = [n["ttft_s"] - o["ttft_s"] for o, n in pairs]
    hit_deltas = [n["ttft_s"] - o["ttft_s"] for o, n in hits]
    return {
        "lookup_hit_ms": _summary(hit_d, 1000.0),
        "lookup_miss_ms": _summary(miss_d, 1000.0),
        "store": store,
        "store_all_pairs": store_all,
        "load_gbps_under_load": {
            "median": statistics.median(bw) if bw else None, "n": len(bw),
            "fit_gbps": (8 / bw_fit["slope"] / 1e9 if bw_fit["slope"] and bw_fit["slope"] > 0
                         else None),
            "fit_intercept_ms": None if bw_fit["intercept"] is None else bw_fit["intercept"] * 1000.0,
            "fit_r": bw_fit["r"]},
        "tpot": {"p50_off_ms": None if tpot_off is None else tpot_off * 1000.0,
                 "p50_on_ms": None if tpot_on is None else tpot_on * 1000.0,
                 "mean_off_ms": statistics.mean([a for a, _ in tp]) * 1000.0 if tp else None,
                 "mean_on_ms": statistics.mean([b for _, b in tp]) * 1000.0 if tp else None,
                 "n": len(tp)},
        "ttft_delta": {"mean_s": statistics.mean(deltas) if deltas else None,
                       "change": ttft_change(ttft_off, ttft_on),
                       "hit_mean_s": statistics.mean(hit_deltas) if hit_deltas else None,
                       "hit_n": len(hit_deltas), "n": len(deltas)},
        "pairs_with_connector": len(both),
    }


def _value(value: Optional[float], unit: str, method: str, **extra: Any) -> Dict[str, Any]:
    return {"value": value, "unit": unit, "method": method, **extra}


def calibrate(breakeven_path: str, off_dir: str, on_dir: str,
              kv_bytes_per_token: Optional[int] = None,
              queue_threshold_ms: float = DEFAULT_QUEUE_THRESHOLD_MS,
              tolerance_s: float = DEFAULT_JOIN_TOLERANCE_S) -> Dict[str, Any]:
    """Calibration JSON from a break-even file and an OFF/ON run pair.

    ``kv_bytes_per_token`` defaults to ``b_tok`` of the break-even file.

    Raises:
        CalibrationError: an input is missing, or no request pairs.
    """
    with open(breakeven_path, encoding="utf-8") as fh:
        be = json.load(fh)
    if be.get("t_fixed_s") is None:
        raise CalibrationError(f"{breakeven_path}: no t_fixed_s")
    kvb = kv_bytes_per_token or be.get("b_tok")
    if not kvb:
        raise CalibrationError("give --kv-bytes-per-token (the break-even file has no b_tok)")
    off, off_prov = load_run(off_dir, tolerance_s)
    on, on_prov = load_run(on_dir, tolerance_s)
    pairs, method = pair_runs(off, on, tolerance_s)
    if not pairs:
        raise CalibrationError(f"no request of {off_dir} pairs with one of {on_dir}")
    fit = fit_pair(pairs, int(kvb), queue_threshold_ms)
    t_fixed_ms = float(be["t_fixed_s"]) * 1000.0
    bw_req = (be.get("BW_req_B_s") or {}).get("median")
    hit, miss, store, tpot = fit["lookup_hit_ms"], fit["lookup_miss_ms"], fit["store"], fit["tpot"]
    tpot_delta = (None if tpot["p50_off_ms"] is None or tpot["p50_on_ms"] is None
                  else tpot["p50_on_ms"] - tpot["p50_off_ms"])
    queue_note = (f"ON - OFF t_lookup - t_arrival per request, median over requests with OFF "
                  f"wait < {queue_threshold_ms:g} ms")
    values = {
        "l2_rtt_ms": _value(t_fixed_ms, "ms", "breakeven t_fixed_s (fixed cost per L2 load, idle engine)"),
        "gpu_rtt_ms": _value(t_fixed_ms, "ms", "breakeven t_fixed_s (fixed cost per L2 load, idle engine)"),
        "l2_read_gbps_per_node": _value(None if bw_req is None else bw_req * 8 / 1e9, "Gbit/s",
                                        "breakeven BW_req_B_s median (fit with t_fixed_s)",
                                        n=(be.get("BW_req_B_s") or {}).get("n")),
        "l2_lookup_ms_hit": _value(hit["median"], "ms",
                                   queue_note + ", L2 hit (ON external > OFF external)",
                                   mean=hit["mean"], n=hit["n"]),
        "l2_lookup_ms_miss": _value(miss["median"], "ms", queue_note + ", no L2 hit",
                                    mean=miss["mean"], n=miss["n"]),
        "store_us_per_token": _value(None if store["slope"] is None else store["slope"] * 1e6,
                                     "us/token", "OLS of ON - OFF TTFT on ON computed tokens "
                                     "(prompt - computed_at_sched), pairs without an L2 hit",
                                     intercept_ms=(None if store["intercept"] is None
                                                   else store["intercept"] * 1000.0),
                                     r=store["r"], n=store["n"]),
        "tpot_delta_ms": _value(tpot_delta, "ms", "TPOT p50 ON - p50 OFF (negative = ON faster)",
                                n=tpot["n"]),
        "tpot_change": _value(None if tpot_delta is None or not tpot["p50_off_ms"]
                              else tpot_delta / tpot["p50_off_ms"], "ratio",
                              "tpot_delta_ms / TPOT p50 OFF", n=tpot["n"]),
    }
    return {
        "kind": CALIBRATION_KIND, "version": CALIBRATION_VERSION, "tool": TOOL_NAME,
        "values": values,
        "diagnostics": {
            "load_gbps_under_load": {
                **fit["load_gbps_under_load"],
                "method": "L2 tokens (ON - OFF external_tokens) x kv bytes / (t_sched - t_lookup) "
                          "of L2 hit requests",
                "note": "not applied: t_sched - t_lookup also holds the fixed cost and queueing, "
                        "so a bandwidth from it would count the fixed cost twice"},
            "lookup_ms": {"hit": hit, "miss": miss},
            "ttft_delta": fit["ttft_delta"], "tpot": tpot,
            "store_all_pairs": {**fit["store_all_pairs"],
                                "note": "not applied: hit pairs lower both computed tokens and "
                                        "TTFT, so this slope also holds the recompute saving"},
            "breakeven": {"t_fixed_s": be["t_fixed_s"], "BW_req_B_s": be.get("BW_req_B_s"),
                          "R_prefill_tok_s": be.get("R_prefill_tok_s")},
        },
        "inputs": {"kv_bytes_per_token": int(kvb), "queue_threshold_ms": queue_threshold_ms,
                   "join_tolerance_s": tolerance_s, "pair_join": method,
                   "pairs": len(pairs), "pairs_with_connector": fit["pairs_with_connector"]},
        "provenance": {"breakeven": {"path": breakeven_path, "sha256": sha256_of(breakeven_path)},
                       "off": off_prov, "on": on_prov},
    }


def read_calibration(path: str) -> Dict[str, Any]:
    """A calibration JSON with its path and SHA-256 under ``"_file"``.

    Raises:
        CalibrationError: the file is not a kvtrace calibration.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("kind") != CALIBRATION_KIND or not isinstance(data.get("values"), dict):
        raise CalibrationError(f"{path}: not a kvtrace calibration file (kind {CALIBRATION_KIND})")
    data["_file"] = {"path": path, "sha256": sha256_of(path)}
    return data


def resolve(explicit: Optional[float], calibration: Optional[Dict[str, Any]], key: str,
            default: Optional[float]) -> Tuple[Optional[float], str]:
    """An input value and its source: an explicit flag, else the calibration, else the default."""
    if explicit is not None:
        return explicit, SOURCE_INPUT
    if calibration is not None:
        value = (calibration["values"].get(key) or {}).get("value")
        if value is not None:
            return float(value), SOURCE_CALIBRATED
    return default, SOURCE_DEFAULT


def calibration_quality(calibration: Dict[str, Any]) -> Dict[str, Any]:
    """Sample sizes and fit quality of the applied values: store r and n, lookup hit/miss n."""
    values = calibration["values"]
    store = values.get("store_us_per_token") or {}
    return {"store_r": store.get("r"), "store_n": store.get("n"),
            "lookup_hit_n": (values.get("l2_lookup_ms_hit") or {}).get("n"),
            "lookup_miss_n": (values.get("l2_lookup_ms_miss") or {}).get("n")}


def provenance_record(calibration: Dict[str, Any], applied: Dict[str, Tuple[Any, str]]
                      ) -> Dict[str, Any]:
    """What a command stores about ``--calibration``: the file, each value with its source,
    and :func:`calibration_quality`."""
    return {"path": calibration["_file"]["path"], "sha256": calibration["_file"]["sha256"],
            "values": {k: {"value": v, "source": s} for k, (v, s) in applied.items()},
            "quality": calibration_quality(calibration)}
