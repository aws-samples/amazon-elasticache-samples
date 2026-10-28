"""Measured cost-model inputs: calibration, measured TTFT adds, store and decode line items,
GPU servers per cache node, and flag-over-calibration precedence."""

import gzip
import json
import math

import pytest
from conftest import workload_records

from kvtrace.calibrate import (
    CALIBRATION_KIND,
    CalibrationError,
    calibrate,
    fit_pair,
    l2_tokens,
    load_run,
    ols,
    pair_runs,
    read_calibration,
    resolve,
    sha256_of,
)
from kvtrace.cli import main
from kvtrace.contention import HIT, MISS, ContentionError, ContentionParams, evaluate
from kvtrace.costmodel import (
    SCALING_ASSUMPTION,
    CostInputs,
    GpuInputs,
    cost_rows,
    decode_saved,
    generation_tokens,
    gpu_cost,
    server_scaling,
    store_cost,
    weak_calibration_warnings,
)

KVB = 1000  # KV bytes per token of the synthetic runs


# Synthetic OFF/ON run pair. Request i has prompt 1000 + 100 * i tokens. OFF waits 10 ms
# from arrival to lookup (request 0 waits 100 ms: queued). ON adds 40 ms to the wait of a
# request without an L2 hit and 200 ms with one (requests 3 and 4, 512 L2 tokens).
# Request 5 gets 256 CPU-tier tokens in both runs, which is not an L2 hit.
# ON TTFT = OFF TTFT + 20 ms + 2 us x computed tokens - 1 ms per L2 token not recomputed.

N_REQ = 6
HIT_IDS = (3, 4)
CPU_ID = 5
CPU_TOKENS = 256


def _driver_row(i, t0, ttft, tpot):
    return {"session": f"s:{i}", "line": i, "pass": 0, "status": "ok", "prompt_tokens": 1000 + 100 * i,
            "t_send": t0 + i, "ttft_s": ttft, "tpot_s": tpot, "output_tokens": 50}


def _connector_row(i, t0, wait, external, l2):
    prompt = 1000 + 100 * i
    arrival = t0 + i + 0.005
    return {"kind": "kvtrace.request", "prompt_tokens": prompt, "t_arrival": arrival,
            "t_lookup": arrival + wait, "t_sched": arrival + wait + (0.1 if l2 else 0.001),
            "computed_at_sched": external, "external_tokens": external}


