"""Phase 1 report side: GPU cost, break-evens, TPOT and P checks, priority sections.
"""

import copy
import gzip
import json

import pytest
from conftest import workload_records
from golden_cases import CASES, load

from kvtrace.cli import main
from kvtrace.contention import ContentionParams
from kvtrace.costmodel import (
    LOAD_MODE_REFUSAL,
    P1_SWEEP_REFUSAL,
    CostInputs,
    GpuInputs,
    break_even,
    cost_rows,
    gpu_cost,
    omega_star,
    replica_rate_check,
    tpot_test,
)
from kvtrace.report import (
    build_report,
    fmt_num,
    phase1_headlines,
    phase1_json,
    render_html,
    render_markdown,
    spend_table,
    write_table,
)
from kvtrace.simulator import SimParams, parse_capacities, simulate

# Reference fetch-all case at 3,619.82 GB, r_g = 0.5 ms.
COMPUTED = 114_390_304
SAVED = 20_889_344
WALL = 10_134.0
FETCHING_RUNS = 2110
READ_GBPS = 80.0
L_G = 403.0
# read_gb chosen so that 2,110 x 0.5 ms + read_gb x 8 / 80 = 403 replica-s.
READ_GB = (L_G - FETCHING_RUNS * 0.0005) * READ_GBPS / 8


def _policy(**kwargs):
    pol = {"fetching_runs": FETCHING_RUNS, "read_gb": READ_GB, "queue_wait_sum": 34.7,
           "fetched_tokens": SAVED, "host_enabled_tokens": 0,
           "per_node": {"a": {"write_bytes_per_sec": 1.73e9}, "b": {"write_bytes_per_sec": 1.77e9}},
           "runs_slower": 10, "runs_faster": 5, "recency": "exact",
           "ttft_delta_hit_runs": {"p50": 0.1, "p90": 1.7, "p99": 2.0, "mean": 0.5}}
    pol.update(kwargs)
    return pol


def _ref_sim(l2_rtt_ms=0.5, gpu_rtt_ms=0.5, tiles=None):
    """A hand-built sim.json with reference counters (2 engine nodes, 1 cache node)."""
    contention = {"l2_read_gbps_per_node": READ_GBPS, "l2_rtt_ms": l2_rtt_ms}
    if gpu_rtt_ms is not None:
        contention.update(gpu_rtt_ms=gpu_rtt_ms, gpu_rtt_ms_source="input")
    nodes, duration = ["a", "b"], WALL / 2
    trace = {"tokens": {"computed": COMPUTED}, "nodes": nodes,
             "window": {"duration_seconds": duration}}
    if tiles:
        # Two tiles of the two nodes; the window grows by the offsets, the wall time does not.
        trace["nodes"] = ["a#0", "b#0", "a#1", "b#1"]
        trace["window"]["duration_seconds"] = WALL / 4 + 30.0
        trace["load_scale"] = {"tiles": 2, "wall_replica_seconds": WALL}
    return {"inputs": {"params": {}, "contention": contention}, "trace": trace,
            "capacities": [{"label": "1 x i8ge.48xlarge", "cache_nodes": 1, "capacity_bytes": 1237e9,
                            "contention": {"policies": {"ideal": _policy(fetched_tokens=19_536_000),
                                                        "lmcache": _policy()}}}]}


def _fetch_all(costs):
    return next(r for r in costs["rows"] if r["policy"] == "lmcache")


# Test 12: break-even arithmetic.

def test_break_even_arithmetic_matches_the_reference_figures():
    cost = gpu_cost(_policy(), 0.5, READ_GBPS, "blocking")
    assert cost["total"] == pytest.approx(L_G)
    assert cost["total_upper"] == pytest.approx(L_G + 34.7)
    be = break_even(SAVED, COMPUTED, cost["total"], WALL, 2, 1)
    s_pol = SAVED / COMPUTED
    assert be["s_pol"] == pytest.approx(0.1826, abs=1e-4)
    assert be["p_star"] == pytest.approx(SAVED / L_G)
    assert be["p_star"] == pytest.approx(51_800, rel=0.01)
    assert be["beta_star"] == pytest.approx(L_G / (s_pol * WALL))
    assert be["beta_star"] == pytest.approx(0.218, abs=1e-3)
    expected = {b: s_pol * float(b) * 2 - L_G / (WALL / 2) for b in ("0.2", "0.4", "0.6")}
    assert be["q_star"] == pytest.approx(expected)
    assert list(be["q_star"].values()) == pytest.approx([-0.007, 0.067, 0.140], abs=1e-3)


