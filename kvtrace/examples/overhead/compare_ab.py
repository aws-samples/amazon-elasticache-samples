#!/usr/bin/env python3
"""Compare two load-driver runs: KV events off (A, baseline) vs on (B).

Reads each run's `requests.jsonl` (or `.jsonl.gz`, or a run directory that holds one)
as written by `mooncake_driver.py`, keeps the rows of one phase (default `measure`), and
reports per run: rows, status counts, ok, err, TTFT p50/p90/p99 and TPOT p50 of the ok
rows. Then the difference B - A (seconds and percent of A) and a verdict:

  pass  when TTFT p50 of B is at most --max-ttft-p50-delta-pct percent above A (default 1.0)
        and B has at most --max-err-delta more non-ok rows than A (default 0;
        a negative value such as -1 disables the error check)
  fail  otherwise (fail_reasons says why)

Exit status: 0 pass, 1 fail, 2 a run has no ok rows. Percentiles are nearest-rank. A
TTFT p50 difference under run-to-run noise is not a cost; repeat the pair (A B A B) and
compare the spread of A vs A before trusting a small delta. Standard library only.

    compare_ab.py run/off-ev run/on-ev --max-ttft-p50-delta-pct 1.0 -o ab.json
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from typing import Any, Dict, List, Optional

METRICS = (("ttft_p50_s", "ttft_s", 50), ("ttft_p90_s", "ttft_s", 90), ("ttft_p99_s", "ttft_s", 99),
           ("tpot_p50_s", "tpot_s", 50))


def resolve(path: str) -> str:
    if os.path.isdir(path):
        for name in ("requests.jsonl", "requests.jsonl.gz"):
            candidate = os.path.join(path, name)
            if os.path.exists(candidate):
                return candidate
        raise FileNotFoundError(f"no requests.jsonl(.gz) in {path}")
    return path


def load_rows(path: str, phase: Optional[str]) -> List[Dict[str, Any]]:
    path = resolve(path)
    opener = gzip.open if path.endswith(".gz") else open
    rows = []
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if phase is None or row.get("phase", "measure") == phase:
                rows.append(row)
    return rows


def percentile(values: List[float], p: float) -> Optional[float]:
    """Nearest-rank percentile (p in 0..100). Same as measure_overhead.percentile; keep them in step."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * p // 100))  # ceil(n * p / 100)
    return ordered[int(rank) - 1]


def run_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    status: Dict[str, int] = {}
    for row in rows:
        key = str(row.get("status"))
        status[key] = status.get(key, 0) + 1
    ok = [r for r in rows if r.get("status") == "ok"]
    out: Dict[str, Any] = {"rows": len(rows), "status": dict(sorted(status.items())), "ok": len(ok),
                           "err": len(rows) - len(ok)}
    for name, field, p in METRICS:
        values = [float(r[field]) for r in ok if r.get(field) is not None]
        out[name] = percentile(values, p)
    return out


def compare(a: Dict[str, Any], b: Dict[str, Any], max_ttft_p50_delta_pct: float,
            max_err_delta: Optional[int] = None) -> Dict[str, Any]:
    delta: Dict[str, Any] = {"ok": b["ok"] - a["ok"], "err": b["err"] - a["err"]}
    for name, _field, _p in METRICS:
        va, vb = a[name], b[name]
        pct_name = name[: -len("_s")] + "_pct"
        if va is None or vb is None:
            delta[name] = delta[pct_name] = None
            continue
        delta[name] = vb - va
        delta[pct_name] = 100.0 * (vb - va) / va if va else None
    p50_pct = delta["ttft_p50_pct"]
    reasons = []
    if p50_pct is None:
        verdict = "no-data"
    else:
        if p50_pct > max_ttft_p50_delta_pct:
            reasons.append(f"TTFT p50 +{p50_pct:.2f}% > {max_ttft_p50_delta_pct}%")
        # Failed requests are not in the percentiles, so more errors could hide a slowdown.
        if max_err_delta is not None and delta["err"] > max_err_delta:
            reasons.append(f"err +{delta['err']} > {max_err_delta}")
        verdict = "fail" if reasons else "pass"
    return {"delta_b_minus_a": delta, "max_ttft_p50_delta_pct": max_ttft_p50_delta_pct,
            "max_err_delta": max_err_delta, "verdict": verdict, "fail_reasons": reasons}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a", help="baseline run (events off): requests.jsonl(.gz) or its directory")
    ap.add_argument("b", help="candidate run (events on + recorder)")
    ap.add_argument("--phase", default="measure", help="driver phase label to keep ('' = all rows)")
    ap.add_argument("--max-ttft-p50-delta-pct", type=float, default=1.0,
                    help="pass when TTFT p50 of B exceeds A by at most this percent (default 1.0)")
    ap.add_argument("--max-err-delta", type=int, default=0,
                    help="fail when B has more than this many extra non-ok rows (default 0; -1 = ignore)")
    ap.add_argument("-o", "--output", help="also write the result JSON here")
    args = ap.parse_args(argv)

    phase = args.phase or None
    a = {"path": resolve(args.a), **run_stats(load_rows(args.a, phase))}
    b = {"path": resolve(args.b), **run_stats(load_rows(args.b, phase))}
    max_err = None if args.max_err_delta < 0 else args.max_err_delta
    result = {"phase": phase, "a": a, "b": b, **compare(a, b, args.max_ttft_p50_delta_pct, max_err)}
    text = json.dumps(result, indent=1)
    print(text)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if result["verdict"] == "no-data":
        return 2
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