def _write_run(path, on):
    t0 = 5000.0 if on else 1000.0
    (path / "connector").mkdir(parents=True)
    rows, conn = [], []
    for i in range(N_REQ):
        l2 = 512 if on and i in HIT_IDS else 0
        external = l2 + (CPU_TOKENS if i == CPU_ID else 0)
        off_ttft = 0.5 + 0.01 * i
        computed = 1000 + 100 * i - external
        ttft = off_ttft + (0.02 + 2e-6 * computed - 1e-3 * l2 if on else 0.0)
        rows.append(_driver_row(i, t0, ttft, 0.020 if not on else 0.019))
        wait = (0.1 if i == 0 else 0.01) + ((0.2 if l2 else 0.04) if on else 0.0)
        conn.append(_connector_row(i, t0, wait, external, l2))
    # A failed request and a stale .part connector file are not used.
    rows.append({**_driver_row(99, t0, None, None), "status": "error"})
    (path / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (path / "connector" / "requests-1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in conn))
    (path / "connector" / "requests-2.jsonl.part").write_text("{not json\n")
    return path


@pytest.fixture
def run_pair(tmp_path):
    off = _write_run(tmp_path / "off", on=False)
    on = _write_run(tmp_path / "on", on=True)
    be = tmp_path / "breakeven.json"
    be.write_text(json.dumps({"b_tok": KVB, "t_fixed_s": 0.25, "BW_req_B_s": {"median": 2.5e10, "n": 4}}))
    return off, on, be


def test_load_run_joins_connector_rows_and_skips_part_files_and_failures(run_pair):
    rows, prov = load_run(str(run_pair[0]))
    assert len(rows) == N_REQ and all("connector" in r for r in rows)
    assert all(r["connector"]["prompt_tokens"] == r["prompt_tokens"] for r in rows)
    assert [f["path"].endswith(".part") for f in prov["files"]] == [False, False]
    assert prov["files"][0]["sha256"] == sha256_of(str(run_pair[0] / "requests.jsonl"))
    assert prov["requests_with_connector"] == N_REQ


def test_load_run_leaves_a_row_unjoined_beyond_the_tolerance(run_pair):
    rows, prov = load_run(str(run_pair[0]), tolerance_s=0.001)
    assert prov["requests_with_connector"] == 0 and not any("connector" in r for r in rows)


def test_load_run_needs_both_files(tmp_path):
    with pytest.raises(CalibrationError, match="requests.jsonl"):
        load_run(str(tmp_path))


def test_pair_runs_uses_the_session_key_else_tokens_and_relative_send_time(run_pair):
    off, _ = load_run(str(run_pair[0]))
    on, _ = load_run(str(run_pair[1]))
    pairs, method = pair_runs(off, on)
    assert method == "session/line/pass" and len(pairs) == N_REQ
    for row in off + on:
        del row["session"]
    pairs, method = pair_runs(off, on)
    assert method.startswith("prompt_tokens + relative t_send") and len(pairs) == N_REQ
    assert all(o["prompt_tokens"] == n["prompt_tokens"] for o, n in pairs)


def test_fit_pair_lookup_store_and_tpot(run_pair):
    off, _ = load_run(str(run_pair[0]))
    on, _ = load_run(str(run_pair[1]))
    fit = fit_pair(pair_runs(off, on)[0], KVB, queue_threshold_ms=50.0)
    # Request 0 is queued in OFF (100 ms), so 3 misses (one with CPU-tier tokens) and 2 hits remain.
    assert fit["lookup_miss_ms"]["n"] == 3 and fit["lookup_hit_ms"]["n"] == 2
    assert fit["lookup_miss_ms"]["median"] == pytest.approx(40.0)
    assert fit["lookup_hit_ms"]["median"] == pytest.approx(200.0)
    # Fitted on the 4 pairs without an L2 hit, so the hits' recompute saving stays out.
    assert fit["store"]["n"] == 4
    assert fit["store"]["slope"] == pytest.approx(2e-6) and fit["store"]["r"] == pytest.approx(1.0)
    assert fit["store"]["intercept"] == pytest.approx(0.02)
    assert fit["store_all_pairs"]["slope"] > 1e-4
    assert fit["tpot"]["p50_off_ms"] == pytest.approx(20.0)
    assert fit["tpot"]["p50_on_ms"] == pytest.approx(19.0)
    # 512 L2 tokens x 1000 B in 0.1 s = 0.04096 Gbit/s; CPU-tier tokens are not counted.
    assert fit["load_gbps_under_load"]["median"] == pytest.approx(0.04096)
    assert fit["ttft_delta"]["hit_n"] == 2


def test_l2_tokens_subtract_the_off_cpu_tier():
    off = {"connector": {"external_tokens": 256}}
    assert l2_tokens(off, {"connector": {"external_tokens": 768}}) == 512
    assert l2_tokens(off, {"connector": {"external_tokens": 256}}) == 0
    assert l2_tokens(off, {"connector": {"external_tokens": 0}}) == 0


def test_ols_is_undefined_for_one_point_or_constant_x():
    assert ols([1.0], [2.0])["slope"] is None
    assert ols([1.0, 1.0], [2.0, 3.0])["slope"] is None


def test_calibrate_writes_values_diagnostics_and_provenance(run_pair):
    off, on, be = run_pair
    cal = calibrate(str(be), str(off), str(on))
    v = cal["values"]
    assert cal["kind"] == CALIBRATION_KIND
    assert v["l2_rtt_ms"]["value"] == v["gpu_rtt_ms"]["value"] == pytest.approx(250.0)
    assert v["l2_read_gbps_per_node"]["value"] == pytest.approx(200.0)
    assert v["l2_lookup_ms_miss"]["value"] == pytest.approx(40.0) and v["l2_lookup_ms_miss"]["n"] == 3
    assert v["l2_lookup_ms_hit"]["mean"] == pytest.approx(200.0)
    assert v["store_us_per_token"]["value"] == pytest.approx(2.0) and v["store_us_per_token"]["r"] > 0.99
    assert v["tpot_delta_ms"]["value"] == pytest.approx(-1.0)
    assert v["tpot_change"]["value"] == pytest.approx(-0.05)
    assert "not applied" in cal["diagnostics"]["load_gbps_under_load"]["note"]
    assert cal["provenance"]["breakeven"]["sha256"] == sha256_of(str(be))
    assert cal["inputs"]["kv_bytes_per_token"] == KVB and cal["inputs"]["pairs"] == N_REQ


def test_read_calibration_refuses_other_files(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"values": {}}))
    with pytest.raises(CalibrationError, match="not a kvtrace calibration"):
        read_calibration(str(path))


