import csv
import gzip
import io
import json
import os

import pytest

from kvtrace.cli import main
from kvtrace.metrics import filter_metrics
from kvtrace.models import config_kv_dtype_bytes
from kvtrace.tiers import (
    _quantile_from_buckets,
    compare_with_sim,
    measure_tiers,
    node_increments,
    render_csv,
    render_markdown,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
# Measure windows of a real OFF/ON run pair (vLLM 0.30.0 + LMCache 0.5.5), from
# each run's summary.json; the fixtures are trimmed copies of its /metrics scrapes.
WINDOWS = {"off1": (1792445719.26, 1792450788.70), "on1": (1792452237.37, 1792457307.71)}
LBL = 'engine="0",model_name="qwen3-235b"'


def fixture_samples(run):
    out = []
    for which, ts in zip(("start", "end"), WINDOWS[run]):
        with open(os.path.join(FIX, f"vllm030_lmcache_{run}_metrics_{which}.txt"), encoding="utf-8") as fh:
            out.append((ts, filter_metrics(fh.read())))
    return out


def m_records(samples, node="A1"):
    return [{"t": "M", "ts": ts, "node": node, "metrics": m} for ts, m in samples]


def test_allowlist_keeps_source_split_and_ttft_buckets_from_real_vllm_030_text():
    (_, start), (_, end) = fixture_samples("on1")
    by_source = {k: v for k, v in end.items() if k.startswith("vllm:prompt_tokens_by_source_total")}
    assert by_source == {
        f'vllm:prompt_tokens_by_source_total{{{LBL},source="local_compute"}}': 55156176.0,
        f'vllm:prompt_tokens_by_source_total{{{LBL},source="local_cache_hit"}}': 12077664.0,
        f'vllm:prompt_tokens_by_source_total{{{LBL},source="external_kv_transfer"}}': 8672096.0,
    }
    assert end[f'vllm:time_to_first_token_seconds_bucket{{{LBL},le="+Inf"}}'] == 6300.0
    assert end[f"vllm:time_to_first_token_seconds_count{{{LBL}}}"] == 6300.0
    assert not any("request_success" in k or "process_cpu" in k for k in end)


def test_p5_off_and_on_reproduce_the_measured_summary():
    off = measure_tiers(m_records(fixture_samples("off1")), interval=60)["fleet"]["total"]
    on = measure_tiers(m_records(fixture_samples("on1")), interval=60)["fleet"]["total"]
    # The run summary: OFF CPU-tier (LMCache) tokens 3,867,744 and prompt_tokens_by_source totals.
    assert off["external_tokens"] == 3867744 and off["l0_tokens"] == 11164208
    assert off["computed_tokens"] == 56494331 and off["prompt_tokens"] == 71526283
    assert on["external_tokens"] == 8544976 and on["prompt_tokens"] == 71402075
    # Valkey share = external ON minus OFF, as a share of ON prompt tokens: 6.55%.
    share = 100 * (on["external_tokens"] - off["external_tokens"]) / on["prompt_tokens"]
    assert round(share, 2) == 6.55
    assert on["requests"] == 5981  # request_prompt_tokens_count: 6300 at end - 319 at start
    assert 2.0 < on["ttft_p50_s"] < 2.6 and 7.0 < on["ttft_p90_s"] < 8.5  # driver: 2.39 s, 7.81 s
    assert on["l1_tokens"] is None and on["l2_tokens"] is None


def collapse_source(metrics):
    # How kvtrace <= a3bd5cd recorded vLLM: the source label was dropped and summed.
    out = {}
    for k, v in metrics.items():
        if "source=" in k:
            k = f"vllm:prompt_tokens_by_source_total{{{LBL}}}"
        out[k] = out.get(k, 0.0) + v
    return out


def test_old_traces_without_source_label_fall_back_to_cached_minus_prefix_hits():
    samples = [(ts, collapse_source(m)) for ts, m in fixture_samples("on1")]
    result = measure_tiers(m_records(samples))
    total = result["fleet"]["total"]
    assert total["l0_tokens"] == 11296704  # prefix_cache_hits_total
    assert total["external_tokens"] == 19839120 - 11296704  # within 0.03% of by_source
    assert result["nodes"]["A1"]["counters"]["external"].startswith("vllm:prompt_tokens_cached_total")
    assert any("no prompt_tokens_by_source" in n for n in result["notes"])


def sample(prompt, l0, ext, **extra):
    m = {f"vllm:prompt_tokens_total{{{LBL}}}": float(prompt),
         f'vllm:prompt_tokens_by_source_total{{{LBL},source="local_cache_hit"}}': float(l0),
         f'vllm:prompt_tokens_by_source_total{{{LBL},source="external_kv_transfer"}}': float(ext),
         f'vllm:prompt_tokens_by_source_total{{{LBL},source="local_compute"}}': float(prompt - l0 - ext),
         f"vllm:num_requests_running{{{LBL}}}": 3.0}
    m.update(extra)
    return m


def test_intervals_counter_reset_and_fleet_of_two_nodes():
    a = [(0.0, sample(100, 10, 0)), (30.0, sample(400, 60, 40)), (70.0, sample(1000, 100, 100)),
         # The engine restarted: counters start again from zero.
         (100.0, sample(50, 5, 5)), (130.0, sample(150, 15, 25))]
    b = [(0.0, sample(0, 0, 0)), (65.0, sample(200, 20, 20))]
    result = measure_tiers(m_records(a, "A") + m_records(b, "B"), interval=60)
    node_a = result["nodes"]["A"]
    assert [r["prompt_tokens"] for r in node_a["intervals"]] == [300, 650, 100]
    assert node_a["total"]["prompt_tokens"] == 1050 and node_a["total"]["external_tokens"] == 125
    fleet = result["fleet"]
    assert fleet["nodes"] == ["A", "B"]
    assert [r["prompt_tokens"] for r in fleet["intervals"]] == [300, 850, 100]
    assert fleet["total"]["external_tokens"] == 145
    # Interval 1: A adds 60 + 5 (after the restart) external tokens, B adds 20.
    assert fleet["intervals"][1]["external_pct"] == pytest.approx(100 * 85 / 850)
    assert node_a["intervals"][0]["t_start"] == 0.0 and node_a["intervals"][0]["t_end"] == 60.0


def test_window_uses_the_sample_before_start_as_baseline():
    a = [(0.0, sample(100, 0, 0)), (50.0, sample(300, 0, 0)), (110.0, sample(600, 0, 0))]
    total = measure_tiers(m_records(a), start=40.0, end=100.0)["fleet"]["total"]
    assert total["prompt_tokens"] == 200


def test_no_metrics_records_gives_a_note():
    result = measure_tiers([{"t": "S", "ts": 1.0}])
    assert result["fleet"] is None and "no metrics" in result["notes"][0]
    assert "no metrics" in render_markdown(result)


# LMCache 0.5.5 counter names and labels (lmcache/observability.py); /metrics of that run
# had none, so these lines are synthetic: two TP workers that each store 512 bytes per token.
def lmcache_text(hit_tokens, read_bytes, stored_tokens=0, write_bytes_per_token=512):
    lines = []
    for worker in (0, 1):
        labels = f'model_name="qwen3-235b",worker_id="{worker}",role="worker",served_model_name=""'
        lines += [f"lmcache:num_hit_tokens_total{{{labels}}} {hit_tokens}",
                  f"lmcache:num_remote_read_bytes_total{{{labels}}} {read_bytes / 2}",
                  f"lmcache:num_remote_read_bytes_created{{{labels}}} 1.79e9",
                  f"lmcache:num_stored_tokens_total{{{labels}}} {stored_tokens}",
                  f"lmcache:num_remote_write_bytes_total{{{labels}}} "
                  f"{stored_tokens * write_bytes_per_token}",
                  f"lmcache:remote_time_to_get_bucket{{{labels},le=\"1.0\"}} 3"]
    # The scheduler's series share worker_id 0 with worker 0 and must not count.
    labels = 'model_name="qwen3-235b",worker_id="0",role="scheduler",served_model_name=""'
    lines += [f"lmcache:num_hit_tokens_total{{{labels}}} {hit_tokens * 7}",
              f"lmcache:num_remote_read_bytes_total{{{labels}}} {read_bytes * 7}"]
    return "\n".join(lines) + "\n"


def lmcache_samples(hit_tokens=3000, read_bytes=1024 * 1000, stored_tokens=0,
                    write_bytes_per_token=512):
    s0 = sample(0, 0, 0)
    s0.update(filter_metrics(lmcache_text(0, 0)))
    s1 = sample(10000, 1000, 3000)
    s1.update(filter_metrics(lmcache_text(hit_tokens, read_bytes, stored_tokens,
                                          write_bytes_per_token)))
    return [(0.0, s0), (10.0, s1)]


def test_lmcache_remote_bytes_split_external_into_l1_and_l2():
    samples = lmcache_samples()
    assert ('lmcache:num_hit_tokens_total{model_name="qwen3-235b",worker_id="1",role="worker"}'
            in samples[1][1])
    assert not any("remote_time_to_get" in k for k in samples[1][1])
    result = measure_tiers(m_records(samples), worker_kv_bytes_per_token=512)
    total = result["fleet"]["total"]
    # 512,000 bytes per worker / 512 bytes per token per worker.
    assert total["l2_tokens"] == 1000 and total["l1_tokens"] == 2000
    assert total["lmcache_remote_read_bytes"] == 1024 * 1000
    assert total["lmcache_retrieved_tokens"] == 3000  # mean of the two workers
    assert total["l2_pct"] == pytest.approx(10.0)
    assert not any("exceed" in n for n in result["notes"])
    without_kv = measure_tiers(m_records(samples))
    assert without_kv["fleet"]["total"]["l2_tokens"] is None
    assert any("--worker-kv-bytes-per-token" in n for n in without_kv["notes"])


def test_l2_above_lmcache_retrieved_tokens_gives_a_note():
    # A per-worker size that is too small (e.g. full-model bytes / TP with copied KV heads).
    result = measure_tiers(m_records(lmcache_samples(hit_tokens=500)), worker_kv_bytes_per_token=512)
    assert any("exceed LMCache retrieved tokens" in n for n in result["notes"])


def test_per_worker_kv_size_is_stated_and_checked_against_write_bytes():
    # Each worker wrote 512 bytes per stored token; a bf16 guess of 1024 on fp8 KV is wrong.
    samples = lmcache_samples(stored_tokens=4000, write_bytes_per_token=512)
    good = measure_tiers(m_records(samples), worker_kv_bytes_per_token=512,
                         worker_kv_source="--worker-kv-bytes-per-token")
    total = good["fleet"]["total"]
    assert total["lmcache_remote_write_bytes"] == 2 * 4000 * 512  # workers only
    assert good["nodes"]["A1"]["total"]["lmcache_implied_worker_kv_bytes_per_token"] == 512
    assert good["notes"][0].startswith("L2 tokens use 512 KV bytes per token per TP worker "
                                       "(from --worker-kv-bytes-per-token)")
    assert not any("WARNING" in n for n in good["notes"])
    bad = measure_tiers(m_records(samples), worker_kv_bytes_per_token=1024)
    (warning,) = [n for n in bad["notes"] if n.startswith("WARNING")]
    assert "imply 512 KV bytes" in warning and "0.50x" in warning
    assert bad["fleet"]["total"]["l2_tokens"] == 500  # half of the true 1000


def test_lmcache_series_without_role_count_as_workers():
    # Traces recorded before the role label was kept.
    samples = [(ts, {k.replace(',role="worker"', ""): v for k, v in m.items()
                     if 'role="scheduler"' not in k}) for ts, m in lmcache_samples()]
    total = measure_tiers(m_records(samples), worker_kv_bytes_per_token=512)["fleet"]["total"]
    assert total["l2_tokens"] == 1000 and total["lmcache_remote_read_bytes"] == 1024 * 1000


def test_external_prefix_cache_hits_and_sglang_fallbacks():
    def vllm_old(prompt, l0, ext):
        return {f"vllm:prompt_tokens_total{{{LBL}}}": prompt, f"vllm:prefix_cache_hits_total{{{LBL}}}": l0,
                f"vllm:external_prefix_cache_hits_total{{{LBL}}}": ext}

    result = measure_tiers(m_records([(0.0, vllm_old(0, 0, 0)), (5.0, vllm_old(100, 20, 30))]))
    total = result["fleet"]["total"]
    assert (total["l0_tokens"], total["external_tokens"], total["computed_tokens"]) == (20, 30, 50)
    assert result["nodes"]["A1"]["counters"]["external"] == "vllm:external_prefix_cache_hits_total"

    def sglang(prompt, cached):
        return {'sglang:prompt_tokens_total{model_name="q"}': prompt,
                'sglang:cached_tokens_total{model_name="q"}': cached}

    total = measure_tiers(m_records([(0.0, sglang(0, 0)), (5.0, sglang(100, 40))]))["fleet"]["total"]
    assert (total["l0_tokens"], total["external_tokens"], total["computed_tokens"]) == (40, None, 60)


def test_l2_split_is_labelled_not_validated_and_gives_the_chunk_overlap_bound():
    samples = lmcache_samples()
    req = f"vllm:request_prompt_tokens_count{{{LBL}}}"
    samples[0][1][req], samples[1][1][req] = 0.0, 2.0
    result = measure_tiers(m_records(samples), worker_kv_bytes_per_token=512)
    total = result["fleet"]["total"]
    assert result["l1_l2_validated"] is False and result["inputs"]["chunk_tokens"] == 256
    # 2 requests x 256 tokens, below L2 = 1,000 tokens.
    assert total["l2_chunk_overlap_bound_tokens"] == 512
    assert result["nodes"]["A1"]["total"]["l2_chunk_overlap_bound_tokens"] == 512
    assert any(n.startswith("L1/L2 split not validated against real LMCache output")
               for n in result["notes"])
    (note,) = [n for n in result["notes"] if "boundary chunk" in n]
    assert "(256 tokens)" in note and "2 finished requests: 512 tokens (5.12 points" in note
    md = render_markdown(result)
    assert "**L1/L2 split not validated against real LMCache output" in md
    # The bound never exceeds L2, and follows --chunk-tokens.
    small = measure_tiers(m_records(samples), worker_kv_bytes_per_token=512, chunk_tokens=1024)
    assert small["fleet"]["total"]["l2_chunk_overlap_bound_tokens"] == 1000
    # Without L2 there is no bound and no validation note.
    plain = measure_tiers(m_records(samples))
    assert plain["fleet"]["total"]["l2_chunk_overlap_bound_tokens"] is None
    assert not any("not validated" in n for n in plain["notes"])
    with pytest.raises(ValueError, match="--chunk-tokens"):
        measure_tiers([], chunk_tokens=0)


def test_cli_tiers_chunk_tokens_and_label(tmp_path, capsys):
    write_segment(tmp_path / "A1-1-00000.kvt.jsonl.gz", m_records(lmcache_samples()))
    out = tmp_path / "t.json"
    assert main(["tiers", str(tmp_path), "-o", str(out), "--worker-kv-bytes-per-token", "512",
                 "--chunk-tokens", "128"]) == 0
    assert json.loads(out.read_text())["inputs"]["chunk_tokens"] == 128
    assert "(L1/L2 not validated against real LMCache output)" in capsys.readouterr().out


def test_node_increments_skip_gauges():
    incs = node_increments([(0.0, {"a_total": 1.0, "g": 5.0}), (1.0, {"a_total": 4.0, "g": 1.0})])
    assert incs == [(0.0, {}), (1.0, {"a_total": 3.0})]


def test_missed_extra_scrape_does_not_inflate_l2():
    # Worker 0 is scraped through two URLs (A and B) that export the same series. Each URL
    # goes 1000 -> 2100 bytes, so the true L2 is 2,200 tokens at 1 byte per token; B misses
    # the middle scrape. Read as a reset of the summed series, L2 was 4,200.
    from kvtrace.metrics import merge_samples, tag_source

    key = 'lmcache:num_remote_read_bytes_total{model_name="m",worker_id="0",role="worker"}'

    def rec(ts, prompt, ext, a, b):
        m = sample(prompt, 0, ext)
        answered = {}
        for url, value in (("A", a), ("B", b)):
            if value is not None:
                merge_samples(m, tag_source({key: float(value)}, url))
                answered[url] = ts
        return {"t": "M", "ts": ts, "node": "A1", "metrics": m, "extra_ts": answered}

    tagged = [rec(0.0, 0, 0, 1000, 1000), rec(10.0, 5000, 2000, 1500, None),
              rec(20.0, 10000, 3000, 2100, 2100)]
    total = measure_tiers(tagged, worker_kv_bytes_per_token=1)["fleet"]["total"]
    assert total["l2_tokens"] == 2200 and total["l1_tokens"] == 800
    # The same scrapes recorded before kvtrace_url: one summed key, guarded by extra_ts.
    untagged = []
    for r in tagged:
        m = {k: v for k, v in r["metrics"].items() if "kvtrace_url" not in k}
        m[key] = sum(v for k, v in r["metrics"].items() if "kvtrace_url" in k)
        untagged.append(dict(r, metrics=m))
    assert measure_tiers(untagged, worker_kv_bytes_per_token=1)["fleet"]["total"]["l2_tokens"] == 2200


def test_series_that_appears_late_counts_from_zero_or_gives_a_note():
    ext = f'vllm:prompt_tokens_by_source_total{{{LBL},source="external_kv_transfer"}}'

    def s(prompt, external):
        m = sample(prompt, 0, external or 0)
        if external is None:
            del m[ext]
        return m

    # vLLM creates the external series at its first increment: 700, then 900 tokens.
    a = [(0.0, s(1000, None)), (10.0, s(2000, 700)), (20.0, s(3000, 900))]
    result = measure_tiers(m_records(a))
    assert result["fleet"]["total"]["external_tokens"] == 900
    assert not any("started late" in n for n in result["notes"])
    # An extra URL that was down at the start and has no *_created time: first value is the
    # baseline, and a note says the window of that series is shorter.
    key = 'lmcache:num_remote_read_bytes_total{worker_id="0",kvtrace_url="U"}'
    recs = [{"t": "M", "ts": 0.0, "node": "A1", "metrics": s(1000, 0)},
            {"t": "M", "ts": 10.0, "node": "A1", "metrics": {**s(2000, 0), key: 500.0},
             "extra_ts": {"U": 10.0}},
            {"t": "M", "ts": 20.0, "node": "A1", "metrics": {**s(3000, 0), key: 800.0},
             "extra_ts": {"U": 20.0}}]
    result = measure_tiers(recs, worker_kv_bytes_per_token=1)
    assert result["fleet"]["total"]["l2_tokens"] == 300
    (note,) = [n for n in result["notes"] if "started late" in n]
    assert "1 counter series" in note and "window is shorter" in note and key in note


def test_histogram_quantile_interpolates_inside_the_bucket():
    buckets = {0.5: 0.0, 1.0: 50.0, 2.0: 100.0, float("inf"): 100.0}
    assert _quantile_from_buckets(buckets, 0.5) == 1.0
    assert _quantile_from_buckets(buckets, 0.75) == pytest.approx(1.5)
    assert _quantile_from_buckets({float("inf"): 0.0}, 0.5) is None
    assert _quantile_from_buckets({1.0: 0.0, float("inf"): 4.0}, 0.5) == 1.0


def test_compare_with_sim_and_renderers():
    a = [(0.0, sample(0, 0, 0)), (59.0, sample(1000, 100, 200)), (119.0, sample(2000, 200, 400))]
    result = measure_tiers(m_records(a), interval=60, start=0.0)
    sim = {"trace": {"window": {"start": 5000.0, "end": 5120.0}},
           "capacities": [{"label": "big", "saved_tokens": 150, "host_tier": {"hit_tokens": 100},
                           "hit_tokens_per_minute": [0, 100, 50]}]}
    cmp_ = compare_with_sim(result, sim)
    result["compare"] = cmp_
    assert cmp_["simulated"]["external_tokens"] == 250 and cmp_["measured"]["external_tokens"] == 400
    assert cmp_["simulated"]["l2_pct"] == pytest.approx(7.5)
    # Minute i of the simulation is placed i minutes after the measured start.
    assert [r["sim_l2_tokens"] for r in result["fleet"]["intervals"]] == [0, 100]
    with pytest.raises(ValueError, match="nope"):
        compare_with_sim(result, sim, "nope")
    md = render_markdown(result)
    assert "Simulated vs measured (capacity big)" in md and "| fleet |" in md
    rows = list(csv.DictReader(io.StringIO(render_csv(result))))
    assert [r["node"] for r in rows] == ["A1", "A1", "fleet", "fleet"]
    assert float(rows[-1]["sim_l2_tokens"]) == 100


def test_compare_with_sim_uses_the_measured_t0():
    # No --start and no increment in interval 0: the first row starts at t0 + 60, and must
    # get simulated minute 1, not minute 0.
    a = [(0.0, sample(0, 0, 0)), (70.0, sample(1000, 100, 200)), (130.0, sample(2000, 200, 400))]
    result = measure_tiers(m_records(a), interval=60)
    assert result["fleet"]["t0"] == 0.0
    assert [r["t_start"] for r in result["fleet"]["intervals"]] == [60.0, 120.0]
    sim = {"capacities": [{"label": "c", "saved_tokens": 1, "hit_tokens_per_minute": [5, 7, 9]}]}
    compare_with_sim(result, sim)
    assert [r["sim_l2_tokens"] for r in result["fleet"]["intervals"]] == [7, 9]


def write_segment(path, records):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "H", "node": "A1", "model": "m", "engine": "vllm"}) + "\n")
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def test_cli_tiers_end_to_end(tmp_path, capsys):
    write_segment(tmp_path / "A1-1-00000.kvt.jsonl.gz", m_records(fixture_samples("on1")))
    out = tmp_path / "tiers.json"
    md, csv_path = tmp_path / "t.md", tmp_path / "t.csv"
    joined = tmp_path / "joined.jsonl"
    rc = main(["tiers", str(tmp_path), "-o", str(out), "--markdown", str(md), "--csv", str(csv_path),
               "--requests", os.path.join(FIX, "vllm030_lmcache_on1_requests.jsonl"),
               "--lmcache-log", os.path.join(FIX, "vllm030_lmcache_on1_vllm.log"),
               "--min-prompt-tokens", "0", "--requests-out", str(joined)])
    assert rc == 0
    result = json.loads(out.read_text())
    assert result["fleet"]["total"]["external_tokens"] == 8544976
    assert result["requests"]["join_stats"]["joined"] == 5
    assert "external (L1+L2) 11.97%" in capsys.readouterr().out
    assert "Per-request TTFT by hit bucket" in md.read_text()
    assert len(joined.read_text().splitlines()) == 5
    assert main(["tiers", str(tmp_path / "missing")]) == 2


