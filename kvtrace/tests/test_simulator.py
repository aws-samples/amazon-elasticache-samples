import gzip
import json
import os

import pytest
from conftest import workload_records

from kvtrace import costmodel as cm
from kvtrace.simulator import (
    SimParams,
    SimulationError,
    parse_capacities,
    parse_size,
    prepare,
    replay,
    simulate,
)

BS = 16
CHUNK = 32  # two blocks per chunk
KVB = 1  # 1 byte/token -> one chunk = 32 bytes, so capacity "64" holds two chunks


def S(ts, node, hashes, parent=None, medium="GPU", ns="", model="m", rank=0, bs=BS):
    return {"t": "S", "ts": ts, "node": node, "rank": rank, "model": model, "ns": ns,
            "medium": medium, "bs": bs, "h": list(hashes), "p": parent, "n": len(hashes) * bs}


def R(ts, node, hashes, medium="GPU", rank=0):
    return {"t": "R", "ts": ts, "node": node, "rank": rank, "medium": medium, "h": list(hashes)}


def C(ts, node, rank=0):
    return {"t": "C", "ts": ts, "node": node, "rank": rank}


def run(records, capacity="inf", **kwargs):
    params = SimParams(chunk_tokens=kwargs.pop("chunk_tokens", CHUNK),
                       capacities=parse_capacities(capacity), kv_bytes_per_token=KVB, **kwargs)
    return simulate(records, params)


def cap0(result):
    return result["capacities"][0]


PREFIX = ["a1", "a2", "a3", "a4"]


def test_parse_size():
    assert parse_size("64GiB") == 64 * 2**30
    assert parse_size("1TB") == 10**12
    assert parse_size("1.5GB") == 1_500_000_000
    assert parse_size("inf") is None
    with pytest.raises(SimulationError):
        parse_size("lots")


def test_same_node_recompute_after_gpu_eviction_hits_l2():
    res = run([S(1, "A", PREFIX), R(2, "A", PREFIX), S(3, "A", PREFIX)])
    c = cap0(res)
    assert c["lookups"] == 4 and c["hits"] == 2 and c["saved_tokens"] == 64
    assert c["same_node_hit_tokens"] == 64 and c["cross_node_hit_tokens"] == 0
    assert res["trace"]["tokens"]["computed"] == 128


def test_cross_node_hit():
    c = cap0(run([S(1, "A", PREFIX), S(2, "B", PREFIX)]))
    assert c["hits"] == 2 and c["cross_node_hits"] == 2 and c["same_node_hits"] == 0
    assert c["reuse_histogram"]["<1s"] == 0 and c["reuse_histogram"]["<10s"] == 2


def test_prefix_semantics_miss_blocks_later_chunks():
    # write-policy evict lets us put only the second chunk (a4) into L2.
    records = [
        S(1, "A", PREFIX),
        R(2, "A", ["a3", "a4"]),      # A drops its second chunk -> a4 written to L2
        S(3, "C", PREFIX),            # a2 misses, so a4 is not served although present
    ]
    c = cap0(run(records, write_policy="evict"))
    assert c["hits"] == 0 and c["prefix_blocked"] == 1
    assert c["lookups"] == 4 and c["writes"] == 1


def test_chunked_prefill_continues_one_run():
    records = [S(1.0, "A", ["a1"]), S(1.1, "A", ["a2", "a3", "a4"], parent="a1"),
               S(5.0, "B", PREFIX)]
    res = run(records)
    c = cap0(res)
    assert res["trace"]["lookup_runs"] == 2
    assert c["hits"] == 2 and c["saved_tokens"] == 64
    assert c["hit_tokens_histogram"] == [[32, 2]]


def test_late_continuation_starts_a_new_run_and_leaves_tail():
    res = run([S(1.0, "A", ["a1"]), S(9.0, "A", ["a2"], parent="a1")])
    assert res["trace"]["lookup_runs"] == 2
    # a1 never reached a boundary within its run: tail. a2 closes the chunk with 16 computed tokens.
    assert res["trace"]["tokens"]["tail"] == 16
    assert res["trace"]["l2_lookups"] == 1


@pytest.mark.parametrize("gap,expected_hits", [(0.1, 0), (10.0, 1)])
def test_run_miss_carries_across_chunked_prefill_events(gap, expected_hits):
    records = [
        S(1.0, "A", PREFIX),
        R(2.0, "A", ["a3", "a4"]),                          # only a4 reaches L2
        S(3.0, "B", ["a1", "a2"]),                          # a2 misses
        S(3.0 + gap, "B", ["a3", "a4"], parent="a2"),       # same run only when close in time
    ]
    c = cap0(run(records, write_policy="evict"))
    assert c["hits"] == expected_hits


