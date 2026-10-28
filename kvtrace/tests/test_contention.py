import gzip
import json
import os

import pytest
from conftest import workload_records

from kvtrace.cli import main
from kvtrace.contention import (
    HIT,
    MISS,
    WRITE,
    ContentionError,
    ContentionParams,
    chunk_seconds,
    evaluate,
    fit_quadratic_rate,
)
from kvtrace.costmodel import CostInputs
from kvtrace.report import build_report, render_markdown, summary_lines
from kvtrace.simulator import SimParams, parse_capacities, simulate

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CHUNK_BYTES = 1000
SLOW_GBPS = 8e-6  # 1000 bytes/s: one chunk takes 1 s
TOK = 256  # one chunk; at 100 tokens/s it takes 2.56 s to recompute


def params(**kwargs):
    base = dict(prefill_tokens_per_sec=100.0, l2_read_gbps_per_node=SLOW_GBPS,
                l2_write_gbps_per_node=SLOW_GBPS, l2_rtt_ms=0.0)
    base.update(kwargs)
    return ContentionParams(**base)


def hit(ts, node, run, end=TOK):
    return (HIT, ts, node, run, TOK, end)


def miss(ts, node, run, end=TOK):
    return (MISS, ts, node, run, TOK, end)


def write(ts, node, run):
    return (WRITE, ts, node, run, 0, 0)


def deltas(result):
    return {p: [r[p] for r in result["run_deltas"]] for p in ("ideal", "lmcache")}


def test_single_node_burst_queues_fifo():
    events = [hit(0, 0, 0), hit(0, 0, 0, end=2 * TOK), hit(0, 0, 1)]
    out = evaluate(events, CHUNK_BYTES, ["a"], 10.0, params(), keep_runs=True)
    d = deltas(out)
    # Run 0 fetches 2 chunks in 2 s and saves 5.12 s; run 1 waits 2 s behind it.
    assert d["lmcache"] == pytest.approx([2 - 5.12, 2 + 1 - 2.56])
    # kvtrace-ideal skips run 1's fetch: 3 s to fetch beats 2.56 s of recompute only for run 0.
    assert d["ideal"] == pytest.approx([2 - 5.12, 0.0])
    lm = out["policies"]["lmcache"]
    assert lm["hits_slower_than_recompute"] == 1 and lm["hits_slower_fraction"] == pytest.approx(1 / 3)
    assert lm["per_node"]["a"]["link"]["read"]["queue_wait_mean"] == pytest.approx(1.0)
    assert lm["per_node"]["a"]["read_bytes_per_sec"] == pytest.approx(300.0)
    assert out["policies"]["ideal"]["fetched_chunks"] == 2


def test_two_nodes_have_independent_links():
    events = [hit(0, 0, 0), hit(0, 0, 0, end=2 * TOK), hit(0, 1, 1)]
    out = evaluate(events, CHUNK_BYTES, ["a", "b"], 10.0, params(), keep_runs=True)
    assert deltas(out)["lmcache"] == pytest.approx([2 - 5.12, 1 - 2.56])


def test_shared_link_makes_writes_delay_reads():
    # Run 0 misses and saves 3 chunks at t=0; run 1 hits one chunk at t=0.5 on the same node.
    events = [miss(0, 0, 0), write(0, 0, 0), write(0, 0, 0), write(0, 0, 0), hit(0.5, 0, 1)]
    separate = evaluate(events, CHUNK_BYTES, ["a"], 10.0, params(), keep_runs=True)
    shared = evaluate(events, CHUNK_BYTES, ["a"], 10.0, params(l2_link_shared=True), keep_runs=True)
    assert deltas(separate)["lmcache"] == pytest.approx([0.0, 1 - 2.56])
    # The writes hold the shared link until t=3: wait 2.5 s, then 1 s transfer.
    assert deltas(shared)["lmcache"] == pytest.approx([0.0, 2.5 + 1 - 2.56])
    assert "shared" in shared["policies"]["lmcache"]["per_node"]["a"]["link"]
    assert shared["policies"]["lmcache"]["per_node"]["a"]["write_bytes_per_sec"] == pytest.approx(300.0)


