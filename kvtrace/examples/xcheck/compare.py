#!/usr/bin/env python3
"""Tables for the simulator cross-check: reads ``xcheck.sh`` outputs, writes CSV and JSON.

Writes ``c1.csv``, ``c2.csv``, ``c2_l1.csv``, ``c3.csv`` and ``summary.json`` into
the output directory and prints Markdown tables. Rates are token hit rates over all
prompt tokens of the window (tails count as misses). Standard library only.

    python examples/xcheck/compare.py out/xcheck --work /tmp/xcheck/work
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence


def load(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def by_cap(doc: Optional[Dict[str, Any]]) -> Dict[Optional[int], Dict[str, Any]]:
    return {r["capacity_chunks"]: r for r in (doc or {}).get("results", [])}


def rate(row: Optional[Dict[str, Any]], window: str) -> Optional[float]:
    if not row or window not in row:
        return None
    return row[window]["token_hit_rate"]


def kvsim_rate(row: Optional[Dict[str, Any]], pol: str) -> Optional[float]:
    if not row or pol not in row:
        return None
    return row[pol]["token_hit_rate"]


def pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:.2f}"


def diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else a - b


def cap_label(cap: Optional[int], unique: int) -> str:
    return "inf" if cap is None else f"{cap / unique * 100:.0f}%"


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    with open(path, "w", newline="") as fh:
        fields = list(dict.fromkeys(k for r in rows for k in r))
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def markdown(rows: List[Dict[str, Any]], cols: Sequence[str]) -> str:
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        lines.append("| " + " | ".join(
            pct(r.get(c)) if isinstance(r.get(c), float) or r.get(c) is None else str(r.get(c))
            for c in cols) + " |")
    return "\n".join(lines)


def c1(out: str) -> List[Dict[str, Any]]:
    rows = []
    for v in ("full", "k32", "tool"):
        check = load(os.path.join(out, f"conv-{v}.check.json"))
        kvt = load(os.path.join(out, f"c1-{v}-kvtrace.json"))
        if not check or not kvt:
            continue
        lmc = by_cap(load(os.path.join(out, f"c1-{v}-lmcache.json")))
        kvs = by_cap(load(os.path.join(out, f"c1-{v}-kvsim.json")))
        gt = load(os.path.join(out, f"gt-{v}.json")) or {}
        for k in kvt["results"]:
            cap = k["capacity_chunks"]
            row = {"variant": v, "capacity": cap_label(cap, check["unique_chunks"]),
                   "chunks": "inf" if cap is None else cap,
                   "kvtrace_all": rate(k, "all"), "lmcache_all": rate(lmc.get(cap), "all"),
                   "kvtrace_last50": rate(k, "last50"), "lmcache_last50": rate(lmc.get(cap), "last50"),
                   "kvsim_lru_last50": kvsim_rate(kvs.get(cap), "lru"),
                   "kvsim_fifo_last50": kvsim_rate(kvs.get(cap), "fifo"),
                   "kvsim_optimal_last50": kvsim_rate(kvs.get(cap), "optimal"),
                   "gt_chunk256_all": (gt.get("ground_truth_chunk256", {}).get("reusable_fraction")
                                       if cap is None else None)}
            row["d_lmcache_all"] = diff(row["kvtrace_all"], row["lmcache_all"])
            row["d_kvsim_lru_last50"] = diff(row["kvtrace_last50"], row["kvsim_lru_last50"])
            rows.append(row)
    return rows


def blis_nodes(path: str) -> Dict[str, Dict[str, int]]:
    nodes: Dict[str, Dict[str, int]] = {}
    if not os.path.exists(path):
        return nodes
    with open(path) as fh:
        for line in fh:
            r = json.loads(line)
            n = nodes.setdefault(r["node"], {"input_tokens": 0, "gpu": 0, "cpu": 0, "fail": 0})
            n["input_tokens"] += r["input_tokens"]
            n["gpu"] += r["gpu_hit_tokens"]
            n["cpu"] += r["cpu_hit_tokens"]
            n["fail"] += not r["alloc_ok"]
    return nodes


def same_requests(index: str, blis: str) -> Optional[float]:
    """Share of requests whose GPU hit tokens are equal in synth and BLIS."""
    if not (os.path.exists(index) and os.path.exists(blis)):
        return None
    with open(index) as fh:
        synth = {json.loads(line)["line"]: json.loads(line)["gpu_hit_tokens"] for line in fh}
    n = same = 0
    with open(blis) as fh:
        for line in fh:
            r = json.loads(line)
            n += 1
            same += synth.get(r["line"]) == r["gpu_hit_tokens"]
    return same / n if n else None


def c2(out: str, work: Optional[str]) -> Dict[str, List[Dict[str, Any]]]:
    l1_rows, l2_rows = [], []
    gt = (load(os.path.join(out, "gt-full.json")) or {}).get("ground_truth", {})
    unique = (load(os.path.join(out, "conv-full.check.json")) or {}).get("unique_chunks", 1)
    names = sorted(f[:-len("-kvtrace.json")] for f in os.listdir(out)
                   if f.endswith("-kvtrace.json") and (f.startswith("c2-") or f.startswith("bench")))
    for name in names:
        kvt = load(os.path.join(out, f"{name}-kvtrace.json"))
        assert kvt is not None
        synth_nodes = kvt["per_node"]
        blis_files = ([("blis_gpu", f"{name}-blis.jsonl")] if name.startswith("c2-") else
                      [("blis_gpu", "bench-blis-gpu.jsonl"), ("blis_gpu_cpu", "bench-blis-gpucpu.jsonl")])
        l1 = {"config": name, "nodes": len(synth_nodes),
              "synth_gpu": sum(n["gpu_hit_tokens"] for n in synth_nodes.values())
              / sum(n["input_tokens"] for n in synth_nodes.values()),
              "synth_gpu_cpu": kvt["l1"]["all"]["l1_hit_rate"]}
        for label, fname in blis_files:
            bn = blis_nodes(os.path.join(out, fname))
            if not bn:
                continue
            tot = sum(n["input_tokens"] for n in bn.values())
            l1[label] = sum(n["gpu"] + (n["cpu"] if label == "blis_gpu_cpu" else 0)
                            for n in bn.values()) / tot
            if label == "blis_gpu":
                l1["max_node_gap_gpu"] = max(
                    abs(bn[k]["gpu"] / bn[k]["input_tokens"]
                        - synth_nodes[k]["gpu_hit_tokens"] / synth_nodes[k]["input_tokens"])
                    for k in bn)
                l1["blis_alloc_failures"] = sum(n["fail"] for n in bn.values())
                if work:
                    wdir = "kvt-bench" if name == "bench" else f"kvt-{name}"
                    l1["same_gpu_hit_requests"] = same_requests(
                        os.path.join(work, wdir, "index.jsonl"), os.path.join(out, fname))
        l1_rows.append(l1)
        lmc = by_cap(load(os.path.join(out, f"{name}-l2-lmcache.json")))
        lmf = by_cap(load(os.path.join(out, f"{name}-l2-lmcache-full.json")))
        kvs = by_cap(load(os.path.join(out, f"{name}-l2-kvsim-full.json")))
        inf_l2 = rate(by_cap(kvt).get(None), "all") or 0.0
        for k in kvt["results"]:
            cap = k["capacity_chunks"]
            row = {"config": name, "capacity": cap_label(cap, unique),
                   "chunks": "inf" if cap is None else cap,
                   "kvtrace_all": rate(k, "all"), "lmcache_miss_all": rate(lmc.get(cap), "all"),
                   "lmcache_fullprompt_all": rate(lmf.get(cap), "all"),
                   "kvtrace_last50": rate(k, "last50"),
                   "lmcache_fullprompt_last50": rate(lmf.get(cap), "last50"),
                   "kvsim_lru_fullprompt_last50": kvsim_rate(kvs.get(cap), "lru")}
            row["d_lmcache_miss_all"] = diff(row["kvtrace_all"], row["lmcache_miss_all"])
            row["d_kvsim_lmcache_fullprompt_last50"] = diff(row["kvsim_lru_fullprompt_last50"],
                                                            row["lmcache_fullprompt_last50"])
            l2_rows.append(row)
        l1["l1_plus_l2_inf"] = kvt["l1"]["all"]["l1_hit_rate"] + inf_l2
        l1["ground_truth"] = gt.get("reusable_fraction")
        l1["invariant_ok"] = l1["l1_plus_l2_inf"] <= (gt.get("reusable_fraction") or 1.0) + 1e-12
    return {"l1": l1_rows, "l2": l2_rows}


def c3(out: str) -> List[Dict[str, Any]]:
    rows = []
    for f in sorted(os.listdir(out)):
        if not (f.startswith("c3-rr-") and f.endswith(".json")):
            continue
        d = load(os.path.join(out, f))
        assert d is not None
        _, _, g, speed = f[:-5].split("-")
        kvt = load(os.path.join(out, f"c2-round_robin-{g}-kvtrace.json"))
        synth = None
        if kvt:
            synth = (sum(n["gpu_hit_tokens"] for n in kvt["per_node"].values())
                     / sum(n["input_tokens"] for n in kvt["per_node"].values()))
        dyn = (d.get("summary") or {}).get("prefix_cache_reused_ratio")
        rows.append({"gpu_tokens": int(g), "gpu_blocks": d["inputs"]["gpu_blocks"], "speedup": speed,
                     "dynamo_prefix_reuse": dyn, "kvtrace_synth_gpu": synth, "d": diff(synth, dyn)})
    return rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("out")
    p.add_argument("--work", help="xcheck.sh WORK directory (per-request L1 comparison)")
    a = p.parse_args(argv)
    r1, r2, r3 = c1(a.out), c2(a.out, a.work), c3(a.out)
    write_csv(os.path.join(a.out, "c1.csv"), r1)
    write_csv(os.path.join(a.out, "c2_l1.csv"), r2["l1"])
    write_csv(os.path.join(a.out, "c2.csv"), r2["l2"])
    write_csv(os.path.join(a.out, "c3.csv"), r3)
    with open(os.path.join(a.out, "summary.json"), "w") as fh:
        json.dump({"c1": r1, "c2": r2, "c3": r3}, fh, indent=1)
    print(markdown(r1, ["variant", "capacity", "kvtrace_all", "lmcache_all", "kvtrace_last50",
                        "lmcache_last50", "kvsim_lru_last50", "kvsim_optimal_last50", "gt_chunk256_all"]))
    print()
    print(markdown(r2["l1"], [c for c in ("config", "synth_gpu", "blis_gpu", "max_node_gap_gpu",
                                          "same_gpu_hit_requests", "synth_gpu_cpu", "blis_gpu_cpu",
                                          "l1_plus_l2_inf", "ground_truth", "invariant_ok")
                              if any(c in r for r in r2["l1"])]))
    print()
    print(markdown(r2["l2"], ["config", "capacity", "kvtrace_all", "lmcache_miss_all",
                              "lmcache_fullprompt_all", "kvtrace_last50", "lmcache_fullprompt_last50",
                              "kvsim_lru_fullprompt_last50"]))
    if r3:
        print()
        print(markdown(r3, ["gpu_tokens", "speedup", "dynamo_prefix_reuse", "kvtrace_synth_gpu", "d"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
