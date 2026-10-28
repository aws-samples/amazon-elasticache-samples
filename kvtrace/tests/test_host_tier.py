"""Tests for the emulated per-node host tier (--host-tier-bytes-per-node)."""

import gzip
import json

import pytest

from kvtrace.cli import main
from kvtrace.contention import ContentionParams
from kvtrace.simulator import SimParams, SimulationError, parse_capacities, simulate

BS = 16
CHUNK = 32  # two blocks per chunk
KVB = 1  # one chunk = 32 bytes
PREFIX = ["a1", "a2", "a3", "a4"]


def S(ts, node, hashes, parent=None, rank=0):
    return {"t": "S", "ts": ts, "node": node, "rank": rank, "model": "m", "ns": "", "medium": "GPU",
            "bs": BS, "h": list(hashes), "p": parent, "n": len(hashes) * BS}


def R(ts, node, hashes, rank=0):
    return {"t": "R", "ts": ts, "node": node, "rank": rank, "medium": "GPU", "h": list(hashes)}


def C(ts, node):
    return {"t": "C", "ts": ts, "node": node, "rank": 0}


def run(records, capacity="inf", host=None, touch=False):
    params = SimParams(chunk_tokens=CHUNK, capacities=parse_capacities(capacity),
                       kv_bytes_per_token=KVB, host_tier_bytes_per_node=host,
                       host_tier_touch_l2=touch)
    return simulate(records, params)


def test_reload_from_host_tier_is_not_an_l2_lookup():
    recs = [S(1, "A", PREFIX), R(2, "A", PREFIX), S(3, "A", PREFIX)]
    off = run(recs)
    assert off["capacities"][0]["saved_tokens"] == 64
    assert "host_tier" not in off["trace"] and "host_tier" not in off["capacities"][0]
    on = run(recs, host=10 * CHUNK)
    c = on["capacities"][0]
    assert c["lookups"] == 2 and c["saved_tokens"] == 0
    assert c["host_tier"]["hit_tokens"] == 64
    assert on["trace"]["host_tier"]["hit_tokens"] == 64
    assert on["trace"]["host_tier"]["chunks_per_node"] == 10
    assert on["trace"]["tokens"]["computed"] == 64


def test_host_tier_is_lru_with_its_capacity():
    # One chunk of host space: the first chunk is evicted by the second, so the
    # reload fetches chunk 1 from L2 and chunk 2 from the host tier.
    recs = [S(1, "A", PREFIX), R(2, "A", PREFIX), S(3, "A", PREFIX)]
    c = run(recs, host=CHUNK)["capacities"][0]
    assert c["saved_tokens"] == 32
    assert c["host_tier"]["hit_tokens"] == 32


def test_host_hit_after_l2_miss_is_prefix_blocked():
    # L2 holds one chunk: chunk 1 is evicted from L2 and from the host tier, so the
    # reload misses chunk 1 and cannot use the host copy of chunk 2.
    recs = [S(1, "A", PREFIX), R(2, "A", PREFIX), S(3, "A", PREFIX)]
    c = run(recs, capacity="32", host=CHUNK)["capacities"][0]
    assert c["saved_tokens"] == 0
    assert c["host_tier"]["hit_tokens"] == 0
    assert c["host_tier"]["prefix_blocked_tokens"] == 32


def test_gpu_held_prefix_refreshes_host_recency():
    # Host space for three chunks. A new run on top of the GPU-held prefix a1..a4
    # refreshes chunks a2 and a4, so the next insert evicts z2, not a2.
    recs = [S(1, "A", PREFIX), S(2, "A", ["z1", "z2"]), S(4, "A", ["b1", "b2"], parent="a4"),
            R(5, "A", PREFIX + ["z1", "z2", "b1", "b2"]),
            S(6, "A", PREFIX), S(8, "A", ["z1", "z2"])]
    res = run(recs, host=3 * CHUNK)
    c = res["capacities"][0]
    assert c["host_tier"]["hit_tokens"] == 64
    assert c["saved_tokens"] == 32  # z2 comes back from L2
    assert res["trace"]["host_tier"]["prefix_chunks_refreshed"] == 2


def test_touch_l2_keeps_host_served_prefix_in_l2():
    # L2 holds two chunks. Node A extends a1,a2 after z1,z2 was written: with the
    # touch, chunk a2 is the most recent L2 entry, so writing a4 evicts z2 and
    # node B later hits a2 and a4 in L2. Without the touch, a2 is evicted.
    recs = [S(1, "A", ["a1", "a2"]), S(2, "A", ["z1", "z2"]),
            S(4, "A", ["a3", "a4"], parent="a2"), S(6, "B", PREFIX)]
    plain = run(recs, capacity="64", host=10 * CHUNK)["capacities"][0]
    touched = run(recs, capacity="64", host=10 * CHUNK, touch=True)["capacities"][0]
    assert plain["saved_tokens"] == 0
    assert touched["saved_tokens"] == 64
    assert touched["host_tier"]["l2_refreshed"] == 1


def test_host_tier_options_are_validated():
    with pytest.raises(SimulationError):
        SimParams(capacities=parse_capacities("inf"), host_tier_touch_l2=True).validate()
    with pytest.raises(SimulationError):
        SimParams(capacities=parse_capacities("inf"), host_tier_bytes_per_node=100).validate()
    with pytest.raises(SimulationError):
        SimParams(capacities=parse_capacities("inf"), kv_bytes_per_token=1,
                  host_tier_bytes_per_node=0).validate()
    with pytest.raises(SimulationError):
        SimParams(chunk_tokens=32, capacities=parse_capacities("inf"), kv_bytes_per_token=1,
                  host_tier_bytes_per_node=31).validate()