def test_break_even_uses_wall_time_and_n_prime_with_tiles():
    plain = _fetch_all(cost_rows(_ref_sim(), CostInputs(), GpuInputs(load_mode="blocking")))
    tiled = _fetch_all(cost_rows(_ref_sim(tiles=2), CostInputs(), GpuInputs(load_mode="blocking")))
    s_pol = SAVED / COMPUTED
    # N' = 4 engine nodes, D = wall / N' (not the window D' with offsets).
    expected = {b: s_pol * float(b) * 4 - L_G / (WALL / 4) for b in ("0.2", "0.4", "0.6")}
    assert tiled["break_even"]["q_star"] == pytest.approx(expected)
    assert tiled["break_even"]["beta_star"] == pytest.approx(plain["break_even"]["beta_star"])
    assert tiled["break_even"]["p_star"] == pytest.approx(plain["break_even"]["p_star"])


def test_trace_utilization_drops_beta_above_u():
    be = break_even(SAVED, COMPUTED, L_G, WALL, 2, 1, utilization=0.5)
    assert list(be["q_star"]) == ["0.2", "0.4"]


# Test 7: GPU cost inputs and the TPOT reject test.

def _warnings(sim, inputs=None, gpu=None):
    return cost_rows(sim, inputs or CostInputs(), gpu or GpuInputs(load_mode="blocking"))["warnings"]


def test_fitted_l2_rtt_warns_when_r_g_is_the_default():
    old = _ref_sim(l2_rtt_ms=750.0, gpu_rtt_ms=None)
    for inputs in (CostInputs(), CostInputs(l2_rtt_ms=0.5)):
        warnings = _warnings(old, inputs)
        assert any("l2_rtt_ms = 750 ms" in w and "fitted" in w for w in warnings)
    costs = cost_rows(old, CostInputs(), GpuInputs(load_mode="blocking"))
    assert costs["gpu_rtt_ms"] == 0.5 and costs["gpu_rtt_ms_source"] == "default"
    # The fitted value never enters GPU cost.
    assert _fetch_all(costs)["gpu_cost"]["total"] == pytest.approx(L_G)
    assert not any("fitted" in w for w in _warnings(_ref_sim(l2_rtt_ms=750.0)))
    assert not any("fitted" in w for w in _warnings(_ref_sim(l2_rtt_ms=5.0, gpu_rtt_ms=None)))


def test_report_gpu_rtt_unlike_the_stored_value_warns_and_names_both():
    costs = cost_rows(_ref_sim(), CostInputs(), GpuInputs(load_mode="blocking", gpu_rtt_ms=750.0))
    assert any("--gpu-rtt-ms 750" in w and "stored r_g 0.5" in w for w in costs["warnings"])
    assert _fetch_all(costs)["gpu_cost"]["total"] == pytest.approx(L_G + FETCHING_RUNS * 0.7495)
    assert not _warnings(_ref_sim(), gpu=GpuInputs(load_mode="blocking", gpu_rtt_ms=0.5))


def test_tpot_test_rejects_when_its_beta_limit_is_above_u():
    s_pol = SAVED / COMPUTED
    l_g = 0.196 * WALL  # fetch-all at r_g = 750 ms
    ok = tpot_test(l_g, WALL, s_pol, 0.09)
    assert ok["beta_limit"] == pytest.approx((0.196 - 0.09) / s_pol)
    assert ok["beta_limit"] == pytest.approx(0.580, abs=1e-3) and not ok["rejected"]
    assert ok["u_source"] == "assumed 1"
    low_u = tpot_test(l_g, WALL, s_pol, 0.09, utilization=0.5)
    assert low_u["rejected"] and "above u = 0.5" in low_u["reasons"][0]
    assert tpot_test(l_g, WALL, s_pol, 0.09, measured_beta=0.3)["rejected"]
    row = _fetch_all(cost_rows(_ref_sim(), CostInputs(),
                               GpuInputs(load_mode="blocking", gpu_rtt_ms=750.0, tpot_change=0.0,
                                         trace_utilization=0.5)))
    assert row["tpot_test"]["rejected"]


