#!/usr/bin/env python3
"""Synthetic kvtrace trace from a Mooncake trace, for offline what-if runs.

Emulates each node's local cache tiers for a Mooncake FAST'25 trace replay and
writes the kvtrace records those tiers would have published, so ``kvtrace
simulate`` can estimate what a shared L2 adds. No engine and no GPU are needed.

Per node (one engine replica):

- GPU prefix cache: LRU of ``--block-tokens`` blocks, ``--gpu-tokens`` in total
  (vLLM's free-block LRU: a request's blocks are freed tail first, so its last
  block is evicted first; blocks of running requests are not pinned).
- Optional host tier (LMCache local CPU): LRU of full ``--chunk-tokens`` chunks,
  ``--cpu-bytes`` in total at ``--kv-bytes-per-token``. A request loads back the
  longest prefix of chunks the tier holds beyond its GPU hit, and every full
  chunk of the request is stored (or refreshed) after its prefill.

Records per request, at its event time: one GPU ``S`` event for the blocks the
GPU did not hold (host-tier blocks are load-backs, the rest computed prefill),
``R`` GPU events for LRU evictions, and ``S``/``R`` events on medium ``CPU``
for host-tier stores and evictions. Decode blocks are not emitted. A block's
hash chains its prefix: ``(previous hash, hash_id, offset)``. Missing hash ids
(trace quirk) become request-unique pad blocks, as in the benchmark driver.

Routing and timing come from ``--requests`` (a driver log with ``line``,
``server``, ``t_send`` and ``ttft_s`` per request; event time = send time +
TTFT, i.e. prefill end), or else from ``--routing`` with event time =
``timestamp / 1000 / --time-scale``. ``cache_aware`` follows the SGLang-router
style rule without a load-balance step (offline there is no in-flight count):
the server owning the longest leading chain of the request's hash ids gets it
when that chain covers ``--cache-threshold`` of its blocks, else the server
owning the fewest blocks.

    python examples/scale_test/synth_from_mooncake.py conversation_trace.jsonl \\
        --out traces/synth --nodes 2 --routing cache_aware --time-scale 0.5 \\
        --gpu-tokens 1000000 --cpu-bytes 200e9 --kv-bytes-per-token 131072 \\
        --index traces/synth-requests.jsonl
    kvtrace simulate traces/synth --kv-bytes-per-token 131072 --run-continue-seconds 0 \\
        --decode-blocks off --capacity 1TB,inf
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, TextIO, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mooncake_replay import BLOCK_TOKENS, Request, load_trace, route  # noqa: E402

CPU = "CPU"
GPU = "GPU"


def block_key(prev: Optional[str], hash_id: Any, offset: int) -> str:
    """Chained 16-hex-digit block hash: equal keys mean an equal prefix."""
    return hashlib.blake2b(repr((prev, hash_id, offset)).encode(), digest_size=8).hexdigest()


def prompt_length(req: Request, max_model_len: Optional[int] = None) -> int:
    """Prompt tokens sent: cut so that prompt + output fits ``max_model_len``."""
    if not max_model_len:
        return req.input_length
    return min(req.input_length, max(1, max_model_len - max(1, req.output_length)))


def request_blocks(req: Request, block: int, max_model_len: Optional[int] = None) -> List[str]:
    """Keys of the request's full ``block``-token blocks (a partial last block is never cached)."""
    n_tokens = prompt_length(req, max_model_len)
    keys: List[str] = []
    prev: Optional[str] = None
    for j in range(n_tokens // block):
        off = j * block
        idx = off // BLOCK_TOKENS
        hid: Any = req.hash_ids[idx] if idx < len(req.hash_ids) else f"pad:{req.index}:{idx}"
        prev = block_key(prev, hid, off % BLOCK_TOKENS)
        keys.append(prev)
    return keys


def cache_aware_route(requests: Sequence[Request], n_nodes: int, threshold: float = 0.5,
                      max_model_len: Optional[int] = None) -> List[int]:
    """The benchmark driver's cache_aware rule without the load-balance step."""
    owner: Dict[int, int] = {}
    blocks = [0] * n_nodes
    out: List[int] = []
    for req in requests:
        n_blocks = -(-prompt_length(req, max_model_len) // BLOCK_TOKENS)
        chain: List[int] = []
        c = 17
        for h in req.hash_ids[:n_blocks]:
            c = hash((c, h))  # ints only: deterministic across processes
            chain.append(c)
        best, matched = None, 0
        for i, key in enumerate(chain):
            o = owner.get(key)
            if o is None:
                break
            best, matched = o, i + 1
        if best is not None and chain and matched / len(chain) >= threshold:
            node = best
        else:
            node = min(range(n_nodes), key=lambda n: (blocks[n], n))
        for key in chain:
            o = owner.get(key)
            if o != node:
                if o is not None:
                    blocks[o] -= 1
                owner[key] = node
                blocks[node] += 1
        out.append(node)
    return out


@dataclass
class NodeTiers:
    name: str
    gpu_blocks: int
    cpu_chunks: int
    gpu: OrderedDict[str, None] = field(default_factory=OrderedDict)
    cpu: OrderedDict[str, Tuple[str, ...]] = field(default_factory=OrderedDict)


@dataclass
class Outcome:
    gpu_hit_tokens: int
    cpu_hit_tokens: int
    computed_tokens: int
    full_tokens: int
    # Blocks the GPU held behind a missed block (not reachable as a prefix hit).
    gpu_orphan_tokens: int = 0


def process(tiers: NodeTiers, keys: List[str], ts: float, block: int, chunk: int,
            out: TextIO, model: str) -> Outcome:
    """Serve one request on one node's tiers and write its records."""
    per_chunk = chunk // block
    g = 0
    while g < len(keys) and keys[g] in tiers.gpu:
        g += 1
    n_chunks = len(keys) // per_chunk
    c = 0
    while c < n_chunks and keys[(c + 1) * per_chunk - 1] in tiers.cpu:
        c += 1
    loaded = max(g, c * per_chunk)

    def emit(rec: Dict[str, Any]) -> None:
        out.write(json.dumps(rec, separators=(",", ":")) + "\n")

    new = keys[g:]
    if new:
        emit({"t": "S", "ts": ts, "node": tiers.name, "rank": 0, "model": model, "ns": "",
              "medium": GPU, "bs": block, "h": new, "p": keys[g - 1] if g else None,
              "n": len(new) * block})
    orphans = sum(1 for k in new if k in tiers.gpu)
    # vLLM frees a finished request's blocks tail first, so the tail is evicted first
    # and the shared prefix stays longest: touch the blocks in reverse order.
    for k in reversed(keys):
        tiers.gpu[k] = None
        tiers.gpu.move_to_end(k)
    evicted: List[str] = []
    while len(tiers.gpu) > tiers.gpu_blocks:
        evicted.append(tiers.gpu.popitem(last=False)[0])
    if evicted:
        emit({"t": "R", "ts": ts, "node": tiers.name, "rank": 0, "medium": GPU, "h": evicted})

    if tiers.cpu_chunks > 0:
        stored: List[str] = []
        parent = None
        for i in range(n_chunks):
            ck = keys[(i + 1) * per_chunk - 1]
            if ck in tiers.cpu:
                tiers.cpu.move_to_end(ck)
                if stored:
                    emit({"t": "S", "ts": ts, "node": tiers.name, "rank": 0, "model": model,
                          "ns": "", "medium": CPU, "bs": block, "h": stored, "p": parent,
                          "n": len(stored) * block})
                    stored = []
                parent = ck
                continue
            if not stored:
                parent = keys[i * per_chunk - 1] if i else None
            blocks = tuple(keys[i * per_chunk:(i + 1) * per_chunk])
            tiers.cpu[ck] = blocks
            stored.extend(blocks)
        if stored:
            emit({"t": "S", "ts": ts, "node": tiers.name, "rank": 0, "model": model, "ns": "",
                  "medium": CPU, "bs": block, "h": stored, "p": parent, "n": len(stored) * block})
        dropped: List[str] = []
        while len(tiers.cpu) > tiers.cpu_chunks:
            dropped.extend(tiers.cpu.popitem(last=False)[1])
        if dropped:
            emit({"t": "R", "ts": ts, "node": tiers.name, "rank": 0, "medium": CPU, "h": dropped})
    return Outcome(gpu_hit_tokens=g * block, cpu_hit_tokens=(loaded - g) * block,
                   computed_tokens=(len(keys) - loaded) * block, full_tokens=len(keys) * block,
                   gpu_orphan_tokens=orphans * block)


def read_log(path: str) -> Dict[int, Dict[str, Any]]:
    """Driver log rows by trace line: only completed requests of the measured phase."""
    opener = gzip.open if path.endswith(".gz") else open
    rows: Dict[int, Dict[str, Any]] = {}
    with opener(path, "rt") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") != "ok" or row.get("phase", "measure") != "measure":
                continue
            rows[int(row["line"])] = row
    return rows


def schedule(requests: Sequence[Request], args: argparse.Namespace,
             log: Optional[Dict[int, Dict[str, Any]]]
             ) -> Iterator[Tuple[float, int, Request, Optional[Dict[str, Any]]]]:
    """(event time, node, request, log row) in event-time order."""
    items: List[Tuple[float, int, Request, Optional[Dict[str, Any]]]] = []
    if log is not None:
        servers = sorted({row["server"] for row in log.values()})
        t0 = min(row["t_send"] for row in log.values())
        for req in requests:
            row = log.get(req.index)
            if row is None:
                continue
            ts = row["t_send"] - t0 + float(row.get("ttft_s") or 0.0)
            items.append((ts, servers.index(row["server"]), req, row))
    else:
        if args.routing == "cache_aware":
            nodes = cache_aware_route(requests, args.nodes, args.cache_threshold, args.max_model_len)
        elif args.routing == "sticky":
            nodes = route(requests, args.nodes, "sticky")
        else:
            nodes = route(requests, args.nodes, "rr")
        for req, node in zip(requests, nodes):
            items.append((req.timestamp_ms / 1000.0 / args.time_scale, node, req, None))
    items.sort(key=lambda it: (it[0], it[2].index))
    return iter(items)


def generate(args: argparse.Namespace) -> Dict[str, Any]:
    requests = sorted(load_trace(args.trace), key=lambda r: (r.timestamp_ms, r.index))
    n_nodes = args.nodes
    log = read_log(args.requests) if args.requests else None
    if log is not None:
        if not log:
            raise SystemExit(f"ERROR: no completed measured requests in {args.requests}")
        n_nodes = len({row["server"] for row in log.values()})
    os.makedirs(args.out, exist_ok=True)
    cpu_chunks = int(args.cpu_bytes // (args.chunk_tokens * args.kv_bytes_per_token)) if args.cpu_bytes else 0
    tiers = [NodeTiers(f"node{n}", args.gpu_tokens // args.block_tokens, cpu_chunks) for n in range(n_nodes)]
    files: List[TextIO] = []
    for t in tiers:
        d = os.path.join(args.out, t.name)
        os.makedirs(d, exist_ok=True)
        fh = gzip.open(os.path.join(d, f"{t.name}-0-0.kvt.jsonl.gz"), "wt", encoding="utf-8")
        fh.write(json.dumps({"t": "H", "version": 1, "tool": "synth_from_mooncake", "node": t.name,
                             "model": args.model, "engine": "vllm", "anonymized": False,
                             "started": args.epoch}) + "\n")
        files.append(fh)
    index = open(args.index, "w", encoding="utf-8") if args.index else None
    totals: Counter = Counter()
    per_node: Counter = Counter()
    try:
        for ts, node, req, row in schedule(requests, args, log):
            keys = request_blocks(req, args.block_tokens, args.max_model_len)
            res = process(tiers[node], keys, args.epoch + ts, args.block_tokens, args.chunk_tokens,
                          files[node], args.model)
            n_in = prompt_length(req, args.max_model_len)
            totals["requests"] += 1
            totals["input_tokens"] += n_in
            totals["gpu_hit_tokens"] += res.gpu_hit_tokens
            totals["cpu_hit_tokens"] += res.cpu_hit_tokens
            totals["computed_full_block_tokens"] += res.computed_tokens
            totals["gpu_orphan_tokens"] += res.gpu_orphan_tokens
            per_node[tiers[node].name] += 1
            if index is not None:
                index.write(json.dumps({
                    "line": req.index, "node": tiers[node].name, "ts": args.epoch + ts,
                    "input_tokens": n_in, "gpu_hit_tokens": res.gpu_hit_tokens,
                    "cpu_hit_tokens": res.cpu_hit_tokens, "computed_tokens": n_in - res.gpu_hit_tokens
                    - res.cpu_hit_tokens, "ttft_s": row.get("ttft_s") if row else None}) + "\n")
    finally:
        for fh in files:
            fh.close()
        if index is not None:
            index.close()
    hit = totals["gpu_hit_tokens"] + totals["cpu_hit_tokens"]
    return {"requests": totals["requests"], "nodes": dict(per_node),
            "input_tokens": totals["input_tokens"], "gpu_hit_tokens": totals["gpu_hit_tokens"],
            "cpu_hit_tokens": totals["cpu_hit_tokens"],
            "local_hit_rate": hit / totals["input_tokens"] if totals["input_tokens"] else 0.0,
            "gpu_orphan_tokens": totals["gpu_orphan_tokens"],
            "gpu_blocks_per_node": tiers[0].gpu_blocks if tiers else 0,
            "cpu_chunks_per_node": cpu_chunks}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("trace", help="Mooncake trace (jsonl, optionally .gz)")
    p.add_argument("--out", required=True, help="output directory (one subdirectory per node)")
    p.add_argument("--requests", help="driver requests.jsonl(.gz): routing and timing per trace line")
    p.add_argument("--nodes", type=int, default=2, help="nodes when routing without --requests")
    p.add_argument("--routing", choices=("cache_aware", "sticky", "round_robin"), default="cache_aware")
    p.add_argument("--cache-threshold", type=float, default=0.5)
    p.add_argument("--time-scale", type=float, default=1.0, help="event time = trace time / this")
    p.add_argument("--gpu-tokens", type=int, required=True, help="GPU KV pool per node, tokens")
    p.add_argument("--cpu-bytes", type=float, default=0.0, help="host (LMCache CPU) tier per node, bytes")
    p.add_argument("--kv-bytes-per-token", type=int, default=0, help="per node; needed with --cpu-bytes")
    p.add_argument("--block-tokens", type=int, default=16)
    p.add_argument("--chunk-tokens", type=int, default=256)
    p.add_argument("--max-model-len", type=int, help="clamp prompts like the driver")
    p.add_argument("--model", default="mooncake")
    p.add_argument("--epoch", type=float, default=1.7e9, help="unix time of trace second 0")
    p.add_argument("--index", help="write a per-request JSON lines index (tier hits per request)")
    a = p.parse_args(argv)
    if a.cpu_bytes and a.kv_bytes_per_token <= 0:
        p.error("--cpu-bytes needs --kv-bytes-per-token")
    if a.chunk_tokens % a.block_tokens or a.time_scale <= 0 or a.nodes < 1:
        p.error("--chunk-tokens must be a multiple of --block-tokens; --time-scale and --nodes > 0")
    return a


def main(argv: Optional[Sequence[str]] = None) -> int:
    print(json.dumps(generate(parse_args(argv)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