def test_blocking_writes_add_save_time_async_do_not():
    events = [miss(0, 0, 0), write(0, 0, 0), write(0, 0, 0)]
    async_out = evaluate(events, CHUNK_BYTES, ["a"], 10.0, params(), keep_runs=True)
    blocking = evaluate(events, CHUNK_BYTES, ["a"], 10.0, params(l2_write_mode="blocking"),
                        keep_runs=True)
    assert deltas(async_out)["lmcache"] == [0.0]
    assert deltas(blocking)["lmcache"] == pytest.approx([2.0])
    assert deltas(blocking)["ideal"] == pytest.approx([2.0])
    assert blocking["run_deltas"][0]["lmcache_write_delay"] == pytest.approx(2.0)
    assert blocking["run_deltas"][0]["ideal_write_delay"] == pytest.approx(2.0)


def test_evict_writes_use_the_link_without_a_run():
    events = [write(0, 0, -1), write(0, 0, -1), hit(0, 0, 0)]
    out = evaluate(events, CHUNK_BYTES, ["a"], 10.0, params(l2_link_shared=True), keep_runs=True)
    assert out["runs"] == 1
    assert deltas(out)["lmcache"] == pytest.approx([2 + 1 - 2.56])


def test_write_load_delays_every_run_on_the_node_that_writes():
    gb = 10**9
    events = ([miss(0, 0, 0)] + [write(0, 0, 0)] * 60
              + [miss(1, 0, 1), hit(1, 0, 2), miss(1, 1, 3)])
    cp = params(l2_write_gbps_per_node=8000.0, l2_read_gbps_per_node=8000.0, write_load_ms_per_gbs=50.0)
    out = evaluate(events, gb, ["a", "b"], 10.0, cp, keep_runs=True)
    d = {r["run"]: r for r in out["run_deltas"]}
    # 60 GB in the last 60 s = 1 GB/s on node a; 50 ms per GB/s, whatever the run's length or hits.
    assert d[1]["lmcache"] == pytest.approx(0.05)
    assert d[1]["write_load"] == pytest.approx(0.05)
    assert d[2]["lmcache"] == pytest.approx(0.001 - 2.56 + 0.05)  # 1 GB chunk at 1000 GB/s, then the load
    assert d[3]["lmcache"] == 0.0  # node b wrote nothing
    assert d[0]["lmcache"] == 0.0  # its own writes come after its lookup


def test_write_load_zero_keeps_results_and_run_rows():
    gb = 10**9
    events = [miss(0, 0, 0)] + [write(0, 0, 0)] * 60 + [miss(1, 0, 1), hit(1, 0, 2)]
    cp = params(l2_write_gbps_per_node=8.0, write_stall_ms_per_gb=100.0)
    base = evaluate(events, gb, ["a"], 10.0, cp, keep_runs=True)
    zero = evaluate(events, gb, ["a"], 10.0, params(l2_write_gbps_per_node=8.0, write_stall_ms_per_gb=100.0,
                                                     write_load_ms_per_gbs=0.0), keep_runs=True)
    assert zero == base
    assert "write_load" not in base["run_deltas"][0]


def test_write_load_uses_the_trailing_window():
    gb = 10**9
    events = [miss(0, 0, 0)] + [write(0, 0, 0)] * 60 + [miss(30, 0, 1), miss(61, 0, 2)]
    out = evaluate(events, gb, ["a"], 100.0, params(l2_write_gbps_per_node=8000.0,
                                                    write_load_ms_per_gbs=50.0), keep_runs=True)
    d = {r["run"]: r["lmcache"] for r in out["run_deltas"]}
    assert d[1] == pytest.approx(0.05)
    assert d[2] == 0.0  # the writes at t=0 left the 60 s window