def test_gpu_section_needs_a_load_mode():
    costs = cost_rows(_ref_sim(), CostInputs(), GpuInputs())
    assert costs["gpu_refusal"] == LOAD_MODE_REFUSAL
    assert "gpu_cost" not in _fetch_all(costs)
    with pytest.raises(ValueError):
        gpu_cost(_policy(), 0.5, READ_GBPS, "overlapped")


def test_async_overlap_scales_only_the_load_wait():
    blocking = gpu_cost(_policy(), 0.5, READ_GBPS, "blocking", load_gb_per_sec_per_replica=100.0,
                        stall_ms_per_gb=10.0, busy_seconds_per_node=WALL / 2)
    half = gpu_cost(_policy(), 0.5, READ_GBPS, "async", omega=0.5, load_gb_per_sec_per_replica=100.0,
                    stall_ms_per_gb=10.0, busy_seconds_per_node=WALL / 2)
    assert half["load_wait"] == pytest.approx(blocking["load_wait"] / 2)
    assert half["copy"] == blocking["copy"] == pytest.approx(READ_GB / 100.0)
    ga, gb = 0.01 * 1.73, 0.01 * 1.77
    assert blocking["stall"] == pytest.approx((ga / (1 - ga) + gb / (1 - gb)) * WALL / 2)


# Test 8: P check, omega* and spend exclusivity.

def test_p_check_refuses_a_per_request_rate():
    check = replica_rate_check(COMPUTED, 6771.0, WALL)
    assert check["ratio"] == pytest.approx(1.67, abs=0.01) and not check["ok"]
    assert check["floor_tokens_per_sec"] == pytest.approx(11_288, abs=1)
    assert not replica_rate_check(COMPUTED, 12_000.0, WALL, utilization=0.8)["ok"]
    costs = cost_rows(_ref_sim(), CostInputs(prefill_tokens_per_sec=6771.0, prefill_tps_source="input",
                                             replica_cost_per_hour=60.0),
                      GpuInputs(load_mode="blocking"))
    assert any(w.startswith("P check failed") for w in costs["warnings"])
    assert all("s_net" not in r and "spend" not in r for r in costs["rows"])


def test_gauge_estimate_gives_no_net_figures_and_is_still_checked():
    inputs = CostInputs(prefill_tokens_per_sec=40_000.0, prefill_tps_source="estimated: per-request x R",
                        replica_cost_per_hour=60.0)
    costs = cost_rows(_ref_sim(), inputs, GpuInputs(load_mode="blocking"))
    assert costs["replica_rate"]["source"] == "estimated, biased high"
    assert any("gauge estimate" in w for w in costs["warnings"])
    assert costs["rate_check"]["ok"]
    assert all("s_net" not in r and "spend" not in r and "net_usd_per_month" not in r
               for r in costs["rows"])
    assert "--prefill-tokens-per-sec" in costs["net_omitted"]
    assert "--trace-utilization with --prefill-time-fraction" in costs["net_omitted"]
    low = cost_rows(_ref_sim(), CostInputs(prefill_tokens_per_sec=6771.0, prefill_tps_source="estimated"),
                    GpuInputs(load_mode="blocking"))
    assert any(w.startswith("P check failed") for w in low["warnings"])


def test_trusted_rate_gives_net_figures():
    inputs = CostInputs(prefill_tokens_per_sec=40_000.0, prefill_tps_source="input",
                        replica_cost_per_hour=60.0)
    costs = cost_rows(_ref_sim(), inputs, GpuInputs(load_mode="blocking"))
    row = _fetch_all(costs)
    assert row["s_net"] == pytest.approx(SAVED / 40_000.0 - L_G)
    assert row["s_net_source"] == "input"
    assert "net_omitted" not in costs