def test_l2_hit_enables_later_host_hits():
    # Host space for one chunk: chunk a2 only comes from L2, chunk a4 from the host
    # tier. Without L2 the broken prefix makes a4 a recompute as well.
    recs = [S(1, "A", PREFIX), R(2, "A", PREFIX), S(3, "A", PREFIX)]
    params = SimParams(chunk_tokens=CHUNK, capacities=parse_capacities("0,inf"),
                       kv_bytes_per_token=KVB, host_tier_bytes_per_node=CHUNK)
    cp = ContentionParams(prefill_tokens_per_sec=32.0, l2_read_gbps_per_node=1e6, l2_rtt_ms=0.0)
    res = simulate(recs, params, contention=cp, keep_run_deltas=True)
    none, inf = res["capacities"]
    assert none["host_tier"] == {"hit_tokens": 0, "prefix_blocked_tokens": 32,
                                 "l2_enabled_tokens": 0, "l2_refreshed": 0}
    assert inf["saved_tokens"] == 32
    assert inf["host_tier"]["hit_tokens"] == 32 and inf["host_tier"]["l2_enabled_tokens"] == 32
    # One chunk recomputes in 1 s: fetching a2 saves a2 and a4, 2 s.
    deltas = [r["lmcache"] for r in inf["contention"].pop("run_deltas")]
    assert min(deltas) == pytest.approx(-2.0, abs=1e-3)
    assert [r["lmcache"] for r in none["contention"].pop("run_deltas")] == [0.0, 0.0]


def test_cache_reset_clears_the_host_tier():
    recs = [S(1, "A", PREFIX), C(2, "A"), S(3, "A", PREFIX)]
    c = run(recs, host=10 * CHUNK)["capacities"][0]
    assert c["host_tier"]["hit_tokens"] == 0
    assert c["saved_tokens"] == 64


def test_host_hit_in_a_chunked_prefill_continuation():
    # The second event continues the run (chunked prefill): a1 and a2 together
    # close chunk a2, which the host tier holds.
    recs = [S(1, "A", PREFIX), R(2, "A", PREFIX), S(3, "A", ["a1"]), S(3.2, "A", ["a2"], parent="a1")]
    c = run(recs, host=10 * CHUNK)["capacities"][0]
    assert c["host_tier"]["hit_tokens"] == 32
    assert c["lookups"] == 2


def test_host_tier_on_an_anchored_unknown_parent():
    recs = [S(1, "A", ["x3", "x4"], parent="u"), R(2, "A", ["x3", "x4"]),
            S(3, "A", ["x3", "x4"], parent="u")]
    res = run(recs, host=10 * CHUNK)
    assert res["capacities"][0]["host_tier"]["hit_tokens"] == 32
    assert res["trace"]["host_tier"]["explicit_host_media"] is False


def test_host_tier_capacity_is_per_node_not_per_rank():
    # One chunk of host space on node A, shared by DP ranks 0 and 1: rank 1's chunk
    # evicts rank 0's chunk, so rank 0's reload is an L2 hit, not a host hit.
    recs = [S(1, "A", ["a1", "a2"], rank=0), S(2, "A", ["b1", "b2"], rank=1),
            R(3, "A", ["a1", "a2"], rank=0), S(4, "A", ["a1", "a2"], rank=0)]
    res = run(recs, host=CHUNK)
    c = res["capacities"][0]
    assert c["host_tier"]["hit_tokens"] == 0
    assert c["saved_tokens"] == 32 and c["same_node_hits"] == 1
    assert res["trace"]["host_tier"]["chunks_per_node"] == 1


def test_ranks_of_a_node_share_its_host_tier():
    # Rank 1 computes a chunk that rank 0 put in the node's host tier: a host hit.
    recs = [S(1, "A", ["a1", "a2"], rank=0), S(2, "A", ["a1", "a2"], rank=1)]
    c = run(recs, host=10 * CHUNK)["capacities"][0]
    assert c["lookups"] == 1 and c["saved_tokens"] == 0
    assert c["host_tier"]["hit_tokens"] == 32


def test_cli_host_tier_flags(tmp_path):
    trace = tmp_path / "t.jsonl.gz"
    recs = [S(1, "A", PREFIX), R(2, "A", PREFIX), S(3, "A", PREFIX)]
    with gzip.open(trace, "wt") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in recs))
    out = tmp_path / "sim.json"
    assert main(["simulate", str(trace), "--kv-bytes-per-token", "1", "--chunk-tokens", "32",
                 "--capacity", "inf", "--host-tier-bytes-per-node", "320B", "--host-tier-touch-l2",
                 "-o", str(out)]) == 0
    sim = json.loads(out.read_text())
    assert sim["inputs"]["params"]["host_tier_bytes_per_node"] == 320
    assert sim["capacities"][0]["host_tier"]["hit_tokens"] == 64
    md = tmp_path / "r.md"
    assert main(["report", str(out), "-o", str(tmp_path / "r.html"), "--markdown", str(md)]) == 0
    assert "Emulated host tier hit tokens" in md.read_text()
    assert "Host hit tokens enabled by L2 hits" in md.read_text()