def test_cli_tiers_window_and_per_worker_kv_from_preset_and_tp(tmp_path):
    write_segment(tmp_path / "A1-1-00000.kvt.jsonl.gz", m_records(lmcache_samples()))
    out = tmp_path / "tiers.json"
    # llama-3.1-8b: 32 layers x 8 KV heads x 128 dims x 2 (K, V) x 2 bytes = 131,072 B/token;
    # TP 16 copies each head to two ranks, so one worker holds one head: 16,384 B/token.
    assert main(["tiers", str(tmp_path), "-o", str(out), "--preset", "llama-3.1-8b", "--tp", "16",
                 "--start", "0", "--end", "20"]) == 0
    result = json.loads(out.read_text())
    assert result["inputs"]["worker_kv_bytes_per_token"] == 16384
    assert result["fleet"]["total"]["l2_tokens"] == pytest.approx(512000 / 16384)
    assert main(["tiers", str(tmp_path), "-o", str(out), "--start", "11"]) == 0
    assert json.loads(out.read_text())["fleet"] is None


def test_mla_l2_uses_the_reading_worker_not_the_mean():
    # LMCache with MLA reads on rank 0 only; the other 7 workers export 0 read bytes.
    def mla(read0):
        m = sample(10000 if read0 else 0, 0, 3000 if read0 else 0)
        for w in range(8):
            labels = f'model_name="m",worker_id="{w}"'
            m[f"lmcache:num_remote_read_bytes_total{{{labels}}}"] = float(read0 if w == 0 else 0)
            m[f"lmcache:num_hit_tokens_total{{{labels}}}"] = 3000.0 if read0 else 0.0
        return m

    total = measure_tiers(m_records([(0.0, mla(0)), (10.0, mla(1000 * 576))]),
                          worker_kv_bytes_per_token=576)["fleet"]["total"]
    assert total["l2_tokens"] == 1000 and total["l1_tokens"] == 2000