def test_busy_share_gives_a_trusted_rate():
    inputs = CostInputs(prefill_time_fraction=0.25, prefill_fraction_source="input")
    costs = cost_rows(_ref_sim(), inputs, GpuInputs(load_mode="blocking", trace_utilization=0.8))
    rate = costs["replica_rate"]
    assert rate["source"] == "from measured busy share" and rate["trusted"]
    assert rate["rate"] == pytest.approx(COMPUTED / (0.8 * 0.25 * WALL))


def test_omega_star_is_null_with_a_direction_outside_zero_to_one():
    assert omega_star(1000.0, 10.0, 50.0, 10.0) == {"omega": None, "direction": "S_net >= 0 at all omega"}
    assert omega_star(1000.0, 10.0, 500.0, 200.0) == {"omega": None, "direction": "S_net < 0 at all omega"}
    mid = omega_star(1000.0, 10.0, 200.0, 0.0)
    assert mid["omega"] == pytest.approx(0.5) and mid["direction"] == "S_net >= 0 above"
    # At P = 20,000 the fetched tokens take 1,044 replica-s, above L_g = 403 at any omega.
    inputs = CostInputs(prefill_tokens_per_sec=20_000.0, prefill_tps_source="input")
    row = _fetch_all(cost_rows(_ref_sim(), inputs, GpuInputs(load_mode="async")))
    assert row["omega_star"] == {"omega": None, "direction": "S_net >= 0 at all omega"}
    # At P = 60,000 they take 348 replica-s: S_net < 0 when blocking, >= 0 above omega*.
    inputs = CostInputs(prefill_tokens_per_sec=60_000.0, prefill_tps_source="input")
    row = _fetch_all(cost_rows(_ref_sim(), inputs, GpuInputs(load_mode="async")))
    assert row["omega_star"]["omega"] == pytest.approx(1 - SAVED / 60_000.0 / L_G)
    assert "omega_star" not in _fetch_all(cost_rows(_ref_sim(), inputs, GpuInputs(load_mode="blocking")))


def test_spend_columns_are_exclusive():
    inputs = CostInputs(prefill_tokens_per_sec=40_000.0, prefill_tps_source="input",
                        replica_cost_per_hour=60.0)
    gpu = GpuInputs(load_mode="blocking", trace_utilization=1.0, cache_node_ec2_usd_per_hour=27.3168,
                    cache_node_margin=0.2)
    row = _fetch_all(cost_rows(_ref_sim(), inputs, gpu))
    spend = row["spend"]
    assert spend["exclusive"] is True
    assert set(spend) == {"exclusive", "same_fleet_throughput_gain", "same_fleet_label",
                          "shrink_fleet_gpu_usd_per_month"}
    s_net = SAVED / 40_000.0 - L_G
    assert row["phi"] == pytest.approx(s_net / WALL)
    assert spend["same_fleet_throughput_gain"] == pytest.approx(row["phi"] / (1 - row["phi"]))
    shrink = s_net / (WALL / 2) * 720 * 60.0
    assert spend["shrink_fleet_gpu_usd_per_month"] == pytest.approx(shrink)
    # The net figure subtracts the cache nodes from the shrink column only, never adds the other.
    assert row["net_usd_per_month"] == pytest.approx(shrink - 27.3168 * 1.2 * 720)
    report = build_report(_report_sim(), inputs, gpu=gpu, priority="all")
    header = spend_table(report["phase1"])[0]
    assert any(h.startswith("EITHER same fleet") for h in header)
    assert any(h.startswith("OR shrink fleet") for h in header)
    assert not any("total" in h.lower() for h in header)
    # No row field and no JSON field is the sum of the two spend modes.
    both = spend["same_fleet_throughput_gain"] + spend["shrink_fleet_gpu_usd_per_month"]
    assert all(v != pytest.approx(both) for v in _numbers(row))
    for prow in report["phase1"]["costs"]["rows"]:
        sp = prow.get("spend")
        if sp and sp["same_fleet_throughput_gain"] is not None and sp["shrink_fleet_gpu_usd_per_month"]:
            pair = sp["same_fleet_throughput_gain"] + sp["shrink_fleet_gpu_usd_per_month"]
            assert all(v != pytest.approx(pair) for v in _numbers(phase1_json(report)))