def test_load_back_from_host_tier_is_not_a_lookup():
    records = [
        S(1, "A", ["a1", "a2"]),
        S(2, "A", ["a1", "a2"], medium="CPU_PINNED"),
        R(2, "A", ["a1", "a2"]),
        S(3, "A", ["a1", "a2"]),
    ]
    res = run(records)
    assert res["trace"]["tokens"]["loadback"] == 32
    assert res["trace"]["tokens"]["computed"] == 32
    assert res["trace"]["tokens"]["tier_move"] == 32
    assert cap0(res)["lookups"] == 1


def test_host_held_block_after_a_computed_block_is_recomputed():
    # The host tier holds only the second chunk: a host tier serves a prefix, so after
    # a1/a2 are computed the engine recomputes a3/a4 too, and L2 can serve them.
    records = [
        S(1, "A", PREFIX),
        S(2, "A", ["a3", "a4"], parent="a2", medium="CPU_PINNED"),
        R(2, "A", PREFIX),
        S(3, "A", PREFIX),
    ]
    res = run(records)
    assert res["trace"]["tokens"]["loadback"] == 0
    assert res["trace"]["tokens"]["computed"] == 128
    c = cap0(res)
    assert c["lookups"] == 4 and c["hits"] == 2 and c["saved_tokens"] == 64


def test_lru_evicts_least_recently_used():
    records = [
        S(1, "A", ["x1", "x2"]),
        S(2, "A", ["y1", "y2"]),
        S(3, "B", ["x1", "x2"]),   # hit refreshes x
        S(4, "C", ["z1", "z2"]),   # capacity 2: evicts y, not x
        S(5, "D", ["x1", "x2"]),   # hit
        S(6, "E", ["y1", "y2"]),   # miss
    ]
    c = cap0(run(records, capacity="64"))
    assert c["hits"] == 2 and c["evictions"] >= 1
    assert c["peak_entries"] == 2 and c["peak_used_bytes"] == 64


def test_ttl_expiry():
    records = [S(0, "A", ["x1", "x2"]), S(5, "B", ["x1", "x2"]), S(20, "C", ["x1", "x2"])]
    c = cap0(run(records, policy="ttl", ttl_seconds=10))
    assert c["hits"] == 1 and c["expirations"] == 1
    assert cap0(run(records))["hits"] == 2


def test_write_policy_evict():
    records = [
        S(1, "A", ["x1", "x2"]),
        S(2, "B", ["x1", "x2"]),          # miss: A has not dropped the chunk yet
        R(3, "A", ["x1", "x2"]),          # A drops it -> written to L2
        S(4, "C", ["x1", "x2"]),          # hit
    ]
    c = cap0(run(records, write_policy="evict"))
    assert c["lookups"] == 3 and c["hits"] == 1 and c["writes"] == 1


def test_write_policy_evict_waits_for_host_eviction():
    records = [
        S(1, "A", ["x1", "x2"]),
        S(2, "A", ["x1", "x2"], medium="CPU_PINNED"),
        R(2, "A", ["x1", "x2"]),                      # offload: still on node, no L2 write
        S(3, "B", ["x1", "x2"]),                      # miss
        R(4, "A", ["x1", "x2"], medium="CPU_PINNED"), # host eviction -> L2 write
        S(5, "C", ["x1", "x2"]),                      # hit
    ]
    c = cap0(run(records, write_policy="evict"))
    assert c["writes"] == 1 and c["hits"] == 1


def test_write_policy_evict_skips_chunks_closed_by_decode():
    # d1+d2 close a chunk with generated tokens only: dropping them writes nothing to L2,
    # as with write-policy store, which never looks such a chunk up.
    records = [S(1.0, "A", ["x1", "x2"]), S(1.2, "A", ["d1"], parent="x2"),
               S(1.4, "A", ["d2"], parent="d1"), R(2.0, "A", ["d1", "d2"])]
    c = cap0(run(records, write_policy="evict", decode_blocks="on"))
    assert c["writes"] == 0
    c = cap0(run([*records, R(3.0, "A", ["x1", "x2"])], write_policy="evict", decode_blocks="on"))
    assert c["writes"] == 1


def test_unknown_parent_is_unaligned_without_anchoring():
    res = run([S(1, "A", ["a3", "a4"], parent="never-seen")], anchor_unknown_parents=False)
    assert res["trace"]["tokens"]["unaligned"] == 32
    assert cap0(res)["lookups"] == 0


