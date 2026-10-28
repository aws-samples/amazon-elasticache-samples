import gzip
import json
import os

import pytest

from kvtrace.cli import main
from kvtrace.metrics import SERIES_LABEL, filter_metrics, merge_samples, source_label, tag_source
from kvtrace.tiers import measure_tiers, render_markdown

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
# A real ON run (vLLM 0.30.0 TP 8 + LMCache MP server from LMCache PR 5556 with a Valkey
# L2): trimmed copies of the vLLM /metrics and of the MP server's
# HTTP /metrics at the start and the end of the measure window (summary.json t_start/t_end).
WINDOW = (1792594742.35, 1792599395.65)
MP_URL = "http://127.0.0.1:8080/metrics"
MODEL = 'model_name="Qwen3-235B-A22B-Instruct-2507-FP8"'
LBL = 'engine="0",model_name="qwen3-235b"'

# Hand computation from the fixtures (end - start):
# vLLM prompt_tokens_by_source{external_kv_transfer}: 7,026,176 - 0.
EXTERNAL = 7026176
# lmcache_mp_lookup_hit_l1_keys_total 17,048 - 0, lmcache_mp_lookup_hit_l2_keys_total 220,576 - 0.
L1_KEYS, L2_KEYS = 17048, 220576
L2 = EXTERNAL * L2_KEYS / (L1_KEYS + L2_KEYS)  # 6,522,092.875
L1 = EXTERNAL - L2  # 504,083.125