def _numbers(value):
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, dict):
        return [n for v in value.values() for n in _numbers(v)]
    if isinstance(value, (list, tuple)):
        return [n for v in value for n in _numbers(v)]
    return []


def test_failed_or_untrusted_p_omits_net_rows_with_a_reason():
    sim = _report_sim()
    gpu = GpuInputs(load_mode="blocking")
    for inputs, why in ((CostInputs(prefill_tokens_per_sec=1.0, prefill_tps_source="input"),
                         "P check failed"),
                        (CostInputs(prefill_tokens_per_sec=50_000.0, prefill_tps_source="estimated"),
                         "gauge estimate")):
        md = render_markdown(build_report(sim, inputs, gpu=gpu, priority="all"))
        gpu_section = md.split("## GPU cost")[1].split("\n## ")[0]
        assert why in gpu_section and "S_net, phi, spend and net $ are omitted" in gpu_section
        assert "L_g, P*, beta* and Q* do not use P, so they stay." in gpu_section
        assert "| L_g |" in gpu_section
        assert "## Value per node count (P3)" in md
        assert "## Spend modes" not in md


def test_write_table_marks_auto_rewrites_and_splits_them_by_cause():
    p = SimParams(chunk_tokens=64, capacities=parse_capacities("16KiB,inf"), kv_bytes_per_token=8,
                  admission="second-hit")
    cp = ContentionParams(prefill_tokens_per_sec=5000.0, l2_read_gbps_per_node=1e-3,
                          gpu_rtt_ms=0.5, gpu_rtt_ms_source="input")
    sim = json.loads(json.dumps(simulate(workload_records(nodes=2, requests=150, gpu_tokens=4096), p,
                                         sources=["fixture"], contention=cp)))
    assert sim["inputs"]["params"]["count_rewrites_source"] == "auto"
    table = write_table(build_report(sim, CostInputs(), priority="all"))
    header, rows = table[0], table[1:]
    assert "Rewrites: prefix blocked" in header and "Rewrites: max load cap" in header
    fetch_all = [r for r in rows if "(rewrites counted, auto)" in r[1]]
    assert fetch_all and len(fetch_all) == len(sim["capacities"])
    for row, cap in zip(fetch_all, sim["capacities"]):
        by_cause = cap["rewrites_by_cause"]
        assert row[header.index("Rewrites: evict write")] == fmt_num(by_cause["evict_write"])


# Rendered sections and CLI.

def _report_sim(sweep=None, gpu_rtt_ms=0.5, l2_rtt_ms=0.5):
    p = SimParams(chunk_tokens=64, capacities=parse_capacities("16KiB,inf"), kv_bytes_per_token=8)
    cp = ContentionParams(prefill_tokens_per_sec=5000.0, l2_read_gbps_per_node=1e-3, l2_rtt_ms=l2_rtt_ms,
                          gpu_rtt_ms=gpu_rtt_ms, gpu_rtt_ms_source=None if gpu_rtt_ms is None else "input")
    sim = simulate(workload_records(nodes=2, requests=150, gpu_tokens=4096), p, sources=["fixture"],
                   contention=cp, sweep=sweep)
    return json.loads(json.dumps(sim))


def _write_sim(tmp_path, sim):
    path = tmp_path / "sim.json"
    path.write_text(json.dumps(sim))
    return path


# Test 13: P1 gate.

