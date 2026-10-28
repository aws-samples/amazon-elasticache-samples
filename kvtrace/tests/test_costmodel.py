import math

import pytest

from kvtrace import costmodel as cm
from kvtrace.models import PRESETS, ModelConfigError, kv_bytes_per_token, resolve_kv_bytes


def test_kv_bytes_gqa_llama_8b():
    assert kv_bytes_per_token(PRESETS["llama-3.1-8b"]) == 2 * 32 * 8 * 128 * 2 == 131072


def test_kv_bytes_presets():
    assert resolve_kv_bytes(preset="llama-3.1-70b") == 2 * 80 * 8 * 128 * 2
    assert resolve_kv_bytes(preset="qwen3-32b") == 2 * 64 * 8 * 128 * 2
    assert resolve_kv_bytes(preset="deepseek-v3") == (512 + 64) * 61 * 2


def test_kv_bytes_mla_config_and_dtype():
    cfg = {"num_hidden_layers": 27, "kv_lora_rank": 512, "qk_rope_head_dim": 64,
           "num_attention_heads": 16, "hidden_size": 2048}
    assert kv_bytes_per_token(cfg, dtype_bytes=1) == 576 * 27


def test_kv_bytes_defaults_and_text_config_nesting():
    mha = {"num_hidden_layers": 2, "num_attention_heads": 4, "hidden_size": 256}
    assert kv_bytes_per_token(mha) == 2 * 2 * 4 * 64 * 2
    nested = {"model_type": "llava", "text_config": dict(mha, num_key_value_heads=2, head_dim=32)}
    assert kv_bytes_per_token(nested) == 2 * 2 * 2 * 32 * 2


def test_kv_bytes_errors():
    with pytest.raises(ModelConfigError):
        kv_bytes_per_token({"hidden_size": 1})
    with pytest.raises(ModelConfigError):
        resolve_kv_bytes(preset="nope")
    assert resolve_kv_bytes() is None


def test_fetch_and_beneficial():
    # 256 tokens * 131072 B = 32 MiB = 268,435,456 bits; at 100 Gbit/s = 2.684 ms, + 0.5 ms RTT.
    fetch = cm.fetch_seconds(256, 131072, 100.0, 0.5)
    assert fetch == pytest.approx(0.0005 + 256 * 131072 * 8 / 100e9)
    assert cm.recompute_seconds(256, 10000) == pytest.approx(0.0256)
    cap = {"label": "x", "saved_tokens": 256 + 16, "capacity_bytes": 10**9,
           "hit_tokens_histogram": [[16, 1], [256, 1]], "runs_histogram": [[272, 2, 1]]}
    inputs = cm.CostInputs(prefill_tokens_per_sec=10000, replica_cost_per_hour=3600.0)
    out = cm.capacity_savings(cap, 256, 131072, 3600.0, 1000, inputs)
    # 16 tokens recompute in 1.6 ms < fetch 3.18 ms: not beneficial.
    assert out["beneficial_hits"] == 1 and out["non_beneficial_hits"] == 1
    assert out["gpu_replica_seconds_saved"] == pytest.approx(0.0256)
    assert out["ttft_seconds_saved"] == pytest.approx(0.0256 - fetch)
    assert out["saved_usd_window"] == pytest.approx(0.0256)
    assert out["saved_usd_per_month"] == pytest.approx(0.0256 * 720)
    assert out["break_even_usd_per_gb_hour"] == pytest.approx(0.0256)
    # One round trip per run, then both chunks' transfer time.
    assert out["mean_ttft_saved_per_run"] == pytest.approx(0.0272 - 0.0005 - 2 * (fetch - 0.0005))