def test_write_stall_slows_the_node_that_writes():
    gb = 10**9
    events = [miss(0, 0, 0)] + [write(0, 0, 0)] * 60 + [miss(1, 0, 1), miss(1, 1, 2)]
    cp = params(l2_write_gbps_per_node=8.0, write_stall_ms_per_gb=100.0)
    out = evaluate(events, gb, ["a", "b"], 10.0, cp, keep_runs=True)
    d = {r["run"]: r["lmcache"] for r in out["run_deltas"]}
    # 60 GB in the last 60 s = 1 GB/s; 100 ms stall per GB = 10% of host time lost,
    # so the 2.56 s of compute takes 2.56 / 0.9 s.
    assert d[1] == pytest.approx(2.56 * 0.1 / 0.9)
    assert d[2] == 0.0  # node b wrote nothing
    assert d[0] == 0.0  # its own writes come after its lookup


def test_lookup_checks_queue_per_node_and_delay_the_fetch():
    events = [miss(0, 0, 0), hit(0, 0, 1), miss(0, 1, 2)]
    out = evaluate(events, CHUNK_BYTES, ["a", "b"], 10.0, params(l2_lookup_ms=100.0), keep_runs=True)
    d = {r["run"]: r["lmcache"] for r in out["run_deltas"]}
    # Node a checks run 0 then run 1 (0.1 s each); run 1 fetches after its check ends.
    assert d[0] == pytest.approx(0.1)
    assert d[1] == pytest.approx(0.2 + 1 - 2.56)
    assert d[2] == pytest.approx(0.1)
    assert out["policies"]["lmcache"]["per_node"]["a"]["link"]["lookup"]["queue_wait_mean"] == \
        pytest.approx(0.05)


def test_quadratic_prefill_model_and_fit():
    cp = params(prefill_model="quadratic", prefill_quadratic_tokens=256.0)
    # Tokens [256, 512): (256 + (512^2 - 256^2) / 512) / P0 = 640 / P0.
    assert chunk_seconds(256, 512, cp, 100.0) == pytest.approx(6.4)
    assert chunk_seconds(256, 512, params(), 100.0) == pytest.approx(2.56)
    # Mean depth 384, so P0 = 100 * (1 + 384 / 256) and the fit preserves total time.
    p0 = fit_quadratic_rate([miss(0, 0, 0, end=512)], cp)
    assert p0 == pytest.approx(250.0)
    assert chunk_seconds(256, 512, cp, p0) == pytest.approx(256 / 100.0)


def test_params_are_validated():
    with pytest.raises(ContentionError):
        params(prefill_tokens_per_sec=0).validate()
    with pytest.raises(ContentionError):
        params(l2_write_mode="sometimes").validate()
    with pytest.raises(ContentionError):
        params(l2_read_gbps_per_node=float("nan")).validate()
    with pytest.raises(ContentionError):
        params(write_load_ms_per_gbs=-1.0).validate()


def _golden():
    with gzip.open(os.path.join(FIXTURES, "sim_pre_contention.json.gz"), "rt") as fh:
        return json.load(fh)


# sim.json fields added after the golden file was made; their defaults keep the results.
NEW_TOP_KEYS = ("lookup_time", "caveats")
NEW_PARAM_KEYS = ("lookup_time", "prefill_tokens_per_sec_per_request", "warmup_seconds",
                  "l2_per_key_overhead_bytes", "l2_usable_fraction", "l2_lookup")


def _without_new_fields(sim):
    for key in NEW_TOP_KEYS:
        sim.pop(key, None)
    for key in NEW_PARAM_KEYS:
        sim["inputs"]["params"].pop(key, None)
    return sim


@pytest.mark.parametrize("write_policy", ["store", "evict"])
def test_contention_off_matches_outputs_before_the_model(write_policy):
    p = SimParams(chunk_tokens=64, capacities=parse_capacities("16KiB,64KiB,inf"),
                  kv_bytes_per_token=8, write_policy=write_policy)
    got = json.loads(json.dumps(simulate(workload_records(), p), sort_keys=True))
    assert _without_new_fields(got) == _golden()[write_policy]


