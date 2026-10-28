#!/usr/bin/env python3
"""Replay a Mooncake FAST'25 trace against a fleet of vLLM or SGLang engines.

Each trace line has ``timestamp`` (ms), ``input_length``, ``output_length`` and
``hash_ids`` (one id per 512-token prefix block). Every hash id is expanded into
512 deterministic token ids (a PRNG seeded by the id), so requests that share
hash ids share exactly the same token prefix. Prompts are sent as token ids, not
text: no tokenizer is needed and the prefix structure is preserved exactly. The
tokens are synthetic and hold no user data; they are never written to the output.

Routing:

- ``rr``: round-robin over the engines in arrival order.
- ``sticky``: session affinity. The trace has no session id. In the conversation
  trace every request starts with the same block (a shared system prompt), so the
  key is the first hash id that fewer than ``--sticky-shared-frac`` of all requests
  hold (normally the first block of the conversation history). All turns of one
  conversation share that block, so they reach the same engine.

Standard library only. Example::

    python examples/mooncake_replay.py replay conversation_trace.jsonl \\
        --url http://127.0.0.1:8000 --url http://127.0.0.1:8001 \\
        --model Qwen/Qwen2.5-7B-Instruct --time-scale 1.0 --out run/
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import random
import sys
import time
import zlib
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

BLOCK_TOKENS = 512
VOCAB_LO = 1000
VOCAB_HI = 30000


@dataclass
class Request:
    index: int
    timestamp_ms: int
    input_length: int
    output_length: int
    hash_ids: List[int]


def load_trace(path: str) -> List[Request]:
    opener = gzip.open if path.endswith(".gz") else open
    out: List[Request] = []
    with opener(path, "rt") as f:
        for i, line in enumerate(f):
            if not line.strip():
                continue
            d = json.loads(line)
            out.append(
                Request(
                    i,
                    int(d["timestamp"]),
                    int(d["input_length"]),
                    int(d["output_length"]),
                    [int(h) for h in d["hash_ids"]],
                )
            )
    return out


def select(
    requests: Sequence[Request],
    max_model_len: int,
    limit: Optional[int] = None,
    max_seconds: Optional[float] = None,
) -> Tuple[List[Request], int]:
    """Drop requests whose prompt plus output does not fit, then cut to the first
    ``limit`` requests and to arrivals before ``max_seconds`` (trace time).
    Returns the kept requests and the number dropped for length."""
    kept = [r for r in requests if r.input_length + r.output_length <= max_model_len]
    dropped = len(requests) - len(kept)
    if max_seconds is not None:
        kept = [r for r in kept if r.timestamp_ms < max_seconds * 1000]
    if limit is not None:
        kept = kept[:limit]
    return kept, dropped


def block_tokens(hash_id: int, n: int = BLOCK_TOKENS, lo: int = VOCAB_LO, hi: int = VOCAB_HI) -> List[int]:
    rng = random.Random(hash_id)
    span = hi - lo
    return [lo + int(rng.random() * span) for _ in range(n)]


def prompt_tokens(req: Request, block: int = BLOCK_TOKENS) -> List[int]:
    toks: List[int] = []
    for h in req.hash_ids:
        need = req.input_length - len(toks)
        if need <= 0:
            break
        toks.extend(block_tokens(h, min(block, need)))
    return toks


def sticky_keys(requests: Sequence[Request], shared_frac: float) -> List[int]:
    """Per request, the first hash id held by fewer than ``shared_frac`` of the requests."""
    freq: Counter = Counter()
    for r in requests:
        freq.update(set(r.hash_ids))
    limit = shared_frac * len(requests)
    keys = []
    for r in requests:
        key = next((h for h in r.hash_ids if freq[h] < limit), r.hash_ids[-1] if r.hash_ids else r.index)
        keys.append(key)
    return keys


def route(requests: Sequence[Request], n_engines: int, policy: str, shared_frac: float = 0.05) -> List[int]:
    if policy == "rr":
        return [i % n_engines for i in range(len(requests))]
    if policy == "sticky":
        return [zlib.crc32(str(k).encode()) % n_engines for k in sticky_keys(requests, shared_frac)]
    raise ValueError(f"unknown routing policy {policy!r}")


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(p / 100.0 * (len(s) - 1)))))
    return s[k]


# --- minimal asyncio HTTP/1.1 client with chunked streaming -------------------------------------


async def _read_body_chunks(reader: asyncio.StreamReader, headers: Dict[str, str]):
    if headers.get("transfer-encoding", "").lower() == "chunked":
        while True:
            size_line = await reader.readline()
            if not size_line:
                return
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                await reader.readline()
                return
            data = await reader.readexactly(size)
            await reader.readexactly(2)
            yield data
    elif "content-length" in headers:
        yield await reader.readexactly(int(headers["content-length"]))
    else:
        while True:
            data = await reader.read(65536)
            if not data:
                return
            yield data


async def post_stream(url: str, path: str, payload: Dict[str, Any], timeout: float):
    """POST JSON and yield ``(status, raw_chunk)``; one connection per request."""
    parts = urlsplit(url)
    host, port = parts.hostname or "127.0.0.1", parts.port or 80
    body = json.dumps(payload, separators=(",", ":")).encode()
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, limit=1 << 22), timeout)
    try:
        writer.write(
            f"POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\n"
            f"Accept: text/event-stream\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout)
        status = int(status_line.split()[1]) if status_line else 0
        headers: Dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout)
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()
        async for chunk in _read_body_chunks(reader, headers):
            yield status, chunk
    finally:
        writer.close()


def build_payload(api: str, model: str, tokens: List[int], max_tokens: int) -> Tuple[str, Dict[str, Any]]:
    if api == "completions":
        return "/v1/completions", {
            "model": model,
            "prompt": tokens,
            "max_tokens": max_tokens,
            "temperature": 0,
            "ignore_eos": True,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    if api == "generate":
        return "/generate", {
            "input_ids": tokens,
            "stream": True,
            "sampling_params": {"max_new_tokens": max_tokens, "temperature": 0, "ignore_eos": True},
        }
    raise ValueError(f"unknown api {api!r}")


class SSEParser:
    """Split an SSE byte stream into the JSON objects of its ``data:`` lines."""

    def __init__(self) -> None:
        self.buf = b""

    def feed(self, chunk: bytes) -> List[Any]:
        self.buf += chunk
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                continue
            try:
                out.append(json.loads(data))
            except ValueError:
                continue
        return out


def _token_progress(api: str, event: Dict[str, Any]) -> Tuple[bool, Optional[int], Dict[str, Any]]:
    """Return (has_output, completion_tokens_so_far, usage) for one stream event."""
    if api == "completions":
        usage = event.get("usage") or {}
        # A choice chunk is only sent after a token is generated (its text can be empty
        # while a multi-byte character is incomplete), so any choice marks first output.
        return bool(event.get("choices")), usage.get("completion_tokens"), usage
    meta = event.get("meta_info") or {}
    has = bool(meta.get("completion_tokens"))
    return has, meta.get("completion_tokens"), meta


async def send_one(api: str, url: str, model: str, req: Request, timeout: float) -> Dict[str, Any]:
    tokens = prompt_tokens(req)
    path, payload = build_payload(api, model, tokens, req.output_length)
    del tokens
    t0 = time.monotonic()
    res: Dict[str, Any] = {
        "ttft": None,
        "e2e": None,
        "out_tokens": None,
        "prompt_tokens": None,
        "cached_tokens": None,
        "error": None,
    }
    parser = SSEParser()
    err_body = b""
    try:
        async for status, chunk in post_stream(url, path, payload, timeout):
            if status != 200:
                err_body += chunk
                continue
            for ev in parser.feed(chunk):
                if isinstance(ev, dict) and "error" in ev and not ev.get("choices"):
                    res["error"] = str(ev["error"])[:300]
                has, ntok, usage = _token_progress(api, ev)
                if has and res["ttft"] is None:
                    res["ttft"] = time.monotonic() - t0
                if ntok is not None:
                    res["out_tokens"] = ntok
                if usage.get("prompt_tokens") is not None:
                    res["prompt_tokens"] = usage["prompt_tokens"]
                cached = (usage.get("prompt_tokens_details") or {}).get(
                    "cached_tokens", usage.get("cached_tokens")
                )
                if cached is not None:
                    res["cached_tokens"] = cached
        if err_body:
            res["error"] = err_body.decode("utf-8", "replace")[:300]
        elif res["ttft"] is None and res["error"] is None:
            res["error"] = "no output"
    except Exception as exc:  # noqa: BLE001 - every failure is recorded per request
        res["error"] = f"{type(exc).__name__}: {exc}"[:300]
    res["e2e"] = time.monotonic() - t0
    return res


async def replay(
    requests: Sequence[Request],
    urls: Sequence[str],
    engines: Sequence[int],
    *,
    api: str,
    model: str,
    time_scale: float,
    timeout: float,
    out,
    progress_every: float = 30.0,
    max_lag_abort: Optional[float] = None,
) -> Dict[str, Any]:
    """Send every request at ``timestamp / time_scale`` after the start and write one JSON line each."""
    start = time.monotonic()
    t_first = requests[0].timestamp_ms if requests else 0
    results: List[Dict[str, Any]] = []
    state = {"max_lag": 0.0, "inflight": 0, "max_inflight": 0, "aborted": False}

    async def run(req: Request, eng: int, sched: float) -> None:
        sent = time.monotonic() - start
        state["inflight"] += 1
        state["max_inflight"] = max(state["max_inflight"], state["inflight"])
        try:
            r = await send_one(api, urls[eng], model, req, timeout)
        finally:
            state["inflight"] -= 1
        row = {
            "idx": req.index,
            "arrival": round(sched, 4),
            "sent": round(sent, 4),
            "engine": eng,
            "input_len": req.input_length,
            "output_len": req.output_length,
            **r,
        }
        results.append(row)
        out.write(json.dumps(row) + "\n")

    async def reporter() -> None:
        while True:
            await asyncio.sleep(progress_every)
            done = [r for r in results if r["ttft"] is not None]
            recent = [r["ttft"] for r in done[-200:]]
            print(
                f"[{time.monotonic() - start:7.0f}s] done {len(results)}/{len(requests)} "
                f"inflight {state['inflight']} lag {state['max_lag']:.1f}s "
                f"ttft_p50(last200) {percentile(recent, 50) or 0:.2f}s",
                file=sys.stderr,
                flush=True,
            )

    tasks = []
    rep = asyncio.ensure_future(reporter())
    for req, eng in zip(requests, engines):
        sched = (req.timestamp_ms - t_first) / 1000.0 / time_scale
        delay = sched - (time.monotonic() - start)
        if delay > 0:
            await asyncio.sleep(delay)
        lag = (time.monotonic() - start) - sched
        state["max_lag"] = max(state["max_lag"], lag)
        if max_lag_abort is not None and lag > max_lag_abort:
            state["aborted"] = True
            break
        tasks.append(asyncio.ensure_future(run(req, eng, sched)))
    if tasks:
        await asyncio.gather(*tasks)
    rep.cancel()
    wall = time.monotonic() - start
    return summarize(results, wall, state["max_lag"], state["max_inflight"], aborted=state["aborted"])


def summarize(
    results: Sequence[Dict[str, Any]],
    wall: float,
    max_lag: float,
    max_inflight: int = 0,
    aborted: bool = False,
) -> Dict[str, Any]:
    ok = [r for r in results if not r.get("error")]
    ttft = [r["ttft"] for r in ok if r.get("ttft") is not None]
    e2e = [r["e2e"] for r in ok if r.get("e2e") is not None]
    prompt = sum(r["input_len"] for r in ok)
    output = sum(r.get("out_tokens") or r["output_len"] for r in ok)
    cached = [r["cached_tokens"] for r in ok if r.get("cached_tokens") is not None]
    per_min: Dict[int, List[float]] = {}
    for r in ok:
        if r.get("ttft") is not None:
            per_min.setdefault(int(r["arrival"] // 60), []).append(r["ttft"])
    errors = Counter((r.get("error") or "")[:80] for r in results if r.get("error"))
    return {
        "requests": len(results),
        "completed": len(ok),
        "errors": len(results) - len(ok),
        "error_kinds": dict(errors.most_common(5)),
        "aborted_for_lag": aborted,
        "wall_seconds": round(wall, 1),
        "max_lag_seconds": round(max_lag, 3),
        "max_inflight": max_inflight,
        "ttft_p50": percentile(ttft, 50),
        "ttft_p90": percentile(ttft, 90),
        "ttft_p99": percentile(ttft, 99),
        "ttft_mean": sum(ttft) / len(ttft) if ttft else None,
        "e2e_p50": percentile(e2e, 50),
        "e2e_p99": percentile(e2e, 99),
        "prompt_tokens": prompt,
        "output_tokens": output,
        "prompt_tok_per_s": prompt / wall if wall else None,
        "output_tok_per_s": output / wall if wall else None,
        "cached_tokens": sum(cached) if cached else None,
        "ttft_p50_per_minute": {str(k): round(percentile(v, 50), 3) for k, v in sorted(per_min.items())},
    }


async def prefill_bench(
    url: str, api: str, model: str, prompt_len: int, n: int, timeout: float, seed: int = 1 << 40
) -> Dict[str, Any]:
    """Unloaded prefill throughput: ``n`` sequential unique prompts with one output token."""
    times = []
    for i in range(n):
        req = Request(
            -1, 0, prompt_len, 1, [seed + i * 1000 + b for b in range(-(-prompt_len // BLOCK_TOKENS))]
        )
        r = await send_one(api, url, model, req, timeout)
        if r["error"]:
            raise RuntimeError(r["error"])
        times.append(r["e2e"])
    warm = times[1:] or times
    mean = sum(warm) / len(warm)
    return {"prompt_len": prompt_len, "n": n, "seconds": times, "prefill_tok_per_s": prompt_len / mean}


def _parse(argv: Optional[Iterable[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay", help="replay a trace against the engines")
    r.add_argument("trace")
    r.add_argument("--url", action="append", required=True, help="engine base URL; repeat per engine")
    r.add_argument("--model", required=True)
    r.add_argument("--api", choices=["completions", "generate"], default="completions")
    r.add_argument("--routing", choices=["rr", "sticky"], default="rr")
    r.add_argument("--sticky-shared-frac", type=float, default=0.05)
    r.add_argument(
        "--time-scale", type=float, default=1.0, help="arrival = timestamp / scale (1.0 = real time)"
    )
    r.add_argument("--max-model-len", type=int, default=32768)
    r.add_argument("--limit", type=int, help="first M requests after the length filter")
    r.add_argument("--max-seconds", type=float, help="only arrivals before this trace time")
    r.add_argument("--max-lag-abort", type=float, help="stop sending when this far behind schedule (s)")
    r.add_argument("--timeout", type=float, default=1800.0)
    r.add_argument("--out", required=True, help="output directory")
    b = sub.add_parser("prefill-bench", help="measure unloaded prefill tokens/s on one engine")
    b.add_argument("--url", required=True)
    b.add_argument("--model", required=True)
    b.add_argument("--api", choices=["completions", "generate"], default="completions")
    b.add_argument("--prompt-len", type=int, default=8192)
    b.add_argument("-n", type=int, default=6)
    b.add_argument("--timeout", type=float, default=600.0)
    return p.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    import os

    a = _parse(argv)
    if a.cmd == "prefill-bench":
        res = asyncio.run(prefill_bench(a.url, a.api, a.model, a.prompt_len, a.n, a.timeout))
        print(json.dumps(res, indent=2))
        return 0
    reqs, dropped = select(load_trace(a.trace), a.max_model_len, a.limit, a.max_seconds)
    engines = route(reqs, len(a.url), a.routing, a.sticky_shared_frac)
    os.makedirs(a.out, exist_ok=True)
    with gzip.open(os.path.join(a.out, "requests.jsonl.gz"), "wt") as out:
        summary = asyncio.run(
            replay(
                reqs,
                a.url,
                engines,
                api=a.api,
                model=a.model,
                time_scale=a.time_scale,
                timeout=a.timeout,
                out=out,
                max_lag_abort=a.max_lag_abort,
            )
        )
    summary.update(
        {
            "trace": a.trace,
            "routing": a.routing,
            "time_scale": a.time_scale,
            "engines": len(a.url),
            "dropped_too_long": dropped,
            "selected": len(reqs),
            "per_engine_requests": dict(sorted(Counter(engines).items())),
        }
    )
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != "ttft_p50_per_minute"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