def test_unknown_parent_anchors_a_shared_chunk_grid():
    # Both nodes extend the same pre-recording prefix "p0": the grid starts at p0 for both,
    # so their chunk keys agree and B hits what A computed.
    records = [S(1, "A", ["a3", "a4"], parent="p0"), S(2, "B", ["a3", "a4"], parent="p0")]
    res = run(records)
    assert res["trace"]["tokens"]["unaligned"] == 0
    assert res["trace"]["anchored_unknown_parents"] == 1
    c = cap0(res)
    assert c["lookups"] == 2 and c["hits"] == 1 and c["cross_node_hits"] == 1


def test_anchored_tokens_count_computed_tokens_on_shifted_grids():
    records = [S(1, "A", ["a3", "a4"], parent="p0"), S(2, "A", ["a5"], parent="a4"),
               S(3, "B", PREFIX)]
    trace = run(records, decode_blocks="off")["trace"]
    # a3, a4 (anchored at p0) and a5 (extends the anchored chain); B's root chain is absolute.
    assert trace["anchored_tokens"] == 48
    assert run([S(3, "B", PREFIX)])["trace"]["anchored_tokens"] == 0


def test_one_block_continuation_is_decode_not_prefill():
    records = [S(1.0, "A", PREFIX), S(1.2, "A", ["d1"], parent="a4"),
               S(1.4, "A", ["d2"], parent="d1")]
    res = run(records, decode_blocks="on")
    tok = res["trace"]["tokens"]
    assert tok["computed"] == 64 and tok["decode"] == 32
    assert res["trace"]["l2_lookups"] == 2 and res["trace"]["lookup_runs"] == 1
    off = run(records, decode_blocks="off")["trace"]
    assert off["tokens"]["computed"] == 96 and off["l2_lookups"] == 3


def test_slow_decode_outside_run_window_is_still_decode():
    # 3 s between decode blocks (16 tokens at ~5 tok/s): beyond the 1 s chunked-prefill
    # window, but within the decode window.
    records = [S(1.0, "A", PREFIX), S(4.0, "A", ["d1"], parent="a4"),
               S(7.0, "A", ["d2"], parent="d1")]
    tok = run(records, decode_blocks="on")["trace"]["tokens"]
    assert tok["decode"] == 32 and tok["computed"] == 64
    short = run(records, decode_blocks="on", decode_window_seconds=1.0)["trace"]["tokens"]
    assert short["decode"] == 0 and short["computed"] == 96


def test_decode_blocks_auto_follows_engine_header():
    records = [S(1.0, "A", PREFIX), S(1.2, "A", ["d1"], parent="a4"),
               S(1.0, "B", PREFIX), S(1.2, "B", ["d1"], parent="a4")]
    header = {"t": "H", "node": "A", "engine": "vllm"}
    tok = run([header, *records])["trace"]["tokens"]
    # Only node A (vLLM) has its one-block continuation counted as decode.
    assert tok["decode"] == 16 and tok["duplicate"] == 0


def _auto_vs_vllm_metrics(extra):
    # A short --engine auto recording: the only header says "auto"; the engine is
    # known only from the node's metrics (or from an E record).
    records = [{"t": "H", "node": "A", "engine": "auto"}, S(1.0, "A", PREFIX),
               S(1.2, "A", ["d1"], parent="a4"), *extra]
    return run(records)["trace"]["tokens"]


def test_decode_blocks_auto_detects_vllm_from_metrics_despite_auto_header():
    metrics = {"t": "M", "ts": 5.0, "node": "A",
               "metrics": {'vllm:prompt_tokens_total{model_name="m"}': 1.0}}
    assert _auto_vs_vllm_metrics([metrics])["decode"] == 16
    sglang = dict(metrics, metrics={"sglang:prompt_tokens_total": 1.0})
    assert _auto_vs_vllm_metrics([sglang])["decode"] == 0
    assert _auto_vs_vllm_metrics([])["decode"] == 0


def test_decode_blocks_auto_honors_engine_record():
    engine = {"t": "E", "ts": 5.0, "node": "A", "engine": "vllm", "source": "metrics"}
    tok = _auto_vs_vllm_metrics([engine])
    assert tok["decode"] == 16 and tok["computed"] == 64


