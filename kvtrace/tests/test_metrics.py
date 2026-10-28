import pytest

from kvtrace.metrics import aggregate_samples, detect_engine, filter_metrics, parse_prometheus_text

VLLM_TEXT = """\
# HELP vllm:prompt_tokens_total Number of prefill tokens processed.
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{engine="0",model_name="meta-llama/Llama-3.1-8B"} 12345.0
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="meta-llama/Llama-3.1-8B"} 4.0
# TYPE vllm:prefix_cache_queries_total counter
vllm:prefix_cache_queries_total{engine="0",model_name="meta-llama/Llama-3.1-8B"} 900.0
vllm:prefix_cache_hits_total{engine="0",model_name="meta-llama/Llama-3.1-8B"} 300.0
# TYPE vllm:request_prefill_time_seconds histogram
vllm:request_prefill_time_seconds_bucket{engine="0",le="0.3",model_name="meta-llama/Llama-3.1-8B"} 2.0
vllm:request_prefill_time_seconds_bucket{engine="0",le="+Inf",model_name="meta-llama/Llama-3.1-8B"} 5.0
vllm:request_prefill_time_seconds_sum{engine="0",model_name="meta-llama/Llama-3.1-8B"} 1.25
vllm:request_prefill_time_seconds_count{engine="0",model_name="meta-llama/Llama-3.1-8B"} 5.0
vllm:request_success_total{engine="0",finished_reason="stop",model_name="m"} 3.0
vllm:generation_tokens_total{engine="0",model_name="meta-llama/Llama-3.1-8B"} 777
python_gc_objects_collected_total{generation="0"} 99.0
process_cpu_seconds_total 1.5
my_app_prompt_tokens_total 5.0
garbage line here
vllm:num_requests_waiting{engine="0",model_name="meta-llama/Llama-3.1-8B"} NaN
"""

SGLANG_TEXT = """\
# HELP sglang:prompt_tokens_total Number of prefill tokens processed.
# TYPE sglang:prompt_tokens_total counter
sglang:prompt_tokens_total{model_name="qwen",tp_rank="0"} 1000.0 1700000000000
sglang:prompt_tokens_total{model_name="qwen",tp_rank="1"} 500.0
# TYPE sglang:cache_hit_rate gauge
sglang:cache_hit_rate{model_name="qwen"} 0.42
sglang:num_running_reqs{model_name="qwen"} 3
sglang:num_queue_reqs{model_name="qwen"} 1
sglang:token_usage{model_name="qwen"} 0.9
"""

M = 'model_name="meta-llama/Llama-3.1-8B"'


def test_parse_prometheus_text_handles_labels_and_bad_lines():
    samples = parse_prometheus_text(VLLM_TEXT)
    names = [s[0] for s in samples]
    assert "vllm:prompt_tokens_total" in names and "process_cpu_seconds_total" in names
    assert all("garbage" not in n for n in names)
    assert "vllm:num_requests_waiting" not in names  # NaN dropped


def test_filter_vllm_metrics():
    out = filter_metrics(VLLM_TEXT)
    assert out[f'vllm:prompt_tokens_total{{engine="0",{M}}}'] == 12345.0
    assert out[f'vllm:num_requests_running{{engine="0",{M}}}'] == 4.0
    assert out[f'vllm:prefix_cache_hits_total{{engine="0",{M}}}'] == 300.0
    assert out[f'vllm:request_prefill_time_seconds_sum{{engine="0",{M}}}'] == 1.25
    assert not any("_bucket" in k or "python_gc" in k or "process_cpu" in k for k in out)
    assert not any(k.startswith("my_app") for k in out)
    assert not any("request_success" in k for k in out)
    assert not any("finished_reason" in k or "le=" in k for k in out)


def test_filter_sglang_metrics_drops_labels_sums_counters_and_averages_gauges():
    text = SGLANG_TEXT + 'sglang:cache_hit_rate{model_name="qwen",tp_rank="1"} 0.62\n'
    out = filter_metrics(text)
    # Counter series stay apart per dropped label set; their increments are summed.
    assert out['sglang:prompt_tokens_total{model_name="qwen",kvtrace_series="tp_rank=0"}'] == 1000.0
    assert out['sglang:prompt_tokens_total{model_name="qwen",kvtrace_series="tp_rank=1"}'] == 500.0
    agg = aggregate_samples([(0.0, {}), (1.0, out)])
    assert agg["counters"] == {'sglang:prompt_tokens_total{model_name="qwen"}': 1500.0}
    assert out['sglang:cache_hit_rate{model_name="qwen"}'] == pytest.approx(0.52)
    assert out['sglang:num_queue_reqs{model_name="qwen"}'] == 1
    assert not any("token_usage" in k for k in out)
    assert detect_engine(out) == "sglang"
    assert detect_engine(filter_metrics(VLLM_TEXT)) == "vllm"


