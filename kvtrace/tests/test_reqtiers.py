import json
import os

import pytest

from kvtrace.reqtiers import (
    ENGINE_KIND,
    build,
    join,
    join_warnings,
    load_rows,
    parse_lmcache_log,
    split_tagged,
    summarize,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
DRIVER = os.path.join(FIX, "vllm030_lmcache_on1_requests.jsonl")
LOG = os.path.join(FIX, "vllm030_lmcache_on1_vllm.log")


def test_lmcache_lookup_lines_are_parsed_once_per_request():
    with open(LOG, encoding="utf-8") as fh:
        rows = list(parse_lmcache_log(fh))
    # Five lookup lines; the "Retrieved" line and the repeated first line are skipped.
    assert len(rows) == 5
    first = rows[0]
    assert first["req_id"] == "cmpl-bb0c63f568cf1512-0-96f366e2"
    assert (first["prompt_tokens"], first["l0_tokens"], first["lmcache_hit_tokens"],
            first["external_tokens"]) == (23983, 512, 15360, 14848)
    assert first["t_lookup"] == pytest.approx(1792452396.846)  # the log time 23:26:36,846 UTC
    with open(LOG, encoding="utf-8") as fh:
        shifted = next(parse_lmcache_log(fh, tz_offset_s=3600.0))
    assert shifted["t_lookup"] == pytest.approx(first["t_lookup"] - 3600.0)


def test_driver_rows_join_lmcache_rows_and_hits_add_up():
    req, rows = build([DRIVER], [LOG], min_prompt_tokens=0)
    assert req["join_stats"]["joined"] == 5 and req["join_stats"]["skipped_lines"] == 0
    for row in rows:
        # The driver's cached_tokens (vLLM usage) is L0 plus the external tokens loaded.
        assert row["cached_tokens"] == row["l0_tokens"] + row["external_tokens"]
    tiers = req["summary"]["by_tier"]
    assert tiers["L0 only"]["n"] == 2
    # 17,920 of 18,907 and 18,432 of 23,911 tokens came from LMCache.
    assert tiers["external (L1+L2) 75-100% of prompt"]["n"] == 2
    assert sum(b["n"] for b in req["summary"]["by_hit_share"].values()) == 5


def test_join_picks_the_first_unused_engine_row_in_the_time_window():
    driver = [{"t_send": 10.0, "t_first": 12.0, "ttft_s": 2.0, "prompt_tokens": 100,
               "cached_tokens": 50, "status": "ok"},
              {"t_send": 10.5, "t_first": 13.0, "ttft_s": 2.5, "prompt_tokens": 100,
               "cached_tokens": 0, "status": "ok"},
              {"t_send": 30.0, "t_first": 31.0, "ttft_s": 1.0, "prompt_tokens": 100,
               "cached_tokens": 0, "status": "ok"}]
    engine = [{"kind": ENGINE_KIND, "req_id": "a", "t_lookup": 10.2, "prompt_tokens": 100,
               "l0_tokens": 50, "external_tokens": 0},
              {"kind": ENGINE_KIND, "req_id": "b", "t_lookup": 10.6, "prompt_tokens": 100,
               "l0_tokens": 0, "external_tokens": 0},
              {"kind": ENGINE_KIND, "req_id": "late", "t_lookup": 50.0, "prompt_tokens": 100,
               "l0_tokens": 0, "external_tokens": 0}]
    rows, stats = join(driver, engine)
    assert [r.get("req_id") for r in rows] == ["a", "b", None]
    assert stats["joined"] == 2


def test_connector_rows_alone_and_short_prompts_filtered(tmp_path):
    path = tmp_path / "engine.jsonl"
    lines = [{"kind": ENGINE_KIND, "req_id": "x", "t_lookup": 1.0, "prompt_tokens": 2000,
              "l0_tokens": 1024, "external_tokens": 512},
             {"kind": ENGINE_KIND, "req_id": "y", "t_lookup": 2.0, "prompt_tokens": 100,
              "l0_tokens": 0, "external_tokens": 0},
             {"kind": "kvtrace.request_log_stats", "written": 2, "dropped": 0}]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    driver, engine, skipped = load_rows([str(path)])
    assert (len(driver), len(engine), skipped) == (0, 2, 2)
    rows, _ = join(driver, engine)
    summary = summarize(rows)
    assert summary["used"] == 1  # the 100-token prompt is below 1,024
    assert summary["by_hit_share"]["75-100%"]["n"] == 1
    assert summary["by_tier"]["external (L1+L2) 25-75% of prompt"]["n"] == 1


def test_connector_and_log_rows_of_one_request_are_counted_once(tmp_path):
    with open(LOG, encoding="utf-8") as fh:
        log_row = next(parse_lmcache_log(fh))
    connector_row = {k: v for k, v in log_row.items() if k != "source"}
    connector_row["external_tokens"] = 1  # marks the row that must win
    path = tmp_path / "engine.jsonl"
    path.write_text(json.dumps(connector_row) + "\n")
    req, rows = build([DRIVER, str(path)], [LOG], min_prompt_tokens=0)
    assert req["join_stats"]["duplicate_engine_rows"] == 1 and req["join_stats"]["joined"] == 5
    (row,) = [r for r in rows if r.get("req_id") == log_row["req_id"]]
    assert row["external_tokens"] == 1


def test_engine_only_rows_keep_a_non_ok_status():
    engine = [{"kind": ENGINE_KIND, "req_id": "a", "t_lookup": 1.0, "prompt_tokens": 2000,
               "l0_tokens": 0, "external_tokens": 0, "status": "FINISHED_ABORTED"},
              {"kind": ENGINE_KIND, "req_id": "b", "t_lookup": 2.0, "prompt_tokens": 2000,
               "l0_tokens": 0, "external_tokens": 0, "status": "FINISHED_STOPPED"}]
    rows, _ = join([], engine)
    assert [r["status"] for r in rows] == ["FINISHED_ABORTED", "ok"]
    assert summarize(rows)["used"] == 1


def test_load_rows_reads_rotated_connector_logs_oldest_first(tmp_path):
    path = tmp_path / "requests.jsonl"
    for name, rid in (("requests.jsonl.2", "a"), ("requests.jsonl.1", "b"), ("requests.jsonl", "c")):
        (tmp_path / name).write_text(json.dumps({"kind": ENGINE_KIND, "req_id": rid}) + "\n")
    # A rotated sibling also given by name is read once.
    _driver, engine, _skipped = load_rows([str(path), str(tmp_path / "requests.jsonl.1")])
    assert [row["req_id"] for row in engine] == ["a", "b", "c"]


def _two_server_case():
    # Fixed-length prompts on two servers. Server B's engine sees its request first, so a
    # join on prompt length alone gives A's driver row B's engine row (inverted hits).
    driver = [{"t_send": 10.0, "t_first": 12.0, "ttft_s": 2.0, "prompt_tokens": 2048,
               "cached_tokens": 2048, "status": "ok", "server": "http://10.0.0.1:8000"},
              {"t_send": 10.1, "t_first": 11.0, "ttft_s": 0.9, "prompt_tokens": 2048,
               "cached_tokens": 0, "status": "ok", "server": "10.0.0.2:8000"}]
    engine = [{"kind": ENGINE_KIND, "req_id": "b", "t_lookup": 10.15, "prompt_tokens": 2048,
               "l0_tokens": 0, "external_tokens": 0},
              {"kind": ENGINE_KIND, "req_id": "a", "t_lookup": 10.2, "prompt_tokens": 2048,
               "l0_tokens": 0, "external_tokens": 2048}]
    return driver, engine


def test_join_uses_the_server_and_warns_without_it():
    driver, engine = _two_server_case()
    rows, stats = join(driver, engine)
    # Without engine servers: the old join, which inverts the two rows, plus a warning.
    assert [r["req_id"] for r in rows] == ["b", "a"] and stats["join_key"] == "prompt_tokens"
    (warning,) = join_warnings(driver, engine, stats)
    assert "2 servers" in warning and "SERVER=FILE" in warning
    tagged = [dict(engine[0], server="10.0.0.2:8000"), dict(engine[1], server="http://10.0.0.1:8000/")]
    rows, stats = join(driver, tagged)
    assert [r["req_id"] for r in rows] == ["a", "b"]
    assert stats["join_key"] == "server+prompt_tokens" and join_warnings(driver, tagged, stats) == []
    no_server = [{k: v for k, v in d.items() if k != "server"} for d in driver]
    _, stats = join(no_server, engine)
    assert "no 'server' field" in join_warnings(no_server, engine, stats)[0]


def test_server_tagged_files_join_per_server(tmp_path):
    driver, engine = _two_server_case()
    dpath, apath, bpath = tmp_path / "driver.jsonl", tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    dpath.write_text("\n".join(json.dumps(d) for d in driver) + "\n")
    bpath.write_text(json.dumps(engine[0]) + "\n")
    apath.write_text(json.dumps(engine[1]) + "\n")
    req, rows = build([str(dpath), f"10.0.0.1:8000={apath}", f"http://10.0.0.2:8000={bpath}"],
                      min_prompt_tokens=0)
    assert [r["req_id"] for r in rows] == ["a", "b"] and req["warnings"] == []
    assert "server + prompt_tokens" in req["join"]
    assert split_tagged(str(apath)) == (None, str(apath))


def test_server_tag_applies_to_rotated_siblings(tmp_path):
    path = tmp_path / "a.jsonl"
    for name, rid in (("a.jsonl.1", "old"), ("a.jsonl", "new")):
        (tmp_path / name).write_text(json.dumps({"kind": ENGINE_KIND, "req_id": rid}) + "\n")
    _driver, engine, _skipped = load_rows([f"10.0.0.1:8000={path}"])
    assert [(row["req_id"], row["server"]) for row in engine] == [
        ("old", "10.0.0.1:8000"), ("new", "10.0.0.1:8000")]