def test_resolve_prefers_the_flag_then_the_calibration_then_the_default():
    cal = {"values": {"a": {"value": 2.0}, "b": {"value": None}}}
    assert resolve(1.0, cal, "a", 3.0) == (1.0, "input")
    assert resolve(None, cal, "a", 3.0) == (2.0, "calibrated")
    assert resolve(None, cal, "b", 3.0) == (3.0, "default")
    assert resolve(None, None, "a", 3.0) == (3.0, "default")


# Measured TTFT adds in the contention model.

GB = 10**9


def _events():
    # Run 1: one hit chunk and one computed chunk. Run 2: two computed chunks, no hit.
    return [(HIT, 0.0, 0, 1, 256, 256), (MISS, 0.0, 0, 1, 256, 512),
            (MISS, 5.0, 0, 2, 256, 256), (MISS, 5.0, 0, 2, 256, 512)]


def _cp(**kwargs):
    base = dict(prefill_tokens_per_sec=1000.0, l2_read_gbps_per_node=8000.0, l2_rtt_ms=0.0)
    base.update(kwargs)
    return ContentionParams(**base)


def _deltas(cp):
    out = evaluate(_events(), 1000, ["a"], 10.0, cp, keep_runs=True)
    return {row["run"]: row["lmcache"] for row in out["run_deltas"]}


def test_measured_lookup_and_store_add_by_hit_or_miss():
    base = _deltas(_cp())
    meas = _deltas(_cp(l2_lookup_ms_hit=200.0, l2_lookup_ms_miss=40.0, store_us_per_token=2.0))
    # Run 1 fetches its hit, so it computes (and stores) only 256 tokens.
    assert meas[1] == pytest.approx(base[1] + 0.2 + 256 * 2e-6)
    assert meas[2] == pytest.approx(base[2] + 0.04 + 512 * 2e-6)


def test_measured_inputs_default_to_none_and_stay_out_of_sim_json():
    cp = _cp()
    assert _deltas(cp) == _deltas(_cp(l2_lookup_ms_hit=None))
    d = cp.as_dict()
    assert not {"l2_lookup_ms_hit", "l2_lookup_ms_miss", "store_us_per_token"} & set(d)
    assert _cp(store_us_per_token=1.5).as_dict()["store_us_per_token"] == 1.5


def test_measured_inputs_must_be_finite():
    with pytest.raises(ContentionError, match="l2-lookup-ms-miss"):
        _cp(l2_lookup_ms_miss=math.inf).validate()


def test_modelled_and_measured_lookup_are_refused_together():
    with pytest.raises(ContentionError, match="give one of them"):
        _cp(l2_lookup_ms=5.0, l2_lookup_ms_hit=200.0).validate()
    # So a lookup-timeout policy, which needs --l2-lookup-ms, never meets the measured values.
    with pytest.raises(ContentionError, match="give one of them"):
        evaluate(_events(), 1000, ["a"], 10.0, _cp(l2_lookup_ms=5.0, l2_lookup_ms_miss=40.0),
                 policies=("lookup-timeout:10",))