def test_net_savings_with_l2_price():
    cap = {"label": "1GB", "saved_tokens": 10000, "capacity_bytes": 10**9,
           "hit_tokens_histogram": [[256, 100]], "runs_histogram": []}
    inputs = cm.CostInputs(prefill_tokens_per_sec=1000, replica_cost_per_hour=36.0,
                           l2_cost_per_gb_hour=0.5)
    out = cm.capacity_savings(cap, 256, 1000, 7200.0, 25600, inputs)
    saved = 100 * 0.256 / 3600 * 36.0
    assert out["l2_cost_usd_window"] == pytest.approx(1.0)
    assert out["net_usd_window"] == pytest.approx(saved - 1.0)


def test_savings_unavailable_without_tps():
    cap = {"label": "x", "saved_tokens": 5, "capacity_bytes": None, "hit_tokens_histogram": []}
    assert cm.capacity_savings(cap, 256, 100, 10.0, 10, cm.CostInputs())["available"] is False


def test_break_even_bandwidth():
    bw = cm.break_even_bandwidth_gbps(256, 131072, 256, 10000, 0.5)
    assert cm.fetch_seconds(256, 131072, bw, 0.5) == pytest.approx(cm.recompute_seconds(256, 10000))
    assert cm.break_even_bandwidth_gbps(256, 131072, 1, 10000, 0.5) == math.inf


def test_throughput_formula():
    assert cm.throughput_gain_upper_bound(0.5, 0.5) == pytest.approx(1 / 0.75 - 1)
    assert cm.throughput_gain_upper_bound(0.0, 1.0) == 0.0
    assert cm.throughput_gain_upper_bound(1.0, 1.0) == math.inf


def test_prefill_estimates_from_metrics():
    sim = {"metrics": {"fleet": {"counters": {
        'vllm:prompt_tokens_total{model_name="m"}': 50000.0,
        "vllm:request_prefill_time_seconds_sum": 10.0,
        "vllm:request_decode_time_seconds_sum": 30.0,
    }}}}
    assert cm.estimate_prefill_tps(sim) == pytest.approx(5000.0)
    assert cm.estimate_prefill_fraction(sim) == pytest.approx(0.25)
    assert cm.estimate_prefill_tps({"metrics": {"fleet": {"counters": {}}}}) is None


def test_prefill_tps_excludes_prefix_cache_hits_real_vllm_031_counters():
    # Counter deltas from a 9-minute vLLM 0.31.0 run: 87% of prompt tokens were GPU
    # prefix-cache hits, which cost no prefill compute.
    labels = '{engine="0",model_name="Qwen/Qwen2.5-1.5B-Instruct"}'
    sim = {"metrics": {"fleet": {"counters": {
        f"vllm:prompt_tokens_total{labels}": 7003117.0,
        f"vllm:prompt_tokens_by_source_total{labels}": 7003117.0,
        f"vllm:prompt_tokens_cached_total{labels}": 6093696.0,
        f"vllm:prefix_cache_hits_total{labels}": 6093696.0,
        f"vllm:prefix_cache_queries_total{labels}": 7003117.0,
        f"vllm:request_prefill_time_seconds_sum{labels}": 188.10327826497632,
    }}}}
    assert cm.estimate_prefill_tps(sim) == pytest.approx((7003117 - 6093696) / 188.10327826497632)


def test_prefill_tps_uses_sglang_cached_tokens():
    sim = {"metrics": {"fleet": {"counters": {
        "sglang:prompt_tokens_total": 1000.0, "sglang:cached_tokens_total": 600.0,
        "sglang:request_prefill_time_seconds_sum": 2.0}}}}
    assert cm.estimate_prefill_tps(sim) == pytest.approx(200.0)


def test_cached_prompt_aliases_are_not_double_subtracted():
    # vLLM exports the deprecated gpu_ alias next to prefix_cache_hits_total.
    sim = {"metrics": {"fleet": {"counters": {
        "vllm:prompt_tokens_total": 1000.0, "vllm:prefix_cache_hits_total": 400.0,
        "vllm:gpu_prefix_cache_hits_total": 400.0,
        "vllm:request_prefill_time_seconds_sum": 1.0}}}}
    assert cm.metric_totals(sim)["cached_prompt_tokens"] == 400.0
    assert cm.estimate_prefill_tps(sim) == pytest.approx(600.0)


