"""Phase 1 simulation options: rewrites, static fetch policies, recency, admission, tiles,
the rtt shortcut and X_t* bisection, the OFF TTFT join and the store-mode refusal.
"""

import gzip
import json
import os
import sys
from dataclasses import replace

import pytest
from conftest import workload_records

from kvtrace.cli import main
from kvtrace.contention import (
    HIT,
    MISS,
    WRITE,
    ContentionParams,
    auto_min_tokens,
    evaluate,
    join_off_ttft,
    normalize_policy,
    stall_sweep,
    ttft_change,
)
from kvtrace.segments import TracePaths, iter_records
from kvtrace.simulator import (
    EVICT_WRITE,
    HOST_HIT,
    LOOKUP,
    STORE,
    STORE_FALLBACK_REFUSAL,
    Prepared,
    SimParams,
    SimulationError,
    parse_capacities,
    prepare,
    read_off_ttft,
    replay,
    simulate,
    static_policy,
)

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
sys.path.insert(0, EXAMPLES)
sys.path.insert(0, os.path.join(EXAMPLES, "scale_test"))

import synth_from_mooncake as sm  # noqa: E402

C = 256  # chunk tokens; with kv_bytes_per_token 1 a chunk is 256 bytes


def make_prep(ops, n_keys, nodes=("a",), duration=100.0, epoch=None):
    prep = Prepared(ops=list(ops), key_model=[0] * n_keys, models=["m"], nodes=list(nodes),
                    summary={"window": {"start": 0.0, "end": duration, "duration_seconds": duration}})
    prep.trace_epoch = epoch
    return prep


def sim_params(**kwargs):
    base = dict(chunk_tokens=C, capacities=parse_capacities("inf"), kv_bytes_per_token=1)
    base.update(kwargs)
    return SimParams(**base)


def lookup(ts, key, run, node=0, end=None):
    return (LOOKUP, ts, key, node, run, C, end if end is not None else (key + 1) * C)


def store(ts, key, run, node=0):
    return (STORE, ts, key, node, run, 0, (key + 1) * C)


def write_segment(tmp_path, records, name="n0"):
    seg = tmp_path / name / f"{name}-0-0.kvt.jsonl.gz"
    seg.parent.mkdir()
    with gzip.open(seg, "wt") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return seg.parent


# Test 2: rewrites.

PREFIX_BLOCKED_OPS = [
    lookup(0, 1, 0), store(0, 1, 0),
    # Run 1 misses key 0, so key 1 (in L2) is prefix-blocked and is written again.
    lookup(1, 0, 1), lookup(1, 1, 1), store(1, 0, 1), store(1, 1, 1),
]


def test_count_rewrites_counts_a_prefix_blocked_chunk_as_a_write():
    prep = make_prep(PREFIX_BLOCKED_OPS, 2)
    plain = replay(prep, None, sim_params())
    counted = replay(prep, None, sim_params(count_rewrites=True), track=True)
    assert plain["prefix_blocked"] == 1 and plain["writes"] == 2 and "rewrites" not in plain
    assert counted["writes"] == 3 and counted["rewrites"] == 1
    assert counted["rewrites_by_cause"]["prefix_blocked"] == 1
    timeline = []
    replay(prep, None, sim_params(count_rewrites=True), timeline)
    assert sum(1 for ev in timeline if ev[0] == WRITE) == 3


def test_fetch_policies_turn_count_rewrites_on_and_say_so(tmp_path, capsys):
    trace = write_segment(tmp_path, workload_records(nodes=1, requests=80, gpu_tokens=2048))
    out = tmp_path / "sim.json"
    assert main(["simulate", str(trace), "--chunk-tokens", "64", "--kv-bytes-per-token", "8",
                 "--capacity", "4KiB,inf", "--prefill-tokens-per-sec-per-request", "2000",
                 "--fetch-policies", "fetch-all,max-load:4096", "-o", str(out)]) == 0
    assert "count-rewrites: on (auto)" in capsys.readouterr().out
    sim = json.loads(out.read_text())
    params = sim["inputs"]["params"]
    assert params["count_rewrites"] is True and params["count_rewrites_source"] == "auto"
    assert params["fetch_policies"] == ["lmcache", "max-load:4096"]
    for cap in sim["capacities"]:
        assert "rewrites" in cap and "max-load:4096" in cap["fetch_policies"]
    assert any("on (auto)" in c for c in sim["caveats"])