def test_a_calibrated_r_g_above_the_fitted_limit_warns():
    gpu = GpuInputs(load_mode="blocking")
    sim = _sim()
    sim["inputs"]["contention"]["gpu_rtt_ms"] = 367.0
    warnings = cost_rows(sim, _inputs(), gpu)["warnings"]
    assert any("calibrated from the fixed cost" in w for w in warnings)
    sim["inputs"]["contention"]["gpu_rtt_ms_source"] = "input"
    assert not any("calibrated from" in w for w in cost_rows(sim, _inputs(), gpu)["warnings"])


def test_the_oracle_counts_the_store_saving_when_it_picks_k():
    # One hit chunk of 256 tokens: recompute 256 ms; fetch 2.1 ms rtt + transfer. With rtt
    # 256.5 ms, fetching costs 0.5 ms more than recomputing, so the oracle skips the hit.
    # A store cost of 2 us/token makes the fetch save 0.512 ms more, so it now fetches.
    events = [(HIT, 0.0, 0, 1, 256, 256)]
    plain = evaluate(events, 1000, ["a"], 10.0, _cp(l2_rtt_ms=256.5), keep_runs=True)
    store = evaluate(events, 1000, ["a"], 10.0, _cp(l2_rtt_ms=256.5, store_us_per_token=2.0),
                     keep_runs=True)
    assert plain["run_deltas"][0]["ideal_fetched_chunks"] == 0
    assert store["run_deltas"][0]["ideal_fetched_chunks"] == 1
    assert store["run_deltas"][0]["ideal"] <= store["run_deltas"][0]["lmcache"]


def test_an_oracle_run_that_found_hits_pays_the_hit_value():
    # lookup-timeout needs --l2-lookup-ms, which the measured values refuse (test above).
    out = evaluate(_events(), 1000, ["a"], 10.0,
                   _cp(l2_lookup_ms_hit=200.0, l2_lookup_ms_miss=40.0),
                   keep_runs=True, policies=("ideal", "lmcache"))
    rows = {row["run"]: row for row in out["run_deltas"]}
    base = evaluate(_events(), 1000, ["a"], 10.0, _cp(), keep_runs=True,
                    policies=("ideal", "lmcache"))
    base_rows = {row["run"]: row for row in base["run_deltas"]}
    assert rows[1]["ideal"] - base_rows[1]["ideal"] == pytest.approx(0.2)
    assert rows[2]["ideal"] - base_rows[2]["ideal"] == pytest.approx(0.04)


# Store, decode and server scaling math.

POL = {"fetching_runs": 10, "read_gb": 8.0, "queue_wait_sum": 1.0, "per_node": {}}


def test_gpu_cost_adds_store_only_when_given():
    plain = gpu_cost(POL, 100.0, 8.0, "blocking")
    with_store = gpu_cost(POL, 100.0, 8.0, "blocking", store_seconds=5.0)
    assert "store" not in plain
    assert with_store["store"] == 5.0
    assert with_store["total"] == pytest.approx(plain["total"] + 5.0)
    assert with_store["total_upper"] == pytest.approx(plain["total_upper"] + 5.0)


def test_store_cost_fitted_and_block_hold():
    fitted = store_cost(1e6, 10, KVB, GpuInputs(store_us_per_token=2.0))
    assert fitted["seconds"] == pytest.approx(2.0) and fitted["method"] == "fitted"
    # 1e6 tokens x 1000 B = 1 GB over 10 runs at 0.5 GB/s: 1^2 / (10 x 0.5) = 0.2 GB-s, / 4 GB.
    hold = store_cost(1e6, 10, KVB, GpuInputs(store_gb_per_sec=0.5, gpu_kv_capacity_gb=4.0))
    assert hold["held_gb_seconds"] == pytest.approx(0.2) and hold["seconds"] == pytest.approx(0.05)
    assert store_cost(1e6, 10, KVB, GpuInputs()) is None
    with pytest.raises(ValueError, match="not both"):
        store_cost(1e6, 10, KVB, GpuInputs(store_us_per_token=1.0, store_gb_per_sec=1.0))
    with pytest.raises(ValueError, match="go together"):
        store_cost(1e6, 10, KVB, GpuInputs(store_gb_per_sec=1.0))