def test_real_vllm_trace_is_aligned_and_separates_decode():
    # 300 events captured from vLLM 0.31.0 (hashes only, node renamed, times rebased).
    # Recording began with blocks already cached, and decoding stores one block per event.
    path = os.path.join(os.path.dirname(__file__), "fixtures", "vllm031_events.jsonl.gz")
    with gzip.open(path, "rt") as fh:
        records = [json.loads(line) for line in fh]
    params = SimParams(chunk_tokens=256, capacities=parse_capacities("inf"), kv_bytes_per_token=1,
                       decode_blocks="on")
    trace = simulate(records, params)["trace"]
    tok = trace["tokens"]
    assert tok["unaligned"] == 0 and trace["anchored_unknown_parents"] > 0
    assert tok["decode"] > 0 and trace["l2_lookups"] > 0
    assert tok["computed"] + tok["decode"] == tok["stored"]
    old = simulate(records, SimParams(chunk_tokens=256, capacities=parse_capacities("inf"),
                                      kv_bytes_per_token=1, anchor_unknown_parents=False,
                                      decode_blocks="off"))["trace"]
    assert old["l2_lookups"] == 0 and old["tokens"]["unaligned"] == old["tokens"]["computed"]


def test_chunk_not_multiple_of_block_size_errors():
    with pytest.raises(SimulationError):
        run([S(1, "A", PREFIX)], chunk_tokens=24)


def test_odd_block_size_events_are_skipped_and_counted():
    res = run([S(1, "A", PREFIX), S(2, "A", ["odd"], parent="a4", bs=5), S(3, "B", PREFIX)])
    assert res["trace"]["skipped_block_sizes"] == {"5": 1}
    assert res["trace"]["tokens"]["bad_block_size"] == 5
    assert cap0(res)["hits"] == 2


def test_publisher_restart_resets_local_state():
    records = [S(1, "A", ["a1", "a2"]),
               {"t": "X", "ts": 2, "node": "A", "rank": 0, "from_seq": 9, "to_seq": 0},
               S(3, "A", ["a1", "a2"])]
    res = run(records)
    assert res["trace"]["tokens"]["computed"] == 64 and res["trace"]["tokens"]["duplicate"] == 0
    assert res["trace"]["gaps"]["restarts"] == 1
    c = cap0(res)
    assert c["lookups"] == 2 and c["hits"] == 1


def test_run_does_not_continue_across_reset():
    records = [S(1.0, "A", ["a1"]), C(1.1, "A"), S(1.2, "A", ["a2"], parent="a1")]
    res = run(records)
    assert res["trace"]["lookup_runs"] == 2
    # a1's run was retired as tail; a2 starts a fresh run whose parent depth is still known.
    assert res["trace"]["tokens"]["tail"] == 16 and res["trace"]["l2_lookups"] == 1


def test_rewrite_keeps_original_writer():
    # write-policy evict: node A writes the chunk; when node B drops the same chunk later,
    # the L2 entry still belongs to A, so C's hit counts as A's (cross-node from C).
    records = [S(1, "A", ["x1", "x2"]), R(2, "A", ["x1", "x2"]), S(3, "B", ["x1", "x2"]),
               R(4, "B", ["x1", "x2"]), S(5, "A", ["x1", "x2"])]
    c = cap0(run(records, write_policy="evict"))
    assert c["hits"] == 2 and c["same_node_hits"] == 1 and c["writes"] == 1


def test_namespaces_and_models_never_share():
    records = [S(1, "A", PREFIX), S(2, "B", PREFIX, ns="lora=x"), S(3, "C", PREFIX, model="other")]
    c = cap0(run(records))
    assert c["hits"] == 0
    assert set(c["per_model"]) == {"m", "other"}


def test_clear_forgets_local_state():
    records = [S(1, "A", ["a1", "a2"], medium="CPU_PINNED"), C(2, "A"), S(3, "A", ["a1", "a2"])]
    res = run(records)
    assert res["trace"]["tokens"]["loadback"] == 0 and res["trace"]["tokens"]["computed"] == 32


def test_finite_capacity_requires_kv_size():
    with pytest.raises(SimulationError):
        simulate([], SimParams(capacities=parse_capacities("1GiB")))


def test_inf_is_the_ceiling_and_hits_grow_with_capacity():
    records = workload_records(nodes=3, requests=150, gpu_tokens=4096)
    params = SimParams(chunk_tokens=64, kv_bytes_per_token=1024,
                       capacities=parse_capacities("256KiB,1MiB,4MiB,16MiB,inf"))
    res = simulate(records, params)
    saved = [c["saved_tokens"] for c in res["capacities"]]
    assert saved == sorted(saved) and saved[-1] > 0
    inf = res["capacities"][-1]
    assert inf["cross_node_hit_tokens"] > 0
    assert len(inf["hit_tokens_per_minute"]) >= 1
    assert sum(inf["hit_tokens_per_minute"]) == inf["saved_tokens"]