def test_policy_ids_accept_display_and_internal_forms():
    assert normalize_policy("oracle") == "ideal" and normalize_policy("ideal") == "ideal"
    assert normalize_policy("fetch-all") == "lmcache"
    assert normalize_policy("min-tokens:auto") == "min-tokens:auto"
    with pytest.raises(ValueError):
        normalize_policy("max-load:lots")


# Test 3: recency with --lookup-touch.

RECENCY_OPS = [
    lookup(0, 0, 0), store(0, 0, 0),
    lookup(1, 1, 1), store(1, 1, 1),
    lookup(2, 0, 2),                      # a hit; only a refresh keeps key 0 newer than key 1
    lookup(3, 2, 3), store(3, 2, 3),      # evicts the least recent of keys 0 and 1
    lookup(4, 0, 4),
]


@pytest.mark.parametrize("touch, later_hit", [("off", False), ("on", True)])
def test_lookup_touch_off_refreshes_only_fetched_hits(touch, later_hit):
    prep = make_prep(RECENCY_OPS, 3)
    params = sim_params(lookup_touch=touch)
    fetch_all = replay(prep, 2 * C, params)
    skip_all = replay(prep, 2 * C, params, policy=static_policy("min-tokens:100000", C), track=True)
    assert fetch_all["hits"] == 2  # fetch-all refreshes key 0 at t=2 in both modes
    assert skip_all["hits"] == 0
    # With touch on, run 4 still finds key 0 (an unfetched hit); with touch off it misses.
    assert skip_all["fetch_policy"]["unfetched_hits"] == (2 if later_hit else 1)


def test_max_load_writes_the_chunks_after_the_cap_again():
    ops = [lookup(0, 0, 0), lookup(0, 1, 0), lookup(0, 2, 0), store(0, 0, 0), store(0, 1, 0), store(0, 2, 0),
           lookup(1, 0, 1), lookup(1, 1, 1), lookup(1, 2, 1), store(1, 0, 1), store(1, 1, 1), store(1, 2, 1)]
    prep = make_prep(ops, 3)
    params = sim_params(count_rewrites=True)
    capped = replay(prep, None, params, policy=static_policy("max-load:512", C), track=True)
    assert capped["hits"] == 2 and capped["fetch_policy"]["unfetched_hits"] == 1
    assert capped["rewrites_by_cause"]["max_load_cap"] == 1 and capped["writes"] == 4
    skipped = replay(prep, None, params, policy=static_policy("min-tokens:10000", C), track=True)
    # min-tokens skips the true hit count, so LMCache does not write the skipped hits again.
    assert skipped["hits"] == 0 and skipped["writes"] == 3 and skipped["rewrites"] == 0


# Test 4: write admission.

def test_second_hit_admits_a_chunk_on_its_second_miss_for_both_write_kinds():
    ops = [lookup(0, 0, 0), store(0, 0, 0), lookup(1, 0, 1), store(1, 0, 1), lookup(2, 0, 2)]
    res = replay(make_prep(ops, 1), None, sim_params(admission="second-hit"), track=True)
    assert res["writes"] == 1 and res["hits"] == 1 and res["admission"]["dropped_writes"] == 1
    evict = [(EVICT_WRITE, 0, 0, 0, -1, 0, 0), (EVICT_WRITE, 1, 0, 0, -1, 0, 0), lookup(2, 0, 2)]
    evict_params = sim_params(admission="second-hit", write_policy="evict")
    res = replay(make_prep(evict, 1), None, evict_params, track=True)
    assert res["writes"] == 1 and res["hits"] == 1 and res["admission"]["dropped_writes"] == 1