def test_decode_saved_is_output_tokens_times_tpot_change_over_batch():
    assert decode_saved(1e6, -1.0, 10.0) == pytest.approx(100.0)
    assert decode_saved(1e6, 2.0, 10.0) == pytest.approx(-200.0)


def test_generation_tokens_sums_the_first_counter_per_node():
    sim = {"metrics": {"per_node": {
        "a": {"counters": {'vllm:generation_tokens_total{engine="0"}': 10.0}},
        "b": {"counters": {"sglang:generation_tokens_total": 5.0}},
        "c": {"counters": {}}}}}
    assert generation_tokens(sim) == 15.0
    assert generation_tokens({"metrics": {"per_node": {"a": {"counters": {}}}}}) is None


def test_server_scaling_finds_n_star_and_flags_limits():
    limits = {"read_gbps_per_server": 50.0, "write_gbps_per_server": 10.0, "cache_node_gbps": 180.0,
              "peak_bytes": 4e12, "capacity_bytes": 10e12}
    sc = server_scaling(10.0, 8.0, 1, 25.0, (1, 2, 4), limits)
    assert [r["net_usd_per_hour"] for r in sc["rows"]] == [-15.0, -5.0, 15.0]
    assert [r["net_usd_per_hour_excl_decode"] for r in sc["rows"]] == [-17.0, -9.0, 7.0]
    assert sc["n_star"] == 4 and sc["n_star_min"] == 3
    assert sc["n_star_excl_decode"] == 4 and sc["n_star_min_excl_decode"] == 4
    assert sc["assumption"] == SCALING_ASSUMPTION
    assert sc["rows"][1]["flags"] == []
    flags = " ".join(sc["rows"][2]["flags"])
    assert "read 200.0 Gbit/s above" in flags and "above capacity" in flags


def test_server_scaling_without_node_price_or_nic():
    sc = server_scaling(-1.0, None, 2, None, (1, 8), {"read_gbps_per_server": 1e6})
    assert sc["n_star"] is None and sc["n_star_min"] is None and not sc["nic_modelled"]
    assert all(r["flags"] == [] and r["net_usd_per_hour"] is None for r in sc["rows"])


# cost_rows with line items (hand-built sim.json, 1 engine node).

WALL = 3600.0
COMPUTED = 1_000_000
SAVED = 200_000


def _sim():
    pol = {"fetching_runs": 100, "read_gb": 36.0, "queue_wait_sum": 0.0, "fetched_tokens": SAVED,
           "host_enabled_tokens": 0, "per_node": {"a": {"write_bytes_per_sec": 1e9}},
           "runs_slower": 1, "runs_faster": 1, "ttft_delta_hit_runs": {"p90": 0.1}}
    return {"inputs": {"params": {"kv_bytes_per_token": KVB},
                       "contention": {"l2_read_gbps_per_node": 80.0, "l2_rtt_ms": 0.5,
                                      "gpu_rtt_ms": 10.0, "gpu_rtt_ms_source": "calibrated"},
                       "cache_node": {"gbps": 180.0}},
            "trace": {"tokens": {"computed": COMPUTED}, "nodes": ["a"],
                      "window": {"duration_seconds": WALL}},
            "metrics": {"per_node": {"a": {"counters": {"vllm:generation_tokens_total": 360_000.0},
                                           "gauges_time_weighted": {"vllm:num_requests_running": 4.0}}}},
            "capacities": [{"label": "1 x node", "cache_nodes": 1, "capacity_bytes": 1e12,
                            "peak_used_bytes": 3e11,
                            "contention": {"runs": 100, "policies": {"lmcache": pol}}}]}


def _inputs():
    return CostInputs(prefill_tokens_per_sec=1000.0, prefill_tps_source="input", replica_cost_per_hour=36.0)