def test_replay_reuses_prepared_ops():
    params = SimParams(chunk_tokens=CHUNK, capacities=parse_capacities("inf"), kv_bytes_per_token=KVB)
    prep = prepare([S(1, "A", PREFIX), S(2, "B", PREFIX)], params)
    assert replay(prep, None, params) == replay(prep, None, params)


def M(ts, node, prompt_tokens, prefill_seconds):
    return {"t": "M", "ts": ts, "node": node, "metrics": {
        "vllm:prompt_tokens_total": float(prompt_tokens),
        "vllm:request_prefill_time_seconds_sum": float(prefill_seconds)}}


# A and B compute the same 64-token prefix; B is scheduled before A's store event.
OVERLAP = [S(10, "A", PREFIX), S(10.5, "B", PREFIX)]


def test_scheduled_lookup_misses_a_write_from_an_overlapping_prefill():
    # 64 tokens/s per request: A is scheduled at 9, B at 9.5 < A's store at 10.
    sched = run(OVERLAP, lookup_time="scheduled", prefill_tokens_per_sec_per_request=64.0)
    assert cap0(sched)["hits"] == 0 and cap0(sched)["lookups"] == 4 and cap0(sched)["writes"] == 2
    assert sched["lookup_time"] == {
        "requested": "scheduled", "model": "scheduled", "prefill_tokens_per_sec_per_request": 64.0,
        "rate_source": "input", "mean_shift_seconds": 1.0, "clamped_run_fraction": 0.0}
    store = run(OVERLAP, lookup_time="store")
    assert cap0(store)["hits"] == 2 and cap0(store)["cross_node_hit_tokens"] == 64
    assert store["lookup_time"]["model"] == "store"


def test_scheduled_lookup_rate_from_recorded_metrics():
    # 640 computed prompt tokens over 10 s of summed request prefill time = 64 tokens/s.
    metrics = [M(0, "A", 0, 0), M(20, "A", 640, 10)]
    res = run(OVERLAP + metrics)
    assert res["lookup_time"]["rate_source"] == "metrics"
    assert res["lookup_time"]["prefill_tokens_per_sec_per_request"] == pytest.approx(64.0)
    assert cap0(res)["hits"] == 0
    # Cached prompt tokens cost no prefill, so they do not count in the rate.
    cached = [dict(m, metrics=dict(m["metrics"], **{"vllm:prompt_tokens_cached_total": c}))
              for m, c in zip(metrics, (0.0, 320.0))]
    assert run(OVERLAP + cached)["lookup_time"]["prefill_tokens_per_sec_per_request"] == \
        pytest.approx(32.0)


def test_scheduled_lookup_without_a_rate_falls_back_to_store_time():
    res = run(OVERLAP)
    assert res["lookup_time"]["model"] == "store" and res["lookup_time"]["requested"] == "scheduled"
    assert any("lookup time = store-event time" in c for c in res["caveats"])
    assert cap0(res)["hits"] == 2


def test_scheduled_lookups_keep_prefix_order_and_time_order():
    # B's run is scheduled (at 9.6: its last store minus 1 s of prefill) before A's
    # store (at 10): ops stay sorted by time, and a chunked-prefill continuation keeps
    # its run's lookups after the first ones.
    recs = [S(10, "A", PREFIX), S(10.5, "B", PREFIX[:2]), S(10.6, "B", PREFIX[2:], parent="a2")]
    params = SimParams(chunk_tokens=CHUNK, capacities=parse_capacities("inf"), kv_bytes_per_token=KVB,
                       prefill_tokens_per_sec_per_request=64.0)
    prep = prepare(recs, params)
    times = [op[1] for op in prep.ops]
    assert times == sorted(times)
    b_lookups = [op for op in prep.ops if op[0] == 0 and op[3] == 1]
    assert [op[6] for op in b_lookups] == [32, 64]
    assert b_lookups[0][1] == b_lookups[1][1] == pytest.approx(9.6)
    assert cap0(simulate(recs, params))["hits"] == 0