def test_none_after_stops_writes_but_keeps_store_ts(tmp_path):
    ops = [lookup(104, 0, 0), store(104, 0, 0), lookup(106, 1, 1), store(106, 1, 1)]
    res = replay(make_prep(ops, 2, epoch=100.0), None, sim_params(admission="none-after:5"), track=True)
    assert res["writes"] == 1 and res["admission"]["cutoff_ts"] == 105.0
    assert res["admission"]["epoch_source"] == "trace header started"

    synth, index, _log = _synth(tmp_path)
    with open(index) as fh:
        requests = [(r["node"], r["ts"], r["ttft_s"]) for r in map(json.loads, fh)]
    params = SimParams(chunk_tokens=256, capacities=parse_capacities("inf"), kv_bytes_per_token=8,
                       prefill_tokens_per_sec_per_request=5000.0)
    cp = ContentionParams(prefill_tokens_per_sec=2000.0, l2_read_gbps_per_node=1.0)
    caps = {}
    for name, admission in (("all", "all"), ("cut", "none-after:10")):
        with TracePaths([str(synth)]) as files:
            sim = simulate(iter_records(files, {}), replace(params, admission=admission), contention=cp,
                           keep_run_deltas=True, off_ttft=requests)
        caps[name] = sim["capacities"][0]
    cutoff = caps["cut"]["admission"]["cutoff_ts"]
    assert caps["cut"]["writes"] < caps["all"]["writes"]
    rows = {name: {r["run"]: r["store_ts"] for r in cap["contention"]["run_deltas"]}
            for name, cap in caps.items()}
    late = [rid for rid, ts in rows["all"].items() if ts >= cutoff]
    assert late and all(rows["cut"][rid] == rows["all"][rid] for rid in late)
    join = join_off_ttft(caps["cut"]["contention"]["run_deltas"], requests)
    joined = {rid for rids in join.runs for rid in rids}
    assert set(late) <= joined
    assert caps["cut"]["contention"]["off_ttft_join"]["run_join_rate"] == 1.0


def test_oracle_useful_writes_only_chunks_that_get_a_hit():
    recs = workload_records(nodes=2, requests=150, gpu_tokens=4096)
    base = SimParams(chunk_tokens=64, capacities=parse_capacities("16KiB,inf"), kv_bytes_per_token=8,
                     prefill_tokens_per_sec_per_request=2000.0, count_rewrites=True)
    every = simulate(recs, base)["capacities"]
    useful = simulate(recs, SimParams(**{**base.__dict__, "admission": "oracle-useful"}))
    for a, b in zip(every, useful["capacities"]):
        assert b["writes"] < a["writes"]
        assert b["write_metrics"]["dead_writes"]["total"] < a["write_metrics"]["dead_writes"]["total"]
    inf_all, inf_useful = every[1], useful["capacities"][1]
    # With no eviction, the useful writes are exactly the written chunks that got a hit.
    assert inf_useful["hits"] == inf_all["hits"] and inf_useful["write_metrics"]["dead_writes"]["total"] == 0
    assert any("approximate upper bound" in c for c in useful["caveats"])


def test_lookup_timeout_gives_no_hits_when_the_check_waits_too_long():
    events = [(HIT, 0, 0, 0, 256, 256), (HIT, 0, 0, 1, 256, 256)]
    cp = ContentionParams(prefill_tokens_per_sec=100.0, l2_read_gbps_per_node=8e-6, l2_rtt_ms=0.0,
                          l2_lookup_ms=100.0)
    out = evaluate(events, 1000, ["a"], 10.0, cp, keep_runs=True,
                   policies=("ideal", "lmcache", "lookup-timeout:150"), extended=True)
    rows = {r["run"]: r for r in out["run_deltas"]}
    # Run 1's check waits behind run 0's: 0.2 s > 0.15 s, so it waits 0.15 s and fetches nothing.
    assert rows[1]["lookup-timeout:150"] == pytest.approx(0.15)
    assert rows[1]["lookup-timeout:150_fetched_chunks"] == 0
    assert rows[0]["lookup-timeout:150"] == pytest.approx(rows[0]["lmcache"])
    assert out["policies"]["lookup-timeout:150"]["lookup_timeouts"] == 1
    with pytest.raises(ValueError, match="l2-lookup-ms"):
        evaluate(events, 1000, ["a"], 10.0, ContentionParams(prefill_tokens_per_sec=100.0),
                 policies=("lookup-timeout:150",))


