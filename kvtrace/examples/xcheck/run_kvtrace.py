#!/usr/bin/env python3
"""kvtrace side of the simulator cross-check: synth trace, then ``kvtrace`` replay.

1. ``synth_from_mooncake.py`` emulates each node's L1 (GPU block LRU, optional
   LMCache-like CPU chunk LRU) and writes kvtrace records plus a per-request index.
2. ``kvtrace.simulator.prepare`` and ``replay`` (the code behind ``kvtrace
   simulate``) replay the L2 lookups per capacity. The replay timeline gives the
   hit tokens of every lookup run; runs are mapped back to requests so the
   measurement windows (all requests, last 50%) match the reference tools.

Run it with kvtrace's environment. Capacities are in chunks (``inf`` allowed); the
byte capacity passed to kvtrace is ``chunks * chunk_tokens * kv_bytes_per_token``.

C1 (one shared cache, every chunk an L2 lookup): ``--nodes 1 --gpu-tokens 0
--block-tokens 256``. The synth GPU pool holds nothing, so every full chunk of a
request is computed and looked up; kvtrace runs use ``--run-continue-seconds 0``
and ``--decode-blocks off`` so each request is one lookup run.

    .venv/bin/python examples/xcheck/run_kvtrace.py conversation_trace.jsonl \\
        --work /tmp/xcheck/kvt-c1 --nodes 1 --gpu-tokens 0 --block-tokens 256 \\
        --capacities 5595,inf -o out/xcheck/c1-kvtrace.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence

from kvtrace import contention as contention_mod
from kvtrace.segments import iter_records, iter_segment_files
from kvtrace.simulator import SimParams, prepare, replay

HERE = os.path.dirname(os.path.abspath(__file__))
SYNTH = os.path.join(HERE, "..", "scale_test", "synth_from_mooncake.py")


def run_synth(a: argparse.Namespace, out_dir: str, index: str) -> Dict[str, Any]:
    cmd = [sys.executable, SYNTH, a.trace, "--out", out_dir, "--index", index,
           "--gpu-tokens", str(a.gpu_tokens), "--block-tokens", str(a.block_tokens),
           "--chunk-tokens", str(a.chunk_tokens)]
    if a.requests:
        cmd += ["--requests", a.requests]
    else:
        cmd += ["--nodes", str(a.nodes), "--routing", a.routing]
    if a.cpu_bytes:
        cmd += ["--cpu-bytes", str(a.cpu_bytes), "--kv-bytes-per-token", str(a.kv_bytes_per_token)]
    if a.max_model_len:
        cmd += ["--max-model-len", str(a.max_model_len)]
    done = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(done.stdout)


def read_index(path: str) -> List[Dict[str, Any]]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def run_order(rows: Sequence[Dict[str, Any]], block: int) -> List[int]:
    """Positions (in schedule order) of the requests that open a kvtrace lookup run.

    synth writes one GPU store per request that has a full block beyond its GPU
    prefix; kvtrace sorts records by (ts, node, rank, file order) and numbers runs
    in that order."""
    keyed = []
    for pos, r in enumerate(rows):
        if (r["input_tokens"] // block) * block > r["gpu_hit_tokens"]:
            keyed.append((r["ts"], r["node"], pos))
    keyed.sort()
    return [pos for _ts, _node, pos in keyed]


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("trace")
    p.add_argument("--work", required=True, help="scratch directory for the synthetic trace")
    p.add_argument("--requests", help="driver log for routing and timing (synth --requests)")
    p.add_argument("--nodes", type=int, default=1)
    p.add_argument("--routing", default="round_robin", choices=("round_robin", "sticky", "cache_aware"))
    p.add_argument("--gpu-tokens", type=int, required=True)
    p.add_argument("--cpu-bytes", type=float, default=0.0)
    p.add_argument("--block-tokens", type=int, default=16)
    p.add_argument("--chunk-tokens", type=int, default=256)
    p.add_argument("--kv-bytes-per-token", type=int, default=192512)
    p.add_argument("--max-model-len", type=int)
    p.add_argument("--capacities", required=True, help="comma list of chunk counts or inf")
    p.add_argument("--lookup-time", choices=("scheduled", "store"), default="scheduled",
                   help="kvtrace --lookup-time. Synth input has no metrics, so scheduled "
                        "falls back to store unless --prefill-tokens-per-sec-per-request is set.")
    p.add_argument("--prefill-tokens-per-sec-per-request", type=float,
                   help="per-request prefill rate for --lookup-time scheduled")
    p.add_argument("--miss-stream-out",
                   help="write line, node, skip_tokens (L1 hit) per request, in replay order")
    p.add_argument("-o", "--out", required=True)
    a = p.parse_args(argv)

    os.makedirs(a.work, exist_ok=True)
    traces = os.path.join(a.work, "traces")
    index = os.path.join(a.work, "index.jsonl")
    synth = run_synth(a, traces, index)
    rows = read_index(index)
    order = run_order(rows, a.block_tokens)

    chunk_bytes = a.chunk_tokens * a.kv_bytes_per_token
    caps = [(c, None if c == "inf" else int(c) * chunk_bytes) for c in a.capacities.split(",")]
    params = SimParams(chunk_tokens=a.chunk_tokens, capacities=caps,
                       kv_bytes_per_token=a.kv_bytes_per_token, run_continue_seconds=0.0,
                       decode_blocks="off", lookup_time=a.lookup_time,
                       prefill_tokens_per_sec_per_request=a.prefill_tokens_per_sec_per_request)
    prep = prepare(iter_records(iter_segment_files([traces])), params)
    runs_ok = prep.summary["lookup_runs"] == len(order)

    # kvtrace replays in event-time order, (ts, node, file order); the windows and the
    # miss stream follow the same order so a reference tool sees the same sequence.
    serve = sorted(range(len(rows)), key=lambda i: (rows[i]["ts"], rows[i]["node"], i))
    rank = {pos: k for k, pos in enumerate(serve)}
    half = len(rows) // 2
    windows = {"all": lambda pos: True, "last50": lambda pos: rank[pos] >= half}
    tok = {w: sum(r["input_tokens"] for i, r in enumerate(rows) if f(i)) for w, f in windows.items()}
    l1 = {w: sum(r["gpu_hit_tokens"] + r["cpu_hit_tokens"] for i, r in enumerate(rows) if f(i))
          for w, f in windows.items()}
    gpu = {w: sum(r["gpu_hit_tokens"] for i, r in enumerate(rows) if f(i)) for w, f in windows.items()}

    results = []
    for label, cap in caps:
        timeline: List[contention_mod.Event] = []
        res = replay(prep, cap, params, timeline)
        hit_by_run: Dict[int, int] = defaultdict(int)
        # Events are (kind, ts, node, run_id, tokens, end[, key_id]): index, do not unpack.
        for ev in timeline:
            if ev[0] == contention_mod.HIT:
                hit_by_run[ev[3]] += ev[4]
        row: Dict[str, Any] = {"capacity_chunks": None if label == "inf" else int(label),
                               "kvtrace_token_hit_rate": res["token_hit_rate"],
                               "saved_tokens": res["saved_tokens"],
                               "lookup_tokens": res["lookup_tokens"],
                               "evictions": res["evictions"], "peak_entries": res["peak_entries"]}
        if runs_ok:
            for w, f in windows.items():
                hit = sum(t for run_id, t in hit_by_run.items() if f(order[run_id]))
                row[w] = {"input_tokens": tok[w], "hit_tokens": hit,
                          "token_hit_rate": hit / tok[w] if tok[w] else 0.0}
        results.append(row)

    per_node: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        n = per_node[r["node"]]
        n["requests"] += 1
        n["input_tokens"] += r["input_tokens"]
        n["gpu_hit_tokens"] += r["gpu_hit_tokens"]
        n["cpu_hit_tokens"] += r["cpu_hit_tokens"]
    if a.miss_stream_out:
        with open(a.miss_stream_out, "w") as fh:
            for pos in serve:
                r = rows[pos]
                fh.write(json.dumps({"line": r["line"], "node": r["node"],
                                     "skip_tokens": r["gpu_hit_tokens"] + r["cpu_hit_tokens"]}) + "\n")
    out = {"tool": "kvtrace", "synth": synth, "kvtrace_summary": {
               k: prep.summary[k] for k in ("tokens", "l2_lookups", "lookup_runs")},
           "lookup_time": prep.lookup_time,
           "runs_mapped": runs_ok, "expected_runs": len(order),
           "l1": {w: {"input_tokens": tok[w], "l1_hit_tokens": l1[w], "gpu_hit_tokens": gpu[w],
                      "l1_hit_rate": l1[w] / tok[w] if tok[w] else 0.0} for w in windows},
           "per_node": {k: dict(v) for k, v in sorted(per_node.items())},
           "results": results}
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1)
    for row in results:
        cap = row["capacity_chunks"]
        extra = ""
        if runs_ok:
            extra = (f"  all {row['all']['token_hit_rate'] * 100:6.2f}%  "
                     f"last50 {row['last50']['token_hit_rate'] * 100:6.2f}%")
        print(f"{'inf' if cap is None else cap:>9}: kvtrace {row['kvtrace_token_hit_rate'] * 100:6.2f}%"
              + extra)
    if not runs_ok:
        print(f"WARNING: {prep.summary['lookup_runs']} runs, expected {len(order)}: "
              "windows not mapped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