def test_contention_on_keeps_hit_results_and_adds_the_model():
    p = SimParams(chunk_tokens=64, capacities=parse_capacities("16KiB,inf"), kv_bytes_per_token=8)
    plain = simulate(workload_records(), p)
    cp = ContentionParams(prefill_tokens_per_sec=10000.0)
    with_model = simulate(workload_records(), p, contention=cp)
    assert with_model["inputs"]["contention"]["prefill_tokens_per_sec"] == 10000.0
    for a, b in zip(plain["capacities"], with_model["capacities"]):
        con = b.pop("contention")
        assert a == b
        assert con["hit_chunks"] == a["hits"]
        assert con["policies"]["lmcache"]["fetched_chunks"] == a["hits"]


def _slow_sim():
    p = SimParams(chunk_tokens=64, capacities=parse_capacities("inf"), kv_bytes_per_token=1024)
    cp = ContentionParams(prefill_tokens_per_sec=1e6, l2_read_gbps_per_node=0.01, l2_rtt_ms=5.0,
                          write_stall_ms_per_gb=1e6)
    return simulate(workload_records(nodes=2, requests=150, gpu_tokens=4096), p, contention=cp)


def test_report_says_plainly_when_ttft_gets_worse():
    report = build_report(_slow_sim(), CostInputs(prefill_tokens_per_sec=1e6))
    text = " ".join(summary_lines(report))
    assert "model estimate, not a measurement" in text
    assert "makes TTFT WORSE with LMCache-like fetching" in text
    md = render_markdown(report)
    assert "## Bandwidth and write-cost model (model estimates, not measurements)" in md
    assert "Host stall per GB written" in md


def test_report_without_contention_has_no_model_section(sim_json):
    report = build_report(json.loads(sim_json.read_text()), CostInputs())
    assert "Bandwidth and write-cost model" not in render_markdown(report)


def test_cli_simulate_contention_on_writes_run_deltas(tmp_path, capsys):
    seg = tmp_path / "n0" / "n0-0-0.kvt.jsonl.gz"
    seg.parent.mkdir()
    with gzip.open(seg, "wt") as fh:
        for rec in workload_records(nodes=1, requests=80, gpu_tokens=2048):
            fh.write(json.dumps(rec) + "\n")
    sim_path, runs_path = tmp_path / "sim.json", tmp_path / "runs.jsonl"
    assert main(["simulate", str(tmp_path / "n0"), "--chunk-tokens", "64", "--kv-bytes-per-token", "8",
                 "--capacity", "inf", "--contention", "on", "--prefill-tokens-per-sec", "5000",
                 "--l2-link-shared", "--l2-write-mode", "blocking", "-o", str(sim_path),
                 "--contention-runs-out", str(runs_path)]) == 0
    assert "LMCache-like" in capsys.readouterr().out
    sim = json.loads(sim_path.read_text())
    con = sim["capacities"][0]["contention"]
    assert "run_deltas" not in con and sim["inputs"]["contention"]["l2_write_mode"] == "blocking"
    rows = [json.loads(line) for line in runs_path.read_text().splitlines()]
    assert len(rows) == con["runs"] and rows[0]["capacity"] == "inf"
    assert main(["simulate", str(tmp_path / "n0"), "--kv-bytes-per-token", "8", "--chunk-tokens", "64",
                 "--contention", "on", "-o", str(sim_path)]) == 2


def test_cli_write_load_flag_reaches_the_model_and_report(tmp_path):
    seg = tmp_path / "n0" / "n0-0-0.kvt.jsonl.gz"
    seg.parent.mkdir()
    with gzip.open(seg, "wt") as fh:
        for rec in workload_records(nodes=1, requests=80, gpu_tokens=2048):
            fh.write(json.dumps(rec) + "\n")
    common = ["simulate", str(tmp_path / "n0"), "--chunk-tokens", "64", "--kv-bytes-per-token", "8",
              "--capacity", "inf", "--contention", "on", "--prefill-tokens-per-sec", "5000"]
    base_path, load_path = tmp_path / "base.json", tmp_path / "load.json"
    assert main(common + ["-o", str(base_path)]) == 0
    assert main(common + ["--write-load-ms-per-gbs", "1e9", "-o", str(load_path)]) == 0
    base, load = json.loads(base_path.read_text()), json.loads(load_path.read_text())
    assert base["inputs"]["contention"]["write_load_ms_per_gbs"] == 0.0
    assert load["inputs"]["contention"]["write_load_ms_per_gbs"] == 1e9
    p50 = [s["capacities"][0]["contention"]["policies"]["lmcache"]["ttft_delta"]["p50"] for s in (base, load)]
    assert p50[1] > p50[0]
    md = render_markdown(build_report(load, CostInputs()))
    assert "Delay per request per GB/s written" in md