# Test 5: oracle equals min-tokens:auto in the linear model with no stall or queue.

def test_oracle_equals_min_tokens_auto_without_stall_queue_or_cache_nodes():
    ops, key, run = [], 0, 0
    for k in range(1, 7):
        keys = list(range(key, key + k))
        key += k
        t = 100.0 * k
        ops += [lookup(t, kk, run, end=(i + 1) * C) for i, kk in enumerate(keys)]
        ops += [store(t, kk, run) for kk in keys]
        ops += [lookup(t + 50, kk, run + 1, end=(i + 1) * C) for i, kk in enumerate(keys)]
        run += 2
    prep = make_prep(ops, key, duration=1000.0)
    params = sim_params()
    # rc = 256 / 1000 = 0.256 s, transfer = 256 * 8 / 1e5 = 0.02048 s; rtt / gain = 2.12 chunks.
    cp = ContentionParams(prefill_tokens_per_sec=1000.0, l2_read_gbps_per_node=1e-4,
                          l2_write_gbps_per_node=1e3, l2_rtt_ms=500.0)
    base_tl = []
    replay(prep, None, params, base_tl)
    base = evaluate(base_tl, C, prep.nodes, 1000.0, cp, keep_runs=True)
    auto = auto_min_tokens(cp, C, C, base)
    assert auto["min_tokens"] == 3 * C
    sp_tl = []
    policy = static_policy("min-tokens:auto", C, min_tokens=auto["min_tokens"])
    replay(prep, None, params, sp_tl, policy=policy)
    sp = evaluate(sp_tl, C, prep.nodes, 1000.0, cp, keep_runs=True, policies=("lmcache",))
    oracle = {r["run"]: r["ideal"] for r in base["run_deltas"]}
    got = {r["run"]: r["lmcache"] for r in sp["run_deltas"]}
    assert got.keys() == oracle.keys()
    for rid, value in oracle.items():
        assert got[rid] == pytest.approx(value)
    assert any(v < 0 for v in oracle.values()) and any(r["ideal_fetched_chunks"] == 0 and r["hit_tokens"]
                                                       for r in base["run_deltas"])


# Test 6: rtt shortcut, X_t* bisection and runs with zero trailing write rate.

GB = 10**9


def _stall_events():
    events = [(MISS, 0, 0, 100, 256, 256)] + [(WRITE, 0, 0, 100, 0, 0)] * 60
    # Node 0 (writes 1 GB/s): one hit and one computed chunk. Node 1 writes nothing.
    events += [(HIT, 10, 0, 1, 256, 256), (MISS, 10, 0, 1, 256, 512),
               (HIT, 10, 1, 2, 256, 256), (MISS, 10, 1, 2, 256, 512)]
    return events


def _cp(**kwargs):
    base = dict(prefill_tokens_per_sec=100.0, l2_read_gbps_per_node=8000.0, l2_write_gbps_per_node=8000.0,
                l2_rtt_ms=0.0)
    base.update(kwargs)
    return ContentionParams(**base)


def _p90_hit_runs(events, cp):
    out = evaluate(events, GB, ["a", "b"], 100.0, cp, keep_runs=True, extended=True)
    return out["policies"]["lmcache"]["ttft_delta_hit_runs"]["p90"], out


def test_rtt_adds_one_to_one_to_each_fetching_run_under_fetch_all():
    events = _stall_events()
    zero = evaluate(events, GB, ["a", "b"], 100.0, _cp(write_stall_ms_per_gb=300.0), keep_runs=True,
                    extended=True)
    at = evaluate(events, GB, ["a", "b"], 100.0, _cp(write_stall_ms_per_gb=300.0, l2_rtt_ms=700.0),
                  keep_runs=True, extended=True)
    for r0, r1 in zip(zero["run_deltas"], at["run_deltas"]):
        assert r1["lmcache"] == pytest.approx(r0["lmcache"] + 0.7 * (r0["lmcache_fetched_chunks"] > 0))
    for q in ("p50", "p90", "p99"):
        assert at["policies"]["lmcache"]["ttft_delta_hit_runs"][q] == pytest.approx(
            zero["policies"]["lmcache"]["ttft_delta_hit_runs"][q] + 0.7)