def test_cost_rows_line_items_and_scaling():
    gpu = GpuInputs(load_mode="blocking", store_us_per_token=1.0, tpot_delta_ms=-1.0,
                    cache_node_ec2_usd_per_hour=10.0, cache_node_margin=0.0,
                    gpu_servers_per_node=(1, 2))
    costs = cost_rows(_sim(), _inputs(), gpu)
    row = costs["rows"][0]
    li = row["line_items"]
    # Prefill 200,000 / 1000 = 200 s; decode 360,000 x 1 ms / 4 = 90 s; load wait
    # 100 x 10 ms + 36 x 8 / 80 = 4.6 s; store 800,000 x 1 us = 0.8 s. $/h = s / 3600 x 36.
    assert li["replica_seconds"]["prefill_saved"] == pytest.approx(200.0)
    assert li["replica_seconds"]["decode_saved"] == pytest.approx(90.0)
    assert li["replica_seconds"]["lookup_rtt_overhead"] == pytest.approx(4.6)
    assert li["replica_seconds"]["store_overhead"] == pytest.approx(0.8)
    assert li["usd_per_hour_per_server"]["prefill_saved"] == pytest.approx(2.0)
    assert li["net_usd_per_hour_per_server_excl_decode"] == pytest.approx(2.0 - 0.046 - 0.008)
    assert li["net_usd_per_hour_per_server"] == pytest.approx(2.9 - 0.046 - 0.008)
    assert row["gpu_cost"]["store"] == pytest.approx(0.8)
    assert row["s_net"] == pytest.approx(200.0 - 5.4)
    sc = row["server_scaling"]
    assert sc["rows"][0]["cache_usd_per_hour"] == 10.0 and sc["n_star"] is None
    assert sc["nic_modelled"]
    assert sc["n_star_min"] == math.ceil(10.0 / li["net_usd_per_hour_per_server"])
    assert costs["decode"]["batch_source"].startswith("estimated")


def test_cost_rows_without_line_item_inputs_have_none():
    costs = cost_rows(_sim(), _inputs(), GpuInputs(load_mode="blocking"))
    row = costs["rows"][0]
    assert "decode" not in costs and "line_items" not in row and "store" not in row["gpu_cost"]


def test_line_items_need_a_trusted_rate_and_price():
    costs = cost_rows(_sim(), CostInputs(), GpuInputs(load_mode="blocking", tpot_delta_ms=-1.0))
    assert "line_items_omitted" in costs and "line_items" not in costs["rows"][0]


def test_line_items_without_a_load_mode_say_why():
    costs = cost_rows(_sim(), _inputs(), GpuInputs(tpot_delta_ms=-1.0))
    assert "--load-mode" in costs["line_items_omitted"]


def test_weak_calibration_values_warn():
    quality = {"store_r": 0.1, "store_n": 900, "lookup_hit_n": 10, "lookup_miss_n": 300}
    warnings = weak_calibration_warnings(quality, True, True, "simulate")
    assert len(warnings) == 2
    assert "r = 0.100" in warnings[0] and "only 10 requests" in warnings[1]
    assert weak_calibration_warnings(quality, False, False, "simulate") == []
    sim = _sim()
    sim["inputs"]["calibration"] = {"values": {"store_us_per_token": {"source": "calibrated"}},
                                    "quality": quality}
    costs = cost_rows(sim, _inputs(), GpuInputs(load_mode="blocking", tpot_delta_ms=-1.0))
    assert any("calibrated store cost (simulate)" in w for w in costs["warnings"])


# CLI: calibrate, simulate --calibration precedence, report line items (integration).