def test_ideal_fetches_only_the_beneficial_prefix():
    # The second hit chunk holds only 10 computed tokens: fetching it (1 s) costs more than
    # recomputing it (0.1 s), so kvtrace-ideal stops after the first chunk.
    events = [hit(0, 0, 0), (HIT, 0, 0, 0, 10, 2 * TOK)]
    out = evaluate(events, CHUNK_BYTES, ["a"], 10.0, params(), keep_runs=True)
    row = out["run_deltas"][0]
    assert row["ideal_fetched_chunks"] == 1 and row["ideal"] == pytest.approx(1 - 2.56)
    assert row["lmcache"] == pytest.approx(2 - 2.66)


def test_write_stall_share_is_capped():
    gb = 10**9
    events = [miss(0, 0, 0)] + [write(0, 0, 0)] * 600 + [miss(1, 0, 1)]
    out = evaluate(events, gb, ["a"], 10.0, params(l2_write_gbps_per_node=8000.0,
                                                    write_stall_ms_per_gb=1000.0), keep_runs=True)
    assert out["run_deltas"][1]["lmcache"] == pytest.approx(2.56 * 0.95 / 0.05)


def test_summary_warns_when_no_capacity_has_hits():
    p = SimParams(chunk_tokens=64, capacities=parse_capacities("1B"), kv_bytes_per_token=1024)
    cp = ContentionParams(prefill_tokens_per_sec=1e6, l2_lookup_ms=50.0)
    sim = simulate(workload_records(nodes=1, requests=40, gpu_tokens=2048), p, contention=cp)
    assert sim["capacities"][0]["hits"] == 0
    text = " ".join(summary_lines(build_report(sim, CostInputs())))
    assert "No capacity produced L2 hits" in text and "makes TTFT WORSE" in text


def test_summary_has_no_warning_when_the_model_predicts_faster_ttft():
    p = SimParams(chunk_tokens=64, capacities=parse_capacities("inf"), kv_bytes_per_token=8)
    cp = ContentionParams(prefill_tokens_per_sec=100.0, l2_read_gbps_per_node=1000.0, l2_rtt_ms=0.0)
    sim = simulate(workload_records(nodes=2, requests=150, gpu_tokens=4096), p, contention=cp)
    lm = sim["capacities"][0]["contention"]["policies"]["lmcache"]["ttft_delta"]
    assert lm["p90"] <= 0
    text = " ".join(summary_lines(build_report(sim, CostInputs(prefill_tokens_per_sec=100.0))))
    assert "Bandwidth and write-cost model" in text and "WORSE" not in text
    md = render_markdown(build_report(sim, CostInputs(prefill_tokens_per_sec=200.0)))
    assert "this section uses the simulate-time value" in md


def test_cli_rejects_runs_out_without_contention(tmp_path):
    assert main(["simulate", str(tmp_path), "--contention-runs-out", str(tmp_path / "r.jsonl")]) == 2


FAST_GBPS = 8e-3  # 1,000,000 bytes/s: one chunk takes 1 ms on a client link


def keyed(kind, ts, node, run, key, end=TOK):
    return (kind, ts, node, run, TOK if kind != WRITE else 0, end if kind != WRITE else 0, key)