def test_break_even_stall_agrees_with_a_fine_grid():
    events = _stall_events()
    gpu_rtt = 0.5
    out = stall_sweep(events, GB, ["a", "b"], 100.0, _cp(l2_rtt_ms=200.0), [0, 100], gpu_rtt)
    star = out["x_t_star"]["at_gpu_rtt"]
    assert star["direction"] == "fetch-all faster below"
    assert out["w_min_gb_per_s"] == pytest.approx(1.0) and out["x_max_ms_per_gb"] == pytest.approx(950.0)
    x = star["ms_per_gb"]
    below, _ = _p90_hit_runs(events, _cp(write_stall_ms_per_gb=x, l2_rtt_ms=gpu_rtt))
    above, _ = _p90_hit_runs(events, _cp(write_stall_ms_per_gb=x + 1.0, l2_rtt_ms=gpu_rtt))
    assert below < 0 <= above
    # A larger fixed cost per fetch lowers the break-even stall.
    assert out["x_t_star"]["at_l2_rtt"]["ms_per_gb"] < x
    # Analytic: delta = rtt + 1 ms - 2.56 s + s / (1 - s) * 2.56 s with s = X / 1000.
    assert x == pytest.approx(499.8, abs=1.0)
    # The stored headlines equal the p90 over runs with hits of a direct evaluate at that rtt and X_t.
    assert [pt["write_stall_ms_per_gb"] for pt in out["points"]] == [0, 100]
    for pt in out["points"]:
        x_t = pt["write_stall_ms_per_gb"]
        at_l2, _ = _p90_hit_runs(events, _cp(write_stall_ms_per_gb=x_t, l2_rtt_ms=200.0))
        at_gpu, _ = _p90_hit_runs(events, _cp(write_stall_ms_per_gb=x_t, l2_rtt_ms=gpu_rtt))
        assert pt["headline_at_l2_rtt_s"] == pytest.approx(at_l2)
        assert pt["headline_at_gpu_rtt_s"] == pytest.approx(at_gpu)


def test_run_with_zero_trailing_write_rate_does_not_change_with_stall():
    events = _stall_events()
    rows = [{r["run"]: r for r in _p90_hit_runs(events, _cp(write_stall_ms_per_gb=x))[1]["run_deltas"]}
            for x in (0.0, 300.0, 900.0)]
    assert rows[0][2]["trailing_write_gb_per_s"] == 0.0
    assert rows[0][2]["lmcache"] == rows[1][2]["lmcache"] == rows[2][2]["lmcache"]
    assert rows[0][1]["lmcache"] < rows[1][1]["lmcache"] < rows[2][1]["lmcache"]


def test_break_even_stall_directions():
    hit_only = [(MISS, 0, 0, 100, 256, 256)] + [(WRITE, 0, 0, 100, 0, 0)] * 60 + [(HIT, 10, 0, 1, 256, 256)]
    capped = stall_sweep(hit_only, GB, ["a"], 100.0, _cp(), [0, 10], 0.5)
    assert capped["x_t_star"]["at_gpu_rtt"]["direction"] == "faster at the stall cap (0.95)"
    slow = stall_sweep(_stall_events(), GB, ["a", "b"], 100.0, _cp(l2_rtt_ms=5000.0), [0, 10], 0.5)
    assert slow["x_t_star"]["at_l2_rtt"] == {"ms_per_gb": None, "direction": "slower at all X_t",
                                             "rtt_ms": 5000.0}
    point = slow["points"][0]
    assert point["break_even_rtt"]["ms"] == pytest.approx(2559.0, abs=1.0)


# Test 9: load tiles.

def _tile_sim(load_scale, capacity, offset="none", seed=0, requests=150):
    recs = workload_records(nodes=2, requests=requests, gpu_tokens=4096)
    p = SimParams(chunk_tokens=64, capacities=parse_capacities(capacity), kv_bytes_per_token=8,
                  load_scale=load_scale, tile_offset=offset, tile_seed=seed)
    return simulate(recs, p)