def test_cli_calibrate_simulate_and_report_with_line_items(run_pair, tmp_path, capsys):
    off, on, be = run_pair
    cal = tmp_path / "cal.json"
    assert main(["calibrate", "--off", str(off), "--on", str(on), "--breakeven", str(be),
                 "-o", str(cal)]) == 0
    assert "store_us_per_token: 2 us/token" in capsys.readouterr().out
    trace_dir = tmp_path / "trace" / "n0"
    trace_dir.mkdir(parents=True)


    with gzip.open(trace_dir / "n0-0-0.kvt.jsonl.gz", "wt") as fh:
        for rec in workload_records(nodes=1, requests=80, gpu_tokens=2048):
            fh.write(json.dumps(rec) + "\n")
    sim_path = tmp_path / "sim.json"
    base = ["simulate", str(trace_dir), "--chunk-tokens", "64", "--kv-bytes-per-token", "8",
            "--capacity", "inf", "--contention", "on", "--prefill-tokens-per-sec", "5000"]
    assert main(base + ["--calibration", str(cal), "--l2-rtt-ms", "1.5", "-o", str(sim_path)]) == 0
    sim = json.loads(sim_path.read_text())
    con, rec = sim["inputs"]["contention"], sim["inputs"]["calibration"]["values"]
    assert con["l2_rtt_ms"] == 1.5 and rec["l2_rtt_ms"]["source"] == "input"
    assert con["gpu_rtt_ms"] == pytest.approx(250.0) and con["gpu_rtt_ms_source"] == "calibrated"
    assert con["l2_lookup_ms_miss"] == pytest.approx(40.0)
    assert rec["l2_lookup_ms_miss"]["source"] == "calibrated"
    assert con["l2_read_gbps_per_node"] == pytest.approx(200.0)
    assert sim["inputs"]["calibration"]["sha256"] == sha256_of(str(cal))

    plain = tmp_path / "plain.json"
    assert main(base + ["-o", str(plain)]) == 0
    plain_inputs = json.loads(plain.read_text())["inputs"]
    assert "calibration" not in plain_inputs
    assert plain_inputs["contention"]["l2_rtt_ms"] == 0.5
    assert plain_inputs["contention"]["l2_read_gbps_per_node"] == 100.0
    assert "gpu_rtt_ms" not in plain_inputs["contention"]
    plain_md = tmp_path / "plain.md"
    assert main(base + ["--gpu-rtt-ms", "0.5", "-o", str(plain)]) == 0
    assert main(["report", str(plain), "-o", str(tmp_path / "p.html"), "--markdown", str(plain_md),
                 "--priority", "cost", "--load-mode", "blocking", "--prefill-tokens-per-sec", "1e7",
                 "--replica-cost-per-hour", "100", "--tpot-delta-ms", "-1"]) == 0
    assert "| TTFT l2_rtt_ms | 0.500 ms | sim.json (source not recorded) |" in plain_md.read_text()
    assert main(base + ["--calibration", str(cal), "--l2-lookup-ms", "5", "-o", str(plain)]) == 2
    assert "lookup values of --calibration" in capsys.readouterr().err

    md, js = tmp_path / "r.md", tmp_path / "r.json"
    assert main(["report", str(sim_path), "-o", str(tmp_path / "r.html"), "--markdown", str(md),
                 "--json", str(js), "--priority", "all", "--load-mode", "blocking",
                 "--prefill-tokens-per-sec", "1e7", "--replica-cost-per-hour", "100",
                 "--cache-node-ec2-usd-per-hour", "22.78", "--cache-node-margin", "0.3",
                 "--calibration", str(cal), "--tpot-delta-ms", "-2", "--decode-batch", "8",
                 "--gpu-servers-per-node", "1,2,4"]) == 0
    text = md.read_text()
    assert "## Cost line items" in text and "## GPU servers per cache node" in text
    assert "independent-replica, no cross-server reuse" in text
    assert "calibrated (simulate)" in text
    assert "| Store cost (GPU cost) | 2 us/token | calibrated |" in text
    assert "| TPOT delta ON - OFF | -2.000 ms | input |" in text
    # The synthetic trace has no generation-token counter, so decode saved is refused.
    assert "Decode saved omitted" in text
    data = json.loads(js.read_text())
    row = next(r for r in data["costs"]["rows"] if r["policy"] == "lmcache")
    assert [r["servers"] for r in row["server_scaling"]["rows"]] == [1, 2, 4]
    assert data["costs"]["decode"]["available"] is False
    assert row["line_items"]["replica_seconds"]["decode_saved"] is None


def test_report_rejects_bad_line_item_inputs(tmp_path, capsys):
    sim = tmp_path / "sim.json"
    sim.write_text(json.dumps(_sim()))
    args = ["report", str(sim), "-o", str(tmp_path / "r.html"), "--priority", "cost"]
    assert main(args + ["--gpu-servers-per-node", "0,2"]) == 2
    assert main(args + ["--decode-batch", "0"]) == 2
    assert "must be > 0" in capsys.readouterr().err