def test_sglang_cached_tokens_are_kept():
    out = filter_metrics('sglang:cached_tokens_total{model_name="qwen",tp_rank="0"} 42\n')
    assert out == {'sglang:cached_tokens_total{model_name="qwen",kvtrace_series="tp_rank=0"}': 42.0}


def test_aggregate_counter_deltas_with_reset_and_gauge_mean():
    samples = [(1.0, {"a_total": 10.0, "g": 2.0}), (2.0, {"a_total": 15.0, "g": 4.0}),
               (3.0, {"a_total": 3.0, "g": 6.0})]
    agg = aggregate_samples(samples)
    assert agg["counters"]["a_total"] == 8.0  # +5, then reset to 0 and +3
    assert agg["gauges"]["g"] == 4.0
    assert agg["window"] == [1.0, 3.0]


def test_merge_samples_adds_counters_and_replaces_gauges():
    from kvtrace.metrics import merge_samples

    target = {"a_total": 1.0, "g": 1.0}
    merge_samples(target, {"a_total": 2.0, "g": 5.0, "b_total": 3.0})
    assert target == {"a_total": 3.0, "g": 5.0, "b_total": 3.0}


def test_scrape_many_reads_a_unix_socket_and_reports_failures(tmp_path):
    import http.server
    import socketserver
    import threading

    from kvtrace.metrics import scrape_many

    body = b'lmcache:num_hit_tokens_total{model_name="m",worker_id="0",role="worker"} 7\n'

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/metrics" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class UnixHTTPServer(socketserver.UnixStreamServer):
        def get_request(self):
            conn, _ = super().get_request()
            return conn, ("local", 0)

    sock = str(tmp_path / "lmc.sock")
    server = UnixHTTPServer(sock, Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sample, errors, times = scrape_many([f"unix:{sock}", f"unix:{sock}:/other",
                                             f"unix:{tmp_path / 'missing.sock'}"])
    finally:
        server.shutdown()
        server.server_close()
    assert sample == {
        'lmcache:num_hit_tokens_total{model_name="m",worker_id="0",role="worker",'
        f'kvtrace_url="unix:{sock}"}}': 7.0}
    assert len(errors) == 2 and "HTTP 404" in errors[0]
    assert list(times) == [f"unix:{sock}"]


def test_scrape_many_survives_any_exception_and_bounds_slow_urls(monkeypatch):
    import threading

    from kvtrace import metrics as metrics_mod

    release = threading.Event()

    def fake_scrape(url, allowlist=None, timeout=5.0):
        if url == "bug":
            raise KeyError("parser bug")
        if url == "slow":
            release.wait(5)
        return {f'lmcache:num_hit_tokens_total{{worker_id="{url}"}}': 1.0}

    monkeypatch.setattr(metrics_mod, "scrape", fake_scrape)
    try:
        sample, errors, times = metrics_mod.scrape_many(["0", "bug", "slow", "1"], timeout=0.1,
                                                        clock=lambda: 7.0)
    finally:
        release.set()
    assert set(sample) == {'lmcache:num_hit_tokens_total{worker_id="0",kvtrace_url="0"}',
                           'lmcache:num_hit_tokens_total{worker_id="1",kvtrace_url="1"}'}
    assert times == {"0": 7.0, "1": 7.0}
    assert errors[0].startswith("bug: ") and "parser bug" in errors[0]
    assert errors[1] == "slow: no answer within 0.1 s"


def test_source_label_drops_credentials_query_and_fragment(monkeypatch):
    from kvtrace import metrics as metrics_mod
    from kvtrace.metrics import counter_increments, source_label, tag_source

    secret = "http://user:pw@w0:9000/metrics?token=abc#top"
    assert source_label(secret) == "http://w0:9000/metrics"
    # Other URLs keep their exact text, so their labels do not change.
    for url in ("http://w0:9000/metrics", "unix:/run/lmc.sock:/metrics", "0"):
        assert source_label(url) == url
    (key,) = tag_source({"x_total": 1.0}, secret)
    assert key == 'x_total{kvtrace_url="http://w0:9000/metrics"}'
    assert "pw" not in key and "token" not in key

    monkeypatch.setattr(metrics_mod, "scrape", lambda url, allowlist=None, timeout=5.0:
                        {"x_total": 2.0})
    sample, errors, times = metrics_mod.scrape_many([secret], clock=lambda: 3.0)
    # extra_ts uses the same value as the label: a series born later is still "born".
    assert times == {"http://w0:9000/metrics": 3.0} and errors == []
    late = {}
    counter_increments([(1.0, {}, times), (2.0, sample, times)], late=late)
    assert late == {key: (2.0, "born")}


def test_lmcache_role_label_keeps_scheduler_and_worker_apart():
    text = ('lmcache:num_hit_tokens_total{worker_id="0",role="scheduler"} 5\n'
            'lmcache:num_hit_tokens_total{worker_id="0",role="worker"} 7\n')
    assert filter_metrics(text) == {
        'lmcache:num_hit_tokens_total{worker_id="0",role="scheduler"}': 5.0,
        'lmcache:num_hit_tokens_total{worker_id="0",role="worker"}': 7.0}


def test_recorder_merges_extra_metrics_urls(monkeypatch, tmp_path):
    from kvtrace import metrics as metrics_mod
    from kvtrace.recorder import Recorder, RecorderConfig

    with pytest.raises(ValueError, match="--extra-metrics-url"):
        RecorderConfig(endpoint="tcp://127.0.0.1:1", model="m", out_dir=str(tmp_path),
                       extra_metrics_urls=("http://x",)).validate()
    config = RecorderConfig(endpoint="tcp://127.0.0.1:1", model="m", out_dir=str(tmp_path),
                            metrics_url="http://engine", extra_metrics_urls=("http://w0", "http://w1"))
    recorder = Recorder(config, clock=lambda: 1.0)
    answers = {"http://engine": {"vllm:prompt_tokens_total": 5.0},
               "http://w0": {'lmcache:num_hit_tokens_total{worker_id="0"}': 1.0}}

    def fake_scrape(url, allowlist=None, timeout=5.0):
        recorder.stop()
        if url not in answers:
            raise OSError("refused")
        return dict(answers[url])

    monkeypatch.setattr(metrics_mod, "scrape", fake_scrape)
    recorder._metrics_loop()
    ts, sample, extra_ts = recorder._metrics_q.get_nowait()
    assert sample == {"vllm:prompt_tokens_total": 5.0,
                      'lmcache:num_hit_tokens_total{worker_id="0",kvtrace_url="http://w0"}': 1.0}
    assert extra_ts == {"http://w0": 1.0}
    emitted = []
    monkeypatch.setattr(recorder, "_emit", emitted.append)
    recorder._metrics_q.put((ts, sample, extra_ts))
    recorder._drain_metrics()
    assert emitted[-1]["t"] == "M" and emitted[-1]["extra_ts"] == {"http://w0": 1.0}
    with pytest.raises(ValueError, match="--extra-metrics-timeout"):
        RecorderConfig(endpoint="tcp://127.0.0.1:1", model="m", out_dir=str(tmp_path),
                       metrics_url="http://e", extra_metrics_timeout=0).validate()


def test_missed_scrape_of_a_shared_series_is_not_a_reset():
    from kvtrace.metrics import counter_increments, merge_samples, tag_source

    # Two extra URLs export the same series. A: 1000 -> 1150 -> 1300, B: 1200 -> (missed) -> 1500.
    # The true delta is 300 + 300 = 600; a sum per sample reads 2200, 1150, 2800 and, with
    # the drop read as a reset, reports 2800.
    def sample(a, b):
        out = {}
        if a is not None:
            merge_samples(out, tag_source({"x_total": a}, "A"))
        if b is not None:
            merge_samples(out, tag_source({"x_total": b}, "B"))
        return out

    tagged = [(0.0, sample(1000.0, 1200.0)), (1.0, sample(1150.0, None)), (2.0, sample(1300.0, 1500.0))]
    assert sum(aggregate_samples(tagged)["counters"].values()) == 600.0
    # Traces recorded before kvtrace_url: one summed key, extra_ts names the URLs that answered.
    merged = [(0.0, {"lmcache:x_total": 2200.0}, ["A", "B"]), (1.0, {"lmcache:x_total": 1150.0}, ["A"]),
              (2.0, {"lmcache:x_total": 2800.0}, ["A", "B"])]
    assert sum(v for _, inc in counter_increments(merged) for v in inc.values()) == 600.0
    # Without the URL sets the old reading stays (the documented limit).
    assert aggregate_samples([s[:2] for s in merged])["counters"]["lmcache:x_total"] == 2800.0


def test_url_set_changes_in_untagged_traces_lose_at_most_one_period():
    from kvtrace.metrics import counter_increments

    def total(rows):
        return sum(v for _, inc in counter_increments(rows) for v in inc.values())

    # B is gone for good: the second sample with {A} becomes the baseline (the 2200 -> 1150
    # step is lost, 1150 -> 1300 counts).
    assert total([(0.0, {"lmcache:x_total": 2200.0}, ["A", "B"]),
                  (1.0, {"lmcache:x_total": 1150.0}, ["A"]),
                  (2.0, {"lmcache:x_total": 1300.0}, ["A"])]) == 150.0
    # B comes up late: its cumulative 1000 must not count.
    assert total([(0.0, {"lmcache:x_total": 1000.0}, ["A"]),
                  (1.0, {"lmcache:x_total": 2200.0}, ["A", "B"]),
                  (2.0, {"lmcache:x_total": 2500.0}, ["A", "B"])]) == 300.0
    # A real restart (same URL set) still restarts from zero.
    assert total([(0.0, {"lmcache:x_total": 500.0}, ["A"]),
                  (1.0, {"lmcache:x_total": 40.0}, ["A"])]) == 40.0


def test_late_series_baseline_depends_on_endpoint_and_created_time():
    from kvtrace.metrics import counter_increments, tag_source

    late = {}
    # Engine series absent at t=0 (vLLM creates a labelled series at its first increment).
    rows = [(0.0, {"a_total": 1.0}), (1.0, {"a_total": 2.0, "b_total": 700.0}),
            (2.0, {"a_total": 3.0, "b_total": 900.0})]
    incs = counter_increments(rows, late=late)
    assert sum(inc.get("b_total", 0.0) for _, inc in incs) == 900.0
    assert late == {"b_total": (1.0, "born")}
    # An extra URL that did not answer at t=0: counts from 0 only if *_created is in the window.
    for created, expect, how in ((5.0, 900.0, "created"), (-100.0, 200.0, "first_value")):
        series = tag_source({"c_total": 700.0, "c_created": created}, "U")
        later = tag_source({"c_total": 900.0, "c_created": created}, "U")
        late = {}
        rows = [(0.0, {"a_total": 1.0}, []), (10.0, {"a_total": 2.0, **series}, ["U"]),
                (20.0, {"a_total": 3.0, **later}, ["U"])]
        incs = counter_increments(rows, late=late)
        assert sum(v for _, inc in incs for k, v in inc.items() if k.startswith("c_total")) == expect
        assert [h for _, h in late.values()] == [how]


def test_created_series_are_kept_for_kept_counters_only():
    from kvtrace.metrics import created_key

    text = ('lmcache:num_hit_tokens_total{worker_id="0",role="worker"} 7\n'
            'lmcache:num_hit_tokens_created{worker_id="0",role="worker"} 1.5e9\n'
            'process_cpu_seconds_created 1.5e9\n')
    out = filter_metrics(text)
    assert out['lmcache:num_hit_tokens_created{worker_id="0",role="worker"}'] == 1.5e9
    assert not any(k.startswith("process") for k in out)
    assert created_key('lmcache:num_hit_tokens_total{worker_id="0"}') == \
        'lmcache:num_hit_tokens_created{worker_id="0"}'
    assert created_key('vllm:ttft_seconds_bucket{engine="0",le="1.0"}') == \
        'vllm:ttft_seconds_created{engine="0"}'
    assert created_key("g") is None
    assert "lmcache:num_hit_tokens_created{worker_id=\"0\",role=\"worker\"}" not in aggregate_samples(
        [(0.0, out)])["gauges"]