def test_priority_latency_without_a_sweep_omits_the_p1_headline(tmp_path):
    path = _write_sim(tmp_path, _report_sim())
    md = tmp_path / "r.md"
    assert main(["report", str(path), "-o", str(tmp_path / "r.html"), "--markdown", str(md),
                 "--priority", "latency"]) == 0
    text = md.read_text()
    assert P1_SWEEP_REFUSAL in text
    assert "P1 latency at" not in text
    # The secondary figures are still shown.
    assert "## Latency break-even (P1)" in text and "p90 of per-run TTFT change, runs with hits" in text
    assert "## GPU cost" not in text


def test_priority_latency_with_a_sweep_gives_x_t_star_and_y(tmp_path):
    path = _write_sim(tmp_path, _report_sim(sweep={"write_stall_ms_per_gb": [0, 100]}))
    md = tmp_path / "r.md"
    assert main(["report", str(path), "-o", str(tmp_path / "r.html"), "--markdown", str(md),
                 "--priority", "latency", "--sweep"]) == 0
    text = md.read_text()
    assert "P1 latency at 16KiB:" in text and "at r_g = 0.5 ms" in text
    assert "fitted fixed cost per fetch, not network RTT" in text
    assert "| X_t (ms/GB) |" in text
    assert P1_SWEEP_REFUSAL not in text
    assert "(default, not measured)" not in text.split("## Priority inputs")[0]
    assert "(fitted TTFT value)" not in text


def test_p1_headline_qualifies_a_default_r_g_and_a_fitted_l2_rtt():
    sim = _report_sim(sweep={"write_stall_ms_per_gb": [0, 100]}, gpu_rtt_ms=None, l2_rtt_ms=750.0)
    assert sim["inputs"]["contention"].get("gpu_rtt_ms_source") in (None, "default")
    phase1 = build_report(sim, CostInputs(), priority="latency", sweep=True)["phase1"]
    heads = phase1_headlines(phase1)
    p1 = [h for h in heads if h.startswith("P1 latency at")]
    secondary = [h for h in heads if h.startswith("P1 secondary at")]
    assert p1 and all("r_g = 0.5 ms (default, not measured)" in h for h in p1)
    assert secondary and all("l2_rtt_ms = 750 ms (fitted TTFT value)" in h for h in secondary)


def test_priority_all_renders_every_section(tmp_path):
    sim = _report_sim(sweep={"write_stall_ms_per_gb": [0, 100]})
    inputs = CostInputs(prefill_tokens_per_sec=50_000.0, prefill_tps_source="input",
                        replica_cost_per_hour=60.0)
    gpu = GpuInputs(load_mode="blocking", tpot_change=0.09, cache_node_ec2_usd_per_hour=22.7808)
    report = build_report(sim, inputs, gpu=gpu, priority="all", sweep=True)
    md = render_markdown(report)
    for title in ("Priority headlines", "Latency break-even (P1)", "GPU cost", "Value per node count (P3)",
                  "Spend modes", "Priority inputs and sources"):
        assert f"## {title}" in md
    assert md.index("## Bandwidth and write-cost model") < md.index("## Priority headlines") \
        < md.index("## Per-model breakdown")
    assert "no $ headline (needs --cache-node-ec2-usd-per-hour and --cache-node-margin)" in md
    assert "P2 throughput at" in md and "P3 cost at" in md
    html = render_html(report)
    assert "<h2>GPU cost</h2>" in html and "<h2>Priority inputs and sources</h2>" in html


def test_write_metrics_section_needs_a_phase1_sim():
    sim = _report_sim()
    md = render_markdown(build_report(sim, CostInputs(), priority="cost"))
    assert "## Writes and admission" in md and "Dead write GB share" in md
    assert "GPU cost needs --load-mode" in md


def test_priority_sections_explain_a_sim_without_phase1_fields(tmp_path):
    sim = json.loads(json.dumps(simulate(
        workload_records(nodes=2, requests=60, gpu_tokens=4096),
        SimParams(chunk_tokens=64, capacities=parse_capacities("inf"), kv_bytes_per_token=8))))
    md = render_markdown(build_report(sim, CostInputs(), priority="all"))
    assert "P2 and P3: needs simulate --contention on." in md