def test_mixed_fleet_subtracts_each_engines_cached_tokens():
    vllm = {"vllm:prompt_tokens_total": 1000.0, "vllm:prompt_tokens_cached_total": 400.0,
            "vllm:request_prefill_time_seconds_sum": 1.0}
    sglang = {"sglang:prompt_tokens_total": 500.0, "sglang:cached_tokens_total": 100.0,
              "sglang:request_prefill_time_seconds_sum": 1.0}
    fleet = {**vllm, **sglang}
    sim = {"metrics": {"per_node": {"A": {"counters": vllm}, "B": {"counters": sglang}},
                       "fleet": {"counters": fleet}}}
    totals = cm.metric_totals(sim)
    assert totals["prompt_tokens"] == 1500.0 and totals["cached_prompt_tokens"] == 500.0
    assert cm.estimate_prefill_tps(sim) == pytest.approx((1500 - 500) / 2.0)


def test_recommended_capacity_is_the_knee():
    caps = [{"label": "a", "capacity_bytes": 1, "saved_tokens": 10},
            {"label": "b", "capacity_bytes": 2, "saved_tokens": 95},
            {"label": "c", "capacity_bytes": 3, "saved_tokens": 99},
            {"label": "inf", "capacity_bytes": None, "saved_tokens": 100}]
    assert cm.recommended_capacity(caps) == "b"
    assert cm.recommended_capacity([{"label": "a", "capacity_bytes": 1, "saved_tokens": 0}]) is None


def test_per_rank_kv_bytes_follow_vllm_head_split():
    from kvtrace.models import kv_bytes_per_token_per_rank

    llama = PRESETS["llama-3.1-8b"]  # 8 KV heads
    full = kv_bytes_per_token(llama)
    assert kv_bytes_per_token_per_rank(llama, 1) == full
    assert kv_bytes_per_token_per_rank(llama, 4) == full // 4
    assert kv_bytes_per_token_per_rank(llama, 16) == full // 8  # heads copied, one per rank
    mla = PRESETS["deepseek-v3"]
    assert kv_bytes_per_token_per_rank(mla, 8) == kv_bytes_per_token(mla)


def test_engine_prompt_counters_ignore_lmcache_names():
    sim = {"metrics": {"fleet": {"counters": {"vllm:prompt_tokens_total": 1000.0,
                                              'lmcache:num_prompt_tokens_total{worker_id="0"}': 999.0}}}}
    assert cm.metric_totals(sim)["prompt_tokens"] == 1000.0


def _gauge_sim(running=None, lmcache_workers=0):
    counters = {"vllm:prompt_tokens_total": 10000.0,
                "vllm:request_prefill_time_seconds_sum": 10.0}
    for w in range(lmcache_workers):
        counters[f'lmcache:num_hit_tokens_total{{worker_id="{w}",role="worker"}}'] = 1.0
    if lmcache_workers:
        counters['lmcache:num_hit_tokens_total{worker_id="0",role="scheduler"}'] = 1.0
    gauges = {}
    if running is not None:
        gauges = {f'vllm:num_requests_running{{engine="{i}"}}': v for i, v in enumerate(running)}
    return {"metrics": {"per_node": {"A": {"counters": counters, "gauges": gauges}}}}


def test_replica_rate_is_request_rate_times_mean_running():
    inputs = cm.CostInputs()
    cm.resolve_prefill_rates(inputs, _gauge_sim(running=[3.0, 5.0]), None)
    assert inputs.request_prefill_tokens_per_sec == pytest.approx(1000.0)
    assert inputs.prefill_tokens_per_sec == pytest.approx(4000.0)
    assert "4.00 mean running requests" in inputs.prefill_tps_source


