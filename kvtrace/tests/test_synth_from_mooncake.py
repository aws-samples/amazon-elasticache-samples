"""Tests for examples/scale_test/synth_from_mooncake.py."""

from __future__ import annotations

import gzip
import io
import json
import os
import sys

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
sys.path.insert(0, EXAMPLES)
sys.path.insert(0, os.path.join(EXAMPLES, "scale_test"))

import mooncake_replay as mr  # noqa: E402
import synth_from_mooncake as sm  # noqa: E402

from kvtrace.segments import TracePaths, iter_records  # noqa: E402
from kvtrace.simulator import SimParams, parse_capacities, simulate  # noqa: E402


def req(i, ts, inp, hashes, out=1):
    return mr.Request(i, ts, inp, out, hashes)


def test_request_blocks_chain_prefixes_and_pad_missing_hash_ids():
    a = sm.request_blocks(req(0, 0, 1024, [1, 2]), 16)
    b = sm.request_blocks(req(1, 0, 1030, [1, 3, 4]), 16)
    assert len(a) == 64 and len(b) == 64  # the partial last block is not cached
    assert a[:32] == b[:32] and a[32] != b[32]
    # Same hash id after a different prefix is a different block.
    c = sm.request_blocks(req(2, 0, 1024, [9, 2]), 16)
    assert c[32] != a[32]
    # Fewer hash ids than blocks: request-unique pad blocks, as in the benchmark driver.
    p1 = sm.request_blocks(req(3, 0, 1024, [1]), 16)
    p2 = sm.request_blocks(req(4, 0, 1024, [1]), 16)
    assert p1[:32] == a[:32] and p1[32] != p2[32]
    assert len(sm.request_blocks(req(5, 0, 1024, [1, 2], out=100), 16, max_model_len=612)) == 32


def test_cache_aware_route_follows_owner_or_least_loaded():
    reqs = [req(0, 0, 1024, [1, 2]), req(1, 1, 512, [7]), req(2, 2, 1536, [1, 2, 3]),
            req(3, 3, 2048, [1, 5, 6, 8])]
    # r0 -> node 0 (tie); r1 new -> node 1 (fewest blocks); r2 matches 2/3 -> owner 0;
    # r3 matches 1/4 < 0.5 -> fewest blocks (node 1).
    assert sm.cache_aware_route(reqs, 2) == [0, 1, 0, 1]


def _records(text):
    return [json.loads(line) for line in text.splitlines()]


def test_process_gpu_hits_loadbacks_and_evictions():
    tiers = sm.NodeTiers("n", gpu_blocks=4, cpu_chunks=2)
    keys_a = [f"a{i}" for i in range(4)]  # two chunks of two blocks
    keys_b = [f"b{i}" for i in range(4)]
    out = io.StringIO()
    first = sm.process(tiers, keys_a, 1.0, 16, 32, out, "m")
    assert (first.gpu_hit_tokens, first.cpu_hit_tokens, first.computed_tokens) == (0, 0, 64)
    second = sm.process(tiers, keys_b, 2.0, 16, 32, out, "m")
    assert second.computed_tokens == 64
    recs = _records(out.getvalue())
    gpu_evicted = [h for r in recs if r["t"] == "R" and r["medium"] == "GPU" for h in r["h"]]
    cpu_evicted = [h for r in recs if r["t"] == "R" and r["medium"] == "CPU" for h in r["h"]]
    # The GPU evicts a request's tail first (vLLM frees blocks in reverse order).
    assert gpu_evicted == keys_a[::-1] and cpu_evicted == keys_a
    out = io.StringIO()
    third = sm.process(tiers, keys_b, 3.0, 16, 32, out, "m")
    assert (third.gpu_hit_tokens, third.computed_tokens) == (64, 0) and out.getvalue() == ""
    # Evict b from the GPU only; the CPU tier still holds both chunks of b.
    sm.process(tiers, [f"c{i}" for i in range(4)], 4.0, 16, 32, io.StringIO(), "m")
    tiers.cpu_chunks = 4
    tiers.cpu.update({k: v for k, v in [(keys_b[1], tuple(keys_b[:2])), (keys_b[3], tuple(keys_b[2:]))]})
    fourth = sm.process(tiers, keys_b, 5.0, 16, 32, io.StringIO(), "m")
    assert (fourth.gpu_hit_tokens, fourth.cpu_hit_tokens, fourth.computed_tokens) == (0, 64, 0)