def test_lookup_timeout_rows_need_async():
    p = SimParams(chunk_tokens=64, capacities=parse_capacities("inf"), kv_bytes_per_token=8,
                  prefill_tokens_per_sec_per_request=5000.0, fetch_policies=["lmcache", "ideal",
                                                                             "lookup-timeout:1"])
    cp = ContentionParams(prefill_tokens_per_sec=5000.0, l2_lookup_ms=0.5)
    sim = json.loads(json.dumps(simulate(workload_records(nodes=2, requests=60, gpu_tokens=4096), p,
                                         contention=cp)))
    blocking = cost_rows(sim, CostInputs(), GpuInputs(load_mode="blocking"))
    async_ = cost_rows(sim, CostInputs(), GpuInputs(load_mode="async"))
    assert "lookup-timeout:1" not in {r["policy"] for r in blocking["rows"]}
    assert "lookup-timeout:1" in {r["policy"] for r in async_["rows"]}


def test_cli_json_and_flag_checks(tmp_path):
    path = _write_sim(tmp_path, _report_sim())
    out = tmp_path / "r.json"
    html = str(tmp_path / "r.html")
    assert main(["report", str(path), "-o", html, "--json", str(out), "--load-mode", "blocking",
                 "--prefill-tokens-per-sec", "50000", "--cache-node-ec2-usd-per-hour", "22.7808",
                 "--cache-node-margin", "0.25"]) == 0
    data = json.loads(out.read_text())
    assert data["priority"] == "all" and data["headlines"]
    assert data["costs"]["node_price_usd_per_hour"] == pytest.approx(22.7808 * 1.25)
    assert data["costs"]["copy_gb_per_sec_per_replica"] is None  # unbounded
    assert main(["report", str(path), "-o", html, "--sweep"]) == 2
    assert main(["report", str(path), "-o", html, "--gpu-overlap", "0.5", "--load-mode", "blocking"]) == 2
    assert main(["report", str(path), "-o", html, "--gpu-load-gb-per-sec", "50"]) == 2
    assert main(["report", str(path), "-o", html, "--trace-utilization", "1.5"]) == 2


# On the report side: new flags change nothing without --priority.

def test_report_without_priority_equals_the_goldens(tmp_path):
    from golden_cases import _fixture_report

    base = _fixture_report()
    report = build_report(base["sim"], base["inputs"], gpu=GpuInputs(load_mode="async", tpot_change=0.1))
    assert render_html(report) == load("report_fixture.html")
    assert render_markdown(report) == load("report_fixture.md")
    for name in ("report_fixture.html", "report_fixture.md", "report_contention_store.md"):
        assert CASES[name]() == load(name)


def test_cli_report_output_does_not_change_with_new_flags_but_no_priority(tmp_path):
    seg = tmp_path / "n0" / "n0-0-0.kvt.jsonl.gz"
    seg.parent.mkdir()
    with gzip.open(seg, "wt") as fh:
        for rec in workload_records(nodes=1, requests=80, gpu_tokens=2048):
            fh.write(json.dumps(rec) + "\n")
    sim = tmp_path / "sim.json"
    assert main(["simulate", str(seg.parent), "--chunk-tokens", "64", "--kv-bytes-per-token", "8",
                 "--capacity", "4KiB,inf", "--contention", "on", "--prefill-tokens-per-sec", "5000",
                 "-o", str(sim)]) == 0
    base = ["report", str(sim), "--prefill-tokens-per-sec", "5000"]
    outs = []
    for extra in ([], ["--load-mode", "blocking", "--tpot-change", "0.09", "--gpu-rtt-ms", "750",
                       "--cache-node-ec2-usd-per-hour", "22.78", "--json", str(tmp_path / "x.json")]):
        md, html = tmp_path / f"r{len(outs)}.md", tmp_path / f"r{len(outs)}.html"
        assert main(base + ["-o", str(html), "--markdown", str(md)] + extra) == 0
        outs.append((md.read_text(), html.read_text()))
    assert outs[0] == outs[1]


def test_build_report_refuses_an_unknown_priority():
    with pytest.raises(ValueError):
        build_report(copy.deepcopy(_report_sim()), CostInputs(), priority="speed")