def test_mean_running_reads_time_weighted_gauges_first():
    sim = _gauge_sim(running=[3.0, 5.0])
    node = sim["metrics"]["per_node"]["A"]
    node["gauges_time_weighted"] = {'vllm:num_requests_running{engine="0"}': 1.0}
    # engine 0 uses its time-weighted mean; engine 1 (no such value) keeps its sample mean.
    assert cm.mean_running_requests(sim) == pytest.approx((1.0 + 5.0) / 2)
    del node["gauges_time_weighted"]
    assert cm.mean_running_requests(sim) == pytest.approx(4.0)

def test_replica_rate_missing_without_running_gauge():
    inputs = cm.CostInputs(replica_cost_per_hour=10.0)
    cm.resolve_prefill_rates(inputs, _gauge_sim(), None)
    assert inputs.request_prefill_tokens_per_sec == pytest.approx(1000.0)
    assert inputs.prefill_tokens_per_sec is None
    assert any("vllm:num_requests_running" in m for m in inputs.missing_notes)
    cap = {"label": "x", "saved_tokens": 2560, "capacity_bytes": 10**9,
           "hit_tokens_histogram": [[256, 10]], "runs_histogram": []}
    out = cm.capacity_savings(cap, 256, 1000, 3600.0, 10000, inputs)
    assert out["available"] and not out["gpu_available"]
    assert out["gpu_replica_seconds_saved"] is None and "saved_usd_window" not in out
    assert out["ttft_seconds_saved"] > 0


def test_given_replica_rate_keeps_request_rate_for_ttft():
    inputs = cm.CostInputs()
    cm.resolve_prefill_rates(inputs, _gauge_sim(), 8000.0)
    assert inputs.prefill_tokens_per_sec == 8000.0 and inputs.prefill_tps_source == "input"
    assert inputs.request_prefill_tokens_per_sec == pytest.approx(1000.0)
    cap = {"label": "x", "saved_tokens": 2560, "capacity_bytes": None,
           "hit_tokens_histogram": [[256, 10]], "runs_histogram": []}
    out = cm.capacity_savings(cap, 256, 1000, 3600.0, 10000, inputs)
    fetch = cm.fetch_seconds(256, 1000, 100.0, 0.5)
    assert out["gpu_replica_seconds_saved"] == pytest.approx(2560 / 8000.0)
    assert out["ttft_seconds_saved"] == pytest.approx(10 * (0.256 - fetch))
    only_input = cm.CostInputs()
    cm.resolve_prefill_rates(only_input, {"metrics": {}}, 8000.0)
    assert cm.request_tps(only_input) == 8000.0


def test_headroom_uses_beneficial_tokens_only():
    # 16-token hits recompute faster than the fetch: none is beneficial.
    cap = {"label": "x", "saved_tokens": 1600, "capacity_bytes": 10**9,
           "hit_tokens_histogram": [[16, 100]], "runs_histogram": []}
    inputs = cm.CostInputs(prefill_tokens_per_sec=10000, prefill_time_fraction=0.5)
    sav = cm.capacity_savings(cap, 256, 131072, 3600.0, 2000, inputs)
    assert sav["beneficial_hits"] == 0 and sav["prefill_saved_fraction_tokens"] == 0.0
    sim = {"metrics": {"fleet": {"counters": {"vllm:request_prefill_time_seconds_sum": 1.0}}}}
    hr = cm.headroom(sav, sim, inputs, 2000)
    assert hr["available"] and hr["throughput_gain_upper_bound"] == 0.0
    good = dict(cap, hit_tokens_histogram=[[256, 2]], saved_tokens=512)
    sav = cm.capacity_savings(good, 256, 131072, 3600.0, 2048, inputs)
    hr = cm.headroom(sav, sim, inputs, 2048)
    assert hr["saved_fraction"] == pytest.approx(0.25)
    assert hr["saved_fraction_source"] == "beneficial saved tokens / computed tokens"
    assert cm.headroom({"available": False, "saved_tokens": 5}, sim, inputs, 10) == {"available": False}