def test_scheduled_lookup_counts_back_from_the_last_store_of_a_chunked_prefill():
    # B prefills 64 tokens in two chunks that end at 10.5 and 11.5. At 64 tokens/s the
    # prefill started 1 s before the last store (10.5), not 1 s before the first (9.5).
    recs = [S(10.2, "A", PREFIX), S(10.5, "B", PREFIX[:2]), S(11.5, "B", PREFIX[2:], parent="a2")]
    params = SimParams(chunk_tokens=CHUNK, capacities=parse_capacities("inf"), kv_bytes_per_token=KVB,
                       prefill_tokens_per_sec_per_request=64.0, run_continue_seconds=2.0)
    prep = prepare(recs, params)
    b_lookups = [op for op in prep.ops if op[0] == 0 and op[3] == 1]
    assert [op[1] for op in b_lookups] == pytest.approx([10.5, 10.5])
    assert prep.lookup_time["mean_shift_seconds"] == pytest.approx(0.5)
    # B is scheduled after A's store at 10.2, so B hits both of A's chunks.
    assert cap0(simulate(recs, params))["hits"] == 2


def test_scheduled_lookup_is_never_after_the_first_store_of_its_run():
    # 64 tokens at 640 tokens/s = 0.1 s, but the run stores from 10 to 12: the lookup
    # time clamps to the first store, not 11.9.
    recs = [S(10, "A", PREFIX[:2]), S(12, "A", PREFIX[2:], parent="a2")]
    params = SimParams(chunk_tokens=CHUNK, capacities=parse_capacities("inf"), kv_bytes_per_token=KVB,
                       prefill_tokens_per_sec_per_request=640.0, run_continue_seconds=3.0)
    prep = prepare(recs, params)
    assert {op[1] for op in prep.ops if op[0] == 0} == {10.0}
    assert prep.lookup_time["mean_shift_seconds"] == 0.0
    # The one run is clamped: sim.json reports the fraction and a caveat.
    assert prep.lookup_time["clamped_run_fraction"] == 1.0
    assert any("set to the first store event" in c for c in prep.caveats)


def test_clamped_run_caveat_only_above_a_quarter_of_the_runs():
    # Four runs on separate prefixes; only the first is clamped (its stores span 2 s).
    recs = [S(10, "A", ["c1", "c2"]), S(12, "A", ["c3", "c4"], parent="c2")]
    recs += [S(20 + i, "A", [f"p{i}a", f"p{i}b"]) for i in range(3)]
    params = SimParams(chunk_tokens=CHUNK, capacities=parse_capacities("inf"), kv_bytes_per_token=KVB,
                       prefill_tokens_per_sec_per_request=640.0, run_continue_seconds=3.0)
    prep = prepare(recs, params)
    assert prep.lookup_time["clamped_run_fraction"] == 0.25
    assert not any("set to the first store event" in c for c in prep.caveats)

def test_gauges_time_weighted_with_uneven_scrape_intervals():
    # Three scrapes 1 s apart see 4 running requests, then one scrape 8 s later sees 0.
    # Sample mean: 3.0. Time-weighted (trapezoid): (4 * 2 + (4 + 0) / 2 * 8) / 10 = 2.4.
    running = "vllm:num_requests_running"
    metrics = []
    for ts, value in ((0, 4.0), (1, 4.0), (2, 4.0), (10, 0.0)):
        rec = M(ts, "A", 64 * ts, ts)
        rec["metrics"][running] = value
        metrics.append(rec)
    res = run(OVERLAP + metrics)
    node = res["metrics"]["per_node"]["A"]
    assert node["gauges"][running] == pytest.approx(3.0)
    assert node["gauges_time_weighted"] == {running: pytest.approx(2.4)}
    assert cm.mean_running_requests(res) == pytest.approx(2.4)
    # A single scrape has no time span: its value is the mean.
    one = run(OVERLAP + metrics[:1])["metrics"]["per_node"]["A"]
    assert one["gauges_time_weighted"] == {running: 4.0}

def test_simulate_metrics_skip_a_partial_scrape_of_an_old_trace():
    # A trace recorded before kvtrace_url: one summed lmcache key for extra URLs A and B.
    # B misses the scrape at ts 1 (extra_ts names A only). True delta: 300 + 300 = 600;
    # reading the drop 2200 -> 1150 as a restart would give 2800.
    key = "lmcache:num_hit_tokens_total"
    recs = [{"t": "M", "ts": ts, "node": "A", "metrics": {key: value},
             "extra_ts": dict.fromkeys(urls, ts)}
            for ts, value, urls in ((0.0, 2200.0, "AB"), (1.0, 1150.0, "A"), (2.0, 2800.0, "AB"))]
    res = run(OVERLAP + recs)
    assert res["metrics"]["per_node"]["A"]["counters"][key] == 600.0
    # Without extra_ts there is nothing to tell the partial scrape from a restart.
    plain = [{k: v for k, v in rec.items() if k != "extra_ts"} for rec in recs]
    assert run(OVERLAP + plain)["metrics"]["per_node"]["A"]["counters"][key] == 2800.0