def test_two_disjoint_tiles_at_c_hit_like_one_tile_at_half_c():
    # Ops of one run share a time, so the tiles interleave by run, not by op: the match is
    # close only when the cache holds many runs (here 256 chunks per tile).
    one = _tile_sim(1, "128KiB", requests=400)["capacities"][0]
    two = _tile_sim(2, "256KiB", requests=400)
    assert two["trace"]["load_scale"]["tiles"] == 2 and len(two["trace"]["nodes"]) == 4
    assert two["capacities"][0]["hits"] == pytest.approx(2 * one["hits"], rel=0.01)
    assert any("correlated bursts" in c for c in two["caveats"])


def test_random_tile_offsets_are_seeded():
    a, b = _tile_sim(3, "64KiB", "random", 7), _tile_sim(3, "64KiB", "random", 7)
    offsets = a["trace"]["load_scale"]["offsets_seconds"]
    assert offsets == b["trace"]["load_scale"]["offsets_seconds"] and offsets[0] == 0.0
    assert all(0 <= o < 60 for o in offsets) and len(set(offsets)) == 3
    assert a["capacities"] == b["capacities"]
    busy = a["trace"]["load_scale"]["busy_seconds_per_tile"]
    assert a["trace"]["window"]["duration_seconds"] == pytest.approx(busy + max(offsets))


# Test 10: OFF TTFT join.

def _synth(tmp_path):
    rows = [{"timestamp": i * 2000, "input_length": 512 * 4, "output_length": 4,
             "hash_ids": [1, 2, 3, 100 + i]} for i in range(12)]
    trace = tmp_path / "trace.jsonl"
    trace.write_text("".join(json.dumps(r) + "\n" for r in rows))
    log = tmp_path / "requests.jsonl"
    log.write_text("".join(json.dumps({"line": i, "server": f"s{i % 2}", "t_send": 50.0 + 2.0 * i,
                                       "ttft_s": 0.2 + 0.01 * i, "status": "ok", "phase": "measure"}) + "\n"
                           for i in range(12)))
    index = tmp_path / "index.jsonl"
    sm.generate(sm.parse_args([str(trace), "--out", str(tmp_path / "synth"), "--requests", str(log),
                               "--gpu-tokens", "1024", "--index", str(index)]))
    return tmp_path / "synth", index, log


def test_synth_index_joins_every_run_and_driver_rows_are_refused(tmp_path, capsys):
    synth, index, log = _synth(tmp_path)
    out, runs = tmp_path / "sim.json", tmp_path / "runs.jsonl"
    assert main(["simulate", str(synth), "--kv-bytes-per-token", "8", "--capacity", "inf",
                 "--prefill-tokens-per-sec-per-request", "5000", "--contention", "on",
                 "--prefill-tokens-per-sec", "5000", "--off-ttft", str(index), "-o", str(out),
                 "--contention-runs-out", str(runs)]) == 0
    assert "OFF TTFT join:" in capsys.readouterr().out
    sim = json.loads(out.read_text())
    assert sim["lookup_time"]["model"] == "scheduled"
    con = sim["capacities"][0]["contention"]
    assert con["off_ttft_join"]["run_join_rate"] == 1.0 and con["off_ttft_join"]["requests"] == 12
    assert set(con["policies"]["lmcache"]["ttft_change"]) == {"p50", "p90", "p99"}
    assert sim["inputs"]["off_ttft"] == {"format": "synth-index", "requests": 12, "path": str(index)}
    assert all("store_ts" in json.loads(line) for line in runs.read_text().splitlines())
    with pytest.raises(SimulationError, match="driver request rows"):
        read_off_ttft(str(log))
    assert main(["simulate", str(synth), "--kv-bytes-per-token", "8", "--capacity", "inf",
                 "--contention", "on", "--prefill-tokens-per-sec", "5000", "--off-ttft", str(log),
                 "-o", str(out)]) == 2


