#!/usr/bin/env python3
"""Run an independent prefix-cache simulator on a ``prepare.py`` chunk file.

Run it with the Python of the tool's own environment (never kvtrace's):

- ``--tool lmcache``: LMCache ``tools.cache_simulator.simulator.simulate`` (flat LRU of
  chunk hashes, prefix lookup, every chunk of a request stored or refreshed after
  its lookup). ``--tool-path`` is the directory that holds the ``lmcache`` package.
- ``--tool kvsim``: ``kvcache-simulator`` Python backend (``kvcache_sim``), policies
  ``lru`` (leaf-only LRU on a prefix trie), ``fifo`` and ``optimal``. It measures
  only after warmup and drops a budget whose cache is not full before the window
  (``underfilled``). Requests without a full chunk are not in its trace, so the
  warmup fraction is chosen so its window starts at the same request.

Capacities are in chunks (``inf`` = no eviction). Hit tokens are full chunks times
``--chunk-tokens``; every rate here uses the chunk file's ``input_length`` sum as
the denominator, so partial tail tokens count as misses for every tool.

Optional ``--hit-from`` (a JSON lines file with ``line`` and ``skip_tokens`` per
request) turns it into an L1-miss reference: each request looks up only the
chunks that hold tokens past ``skip_tokens`` (its L1 hit), and a hit chunk is
credited with its tokens past ``skip_tokens``. Requests are then replayed in that
file's order. With ``--full-lookup`` every request
looks up its whole prompt from the first chunk (LMCache's lookup also covers the
part the GPU holds, so those chunks must still be in L2 and are refreshed), and
only the tokens past ``skip_tokens`` are credited. ``kvsim`` keys its prefix trie by
the path from the first looked-up chunk, so it is only valid with ``--full-lookup``
(every lookup starts at the root); it needs positive block weights, so a chunk with
no credit gets weight 1 and credited chunks get ``WEIGHT_SCALE`` times their tokens.

    /tmp/xcheck/venv-lmc/bin/python examples/xcheck/run_ref.py --tool lmcache \\
        --tool-path /tmp/xcheck/lmc --chunks out/xcheck/conv-full.chunks.jsonl \\
        --capacities 5595,27977,inf -o out/xcheck/c1-lmcache.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Sequence


def load_chunks(path: str) -> List[Dict[str, Any]]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_skips(path: Optional[str]) -> Dict[int, int]:
    """L1 hit tokens per line, in the file's order (dicts keep insertion order)."""
    if not path:
        return {}
    with open(path) as fh:
        rows = (json.loads(line) for line in fh if line.strip())
        return {int(r["line"]): int(r["skip_tokens"]) for r in rows}


def in_order(reqs: List[Dict[str, Any]], skips: Dict[int, int]) -> List[Dict[str, Any]]:
    """Requests in the ``--hit-from`` order (kvtrace's replay order), else as given."""
    if not skips:
        return reqs
    by_line = {int(r["line"]): r for r in reqs}
    return [by_line[line] for line in skips]


def lookup_view(reqs: Sequence[Dict[str, Any]], chunk: int, skips: Dict[int, int],
                full_lookup: bool = False) -> List[Dict[str, Any]]:
    """Per request: looked-up chunk keys and the tokens each hit chunk saves."""
    out = []
    for r in reqs:
        skip = skips.get(int(r["line"]), 0)
        first = 0 if full_lookup else skip // chunk
        keys = r["chunks"][first:]
        credit = [max(0, chunk - max(0, skip - (first + i) * chunk)) for i in range(len(keys))]
        out.append({"line": r["line"], "input_length": r["input_length"], "keys": keys,
                    "credit": credit})
    return out


def windows(view: Sequence[Dict[str, Any]], hit_chunks: Sequence[int]) -> Dict[str, Any]:
    n = len(view)
    half = n // 2
    res: Dict[str, Any] = {}
    for name, lo in (("all", 0), ("last50", half)):
        tok = sum(v["input_length"] for v in view[lo:])
        hit = sum(sum(v["credit"][:k]) for v, k in zip(view[lo:], hit_chunks[lo:]))
        res[name] = {"requests": n - lo, "input_tokens": tok, "hit_tokens": hit,
                     "token_hit_rate": hit / tok if tok else 0.0}
    return res


def run_lmcache(view: Sequence[Dict[str, Any]], caps: Sequence[Optional[int]], chunk: int,
                tool_path: str) -> List[Dict[str, Any]]:
    sys.path.insert(0, tool_path)
    from lmcache.tools.cache_simulator.simulator import simulate  # type: ignore

    events = [{"chunk_hashes": v["keys"], "seq_len": v["input_length"], "chunk_size": chunk}
              for v in view]
    total = sum(len(v["keys"]) for v in view)
    kv_bytes_per_chunk = 1  # capacities are given in chunks
    out = []
    for cap in caps:
        n = total if cap is None else cap
        res = simulate(events, n * kv_bytes_per_chunk, kv_bytes_per_chunk, fast=False)
        # simulate() skips events with no hashes and seq_len 0; every request here has
        # seq_len > 0, so hit_prefix_lengths lines up with the requests.
        assert len(res["hit_prefix_lengths"]) == len(view)
        out.append({"capacity_chunks": cap, "evictions": res["eviction_count"],
                    **windows(view, res["hit_prefix_lengths"])})
    return out


