"""Unit tests for simulator paths that the xcheck comparisons do not reach.

xcheck feeds synthetic input only (one block per chunk, no decode, one rank, no
resets, no host tier, TP 1), so these paths are checked here.
"""

import gzip
import json

import pytest

from kvtrace.cli import main
from kvtrace.simulator import SimParams, parse_capacities, prepare, simulate

BS = 16
CHUNK = 32  # two blocks per chunk
PREFIX = ["a1", "a2", "a3", "a4"]


def S(ts, node, hashes, parent=None, rank=0):
    return {"t": "S", "ts": ts, "node": node, "rank": rank, "model": "m", "ns": "", "medium": "GPU",
            "bs": BS, "h": list(hashes), "p": parent, "n": len(hashes) * BS}


def C(ts, node, rank=0):
    return {"t": "C", "ts": ts, "node": node, "rank": rank}


def X(ts, node, rank=0):
    return {"t": "X", "ts": ts, "node": node, "rank": rank, "from_seq": 9, "to_seq": 0}


def run(records, capacity="inf", **kwargs):
    params = SimParams(chunk_tokens=CHUNK, capacities=parse_capacities(capacity),
                       kv_bytes_per_token=1, **kwargs)
    return simulate(records, params)


def test_decode_blocks_do_not_count_as_run_prefill_time():
    # 64 prefill tokens at 64 tokens/s: the run is scheduled 1 s before its first
    # store. The decode block after it adds no prefill time and no lookup.
    recs = [S(10, "A", PREFIX), S(10.2, "A", ["d1"], parent="a4")]
    params = SimParams(chunk_tokens=CHUNK, capacities=parse_capacities("inf"), kv_bytes_per_token=1,
                       decode_blocks="on", prefill_tokens_per_sec_per_request=64.0)
    prep = prepare(recs, params)
    assert prep.summary["tokens"]["decode"] == 16 and prep.summary["l2_lookups"] == 2
    assert {op[1] for op in prep.ops if op[0] == 0} == {9.0}


@pytest.mark.parametrize("gap, window, runs", [(0.5, 1.0, 1), (2.0, 1.0, 2), (2.0, 3.0, 1)])
def test_run_continue_seconds_joins_chunked_prefill(gap, window, runs):
    recs = [S(10, "A", PREFIX[:2]), S(10 + gap, "A", PREFIX[2:], parent="a2")]
    res = run(recs, run_continue_seconds=window, decode_blocks="off")
    assert res["trace"]["lookup_runs"] == runs and res["capacities"][0]["lookup_runs"] == runs
    # Joined: one scheduled time for both chunks, from the last store of the run (64
    # tokens at 32 tokens/s = 2 s before it) and never after the first store; split:
    # each run has its own.
    prep = prepare(recs, SimParams(chunk_tokens=CHUNK, capacities=parse_capacities("inf"),
                                   kv_bytes_per_token=1, run_continue_seconds=window,
                                   decode_blocks="off", prefill_tokens_per_sec_per_request=32.0))
    times = sorted(op[1] for op in prep.ops if op[0] == 0)
    joined = min(10.0, 10 + gap - 2.0)
    assert times == pytest.approx([joined, joined] if runs == 1 else [9.0, 9.0 + gap])


def test_ranks_keep_separate_l1_and_a_reset_clears_one_rank():
    recs = [S(1, "A", ["a1", "a2"], rank=0), S(2, "A", ["a1", "a2"], rank=1),
            C(3, "A", rank=0), S(4, "A", ["a1", "a2"], rank=0), S(5, "A", ["a1", "a2"], rank=1)]
    res = run(recs)
    t = res["trace"]
    assert t["ranks"] == ["A:0", "A:1"]
    # Rank 1 computes what rank 0 holds (separate L1); after the reset of rank 0, only
    # rank 0 computes again. Rank 1 still holds the block: a duplicate store.
    assert t["tokens"]["computed"] == 96 and t["tokens"]["duplicate"] == 32
    c = res["capacities"][0]
    assert c["lookups"] == 3 and c["hits"] == 2 and c["same_node_hits"] == 2


def test_publisher_restart_is_per_rank_and_clears_the_node_host_tier():
    recs = [S(1, "A", ["a1", "a2"], rank=0), X(2, "A", rank=1), S(3, "A", ["a1", "a2"], rank=0),
            S(4, "A", ["b1", "b2"], rank=1), X(5, "A", rank=1), S(6, "A", ["b1", "b2"], rank=0)]
    res = run(recs, host_tier_bytes_per_node=10 * CHUNK)
    t = res["trace"]
    assert t["gaps"]["restarts"] == 2
    # X on rank 1 keeps rank 0's L1 (a duplicate store at t=3) but clears the node's host
    # tier, so rank 0's chunk b at t=6 is an L2 hit, not a host hit.
    assert t["tokens"]["duplicate"] == 32
    c = res["capacities"][0]
    assert c["host_tier"]["hit_tokens"] == 0 and c["saved_tokens"] == 32


def test_host_tiers_of_two_nodes_are_separate():
    recs = [S(1, "A", ["a1", "a2"]), S(2, "B", ["a1", "a2"])]
    c = run(recs, host_tier_bytes_per_node=10 * CHUNK)["capacities"][0]
    assert c["host_tier"]["hit_tokens"] == 0
    assert c["cross_node_hits"] == 1 and c["saved_tokens"] == 32


def test_tp_sizing_from_one_sets_l2_chunk_capacity(tmp_path):
    trace = tmp_path / "t.jsonl.gz"
    with gzip.open(trace, "wt") as fh:
        fh.write(json.dumps(S(1, "A", ["a1", "a2"])) + "\n")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"num_hidden_layers": 94, "num_attention_heads": 64,
                               "num_key_value_heads": 4, "head_dim": 128, "torch_dtype": "bfloat16"}))
    chunks = {}
    for tp in (1, 2, 4, 8):
        out = tmp_path / f"sim{tp}.json"
        assert main(["simulate", str(trace), "--model-config", str(cfg), "--tp", str(tp),
                     "--chunk-tokens", "256", "--capacity", "1TB", "-o", str(out)]) == 0
        chunks[tp] = json.loads(out.read_text())["capacities"][0]["capacity_chunks"]
    # TP 1-4 split 4 KV heads (same L2 total); TP 8 copies each head to 2 workers.
    assert chunks[1] == chunks[2] == chunks[4] == 10**12 // (256 * 192512)
    assert chunks[8] == 10**12 // (256 * 2 * 192512)