def test_gpu_keeps_the_shared_prefix_when_the_pool_overflows():
    tiers = sm.NodeTiers("n", gpu_blocks=6, cpu_chunks=0)
    keys_a = [f"a{i}" for i in range(4)]
    out = io.StringIO()
    sm.process(tiers, keys_a, 1.0, 16, 32, out, "m")
    sm.process(tiers, [f"b{i}" for i in range(4)], 2.0, 16, 32, out, "m")
    evicted = [h for r in _records(out.getvalue()) if r["t"] == "R" for h in r["h"]]
    assert evicted == ["a3", "a2"]
    again = sm.process(tiers, keys_a, 3.0, 16, 32, io.StringIO(), "m")
    assert (again.gpu_hit_tokens, again.computed_tokens, again.gpu_orphan_tokens) == (32, 32, 0)


def _trace(tmp_path, rows):
    path = tmp_path / "trace.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


def test_generated_trace_simulates_like_the_emulated_tiers(tmp_path):
    shared = [1, 2, 3, 4]
    rows = []
    for i in range(12):
        rows.append({"timestamp": i * 1000, "input_length": 512 * 6, "output_length": 4,
                     "hash_ids": shared + [100 + i, 200 + i]})
    trace = _trace(tmp_path, rows)
    out = tmp_path / "synth"
    index = tmp_path / "index.jsonl"
    summary = sm.generate(sm.parse_args([trace, "--out", str(out), "--nodes", "2", "--routing", "round_robin",
                                         "--gpu-tokens", "4096", "--cpu-bytes", "4096",
                                         "--kv-bytes-per-token", "1", "--index", str(index)]))
    assert summary["requests"] == 12 and summary["nodes"] == {"node0": 6, "node1": 6}
    rows_idx = [json.loads(line) for line in index.read_text().splitlines()]
    assert sum(r["gpu_hit_tokens"] + r["cpu_hit_tokens"] for r in rows_idx) == (
        summary["gpu_hit_tokens"] + summary["cpu_hit_tokens"])
    params = SimParams(chunk_tokens=256, capacities=parse_capacities("inf"), kv_bytes_per_token=1,
                       run_continue_seconds=0.0, decode_blocks="off")
    with TracePaths([str(out)]) as files:
        sim = simulate(iter_records(files, {}), params)
    tok = sim["trace"]["tokens"]
    assert tok["computed"] == summary["input_tokens"] - summary["gpu_hit_tokens"] - summary["cpu_hit_tokens"]
    assert tok["loadback"] == summary["cpu_hit_tokens"] and tok["unaligned"] == 0
    # The shared 2048-token prefix is computed once per node; the other node then hits L2.
    cap = sim["capacities"][0]
    assert cap["cross_node_hits"] > 0 and sim["trace"]["lookup_runs"] == 12


def test_requests_log_sets_routing_and_event_time(tmp_path):
    rows = [{"timestamp": i * 1000, "input_length": 512, "output_length": 1, "hash_ids": [i]}
            for i in range(3)]
    trace = _trace(tmp_path, rows)
    log = tmp_path / "requests.jsonl.gz"
    with gzip.open(log, "wt") as fh:
        for line, server, t_send in ((0, "s2", 10.0), (1, "s1", 11.0), (2, "s2", 12.0)):
            fh.write(json.dumps({"line": line, "server": server, "t_send": t_send, "ttft_s": 0.5,
                                 "status": "ok", "phase": "measure"}) + "\n")
        fh.write(json.dumps({"line": 2, "server": "s1", "t_send": 0, "status": "error"}) + "\n")
    index = tmp_path / "index.jsonl"
    sm.generate(sm.parse_args([trace, "--out", str(tmp_path / "o"), "--requests", str(log),
                               "--gpu-tokens", "1024", "--epoch", "0", "--index", str(index)]))
    got = [(r["line"], r["node"], r["ts"]) for r in map(json.loads, index.read_text().splitlines())]
    assert got == [(0, "node1", 0.5), (1, "node0", 1.5), (2, "node1", 2.5)]