def test_sweep_with_off_ttft_uses_the_p90_change_and_bisects_rtt(tmp_path):
    synth, index, _log = _synth(tmp_path)
    with open(index) as fh:
        requests = [(r["node"], r["ts"], r["ttft_s"]) for r in map(json.loads, fh)]
    p = SimParams(chunk_tokens=256, capacities=parse_capacities("inf"), kv_bytes_per_token=8,
                  prefill_tokens_per_sec_per_request=5000.0, fetch_policies=["lmcache", "ideal",
                                                                             "lookup-timeout:1"])
    cp = ContentionParams(prefill_tokens_per_sec=2000.0, l2_read_gbps_per_node=1.0, l2_lookup_ms=0.5)
    with TracePaths([str(synth)]) as files:
        sim = simulate(iter_records(files, {}), p, contention=cp, off_ttft=requests,
                       sweep={"write_stall_ms_per_gb": [0, 50], "l2_rtt_ms": [0.5, 100]})
    con = sim["capacities"][0]["contention"]
    sweep = con["sweep"]
    assert sweep["statistic"] == "ttft_p90_change"
    assert con["policies"]["lookup-timeout:1"]["lookup_timeouts"] == 0
    assert len(sweep["rtt_points"]) == 4 and "ttft_change" in sweep["rtt_points"][0]["policies"]["ideal"]
    y = sweep["points"][0]["break_even_rtt"]
    assert y["direction"] == "fetch-all faster below" and y["ms"] > 0
    # Y is the 1 ms bracket of the change of p90: faster just below, not faster just above.
    with TracePaths([str(synth)]) as files:
        prep = prepare(iter_records(files, {}), p)
    tl = []
    replay(prep, None, p, tl)
    zero = evaluate(tl, 256 * 8, prep.nodes, float(prep.summary["window"]["duration_seconds"]),
                    replace(cp, l2_rtt_ms=0.0), keep_runs=True, policies=("lmcache",), extended=True,
                    store_ts=prep.run_store_ts)
    join = join_off_ttft(zero["run_deltas"], requests)
    delta = {r["run"]: r["lmcache"] for r in zero["run_deltas"]}
    hit = {r["run"]: r["lmcache_fetched_chunks"] > 0 for r in zero["run_deltas"]}
    below = ttft_change(join.off, join.on(delta, y["ms"] / 1000.0, hit))["p90"]
    above = ttft_change(join.off, join.on(delta, (y["ms"] + 1.0) / 1000.0, hit))["p90"]
    assert below < 0 <= above


def test_shared_tile_keys_reuse_chunks_across_tiles():
    disjoint = _tile_sim(2, "inf")["capacities"][0]
    shared = simulate(workload_records(nodes=2, requests=150, gpu_tokens=4096),
                      SimParams(chunk_tokens=64, capacities=parse_capacities("inf"), kv_bytes_per_token=8,
                                load_scale=2, tile_offset="none", tile_keys="shared"))
    assert shared["capacities"][0]["hits"] > disjoint["hits"]
    assert any("optimistic" in c for c in shared["caveats"])


def test_second_hit_ghost_set_forgets_old_misses():
    ops = [lookup(0, 0, 0), store(0, 0, 0), lookup(1, 1, 1), store(1, 1, 1),
           lookup(2, 0, 2), store(2, 0, 2), lookup(3, 0, 3)]
    small = replay(make_prep(ops, 2), None, sim_params(admission="second-hit", admission_ghost_chunks=1),
                   track=True)
    big = replay(make_prep(ops, 2), None, sim_params(admission="second-hit", admission_ghost_chunks=2),
                 track=True)
    # With room for one ghost key, key 1 pushes key 0 out, so key 0's second miss is a first miss.
    assert small["writes"] == 0 and small["hits"] == 0
    assert big["writes"] == 1 and big["hits"] == 1 and big["admission"]["ghost_chunks"] == 2


