"""Tests for the Mooncake trace replayer and ground-truth helpers in examples/."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from collections import Counter

import pytest

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
sys.path.insert(0, EXAMPLES)
sys.path.insert(0, os.path.join(EXAMPLES, "scale_test"))

import collect  # noqa: E402
import ground_truth as gt  # noqa: E402
import mooncake_replay as mr  # noqa: E402


def req(i, ts, inp, out, hashes):
    return mr.Request(i, ts, inp, out, hashes)


def test_block_tokens_deterministic_and_in_vocab():
    a = mr.block_tokens(42)
    assert a == mr.block_tokens(42)
    assert a != mr.block_tokens(43)
    assert len(a) == mr.BLOCK_TOKENS
    assert all(mr.VOCAB_LO <= t < mr.VOCAB_HI for t in a)
    assert mr.block_tokens(42, 100) == a[:100]


def test_prompt_tokens_share_prefix_and_truncate_last_block():
    r1 = req(0, 0, 1000, 5, [7, 8])
    r2 = req(1, 0, 1100, 5, [7, 9, 10])
    p1, p2 = mr.prompt_tokens(r1), mr.prompt_tokens(r2)
    assert len(p1) == 1000 and len(p2) == 1100
    assert p1[:512] == p2[:512]
    assert p1[512:] == mr.block_tokens(8, 488)
    assert p1[512:600] != p2[512:600]


def test_select_drops_too_long_then_limits():
    reqs = [
        req(0, 0, 100, 10, [1]),
        req(1, 10, 32760, 10, [2]),
        req(2, 20, 200, 10, [3]),
        req(3, 3000, 200, 10, [4]),
    ]
    kept, dropped = mr.select(reqs, 32768)
    assert dropped == 1 and [r.index for r in kept] == [0, 2, 3]
    kept, _ = mr.select(reqs, 32768, limit=1)
    assert [r.index for r in kept] == [0]
    kept, _ = mr.select(reqs, 32768, max_seconds=1.0)
    assert [r.index for r in kept] == [0, 2]


def test_route_rr_and_sticky_skips_shared_first_block():
    reqs = [req(i, i, 1500, 1, [0, 100 + i % 3, 200 + i]) for i in range(30)]
    assert mr.route(reqs, 4, "rr") == [i % 4 for i in range(30)]
    keys = mr.sticky_keys(reqs, 0.5)
    assert keys == [100 + i % 3 for i in range(30)]
    engines = mr.route(reqs, 8, "sticky", 0.5)
    by_conv = {}
    for i, e in enumerate(engines):
        by_conv.setdefault(i % 3, set()).add(e)
    assert all(len(v) == 1 for v in by_conv.values())
    with pytest.raises(ValueError):
        mr.route(reqs, 2, "random")


def test_sticky_key_falls_back_to_last_block():
    reqs = [req(i, i, 100, 1, [0]) for i in range(5)]
    assert mr.sticky_keys(reqs, 0.5) == [0] * 5


def test_sse_parser_splits_across_chunks():
    p = mr.SSEParser()
    assert p.feed(b'data: {"a": 1}\n\ndata: {"b"') == [{"a": 1}]
    assert p.feed(b": 2}\n\ndata: [DONE]\n\n") == [{"b": 2}]


def test_percentile_and_summary():
    assert mr.percentile([], 50) is None
    assert mr.percentile([1, 2, 3, 4, 5], 50) == 3
    rows = [
        {
            "arrival": 1,
            "input_len": 100,
            "output_len": 10,
            "ttft": 0.5,
            "e2e": 1.0,
            "out_tokens": 10,
            "cached_tokens": 64,
            "error": None,
        },
        {"arrival": 70, "input_len": 50, "output_len": 5, "ttft": None, "e2e": 0.1, "error": "boom"},
    ]
    s = mr.summarize(rows, 10.0, 0.25)
    assert s["completed"] == 1 and s["errors"] == 1
    assert s["prompt_tok_per_s"] == 10.0 and s["output_tok_per_s"] == 1.0
    assert s["cached_tokens"] == 64 and s["ttft_p50_per_minute"] == {"0": 0.5}


def test_ground_truth_prefix_matching():
    reqs = [
        req(0, 0, 1024, 1, [1, 2]),
        req(1, 1, 1300, 1, [1, 2, 3]),
        req(2, 2, 1100, 1, [1, 4, 3]),
        req(3, 3, 300, 1, [1]),
    ]
    res = gt.ground_truth(reqs)
    # r1 reuses 2 blocks, r2 reuses block 1 only (block 4 is new), r3 reuses its partial block 1.
    assert res["reusable_tokens"] == 1024 + 512 + 300
    assert res["requests_with_reuse"] == 3
    assert gt.ground_truth(reqs, chunk=256)["reusable_tokens"] == 1024 + 512 + 256


class FakeEngine:
    """Local HTTP server that answers streaming completions in either API shape."""

    def __init__(self, api: str, fail: bool = False):
        self.api, self.fail = api, fail
        self.bodies = []

    async def handle(self, reader, writer):
        headers = {}
        await reader.readline()
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
            k, _, v = line.decode().partition(":")
            headers[k.strip().lower()] = v.strip()
        body = json.loads(await reader.readexactly(int(headers["content-length"])))
        self.bodies.append(body)
        if self.fail:
            msg = b'{"error":"bad"}'
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: %d\r\n\r\n%s" % (len(msg), msg))
            await writer.drain()
            writer.close()
            return
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
        )
        n = body.get("max_tokens") or body["sampling_params"]["max_new_tokens"]
        plen = len(body.get("prompt") or body.get("input_ids"))
        events = []
        for i in range(n):
            if self.api == "completions":
                events.append({"choices": [{"text": "x", "finish_reason": None}]})
            else:
                events.append(
                    {
                        "text": "x" * (i + 1),
                        "meta_info": {"completion_tokens": i + 1, "prompt_tokens": plen, "cached_tokens": 16},
                    }
                )
        if self.api == "completions":
            events.append(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": plen,
                        "completion_tokens": n,
                        "prompt_tokens_details": {"cached_tokens": 32},
                    },
                }
            )
        for ev in events:
            data = b"data: " + json.dumps(ev).encode() + b"\n\n"
            writer.write(b"%x\r\n%s\r\n" % (len(data), data))
            await writer.drain()
        done = b"data: [DONE]\n\n"
        writer.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(done), done))
        await writer.drain()
        writer.close()


async def _serve(engine):
    server = await asyncio.start_server(engine.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}"


@pytest.mark.parametrize("api,cached", [("completions", 32), ("generate", 16)])
def test_send_one_streams_and_records_usage(api, cached):
    async def go():
        eng = FakeEngine(api)
        server, url = await _serve(eng)
        async with server:
            r = await mr.send_one(api, url, "m", req(0, 0, 600, 3, [5, 6]), 10)
        return eng, r

    eng, r = asyncio.run(go())
    assert r["error"] is None and r["ttft"] is not None and r["e2e"] >= r["ttft"]
    assert r["out_tokens"] == 3 and r["prompt_tokens"] == 600 and r["cached_tokens"] == cached
    body = eng.bodies[0]
    if api == "completions":
        assert body["prompt"][:512] == mr.block_tokens(5) and body["ignore_eos"] and body["stream"]
    else:
        assert len(body["input_ids"]) == 600 and body["sampling_params"]["ignore_eos"]


def test_send_one_records_http_error():
    async def go():
        server, url = await _serve(FakeEngine("completions", fail=True))
        async with server:
            return await mr.send_one("completions", url, "m", req(0, 0, 10, 1, [1]), 10)

    r = asyncio.run(go())
    assert "bad" in r["error"]


def test_send_one_records_connection_error():
    r = asyncio.run(mr.send_one("completions", "http://127.0.0.1:9", "m", req(0, 0, 10, 1, [1]), 2))
    assert r["error"] and r["ttft"] is None


def test_replay_schedules_routes_and_writes_rows_without_tokens():
    async def go():
        engines = [FakeEngine("completions"), FakeEngine("completions")]
        servers = [await _serve(e) for e in engines]
        reqs = [req(i, i * 100, 520, 2, [0, 10 + i]) for i in range(6)]
        out = io.StringIO()
        summary = await mr.replay(
            reqs,
            [u for _, u in servers],
            mr.route(reqs, 2, "rr"),
            api="completions",
            model="m",
            time_scale=2.0,
            timeout=10,
            out=out,
        )
        for s, _ in servers:
            s.close()
        return engines, summary, out.getvalue()

    engines, summary, text = asyncio.run(go())
    rows = [json.loads(line) for line in text.splitlines()]
    assert summary["completed"] == 6 and summary["errors"] == 0
    assert Counter(r["engine"] for r in rows) == {0: 3, 1: 3}
    assert sorted(r["arrival"] for r in rows) == [0.0, 0.05, 0.1, 0.15, 0.2, 0.25]
    assert all("prompt" not in r and "tokens" not in r for r in rows)
    assert all(len(e.bodies) == 3 for e in engines)
    assert summary["wall_seconds"] >= 0.25


def test_replay_aborts_when_behind_schedule():
    async def go():
        reqs = [req(0, 0, 10, 1, [1]), req(1, 1, 10, 1, [2])]
        return await mr.replay(
            reqs,
            ["http://127.0.0.1:9"],
            [0, 0],
            api="completions",
            model="m",
            time_scale=1.0,
            timeout=1,
            out=io.StringIO(),
            max_lag_abort=-1.0,
        )

    s = asyncio.run(go())
    assert s["aborted_for_lag"] and s["requests"] == 0


def test_collect_sums_fleet_deltas_and_valkey(tmp_path):
    m = tmp_path / "metrics"
    m.mkdir()
    for k, (b, a) in enumerate([(10, 30), (0, 5)]):
        (m / f"gpu{k}-before.prom").write_text(
            f'# HELP x\nvllm:prefix_cache_hits_total{{engine="0"}} {b}\nvllm:other 1\n'
        )
        (m / f"gpu{k}-after.prom").write_text(
            f'vllm:prefix_cache_hits_total{{engine="0"}} {a}\n'
            f'vllm:time_to_first_token_seconds_bucket{{le="1"}} 9\nvllm:other 2\n'
        )
    (tmp_path / "valkey-before.txt").write_text("# Stats\r\nkeyspace_hits:5\r\nevicted_keys:0\r\n")
    (tmp_path / "valkey-after.txt").write_text("keyspace_hits:12\r\nevicted_keys:3\r\nused_memory:100\r\n")
    (tmp_path / "summary.json").write_text('{"completed": 2}')
    res = collect.collect(str(tmp_path))
    assert res["metrics_delta"] == {"vllm:prefix_cache_hits_total": 25.0}
    assert res["valkey"]["keyspace_hits"] == 7 and res["valkey"]["evicted_keys"] == 3
    assert res["valkey"]["used_memory"] == "100"
    assert res["replay"] == {"completed": 2}