def test_few_cache_nodes_make_hits_wait_on_the_server_link():
    # Two client nodes hit one chunk each at t=0. Their own links are fast; one slow
    # cache node (1 s per chunk) serves both, so the second fetch waits 1 s.
    events = [keyed(HIT, 0, 0, 0, key=0), keyed(HIT, 0, 1, 1, key=1)]
    client = dict(l2_read_gbps_per_node=FAST_GBPS, l2_write_gbps_per_node=FAST_GBPS)
    off = evaluate(events, CHUNK_BYTES, ["a", "b"], 10.0, params(**client), keep_runs=True)
    assert deltas(off)["lmcache"] == pytest.approx([0.001 - 2.56, 0.001 - 2.56])
    assert off["model"] == "fifo-fluid-per-node"
    assert "per_cache_node" not in off["policies"]["lmcache"]
    one = evaluate(events, CHUNK_BYTES, ["a", "b"], 10.0,
                   params(cache_nodes=1, cache_node_gbps=SLOW_GBPS, **client), keep_runs=True)
    assert deltas(one)["lmcache"] == pytest.approx([1 - 2.56, 2 - 2.56])
    srv = one["policies"]["lmcache"]["per_cache_node"]["cache-0"]
    assert srv["link"]["read"]["queue_wait_mean"] == pytest.approx(0.5)
    assert srv["read_bytes_per_sec"] == pytest.approx(200.0)
    # Two cache nodes: the keys map to different nodes, so neither fetch waits.
    two = evaluate(events, CHUNK_BYTES, ["a", "b"], 10.0,
                   params(cache_nodes=2, cache_node_gbps=SLOW_GBPS, **client), keep_runs=True)
    assert deltas(two)["lmcache"] == pytest.approx([1 - 2.56, 1 - 2.56])
    waits = [n["link"]["read"]["queue_wait_mean"]
             for n in two["policies"]["lmcache"]["per_cache_node"].values()]
    assert waits == [0.0, 0.0]


def test_blocking_writes_wait_for_the_cache_node():
    events = [keyed(MISS, 0, 0, 0, key=0), keyed(WRITE, 0, 0, 0, key=0),
              keyed(MISS, 0, 1, 1, key=0), keyed(WRITE, 0, 1, 1, key=0)]
    cp = params(l2_read_gbps_per_node=FAST_GBPS, l2_write_gbps_per_node=FAST_GBPS,
                l2_write_mode="blocking", cache_nodes=1, cache_node_gbps=SLOW_GBPS)
    out = evaluate(events, CHUNK_BYTES, ["a", "b"], 10.0, cp, keep_runs=True)
    assert [r["lmcache_write_delay"] for r in out["run_deltas"]] == pytest.approx([1.0, 2.0])


def test_cache_node_params_are_validated():
    with pytest.raises(ContentionError):
        params(cache_nodes=2).validate()
    with pytest.raises(ContentionError):
        params(cache_nodes=0, cache_node_gbps=1.0).validate()
    with pytest.raises(ContentionError):
        params(cache_nodes=1, cache_node_gbps=0.0).validate()
    assert "cache_nodes" not in params().as_dict()
    assert params(cache_nodes=3, cache_node_gbps=100.0).as_dict()["cache_nodes"] == 3


def test_cli_cache_node_flags(tmp_path):
    trace = tmp_path / "t.jsonl.gz"
    recs = [{"t": "S", "ts": float(i), "node": n, "rank": 0, "model": "m", "ns": "", "medium": "GPU",
             "bs": 16, "h": ["a1", "a2"], "p": None, "n": 32} for i, n in ((1, "A"), (2, "B"))]
    with gzip.open(trace, "wt") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in recs))
    out = tmp_path / "sim.json"
    assert main(["simulate", str(trace), "--kv-bytes-per-token", "1", "--chunk-tokens", "32",
                 "--capacity", "inf", "--contention", "on", "--prefill-tokens-per-sec", "100",
                 "--cache-nodes", "2", "--cache-node-gbps", "100", "-o", str(out)]) == 0
    sim = json.loads(out.read_text())
    assert sim["inputs"]["contention"]["cache_nodes"] == 2
    assert len(sim["capacities"][0]["contention"]["policies"]["ideal"]["per_cache_node"]) == 2