def test_recommended_capacity_ignores_non_beneficial_hits():
    caps = [{"label": "a", "capacity_bytes": 1, "saved_tokens": 100},
            {"label": "b", "capacity_bytes": 2, "saved_tokens": 100},
            {"label": "inf", "capacity_bytes": None, "saved_tokens": 100}]
    assert cm.recommended_capacity(caps, saved={"a": 0, "b": 0, "inf": 0}) is None
    assert cm.recommended_capacity(caps, saved={"a": 10, "b": 95, "inf": 100}) == "b"


def test_run_histogram_keeps_slower_runs_signed():
    # 16 tokens recompute in 1.6 ms; one chunk fetch takes about 3.18 ms.
    cap = {"label": "x", "saved_tokens": 16, "capacity_bytes": None, "hit_tokens_histogram": [],
           "runs_histogram": [[16, 1, 3], [2560, 1, 1]]}
    inputs = cm.CostInputs(prefill_tokens_per_sec=10000)
    out = cm.capacity_savings(cap, 256, 131072, 3600.0, 10000, inputs)
    run_fetch = cm.run_fetch_seconds(1, 256, 131072, 100.0, 0.5)
    hist = out["ttft_saved_per_run_histogram"]
    assert list(hist)[0] == cm.SLOWER_BUCKET and hist[cm.SLOWER_BUCKET] == 3
    assert out["runs_slower_with_l2"] == 3
    expected = (3 * (0.0016 - run_fetch) + (0.256 - run_fetch)) / 4
    assert out["mean_ttft_saved_per_run"] == pytest.approx(expected)


def test_run_fetch_charges_one_round_trip_per_run():
    one = cm.transfer_seconds(256, 1000, 100.0)
    assert cm.run_fetch_seconds(4, 256, 1000, 100.0, 0.5) == pytest.approx(0.0005 + 4 * one)
    assert cm.fetch_seconds(256, 1000, 100.0, 0.5) == pytest.approx(0.0005 + one)


def test_replica_cost_has_no_default_and_zero_is_valid():
    inputs = cm.CostInputs()
    cm.resolve_replica_cost(inputs, {}, None, None, None)
    assert inputs.replica_cost_per_hour is None
    inputs = cm.CostInputs(prefill_tokens_per_sec=1000)
    cm.resolve_replica_cost(inputs, {}, 6.88, 8, 0.0)
    assert inputs.replica_cost_per_hour == 0.0
    cap = {"label": "x", "saved_tokens": 256, "capacity_bytes": 10**9,
           "hit_tokens_histogram": [[256, 1]], "runs_histogram": []}
    out = cm.capacity_savings(cap, 256, 1000, 3600.0, 1000, inputs)
    assert out["saved_usd_window"] == 0.0


def test_gpus_per_replica_from_trace_tp_and_mismatch_warning():
    sim = _gauge_sim(lmcache_workers=4)
    assert cm.trace_gpus_per_replica(sim) == 4
    assert cm.trace_gpus_per_replica(_gauge_sim()) is None
    inputs = cm.CostInputs()
    cm.resolve_replica_cost(inputs, sim, 6.88, None, None)
    assert inputs.replica_cost_per_hour == pytest.approx(6.88 * 4)
    assert "TP size from the trace" in inputs.replica_cost_source and not inputs.warnings
    inputs = cm.CostInputs()
    cm.resolve_replica_cost(inputs, sim, 6.88, 8, None)
    assert inputs.replica_cost_per_hour == pytest.approx(6.88 * 8)
    assert inputs.warnings and "4 tensor-parallel ranks" in inputs.warnings[0]
    inputs = cm.CostInputs()
    cm.resolve_replica_cost(inputs, _gauge_sim(), 6.88, None, None)
    assert inputs.replica_cost_per_hour is None
    assert any("--gpus-per-replica" in m for m in inputs.missing_notes)