def test_max_load_writes_skipped_host_chunks_at_the_run_store_time():
    ops = [lookup(0, 0, 0), store(0, 0, 0),
           lookup(1, 0, 1), (HOST_HIT, 1, 1, 0, 1, C, 2 * C),
           lookup(2, 1, 2)]
    prep = make_prep(ops, 2)
    prep.run_store_ts = [0.0, 1.5, 2.0]
    params = sim_params(count_rewrites=True)
    capped = replay(prep, None, params, policy=static_policy("max-load:256", C), track=True)
    # Run 1 loads key 0 from L2; the cap skips host chunk key 1, which is then computed and SET at 1.5 s.
    assert capped["fetch_policy"]["skipped_host_tokens"] == C
    assert capped["writes"] == 2 and capped["hits"] == 2
    timeline = []
    replay(prep, None, params, timeline, policy=static_policy("max-load:256", C))
    assert [(ev[0], ev[1]) for ev in timeline if ev[0] == WRITE] == [(WRITE, 0), (WRITE, 1.5)]


def test_none_after_accepts_an_absolute_wall_time():
    ops = [lookup(104, 0, 0), store(104, 0, 0), lookup(106, 1, 1), store(106, 1, 1)]
    res = replay(make_prep(ops, 2, epoch=10.0), None, sim_params(admission="none-after:@105"), track=True)
    assert res["writes"] == 1 and res["admission"]["cutoff_ts"] == 105.0
    assert res["admission"]["epoch_source"] == "absolute" and res["admission"]["epoch"] is None
    with pytest.raises(SimulationError):
        sim_params(admission="none-after:soon").validate()


def test_cache_node_counts_must_match_the_capacities():
    with pytest.raises(SimulationError, match="one capacity per count"):
        sim_params(cache_node_counts=[1, 2]).validate()


# Test 11: static policies refuse the store-time fallback.

@pytest.mark.parametrize("policy", ["min-tokens:512", "max-load:4096"])
def test_static_policies_refuse_the_store_fallback(policy):
    p = SimParams(chunk_tokens=64, capacities=parse_capacities("inf"), kv_bytes_per_token=8,
                  fetch_policies=["lmcache", policy])
    with pytest.raises(SimulationError) as err:
        simulate(workload_records(nodes=1, requests=20), p)
    assert str(err.value) == STORE_FALLBACK_REFUSAL
    with pytest.raises(SimulationError, match="need --lookup-time scheduled"):
        simulate(workload_records(nodes=1, requests=20),
                 SimParams(**{**p.__dict__, "lookup_time": "store"}))


# Cache node counts, sweep and gpu_rtt_ms in sim.json.

def test_cache_node_counts_make_the_capacities_and_store_the_sweep(tmp_path, capsys):
    trace = write_segment(tmp_path, workload_records(nodes=2, requests=80, gpu_tokens=2048))
    out = tmp_path / "sim.json"
    args = ["simulate", str(trace), "--chunk-tokens", "64", "--kv-bytes-per-token", "8",
            "--cache-node-counts", "1,2", "--cache-node-gb", "0.000004", "--contention", "on",
            "--prefill-tokens-per-sec", "5000", "--contention-sweep", "write_stall_ms_per_gb=0,100",
            "-o", str(out)]
    assert main(args) == 0
    assert "X_t* at r_g = 0.5 ms" in capsys.readouterr().out
    sim = json.loads(out.read_text())
    assert [c["label"] for c in sim["capacities"]] == ["1 x i8ge.48xlarge", "2 x i8ge.48xlarge"]
    assert [c["capacity_bytes"] for c in sim["capacities"]] == [4000, 8000]
    assert sim["capacities"][1]["contention"]["model"] == "fifo-fluid-per-node-and-cache-node"
    assert len(sim["capacities"][1]["contention"]["policies"]["lmcache"]["per_cache_node"]) == 2
    assert sim["inputs"]["cache_node"]["gbps"] == 180.0
    assert sim["inputs"]["contention"]["gpu_rtt_ms"] == 0.5
    assert sim["inputs"]["contention"]["gpu_rtt_ms_source"] == "default"
    assert "x_t_star" in sim["capacities"][0]["contention"]["sweep"]
    assert main(args + ["--capacity", "inf"]) == 2
    assert main(args[:-2] + ["--gpu-rtt-ms", "750", "-o", str(out)]) == 0
    assert json.loads(out.read_text())["inputs"]["contention"]["gpu_rtt_ms_source"] == "input"