def test_cli_lookup_time_flags(tmp_path, capsys):
    from kvtrace.cli import main
    trace = tmp_path / "t.jsonl.gz"
    with gzip.open(trace, "wt") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in OVERLAP))
    out = tmp_path / "sim.json"
    base = ["simulate", str(trace), "--kv-bytes-per-token", "1", "--chunk-tokens", "32",
            "--capacity", "inf", "-o", str(out)]
    assert main(base + ["--prefill-tokens-per-sec-per-request", "64"]) == 0
    sim = json.loads(out.read_text())
    assert sim["lookup_time"]["model"] == "scheduled" and cap0(sim)["hits"] == 0
    assert "lookup time: scheduled" in capsys.readouterr().out
    assert main(base + ["--lookup-time", "store"]) == 0
    assert cap0(json.loads(out.read_text()))["hits"] == 2
    assert main(base + ["--prefill-tokens-per-sec-per-request", "0"]) == 2


def test_warmup_builds_state_but_is_not_counted():
    recs = [S(1, "A", PREFIX), R(2, "A", PREFIX), M(3, "A", 0, 0), M(4, "A", 100, 1),
            S(10, "A", PREFIX)]
    cold = run(recs, start=5)
    assert cap0(cold)["saved_tokens"] == 0 and cap0(cold)["lookups"] == 2
    warm = run(recs, start=5, warmup_seconds=10)
    c = cap0(warm)
    assert c["lookups"] == 2 and c["hits"] == 2 and c["saved_tokens"] == 64
    assert c["writes"] == 0 and c["lookup_runs"] == 1 and c["peak_entries"] == 2
    assert c["runs_histogram"] == [[64, 2, 1]]
    t = warm["trace"]
    assert t["tokens"]["computed"] == 64 and t["l2_lookups"] == 2 and t["lookup_runs"] == 1
    assert t["window"]["start"] == 10 and "M" not in t["record_counts"]
    assert t["record_counts"] == {"S": 1}
    assert t["warmup"] == {"seconds": 10, "from": -5, "ops": 4, "lookup_runs": 1,
                           "tokens": {"stored": 64, "computed": 64, "decode": 0}}
    assert warm["metrics"]["per_node"] == {}
    with pytest.raises(SimulationError):
        run(recs, warmup_seconds=10)


def test_chunked_run_crossing_start_is_counted_with_its_counted_hits_only():
    # Run 1 fills L2 in the warm-up. Run 2 is a chunked prefill: its first chunk is
    # looked up before --start (warm), its second after it (counted).
    recs = [S(1, "A", PREFIX), R(2, "A", PREFIX),
            S(4.5, "A", PREFIX[:2]), S(5.2, "A", PREFIX[2:], parent=PREFIX[1])]
    res = run(recs, start=5, warmup_seconds=10, lookup_time="store")
    c, t = cap0(res), res["trace"]
    assert c["lookups"] == 1 and c["hits"] == 1 and c["saved_tokens"] == 32
    # One rule in both places: run 2 is counted, run 1 is warm.
    assert c["lookup_runs"] == 1 and t["lookup_runs"] == 1 and t["warmup"]["lookup_runs"] == 1
    # Only the counted hit of run 2 is in the run histogram.
    assert c["runs_histogram"] == [[32, 1, 1]]
    assert c["hit_tokens_histogram"] == [[32, 1]]


def test_warmup_ops_stay_out_of_the_contention_timeline():
    from kvtrace.contention import ContentionParams
    recs = [S(1, "A", PREFIX), R(2, "A", PREFIX), S(10, "A", PREFIX)]
    params = SimParams(chunk_tokens=CHUNK, capacities=parse_capacities("inf"), kv_bytes_per_token=KVB,
                       start=5, warmup_seconds=10)
    out = cap0(simulate(recs, params, contention=ContentionParams(prefill_tokens_per_sec=100.0)))
    assert out["contention"]["runs"] == 1 and out["contention"]["hit_chunks"] == 2