def read(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def mp_sample(which):
    """The metrics of one M record, as the recorder builds it with --extra-metrics-url."""
    metrics = filter_metrics(read(f"vllm030_mp_on_metrics_{which}.txt"))
    merge_samples(metrics, tag_source(filter_metrics(
        read(f"lmcache_mp_on_metrics_{which}.txt")), MP_URL))
    return metrics


def m_records(samples, node="A1"):
    return [{"t": "M", "ts": ts, "node": node, "rid": "r1", "metrics": m,
             "extra_ts": {source_label(MP_URL): ts}} for ts, m in samples]


def stage_c_records():
    return m_records([(WINDOW[0], mp_sample("start")), (WINDOW[1], mp_sample("end"))])


def byte_node_records(node="B1"):
    """A node with LMCache in the non-MP mode: two TP workers that each read 512,000 bytes
    from L2; at 512 KV bytes per token per worker its L2 is 1,000 tokens."""
    def sample(read_bytes, prompt, ext, requests):
        text = "".join(
            f'lmcache:num_remote_read_bytes_total{{worker_id="{w}",role="worker"}} {read_bytes}\n'
            for w in (0, 1))
        text += (f"vllm:prompt_tokens_total{{{LBL}}} {prompt}\n"
                 f'vllm:prompt_tokens_by_source_total{{{LBL},source="external_kv_transfer"}} {ext}\n'
                 f'vllm:prompt_tokens_by_source_total{{{LBL},source="local_cache_hit"}} 0\n'
                 f'vllm:prompt_tokens_by_source_total{{{LBL},source="local_compute"}} {prompt - ext}\n'
                 f"vllm:request_prompt_tokens_count{{{LBL}}} {requests}\n")
        return filter_metrics(text)

    return [{"t": "M", "ts": ts, "node": node, "metrics": m}
            for ts, m in ((WINDOW[0], sample(0, 0, 0, 0)),
                          (WINDOW[1], sample(512000, 10000, 3000, 2)))]


def write_segment(path, records):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def test_allowlist_keeps_the_mp_lookup_families_of_a_real_mp_server():
    kept = filter_metrics(read("lmcache_mp_on_metrics_end.txt"))
    assert kept == {
        f"lmcache_mp_lookup_requested_tokens_total{{{MODEL}}}": 65310720.0,
        f"lmcache_mp_lookup_hit_tokens_total{{{MODEL}}}": 21075712.0,
        f"lmcache_mp_lookup_hit_l1_tokens_total{{{MODEL}}}": 13981696.0,
        f"lmcache_mp_lookup_hit_l2_tokens_total{{{MODEL}}}": 7094016.0,
        f"lmcache_mp_lookup_hit_l1_keys_total{{{MODEL}}}": 17048.0,
        f"lmcache_mp_lookup_hit_l2_keys_total{{{MODEL}}}": 220576.0,
        f"lmcache_mp_lookups_requests_total{{{MODEL}}}": 4987.0,
        "lmcache_mp_l2_prefetch_load_completed_chunks_total": 220576.0,
    }


def test_mp_counters_of_all_cache_salts_are_summed_and_no_salt_is_written():
    text = "\n".join(
        f'lmcache_mp_lookup_hit_l2_keys_total{{cache_salt="{salt}",{MODEL}}} {value}'
        for salt, value in (("tenant-a-secret", 10), ("tenant-b-secret", 30))) + "\n"
    kept = filter_metrics(text)
    assert kept == {f"lmcache_mp_lookup_hit_l2_keys_total{{{MODEL}}}": 40.0}
    assert not any("secret" in key or SERIES_LABEL in key for key in kept)


def test_other_dropped_labels_of_mp_counters_still_stay_apart_per_series():
    text = "\n".join(
        f'lmcache_mp_lookup_hit_l2_keys_total{{cache_salt="s",rank="{rank}",{MODEL}}} {value}'
        for rank, value in (("0", 10), ("1", 30))) + "\n"
    assert filter_metrics(text) == {
        f'lmcache_mp_lookup_hit_l2_keys_total{{{MODEL},{SERIES_LABEL}="rank=0"}}': 10.0,
        f'lmcache_mp_lookup_hit_l2_keys_total{{{MODEL},{SERIES_LABEL}="rank=1"}}': 30.0,
    }


def test_mp_hit_keys_split_external_into_l1_and_l2_on_real_stage_c_metrics():
    result = measure_tiers(stage_c_records(), interval=3600)
    total = result["fleet"]["total"]
    assert total["external_tokens"] == EXTERNAL
    assert total["l2_tokens"] == pytest.approx(L2)
    assert total["l1_tokens"] == pytest.approx(L1)
    assert total["l1_tokens"] + total["l2_tokens"] == pytest.approx(EXTERNAL)
    assert total["lmcache_mp_l1_keys"] == L1_KEYS and total["lmcache_mp_l2_keys"] == L2_KEYS
    assert total["lmcache_mp_l2_loaded_keys"] == L2_KEYS
    assert total["l2_chunk_overlap_bound_tokens"] is None
    # The node total is the same window, and the intervals add up to it.
    node = result["nodes"]["A1"]
    assert node["total"]["l2_tokens"] == pytest.approx(L2)
    assert sum(r["l2_tokens"] for r in node["intervals"]) == pytest.approx(L2)
    assert "lmcache_mp_lookup_hit_l2_keys_total" in node["counters"]["l2"]
    assert any(n.startswith("L1/L2 split from LMCache MP lookup keys") for n in result["notes"])
    assert not any("reported together" in n or "WARNING" in n for n in result["notes"])
    assert "L1/L2 split from LMCache MP lookup keys" in render_markdown(result)


def test_mp_l2_hit_keys_above_loaded_chunks_give_a_warning():
    records = stage_c_records()
    loaded = "lmcache_mp_l2_prefetch_load_completed_chunks_total{kvtrace_url=\"" + MP_URL + "\"}"
    records[1]["metrics"][loaded] = 1000.0
    notes = measure_tiers(records)["notes"]
    (warning,) = [n for n in notes if n.startswith("WARNING")]
    assert "220,576 L2 hit keys but 1,000 chunks loaded from L2" in warning
    assert "counters disagree" in warning


def test_mp_without_the_loaded_chunks_series_gives_a_note_not_a_warning():
    records = stage_c_records()
    for rec in records:
        for key in [k for k in rec["metrics"] if "l2_prefetch_load_completed" in k]:
            del rec["metrics"][key]
    result = measure_tiers(records)
    assert result["fleet"]["total"]["l2_tokens"] == pytest.approx(L2)
    assert not any(n.startswith("WARNING") for n in result["notes"])
    assert any("L2 hit keys not checked against loaded chunks" in n for n in result["notes"])


def test_external_without_mp_hit_keys_gives_no_split_and_a_note():
    records = stage_c_records()
    for rec in records:
        for key in [k for k in rec["metrics"] if "_keys_total" in k]:
            rec["metrics"][key] = 0.0
    result = measure_tiers(records)
    total = result["fleet"]["total"]
    assert total["external_tokens"] == EXTERNAL
    assert total["l1_tokens"] is None and total["l2_tokens"] is None
    assert any("counted no L1 or L2 hit keys" in n for n in result["notes"])


def test_mp_split_uses_the_window_key_ratio_in_every_interval():
    # LMCache counts the keys when the prefetch ends (interval 0) and vLLM counts the
    # external tokens when the prompt is done (interval 1).
    start, end = mp_sample("start"), mp_sample("end")
    middle = dict(end)
    for key in [k for k in start if k.startswith("vllm:")]:
        middle[key] = start[key]
    records = m_records([(0.0, start), (60.0, middle), (120.0, end)])
    result = measure_tiers(records, interval=60)
    node = result["nodes"]["A1"]
    first, second = node["intervals"]
    assert first["external_tokens"] == 0 and first["l1_tokens"] == 0 and first["l2_tokens"] == 0
    assert first["lmcache_mp_l2_keys"] == L2_KEYS
    assert second["external_tokens"] == EXTERNAL and second["lmcache_mp_l2_keys"] == 0
    assert second["l2_tokens"] == pytest.approx(L2) and second["l1_tokens"] == pytest.approx(L1)
    assert node["total"]["l2_tokens"] == pytest.approx(L2)
    fleet_rows = result["fleet"]["intervals"]
    assert sum(r["l2_tokens"] for r in fleet_rows) == pytest.approx(L2)


def test_mixed_fleet_states_both_caveats_and_keeps_the_byte_bound():
    result = measure_tiers(stage_c_records() + byte_node_records(), worker_kv_bytes_per_token=512)
    assert result["l1_l2_mp_nodes"] == ["A1"]
    assert result["nodes"]["B1"]["total"]["l2_tokens"] == 1000
    assert result["nodes"]["A1"]["total"]["l2_chunk_overlap_bound_tokens"] is None
    # B1: min(L2 1,000, 2 requests x 256 tokens).
    assert result["nodes"]["B1"]["total"]["l2_chunk_overlap_bound_tokens"] == 512
    assert result["fleet"]["total"]["l2_chunk_overlap_bound_tokens"] == 512
    assert result["fleet"]["total"]["l2_tokens"] == pytest.approx(L2 + 1000)
    assert any(n.startswith("L1/L2 split from LMCache MP lookup keys") for n in result["notes"])
    assert any(n.startswith("L1/L2 split not validated against real") for n in result["notes"])
    md = render_markdown(result)
    assert "On A1, L1 and L2 split" in md and "L2 can also include up to one LMCache chunk" in md


def test_without_lmcache_counters_the_note_names_the_mp_server_url():
    records = m_records([(WINDOW[0], filter_metrics(read("vllm030_mp_on_metrics_start.txt"))),
                         (WINDOW[1], filter_metrics(read("vllm030_mp_on_metrics_end.txt")))])
    for rec in records:
        del rec["extra_ts"]
    result = measure_tiers(records)
    assert result["fleet"]["total"]["l2_tokens"] is None
    (note,) = [n for n in result["notes"] if "reported together" in n]
    assert "--extra-metrics-url" in note and "lmcache server --http-port" in note
    assert "http://127.0.0.1:8080/metrics" in note
    assert "answered" not in note


def test_an_extra_url_that_answered_without_lmcache_counters_is_named():
    # A trace recorded before this fix: the recorder scraped the MP server, and the old allowlist
    # dropped every lmcache_mp_* series.
    records = m_records([(WINDOW[0], filter_metrics(read("vllm030_mp_on_metrics_start.txt"))),
                         (WINDOW[1], filter_metrics(read("vllm030_mp_on_metrics_end.txt")))])
    (note,) = [n for n in measure_tiers(records)["notes"] if "reported together" in n]
    assert f"the extra metrics URL {MP_URL} answered but gave no LMCache tier counters" in note
    assert "record again with this kvtrace" in note


@pytest.mark.parametrize("mixed, caveat", [
    (False, "(L1/L2 from LMCache MP hit keys, not validated against Valkey reads)"),
    (True, "(L1/L2 from LMCache MP hit keys and remote-read bytes, not validated against "
           "Valkey reads)"),
])
def test_cli_tiers_summary_names_the_l1_l2_source(tmp_path, capsys, mixed, caveat):
    write_segment(tmp_path / "A1-1-00000.kvt.jsonl.gz", stage_c_records())
    if mixed:
        write_segment(tmp_path / "B1-1-00000.kvt.jsonl.gz", byte_node_records())
    out = tmp_path / "t.json"
    assert main(["tiers", str(tmp_path), "-o", str(out), "--worker-kv-bytes-per-token", "512"]) == 0
    assert caveat in capsys.readouterr().out