WEIGHT_SCALE = 1_000_000


def run_kvsim(view: Sequence[Dict[str, Any]], caps: Sequence[Optional[int]], chunk: int,
              policies: Sequence[str]) -> List[Dict[str, Any]]:
    from kvcache_sim.plan import build_execution_plan  # type: ignore
    from kvcache_sim.policies import simulate_ceiling, simulate_policy  # type: ignore
    from kvcache_sim.trace import parse_trace_lines  # type: ignore

    kept = [v for v in view if v["keys"]]
    # Block weights are the tokens a hit saves (scaled; 1 for no credit), so its
    # hitTokens // WEIGHT_SCALE are the saved tokens (error < chunks / WEIGHT_SCALE).
    weights = [[c * WEIGHT_SCALE if c else 1 for c in v["credit"]] for v in kept]
    lines = [json.dumps({"hash_ids": v["keys"], "input_length": sum(w),
                         "block_size": chunk, "block_tokens": w})
             for v, w in zip(kept, weights)]
    trace = parse_trace_lines(lines)
    assert trace.request_count == len(kept)
    half = len(view) // 2
    warm = sum(1 for v in view[:half] if v["keys"])
    # floor(len(kept) * w) == warm, so its window starts at request `half` of `view`.
    fraction = (warm + 0.5) / len(kept)
    plan = build_execution_plan(trace, warmup_fraction=fraction)
    assert plan.warmup_requests == warm
    last = view[half:]
    tok = sum(v["input_length"] for v in last)
    out = []
    for cap in caps:
        row: Dict[str, Any] = {"capacity_chunks": cap, "unique_chunks": plan.unique_blocks}
        for pol in policies:
            res = simulate_ceiling(plan) if cap is None else simulate_policy(plan, pol, cap)
            mode = res.measurementMode
            hit = res.hitTokens // WEIGHT_SCALE
            row[pol] = {"mode": mode, "hit_tokens": hit,
                        "token_hit_rate": hit / tok if tok and mode == "fixed_window" else None}
        row["last50_input_tokens"] = tok
        out.append(row)
    return out


def parse_caps(text: str) -> List[Optional[int]]:
    return [None if p.strip() == "inf" else int(p) for p in text.split(",") if p.strip()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tool", choices=("lmcache", "kvsim"), required=True)
    p.add_argument("--tool-path", help="directory holding the lmcache package (lmcache tool)")
    p.add_argument("--chunks", required=True, help="prepare.py output")
    p.add_argument("--capacities", required=True, help="comma list of chunk counts or inf")
    p.add_argument("--chunk-tokens", type=int, default=256)
    p.add_argument("--policies", default="lru,fifo,optimal", help="kvsim policies")
    p.add_argument("--hit-from", help="JSON lines: line, skip_tokens (L1 hit per request)")
    p.add_argument("--full-lookup", action="store_true",
                   help="with --hit-from: look up the whole prompt, credit tokens past the L1 hit")
    p.add_argument("-o", "--out", required=True)
    a = p.parse_args(argv)
    if a.full_lookup and not a.hit_from:
        p.error("--full-lookup needs --hit-from")
    if a.tool == "kvsim" and a.hit_from and not a.full_lookup:
        p.error("--tool kvsim with --hit-from needs --full-lookup (its trie starts at the root)")
    skips = load_skips(a.hit_from)
    view = lookup_view(in_order(load_chunks(a.chunks), skips), a.chunk_tokens, skips, a.full_lookup)
    caps = parse_caps(a.capacities)
    if a.tool == "lmcache":
        if not a.tool_path:
            p.error("--tool lmcache needs --tool-path")
        rows = run_lmcache(view, caps, a.chunk_tokens, a.tool_path)
    else:
        rows = run_kvsim(view, caps, a.chunk_tokens, a.policies.split(","))
    total = sum(len(v["keys"]) for v in view)
    res = {"tool": a.tool, "chunks_file": a.chunks, "hit_from": a.hit_from,
           "full_lookup": a.full_lookup,
           "chunk_tokens": a.chunk_tokens, "lookup_chunks": total,
           "unique_keys": len({k for v in view for k in v["keys"]}), "results": rows}
    with open(a.out, "w") as fh:
        json.dump(res, fh, indent=1)
    for row in rows:
        cap = row["capacity_chunks"]
        label = "inf" if cap is None else str(cap)
        if "all" in row:
            print(f"{label:>9}: all {row['all']['token_hit_rate'] * 100:6.2f}%  "
                  f"last50 {row['last50']['token_hit_rate'] * 100:6.2f}%")
        else:
            rates = {pol: row[pol]["token_hit_rate"] for pol in a.policies.split(",")}
            print(f"{label:>9}: last50 " + "  ".join(
                f"{k} {'n/a' if v is None else f'{v * 100:6.2f}%'}" for k, v in rates.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