def test_cli_tiers_preset_without_tp_is_an_error(tmp_path, capsys):
    write_segment(tmp_path / "A1-1-00000.kvt.jsonl.gz", m_records(lmcache_samples()))
    assert main(["tiers", str(tmp_path), "-o", str(tmp_path / "t.json"), "--preset", "llama-3.1-8b"]) == 2
    assert "needs --tp" in capsys.readouterr().err


# Qwen3-235B-A22B config.json fields that set the KV size (94 layers, 4 KV heads, head_dim 128).
QWEN3_235B = {"num_hidden_layers": 94, "num_attention_heads": 64, "num_key_value_heads": 4,
              "head_dim": 128, "hidden_size": 4096, "torch_dtype": "bfloat16",
              "quantization_config": {"quant_method": "fp8"}}


def test_config_kv_dtype_bytes():
    assert config_kv_dtype_bytes(QWEN3_235B) == (2, "torch_dtype")
    scheme = dict(QWEN3_235B, quantization_config={"kv_cache_scheme": {"num_bits": 8}})
    assert config_kv_dtype_bytes(scheme) == (1, "quantization_config.kv_cache_scheme.num_bits")
    assert config_kv_dtype_bytes({"text_config": {"num_hidden_layers": 2, "dtype": "float32"}}) == (
        4, "dtype")
    assert config_kv_dtype_bytes({}) is None


def test_cli_tiers_kv_dtype_from_config_and_override(tmp_path, capsys):
    write_segment(tmp_path / "A1-1-00000.kvt.jsonl.gz", m_records(lmcache_samples()))
    cfg, out = tmp_path / "config.json", tmp_path / "t.json"
    cfg.write_text(json.dumps(QWEN3_235B))
    base = ["tiers", str(tmp_path), "-o", str(out), "--model-config", str(cfg), "--tp", "8"]
    # FP8 weights, bf16 KV: TP 8 > 4 KV heads, so a worker holds one head = 48,128 B/token.
    assert main(base) == 0
    inputs = json.loads(out.read_text())["inputs"]
    assert inputs["worker_kv_bytes_per_token"] == 48128
    assert "2 bytes per KV element (torch_dtype)" in inputs["worker_kv_source"]
    # vLLM --kv-cache-dtype fp8 (that run): 256 tokens x 24,064 B = 6.16 MB per chunk key.
    assert main(base + ["--kv-dtype-bytes", "1"]) == 0
    result = json.loads(out.read_text())
    assert result["inputs"]["worker_kv_bytes_per_token"] == 24064
    assert "L2 tokens use 24,064 KV bytes per token per TP worker" in capsys.readouterr().err