def test_cli_warmup_seconds(tmp_path):
    from kvtrace.cli import main
    trace = tmp_path / "t.jsonl.gz"
    recs = [S(1, "A", PREFIX), R(2, "A", PREFIX), S(10, "A", PREFIX)]
    with gzip.open(trace, "wt") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in recs))
    out = tmp_path / "sim.json"
    assert main(["simulate", str(trace), "--kv-bytes-per-token", "1", "--chunk-tokens", "32",
                 "--capacity", "inf", "--start", "5", "--warmup-seconds", "10", "-o", str(out)]) == 0
    assert cap0(json.loads(out.read_text()))["saved_tokens"] == 64


def test_l2_per_key_overhead_and_usable_fraction_reduce_capacity():
    # Capacity 64 bytes = two 32-byte chunks by default, so both chunks of A fit. With
    # a per-key overhead or a usable fraction, only one fits.
    base = run([S(1, "A", PREFIX)], capacity="64")
    assert cap0(base)["capacity_chunks"] == 2
    assert any("no allocator, segment or drain-buffer overhead" in c for c in base["caveats"])
    over = run([S(1, "A", PREFIX)], capacity="64", l2_per_key_overhead_bytes=1)
    assert cap0(over)["capacity_chunks"] == 1 and cap0(over)["evictions"] == 1
    assert not any("drain-buffer" in c for c in over["caveats"])
    frac = run([S(1, "A", PREFIX)], capacity="100", l2_usable_fraction=0.5)
    assert cap0(frac)["capacity_chunks"] == 1
    # The overhead turns a hit into a miss: chunk 1 is evicted before the reload.
    recs = [S(1, "A", PREFIX), R(2, "A", PREFIX), S(3, "A", PREFIX)]
    assert cap0(run(recs, capacity="64"))["saved_tokens"] == 64
    assert cap0(run(recs, capacity="64", l2_per_key_overhead_bytes=1))["saved_tokens"] == 0
    with pytest.raises(SimulationError):
        run(recs, capacity="64", l2_usable_fraction=0)


def test_full_prompt_lookup_misses_after_l2_drops_an_l0_held_chunk():
    # L2 holds two chunks. c1 (a1, a2) is evicted by cx while A still holds it on GPU;
    # A then computes c2 (a3, a4) on top of c1.
    recs = [S(1, "A", PREFIX), S(2, "B", ["x1", "x2"]), R(3, "A", ["a3", "a4"]),
            S(4, "A", ["a3", "a4"], parent="a2")]
    computed = cap0(run(recs, capacity="64"))
    assert computed["hits"] == 1 and computed["saved_tokens"] == 32
    assert "l0_prefix" not in computed
    full = cap0(run(recs, capacity="64", l2_lookup="full-prompt"))
    # LMCache looks up c1 first, misses, and stops: c2 is recomputed.
    assert full["hits"] == 0 and full["saved_tokens"] == 0 and full["prefix_blocked"] == 1
    assert full["l0_prefix"] == {"lookups": 1, "hits": 0, "misses": 1}


def test_full_prompt_lookup_refreshes_l0_held_chunks_without_savings():
    # A's second run looks up c1 (held on A's GPU): no savings, but c1 stays recent, so
    # cy is evicted instead of c1 and node D later hits c1.
    recs = [S(1, "A", ["a1", "a2"]), S(1.5, "C", ["y1", "y2"]),
            S(3, "A", ["a3", "a4"], parent="a2"), S(5, "D", ["a1", "a2"])]
    computed = cap0(run(recs, capacity="64"))
    assert computed["saved_tokens"] == 0
    full = cap0(run(recs, capacity="64", l2_lookup="full-prompt"))
    assert full["l0_prefix"] == {"lookups": 1, "hits": 1, "misses": 0}
    assert full["saved_tokens"] == 32 and full["lookup_tokens"] == computed["lookup_tokens"]


def test_cli_l2_lookup_full_prompt(tmp_path):
    from kvtrace.cli import main
    trace = tmp_path / "t.jsonl.gz"
    recs = [S(1, "A", PREFIX), S(2, "B", ["x1", "x2"]), R(3, "A", ["a3", "a4"]),
            S(4, "A", ["a3", "a4"], parent="a2")]
    with gzip.open(trace, "wt") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in recs))
    out = tmp_path / "sim.json"
    assert main(["simulate", str(trace), "--kv-bytes-per-token", "1", "--chunk-tokens", "32",
                 "--capacity", "64", "--l2-lookup", "full-prompt", "-o", str(out)]) == 0
    sim = json.loads(out.read_text())
    assert sim["inputs"]["params"]["l2_lookup"] == "full-prompt" and cap0(sim)["hits"] == 0
