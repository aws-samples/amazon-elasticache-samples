"""Counter reset, collapsed series, shared node id and extra URL coverage in tiers."""

import pytest

from kvtrace.metrics import aggregate_samples, counter_increments, filter_metrics
from kvtrace.tiers import measure_tiers

PROMPT = 'vllm:prompt_tokens_total{engine="0"}'
CREATED = 'vllm:prompt_tokens_created{engine="0"}'


def incs(samples):
    return [inc.get(PROMPT) for _, inc in counter_increments(samples)]


# ---- MR-2: a reset is missed when the new value is above the old one ---------
def test_changed_created_time_is_a_reset_even_when_the_value_went_up():
    samples = [(0.0, {PROMPT: 100.0, CREATED: 1.0}), (10.0, {PROMPT: 200.0, CREATED: 1.0}),
               # Restart, then the counter passed 200 before the next sample.
               (20.0, {PROMPT: 500.0, CREATED: 15.0})]
    assert incs(samples) == [None, 100.0, 500.0]
    assert aggregate_samples(samples)["counters"][PROMPT] == 600.0


def test_without_created_time_only_a_drop_is_a_reset():
    samples = [(0.0, {PROMPT: 100.0}), (10.0, {PROMPT: 200.0}), (20.0, {PROMPT: 500.0}),
               (30.0, {PROMPT: 50.0})]
    assert incs(samples) == [None, 100.0, 300.0, 50.0]


# ---- MR-3: one collapsed series resets, the whole sum is counted ------------
def sglang_text(rank0, rank1):
    return (f'sglang:prompt_tokens_total{{model_name="q",dp_rank="0"}} {rank0}\n'
            f'sglang:prompt_tokens_total{{model_name="q",dp_rank="1"}} {rank1}\n'
            'sglang:num_running_reqs{model_name="q",dp_rank="0"} 2\n'
            'sglang:num_running_reqs{model_name="q",dp_rank="1"} 4\n')


def test_reset_of_one_dropped_label_series_counts_only_its_increment():
    s0 = filter_metrics(sglang_text(1_000_000, 1_000_000))
    # dp_rank 1 restarted and is at 1,000; dp_rank 0 added 1,000.
    s1 = filter_metrics(sglang_text(1_001_000, 1_000))
    merged = 'sglang:prompt_tokens_total{model_name="q"}'
    assert counter_increments([(0.0, s0), (10.0, s1)])[1][1] == {merged: 2000.0}
    agg = aggregate_samples([(0.0, s0), (10.0, s1)])
    assert agg["counters"] == {merged: 2000.0}
    # Gauges are still averaged over the collapsed series.
    assert agg["gauges"] == {'sglang:num_running_reqs{model_name="q"}': 3.0}
    result = measure_tiers([{"t": "M", "ts": 0.0, "node": "A", "metrics": s0},
                            {"t": "M", "ts": 10.0, "node": "A", "metrics": s1}])
    assert result["fleet"]["total"]["prompt_tokens"] == 2000.0


def test_old_trace_with_summed_series_keeps_the_drop_rule():
    key = 'sglang:prompt_tokens_total{model_name="q"}'
    samples = [(0.0, {key: 2_000_000.0}), (10.0, {key: 1_001_000.0})]
    assert counter_increments(samples)[1][1] == {key: 1_001_000.0}


def test_merged_key_removes_only_the_series_label():
    from kvtrace.metrics import merged_key

    assert merged_key('a_total{engine="0",kvtrace_series="x=\\"1\\",y=2",kvtrace_url="u"}') \
        == 'a_total{engine="0",kvtrace_url="u"}'
    assert merged_key('a_total{kvtrace_series="tp_rank=0"}') == "a_total"
    assert merged_key('a_total{engine="0"}') == 'a_total{engine="0"}'


# ---- MR-1: two recorders with one node id ------------------------------------
def m(ts, prompt, rid=None, node="A1", **extra):
    rec = {"t": "M", "ts": ts, "node": node, "metrics": {PROMPT: float(prompt), **extra}}
    if rid is not None:
        rec["rid"] = rid
    return rec


def test_two_recorders_with_one_node_id_are_kept_apart_with_a_warning():
    # Two engines on one host, both engine="0"; each adds 400 prompt tokens.
    records = []
    for i in range(5):
        records.append(m(10.0 * i, 100_000 + 100 * i, rid="aaaa"))
        records.append(m(10.0 * i + 5, 50_000 + 100 * i, rid="bbbb"))
    result = measure_tiers(records)
    assert result["fleet"]["total"]["prompt_tokens"] == 800.0
    assert sorted(result["nodes"]) == ["A1@aaaa", "A1@bbbb"]
    assert any(n.startswith("WARNING A1: 2 recorders") for n in result["notes"])


def test_recorder_restart_on_one_node_counts_every_increment_once():
    records = [m(0.0, 0, rid="aaaa"), m(10.0, 100, rid="aaaa"), m(20.0, 200, rid="aaaa"),
               # The recorder restarted; the engine kept running.
               m(30.0, 250, rid="bbbb"), m(40.0, 300, rid="bbbb")]
    result = measure_tiers(records)
    assert list(result["nodes"]) == ["A1"]
    assert result["fleet"]["total"]["prompt_tokens"] == 300.0
    assert not any("recorders" in n for n in result["notes"])


def test_engine_restart_with_recorder_restart_is_a_reset():
    records = [m(0.0, 0, rid="aaaa", **{CREATED: 1.0}), m(10.0, 300, rid="aaaa", **{CREATED: 1.0}),
               m(30.0, 400, rid="bbbb", **{CREATED: 25.0})]
    assert measure_tiers(records)["fleet"]["total"]["prompt_tokens"] == 700.0


def test_old_traces_without_rid_are_one_series_per_node():
    records = [m(0.0, 0), m(10.0, 100), m(20.0, 300)]
    result = measure_tiers(records)
    assert list(result["nodes"]) == ["A1"]
    assert result["fleet"]["total"]["prompt_tokens"] == 300.0


# ---- MR-4: an extra URL that rarely answered ---------------------------------
def lmc(url, value):
    return {f'lmcache:num_hit_tokens_total{{worker_id="0",kvtrace_url="{url}"}}': float(value)}


@pytest.mark.parametrize("answers, warned", [(10, False), (2, True)])
def test_extra_url_that_answered_in_few_samples_gives_a_warning(answers, warned):
    good, bad = "unix:/tmp/lmc0.sock", "unix:/tmp/lmc1.sock"
    records = []
    for i in range(10):
        extra = {**lmc(good, i)}
        extra_ts = {good: 10.0 * i}
        if i < answers:
            extra.update(lmc(bad, i))
            extra_ts[bad] = 10.0 * i
        rec = m(10.0 * i, 100 * i, **extra)
        rec["extra_ts"] = extra_ts
        records.append(rec)
    notes = measure_tiers(records)["notes"]
    hits = [n for n in notes if "extra metrics URL" in n]
    if warned:
        assert hits == [f"WARNING A1: extra metrics URL {bad} answered in 2 of 10 samples (20%); "
                        "its counters (LMCache L2) can be low. Check the URL and the recorder log"]
    else:
        assert hits == []
