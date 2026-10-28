#!/usr/bin/env python3
"""Microbenchmarks for kvtrace's ``--contention on`` knobs on a vLLM + LMCache + Valkey stack.

Every benchmark sends token-id prompts to ``/v1/completions`` (as ``mooncake_replay.py``
does) and writes one JSON file of per-request rows to ``--out``. Prompts are synthetic
random token ids, unique per case and repeat; they are never written to disk.

Benchmarks (the engine mode is set by whoever starts vLLM, see ``run_knobs.sh``):

- ``ttft``: TTFT of unique prompts per length (B1 with plain vLLM, B2 with LMCache and an
  empty Valkey, the lookup-only arm with ``kv_role: kv_consumer``).
- ``e2e``: end-to-end time with ``max_tokens`` 1 and 64, TPOT, and the time until the
  request's chunks are all in Valkey (B3).
- ``hit``: warm Valkey with a prompt (or its first half), reset the GPU prefix cache, send it
  again (B4).
- ``load``: background stream of unique long prompts at a fixed request rate while probes
  (miss and hit) are sent at a fixed rate (B5).
- ``concurrency``: N hits in flight at once (B6).
- ``analyze``: fit the knobs from the result files.

Valkey is reached at ``--valkey`` with the password in ``VALKEY_PASSWORD``; it can run on the GPU
host or, for latency and contention numbers, on a separate cache node. Standard library only, except
``load --ext-writer glide``, which imports ``glide_sync`` (valkey-glide, as LMCache uses) in its
writer processes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import socket
import statistics
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from mooncake_replay import SSEParser, _token_progress, build_payload, post_stream  # noqa: E402

LENGTHS = (1024, 4096, 8192, 16384, 32768)
CHUNK_TOKENS = 256
VOCAB_LO, VOCAB_HI = 1000, 30000


def tokens(tag: str, n: int) -> List[int]:
    rng = random.Random(tag)
    return [rng.randrange(VOCAB_LO, VOCAB_HI) for _ in range(n)]


def quantile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def summarize(values: Sequence[float]) -> Dict[str, Any]:
    v = [x for x in values if x is not None]
    return {"n": len(v), "median": statistics.median(v) if v else None, "p90": quantile(v, 0.9)}


# --- Valkey: minimal RESP client ------------------------------------------------------------------


def resp(*args: Any) -> bytes:
    parts = [a if isinstance(a, bytes) else str(a).encode() for a in args]
    return b"*%d\r\n" % len(parts) + b"".join(b"$%d\r\n%s\r\n" % (len(p), p) for p in parts)


class Valkey:
    def __init__(self, url: str) -> None:
        parts = urlsplit(url)
        self.addr = (parts.hostname or "127.0.0.1", parts.port or 6379)
        self.password = os.environ.get("VALKEY_PASSWORD", "")

    def _read(self, f) -> Any:
        line = f.readline()
        kind, rest = line[:1], line[1:-2]
        if kind in (b"+", b":"):
            return int(rest) if kind == b":" else rest.decode()
        if kind == b"-":
            raise RuntimeError(rest.decode())
        if kind == b"$":
            n = int(rest)
            return None if n < 0 else f.read(n + 2)[:-2]
        if kind == b"*":
            return [self._read(f) for _ in range(int(rest))]
        raise RuntimeError(f"bad RESP reply {line[:40]!r}")

    def connect(self):
        """An authenticated (socket, reader) pair for repeated commands."""
        s = socket.create_connection(self.addr, timeout=30)
        f = s.makefile("rb")
        if self.password:
            s.sendall(resp("AUTH", self.password))
            self._read(f)
        return s, f

    def cmd(self, *args: Any) -> Any:
        s, f = self.connect()
        with s, f:
            s.sendall(resp(*args))
            return self._read(f)

    def info(self) -> Dict[str, str]:
        raw = self.cmd("INFO", "all")
        text = raw.decode() if isinstance(raw, bytes) else raw
        lines = (line for line in text.splitlines() if ":" in line and not line.startswith("#"))
        return dict(line.split(":", 1) for line in lines)

    def counters(self) -> Dict[str, float]:
        i = self.info()
        db = i.get("db0", "keys=0").split(",")[0].split("=")[1]
        return {"t": time.monotonic(), "in_bytes": float(i["total_net_input_bytes"]),
                "out_bytes": float(i["total_net_output_bytes"]), "used_memory": float(i["used_memory"]),
                "keys": float(db), "cpu_s": float(i["used_cpu_sys"]) + float(i["used_cpu_user"]),
                "evicted_keys": float(i["evicted_keys"])}


# --- engine client --------------------------------------------------------------------------------


class Engine:
    def __init__(self, url: str, model: str, timeout: float, cpu_clear_url: Optional[str] = None) -> None:
        self.url, self.model, self.timeout = url, model, timeout
        # LMCache internal API server of the TP worker that holds the CPU tier (DELETE /cache/clear).
        self.cpu_clear_url = cpu_clear_url

    async def send(self, toks: List[int], max_tokens: int = 1) -> Dict[str, Any]:
        path, payload = build_payload("completions", self.model, toks, max_tokens)
        t0 = time.monotonic()
        res: Dict[str, Any] = {"t0": t0, "ttft": None, "e2e": None, "prompt_tokens": None,
                               "cached_tokens": None, "out_tokens": None, "error": None}
        parser = SSEParser()
        try:
            async for status, chunk in post_stream(self.url, path, payload, self.timeout):
                if status != 200:
                    res["error"] = chunk.decode("utf-8", "replace")[:300]
                    continue
                for ev in parser.feed(chunk):
                    has, ntok, usage = _token_progress("completions", ev)
                    if has and res["ttft"] is None:
                        res["ttft"] = time.monotonic() - t0
                    if ntok is not None:
                        res["out_tokens"] = ntok
                    if usage.get("prompt_tokens") is not None:
                        res["prompt_tokens"] = usage["prompt_tokens"]
                        res["cached_tokens"] = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        except Exception as exc:  # noqa: BLE001 - recorded per request
            res["error"] = f"{type(exc).__name__}: {exc}"[:300]
        res["e2e"] = time.monotonic() - t0
        return res

    @staticmethod
    async def _http_ok(url: str, method: str, path: str) -> bool:
        parts = urlsplit(url)
        reader, writer = await asyncio.open_connection(parts.hostname, parts.port or 80)
        try:
            writer.write(f"{method} {path} HTTP/1.1\r\nHost: {parts.hostname}\r\n"
                         "Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            status = (await reader.readline()).split()
            await reader.read()
            return len(status) > 1 and status[1] == b"200"
        finally:
            writer.close()

    async def reset_prefix_cache(self) -> bool:
        return await self._http_ok(self.url, "POST", "/reset_prefix_cache")

    async def clear_cpu_tier(self) -> None:
        if not await self._http_ok(self.cpu_clear_url, "DELETE", "/cache/clear?locations=LocalCPUBackend"):
            raise RuntimeError("LMCache /cache/clear failed")

    async def reset(self, valkey: Optional[Valkey], tries: int = 50, cpu: bool = True) -> None:
        """Reset the GPU prefix cache, the CPU tier (with ``cpu_clear_url`` and ``cpu``) and Valkey."""
        for _ in range(tries):
            if await self.reset_prefix_cache():
                break
            await asyncio.sleep(0.2)
        else:
            raise RuntimeError("reset_prefix_cache kept failing")
        if cpu and self.cpu_clear_url:
            await self.clear_cpu_tier()
        if valkey is not None:
            valkey.cmd("FLUSHALL", "SYNC")


class ExternalWriter:
    """Threads that SET chunk-sized values into Valkey at a fixed total GB/s, outside vLLM.

    Each thread keeps at most ``keep`` of its own keys (it deletes the key it wrote
    ``keep`` steps earlier), so the stream does not evict the probes' chunks.
    """

    def __init__(self, valkey: Valkey, gbps: float, threads: int, value_bytes: int, keep: int = 16) -> None:
        self.valkey, self.threads, self.keep = valkey, threads, keep
        self.value = os.urandom(value_bytes)
        self.per_thread = gbps * 1e9 / threads / value_bytes  # SETs per second per thread
        self.stop = threading.Event()
        self.sent = [0] * threads
        self.workers = [threading.Thread(target=self._run, args=(i,), daemon=True) for i in range(threads)]

    def _run(self, idx: int) -> None:
        s, f = self.valkey.connect()
        with s, f:
            t0, n = time.monotonic(), 0
            while not self.stop.is_set():
                s.sendall(resp(b"SET", b"knobs-ext-%d-%d" % (idx, n), self.value))
                self.valkey._read(f)
                if n >= self.keep:
                    s.sendall(resp(b"DEL", b"knobs-ext-%d-%d" % (idx, n - self.keep)))
                    self.valkey._read(f)
                n += 1
                self.sent[idx] = n
                delay = t0 + n / self.per_thread - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

    def __enter__(self) -> ExternalWriter:
        for w in self.workers:
            w.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        for w in self.workers:
            w.join(timeout=30)


def _glide_writer_proc(idx: int, host: str, port: int, password: str, threads: int, value_bytes: int,
                      per_thread: float, keep: int, stop, sent, results) -> None:
    """One writer process, like one LMCache TP worker: ``threads`` threads, one GLIDE sync client each."""
    import glide_sync  # type: ignore[import-untyped]  # only the glide writer needs it

    value = memoryview(bytearray(os.urandom(value_bytes)))
    lat: List[float] = []
    errors, failed = [0], [0]

    def run(t: int) -> None:
        cfg: Dict[str, Any] = {"addresses": [glide_sync.NodeAddress(host, port)], "request_timeout": 10000}
        if password:
            cfg["credentials"] = glide_sync.ServerCredentials(password=password)
        try:
            client = glide_sync.GlideClient.create(glide_sync.GlideClientConfiguration(**cfg))
        except Exception:  # noqa: BLE001 - reported as a failed thread
            failed[0] += 1
            return
        slot = idx * threads + t
        t0, n, ok = time.monotonic(), 0, 0
        while not stop.is_set():
            # Overwrite a ring of ``keep`` keys so the stream does not evict the probes' chunks.
            ts = time.monotonic()
            try:
                client.set(b"knobs-glide-%d-%d" % (slot, n % keep), value)
                ok += 1
                sent[slot] = ok  # only SETs that succeeded count as bytes written
            except Exception:  # noqa: BLE001 - counted, the stream goes on
                errors[0] += 1
            if len(lat) < 50000:
                lat.append(time.monotonic() - ts)
            n += 1
            delay = t0 + n / per_thread - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        client.close()

    workers = [threading.Thread(target=run, args=(t,), daemon=True) for t in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    results.put({"proc": idx, "cpu_s": time.process_time(), "errors": errors[0],
                 "failed_threads": failed[0], "set_s": summarize(lat)})


class GlideWriter:
    """Processes x threads of valkey-glide sync clients that SET values at a fixed total GB/s.

    This is LMCache's own put path (``ValkeyWorkerPool``: per TP worker process, N threads with
    one ``GlideClient`` each, ``client.set(key, memoryview)``) without the engine. Processes,
    not threads, so 8 writers stand for 8 TP worker processes and are not held by one GIL.
    ``cpu_s`` is the writers' own CPU time (GLIDE's runtime threads included).
    """

    def __init__(self, valkey: Valkey, gbps: float, procs: int, threads: int, value_bytes: int,
                 keep: int = 16) -> None:
        import multiprocessing

        import glide_sync  # type: ignore[import-untyped]  # noqa: F401 - fail here, not in every child

        ctx = multiprocessing.get_context("spawn")
        self.value_bytes = value_bytes
        self.stop = ctx.Event()
        self.sent = ctx.Array("q", procs * threads, lock=False)
        self.results = ctx.Queue()
        per_thread = gbps * 1e9 / (procs * threads) / value_bytes
        host, port = valkey.addr
        self.procs = [ctx.Process(target=_glide_writer_proc, daemon=True,
                                  args=(i, host, port, valkey.password, threads, value_bytes, per_thread,
                                        keep, self.stop, self.sent, self.results))
                      for i in range(procs)]
        self.stats: List[Dict[str, Any]] = []
        self.nprocs = procs

    def bytes_sent(self) -> int:
        return sum(self.sent) * self.value_bytes

    def __enter__(self) -> GlideWriter:
        for p in self.procs:
            p.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        for _ in self.procs:
            try:
                self.stats.append(self.results.get(timeout=60))
            except Exception:  # noqa: BLE001 - a lost result leaves cpu_s short, reported as missing
                break
        for p in self.procs:
            p.join(timeout=30)

    def summary(self) -> Dict[str, Any]:
        sets = [s["set_s"]["median"] for s in self.stats if s["set_s"]["median"] is not None]
        return {"procs": self.nprocs, "procs_reporting": len(self.stats),
                "cpu_s": sum(s["cpu_s"] for s in self.stats),
                "errors": sum(s["errors"] for s in self.stats),
                "failed_threads": sum(s.get("failed_threads", 0) for s in self.stats),
                "set_s_median_of_procs": statistics.median(sets) if sets else None,
                "set_s_p90_max": max((s["set_s"]["p90"] or 0.0) for s in self.stats) if self.stats else None}


def _default_iface() -> Optional[str]:
    with contextlib.suppress(OSError):
        with open("/proc/net/route") as fh:
            for line in fh.readlines()[1:]:
                f = line.split()
                if len(f) > 1 and f[1] == "00000000":
                    return f[0]
    return None


def _read_cpu() -> Dict[str, List[int]]:
    """/proc/stat jiffies per CPU: user nice system idle iowait irq softirq steal."""
    out = {}
    with open("/proc/stat") as fh:
        for line in fh:
            if line.startswith("cpu"):
                f = line.split()
                out[f[0]] = [int(x) for x in f[1:9]]
    return out


def _read_net(iface: str) -> Tuple[int, int]:
    with open("/proc/net/dev") as fh:
        for line in fh:
            name, _, rest = line.partition(":")
            if name.strip() == iface:
                f = rest.split()
                return int(f[0]), int(f[8])
    return 0, 0


class HostSampler:
    """Samples this host's CPU (all and per core), NIC bytes and, optionally, Valkey INFO each second.

    CPU shares are of all cores; ``busy_cores`` is the number of cores' worth of non-idle time.
    Valkey runs elsewhere in the off-box setup, so its INFO stands for the cache node's server.
    With ``gpu_pcie``, ``nvidia-smi dmon -s t`` adds PCIe rx/tx MB/s.
    """

    FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")

    def __init__(self, interval: float = 1.0, valkey: Optional[Valkey] = None,
                 gpu_pcie: bool = False) -> None:
        self.interval, self.valkey, self.gpu_pcie = interval, valkey, gpu_pcie
        self.iface = _default_iface()
        self.samples: List[Dict[str, Any]] = []
        self.pcie: List[Tuple[float, float, float]] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.dmon = None

    def _valkey_info(self) -> Dict[str, float]:
        i = self.valkey.info()
        keys = ("instantaneous_ops_per_sec", "instantaneous_input_kbps", "instantaneous_output_kbps",
                "evicted_keys", "used_cpu_sys", "used_cpu_user", "used_memory")
        return {k: float(i[k]) for k in keys if k in i}

    def _run(self) -> None:
        prev_cpu, prev_t = _read_cpu(), time.monotonic()
        prev_net = _read_net(self.iface) if self.iface else (0, 0)
        prev_vk, prev_vk_t = None, prev_t
        if self.valkey:
            with contextlib.suppress(Exception):
                prev_vk = self._valkey_info()
        while not self.stop.wait(self.interval):
            cpu, t = _read_cpu(), time.monotonic()
            net = _read_net(self.iface) if self.iface else (0, 0)
            dt = t - prev_t
            s: Dict[str, Any] = {"t": t, "dt": dt, "rx_gbs": (net[0] - prev_net[0]) / dt / 1e9,
                                 "tx_gbs": (net[1] - prev_net[1]) / dt / 1e9}
            per_core = []
            for name, v in cpu.items():
                d = [a - b for a, b in zip(v, prev_cpu.get(name, v))]
                tot = sum(d) or 1
                if name == "cpu":
                    s.update({f: d[k] / tot for k, f in enumerate(self.FIELDS)})
                    s["busy_cores"] = (tot - d[3] - d[4]) / tot * (len(cpu) - 1)
                else:
                    per_core.append((tot - d[3] - d[4]) / tot)
            s["max_core_busy"] = max(per_core) if per_core else None
            s["cores_over_90pct"] = sum(1 for c in per_core if c > 0.9)
            if self.valkey:
                with contextlib.suppress(Exception):
                    vk = self._valkey_info()
                    if prev_vk is not None:
                        # A failed INFO leaves prev_vk older than one interval; divide by its real age.
                        vdt = t - prev_vk_t
                        keep = ("instantaneous", "used_memory")
                        s["valkey"] = {k: vk[k] for k in vk if k.startswith(keep)}
                        cpu_s = vk["used_cpu_sys"] + vk["used_cpu_user"]
                        prev_s = prev_vk["used_cpu_sys"] + prev_vk["used_cpu_user"]
                        s["valkey"]["cpu_cores"] = (cpu_s - prev_s) / vdt
                        s["valkey"]["evicted"] = vk["evicted_keys"] - prev_vk["evicted_keys"]
                    prev_vk, prev_vk_t = vk, t
            self.samples.append(s)
            prev_cpu, prev_net, prev_t = cpu, net, t

    def _read_dmon(self) -> None:
        for line in self.dmon.stdout:
            f = line.split()
            if len(f) >= 3 and f[0].isdigit():
                with contextlib.suppress(ValueError):
                    self.pcie.append((time.monotonic(), float(f[1]), float(f[2])))

    def __enter__(self) -> HostSampler:
        self.thread.start()
        if self.gpu_pcie:
            import subprocess

            with contextlib.suppress(OSError):
                self.dmon = subprocess.Popen(["nvidia-smi", "dmon", "-s", "t", "-d", "1"],
                                             stdout=subprocess.PIPE, text=True)
                threading.Thread(target=self._read_dmon, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.thread.join(timeout=10)
        if self.dmon:
            self.dmon.terminate()
            with contextlib.suppress(Exception):
                self.dmon.wait(timeout=5)

    def summary(self, skip_s: float = 5.0) -> Dict[str, Any]:
        """Means over the samples after the first ``skip_s`` seconds."""
        if not self.samples:
            return {}
        t0 = self.samples[0]["t"] - self.samples[0]["dt"]
        rows = [s for s in self.samples if s["t"] - t0 > skip_s] or self.samples

        def mean(key, src=rows):
            v = [r[key] for r in src if r.get(key) is not None]
            return sum(v) / len(v) if v else None

        out = {"iface": self.iface, "samples": len(rows),
               **{k: mean(k) for k in ("busy_cores", "user", "system", "softirq", "irq", "iowait",
                                       "rx_gbs", "tx_gbs", "max_core_busy", "cores_over_90pct")},
               "cores": os.cpu_count()}
        vk = [r["valkey"] for r in rows if r.get("valkey")]
        if vk:
            out["valkey"] = {k: mean(k, vk) for k in vk[0]}
            out["valkey"]["evicted"] = sum(r.get("evicted", 0) for r in vk)
        if self.pcie:
            # Mean of dmon lines: per GPU, not the host total when there are several GPUs.
            out["pcie_rx_mbs_per_gpu"] = sum(p[1] for p in self.pcie) / len(self.pcie)
            out["pcie_tx_mbs_per_gpu"] = sum(p[2] for p in self.pcie) / len(self.pcie)
        return out


async def wait_keys(valkey: Valkey, target: float, timeout: float = 60.0) -> Optional[float]:
    """Seconds until Valkey holds ``target`` keys (DBSIZE every 20 ms on one connection), or None."""
    t0 = time.monotonic()
    s, f = valkey.connect()
    with s, f:
        while time.monotonic() - t0 < timeout:
            s.sendall(resp("DBSIZE"))
            if valkey._read(f) >= target:
                return time.monotonic() - t0
            await asyncio.sleep(0.02)
    return None


# --- benchmarks -----------------------------------------------------------------------------------


async def bench_ttft(eng: Engine, valkey: Optional[Valkey], args) -> List[Dict[str, Any]]:
    rows = []
    for length in args.lengths:
        for rep in range(-1, args.repeats):  # rep -1 is a warmup, not reported
            await eng.reset(valkey)
            before = valkey.counters() if valkey else None
            r = await eng.send(tokens(f"{args.tag}-ttft-{length}-{rep}", length), 1)
            settle = await wait_keys(valkey, length // CHUNK_TOKENS * args.key_factor, 30) if valkey else None
            after = valkey.counters() if valkey else None
            rows.append({"bench": "ttft", "length": length, "rep": rep, **r, "save_done_s": settle,
                         "valkey_in_bytes": after["in_bytes"] - before["in_bytes"] if valkey else None,
                         "valkey_keys": after["keys"] if valkey else None})
    return rows


async def bench_e2e(eng: Engine, valkey: Optional[Valkey], args) -> List[Dict[str, Any]]:
    rows = []
    for length in args.lengths:
        for max_tokens in (1, 64):
            for rep in range(-1, args.repeats):
                await eng.reset(valkey)
                before = valkey.counters() if valkey else None
                r = await eng.send(tokens(f"{args.tag}-e2e-{length}-{max_tokens}-{rep}", length), max_tokens)
                done = (await wait_keys(valkey, length // CHUNK_TOKENS * args.key_factor, 30)
                        if valkey else None)
                after = valkey.counters() if valkey else None
                tpot = ((r["e2e"] - r["ttft"]) / (max_tokens - 1)
                        if max_tokens > 1 and r["ttft"] is not None else None)
                rows.append({"bench": "e2e", "length": length, "max_tokens": max_tokens, "rep": rep, **r,
                             "tpot": tpot, "save_done_s": done,
                             "valkey_in_bytes": after["in_bytes"] - before["in_bytes"] if valkey else None,
                             "valkey_cpu_s": after["cpu_s"] - before["cpu_s"] if valkey else None})
    return rows


async def bench_hit(eng: Engine, valkey: Valkey, args) -> List[Dict[str, Any]]:
    rows = []
    for length in args.lengths:
        for frac in (1.0, 0.5):
            for rep in range(-1, args.repeats):
                await eng.reset(valkey)
                full = tokens(f"{args.tag}-hit-{length}-{frac}-{rep}", length)
                warm = full[: int(length * frac) // CHUNK_TOKENS * CHUNK_TOKENS]
                w = await eng.send(warm, 1)
                stored = await wait_keys(valkey, len(warm) // CHUNK_TOKENS * args.key_factor, 30)
                await eng.reset(None)
                before = valkey.counters()
                r = await eng.send(full, 1)
                after = valkey.counters()
                rows.append({"bench": "hit", "length": length, "frac": frac, "rep": rep, **r,
                             "warm_ttft": w["ttft"], "warm_stored_s": stored,
                             "hit_tokens_expected": len(warm),
                             "valkey_out_bytes": after["out_bytes"] - before["out_bytes"]})
    return rows


async def bench_load(eng: Engine, valkey: Valkey, args) -> List[Dict[str, Any]]:
    """Background unique prompts at ``--bg-rate`` req/s; probes at ``--probe-rate`` req/s.

    With ``--probe-kinds`` the probes rotate over the listed kinds: ``miss`` (unique prompt),
    ``l2hit`` (warmed, then the GPU cache and the LMCache CPU tier are reset, so only Valkey
    has it) and ``cpuhit`` (warmed after that reset, then the GPU cache is reset, so the CPU
    tier has it).
    """
    await eng.reset(valkey)
    n_probe = int(args.duration * args.probe_rate) + 2
    kinds = args.probe_kinds
    hit_prompts = [tokens(f"{args.tag}-probehit-{i}", args.probe_length) for i in range(n_probe)]
    cpu_prompts: List[List[int]] = []
    if kinds is None and args.probe_kind in ("hit", "both"):
        for toks in hit_prompts:
            await eng.send(toks, 1)
        await wait_keys(valkey, n_probe * args.probe_length // CHUNK_TOKENS * args.key_factor, 60)
        await eng.reset(None)
    elif kinds is not None:
        per_kind = n_probe // len(kinds) + 2
        hit_prompts = hit_prompts[:per_kind] if "l2hit" in kinds else []
        for toks in hit_prompts:
            await eng.send(toks, 1)
        if hit_prompts:
            chunks = len(hit_prompts) * args.probe_length // CHUNK_TOKENS
            await wait_keys(valkey, chunks * args.key_factor, 60)
        await eng.reset(None)
        if "cpuhit" in kinds:
            cpu_prompts = [tokens(f"{args.tag}-probecpu-{i}", args.probe_length) for i in range(per_kind)]
            for toks in cpu_prompts:
                await eng.send(toks, 1)
            await eng.reset(None, cpu=False)
    rows: List[Dict[str, Any]] = []
    tasks = []
    start = time.monotonic()
    c0 = valkey.counters()

    async def one(kind: str, i: int, toks: List[int], max_tokens: int, intent: str = "") -> None:
        r = await eng.send(toks, max_tokens)
        rows.append({"bench": "load", "kind": kind, "intent": intent, "i": i, "bg_rate": args.bg_rate,
                     "length": len(toks), "sent": r["t0"] - start,
                     "cached_share": (r["cached_tokens"] or 0) / len(toks), **r})

    async def stream(kind: str, rate: float, make) -> None:
        if rate <= 0:
            return
        i = 0
        t0 = time.monotonic()
        while time.monotonic() - start < args.duration:
            tasks.append(asyncio.ensure_future(one(kind, i, *make(i))))
            i += 1
            await asyncio.sleep(max(0.0, t0 + i / rate - time.monotonic()))

    def bg(i):
        return tokens(f"{args.tag}-bg-{args.bg_rate}-{i}", args.bg_length), args.bg_max_tokens

    def probe(i):
        if kinds is not None:
            kind, j = kinds[i % len(kinds)], i // len(kinds)
            if kind == "l2hit":
                return hit_prompts[j % len(hit_prompts)], 1, kind
            if kind == "cpuhit":
                return cpu_prompts[j % len(cpu_prompts)], 1, kind
            return tokens(f"{args.tag}-probemiss-{args.bg_rate}-{i}", args.probe_length), 1, "miss"
        if args.probe_kind == "hit" or (args.probe_kind == "both" and i % 2):
            return hit_prompts[i % n_probe], 1, "hit"
        return tokens(f"{args.tag}-probemiss-{args.bg_rate}-{i}", args.probe_length), 1, "miss"

    async def probe_stream():
        await asyncio.sleep(args.probe_delay)
        await stream("probe", args.probe_rate, probe)

    async def closed_loop(worker: int) -> None:
        i = worker
        while time.monotonic() - start < args.duration:
            await one("bg", i, *bg(i))
            i += args.bg_closed

    background = ([closed_loop(w) for w in range(args.bg_closed)] if args.bg_closed
                  else [stream("bg", args.bg_rate, bg)])

    writer = None
    value_bytes = args.ext_value_bytes or CHUNK_TOKENS * args.kv_bytes_per_token
    if args.ext_write_gbps > 0 and args.ext_writer == "glide":
        writer = GlideWriter(valkey, args.ext_write_gbps, args.ext_procs, args.ext_threads, value_bytes)
    elif args.ext_write_gbps > 0:
        writer = ExternalWriter(valkey, args.ext_write_gbps, args.ext_threads, value_bytes)
    sampler = HostSampler(valkey=valkey, gpu_pcie=args.gpu_pcie) if args.host_metrics else None
    with sampler or contextlib.nullcontext(), writer or contextlib.nullcontext():
        await asyncio.gather(*background, probe_stream())
        await asyncio.gather(*tasks)
    c1 = valkey.counters()
    for r in rows:
        if r["kind"] == "probe":
            # An intended hit whose chunks LRU evicted before it ran is neither a hit nor a clean miss.
            if r["intent"] == "miss":
                r["kind"] = "probe_miss"
            else:
                suffix = "" if r["cached_share"] > 0.5 else "_evicted"
                r["kind"] = f"probe_{r['intent']}{suffix}"
    if isinstance(writer, GlideWriter):
        ext_bytes = writer.bytes_sent()
    else:
        ext_bytes = sum(writer.sent) * len(writer.value) if writer else 0
    rows.append({"bench": "load", "kind": "window", "bg_rate": args.bg_rate, "seconds": c1["t"] - c0["t"],
                 "ext_write_gbps": args.ext_write_gbps,
                 "ext_bytes": ext_bytes,
                 "ext_writer": args.ext_writer if writer else None, "ext_value_bytes": value_bytes,
                 "writer": writer.summary() if isinstance(writer, GlideWriter) else None,
                 "host": sampler.summary() if sampler else None,
                 "valkey_in_bytes": c1["in_bytes"] - c0["in_bytes"],
                 "valkey_out_bytes": c1["out_bytes"] - c0["out_bytes"],
                 "valkey_cpu_s": c1["cpu_s"] - c0["cpu_s"],
                 "valkey_evicted_keys": c1["evicted_keys"] - c0["evicted_keys"]})
    if sampler:
        rows.append({"bench": "load", "kind": "host_samples", "samples": sampler.samples,
                     "pcie": sampler.pcie})
    return rows


async def bench_concurrency(eng: Engine, valkey: Valkey, args) -> List[Dict[str, Any]]:
    rows = []
    length = args.probe_length
    for n in args.inflight:
        for rep in range(-1, args.repeats):
            await eng.reset(valkey)
            prompts = [tokens(f"{args.tag}-conc-{n}-{rep}-{i}", length) for i in range(n)]
            for p in prompts:
                await eng.send(p, 1)
            await wait_keys(valkey, n * length // CHUNK_TOKENS * args.key_factor, 60)
            await eng.reset(None)
            before = valkey.counters()
            t0 = time.monotonic()
            res = await asyncio.gather(*(eng.send(p, 1) for p in prompts))
            wall = time.monotonic() - t0
            after = valkey.counters()
            rows.append({"bench": "concurrency", "inflight": n, "rep": rep, "wall_s": wall,
                         "ttfts": [r["ttft"] for r in res], "cached": [r["cached_tokens"] for r in res],
                         "errors": [r["error"] for r in res if r["error"]],
                         "valkey_out_bytes": after["out_bytes"] - before["out_bytes"]})
    return rows


BENCHES = {"ttft": bench_ttft, "e2e": bench_e2e, "hit": bench_hit, "load": bench_load,
           "concurrency": bench_concurrency}


# --- analysis -------------------------------------------------------------------------------------


def linfit(xs: Sequence[float], ys: Sequence[float]) -> Dict[str, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx if sxx else 0.0
    icpt = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - icpt - slope * x) ** 2 for x, y in zip(xs, ys))
    return {"slope": slope, "intercept": icpt, "r2": 1 - ss_res / ss_tot if ss_tot else 1.0, "n": n}


def load_rows(path: str) -> List[Dict[str, Any]]:
    with open(path) as fh:
        return [r for r in json.load(fh)["rows"] if r.get("rep", 0) >= 0 and not r.get("error")]


def by_length(rows, key="ttft", **match) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, List[float]] = {}
    for r in rows:
        if all(r.get(k) == v for k, v in match.items()) and r.get(key) is not None:
            out.setdefault(r["length"], []).append(r[key])
    return {k: summarize(v) for k, v in sorted(out.items())}


def window_gbs(rows) -> float:
    w = next(r for r in rows if r.get("kind") == "window")
    return w["valkey_in_bytes"] / w["seconds"] / 1e9


def load_ttft(rows, kind: str) -> Dict[str, Any]:
    """Load-bench TTFT of one row kind, after the probe warmup."""
    return summarize([r["ttft"] for r in rows if r.get("kind") == kind and r.get("ttft") and r["sent"] > 5])


def stall_pair(off_rows, on_rows, kind: str) -> Optional[Dict[str, Any]]:
    """kvtrace's stall knob from one pair at equal load: s = 1 - T_off / T_on, X = 1000 s / R ms/GB."""
    t_off, t_on = load_ttft(off_rows, kind)["median"], load_ttft(on_rows, kind)["median"]
    rate = window_gbs(on_rows) - window_gbs(off_rows)
    if not t_off or not t_on or rate <= 0:
        return None
    s = 1 - t_off / t_on
    return {"ttft_off": t_off, "ttft_on": t_on, "write_gbs": rate, "share": s,
            "write_stall_ms_per_gb": 1000 * s / rate}


def analyze(args) -> Dict[str, Any]:
    d = args.results
    kvb = args.kv_bytes_per_token

    def path(name):
        p = os.path.join(d, name)
        return p if os.path.exists(p) else None

    if not path("b1_plain_ttft.json"):
        raise SystemExit(f"analyze needs {os.path.join(d, 'b1_plain_ttft.json')} (B1, plain vLLM)")
    res: Dict[str, Any] = {"kv_bytes_per_token": kvb}
    b1 = load_rows(path("b1_plain_ttft.json"))
    t1 = by_length(b1)
    res["b1"] = t1
    pts = [(L, s["median"]) for L, s in t1.items()]
    lin = linfit([p[0] for p in pts], [p[1] for p in pts])
    res["b1_linear_fit"] = lin
    res["prefill_tokens_per_sec_marginal"] = 1 / lin["slope"] if lin["slope"] > 0 else None
    res["prefill_tokens_per_sec_by_length"] = {L: L / s["median"] for L, s in t1.items()}
    # Quadratic: TTFT = a + L / P0 + L^2 / (2 D P0), least squares on the medians.
    xs1, xs2, ys = [p[0] for p in pts], [p[0] ** 2 for p in pts], [p[1] for p in pts]
    res["b1_quadratic_fit"] = quadfit(xs1, xs2, ys) if len(pts) >= 3 else None
    for arm in ("both", "consumer", "bothcpu", "cpuonly"):
        for tc in ("", "_tc"):
            p = path(f"b2_{arm}{tc}_ttft.json")
            if not p:
                continue
            t2 = by_length(load_rows(p))
            res[f"b2_{arm}{tc}"] = {L: {**s, "delta_median": s["median"] - t1[L]["median"]}
                                     for L, s in t2.items() if L in t1}
    if "b2_cpuonly" in res:
        # Off-box miss tax: the arm with Valkey minus the CPU-tier-only arm (the benchmark's ON - OFF).
        for arm in ("bothcpu", "consumer"):
            if f"b2_{arm}" in res:
                for L, s in res[f"b2_{arm}"].items():
                    if L in res["b2_cpuonly"]:
                        s["delta_vs_cpuonly"] = s["median"] - res["b2_cpuonly"][L]["median"]
    if "b2_consumer" in res:
        # Lookup = consumer minus plain, but they are separate server starts. A delta inside the B1 spread
        # at the shortest length, or below zero, is not a measurement: report only the upper bound.
        shortest = min(res["b2_consumer"])
        delta_ms = res["b2_consumer"][shortest]["delta_median"] * 1000
        spread_ms = (t1[shortest]["p90"] - t1[shortest]["median"]) * 1000
        res["l2_lookup_ms_upper_bound"] = max(abs(delta_ms), spread_ms)
        res["l2_lookup_ms_estimate"] = delta_ms if delta_ms > spread_ms else None
        lengths = sorted(res["b2_consumer"])
        deltas_ms = [res["b2_consumer"][L]["delta_median"] * 1000 for L in lengths]
        res["b2_consumer_delta_slope_ms_per_ktok"] = linfit([L / 1000 for L in lengths], deltas_ms)["slope"]
    for tc in ("", "_tc"):
        p = path(f"b3_both{tc}_e2e.json")
        if p:
            rows = load_rows(p)
            res[f"b3_both{tc}"] = {"ttft_mt1": by_length(rows, max_tokens=1),
                                   "tpot_mt64": by_length(rows, "tpot", max_tokens=64),
                                   "save_done_s": by_length(rows, "save_done_s", max_tokens=1),
                                   "valkey_in_bytes": by_length(rows, "valkey_in_bytes", max_tokens=1)}
            # Bytes written / time from the end of the response until Valkey holds every chunk. Chunks
            # saved during prefill are counted but their time is not, so this is an upper bound.
            gbs = []
            for r in rows:
                if r["max_tokens"] == 1 and r.get("save_done_s") is not None and r["valkey_in_bytes"]:
                    gbs.append(r["valkey_in_bytes"] / 1e9 / max(1e-3, r["save_done_s"]))
            res[f"b3_both{tc}"]["save_gb_per_s_after_response"] = summarize(gbs)
    p = path("b3_plain_e2e.json")
    if p:
        rows = load_rows(p)
        res["b3_plain"] = {"tpot_mt64": by_length(rows, "tpot", max_tokens=64),
                           "ttft_mt1": by_length(rows, max_tokens=1)}
    for arm in ("both", "both_tc", "bothcpu", "cpuonly"):
        p = path(f"b4_{arm}_hit.json")
        if not p:
            continue
        rows = load_rows(p)
        full = [r for r in rows if r["frac"] == 1.0]
        meds = by_length(full)
        hit_gb = {L: (L // CHUNK_TOKENS * CHUNK_TOKENS) * kvb / 1e9 for L in meds}
        fit = linfit([hit_gb[L] for L in meds], [meds[L]["median"] for L in meds])
        cached = by_length(full, "cached_tokens")
        half = by_length([r for r in rows if r["frac"] == 0.5])
        a_ms = res["b1_quadratic_fit"]["a_s"] * 1000 if res["b1_quadratic_fit"] else 0.0
        res[f"b4_{arm}"] = {"full": meds, "half": half, "cached_tokens": cached, "fit_ttft_vs_hit_gb": fit,
                            "fixed_ms": fit["intercept"] * 1000, "ms_per_gb": fit["slope"] * 1000,
                            "l2_rtt_ms": fit["intercept"] * 1000 - a_ms,
                            "effective_read_gbps": 8 / fit["slope"] if fit["slope"] > 0 else None,
                            "break_even": {L: {"hit": meds[L]["median"], "recompute": t1[L]["median"]}
                                           for L in meds if L in t1},
                            "break_even_tokens": break_even_tokens(fit, res["b1_quadratic_fit"], kvb)}
    stall: Dict[str, Any] = {}
    for off, on in (("consumer", "both"), ("cpuonly", "bothcpu")):
        for n in (1, 2, 4):
            po, pn = path(f"c_{off}_bg{n}.json"), path(f"c_{on}_bg{n}.json")
            if po and pn:
                stall[f"{on}_vs_{off}_bg{n}"] = stall_pair(load_rows(po), load_rows(pn), "bg")
    for tc in ("", "_tc"):
        base = path(f"b5_both{tc}_bg0.00.json")
        for name in sorted(os.listdir(d)):
            if base and name.startswith(f"b5_both{tc}_ext"):
                on_rows = load_rows(os.path.join(d, name))
                w = next(r for r in on_rows if r.get("kind") == "window")
                for kind in ("probe_miss", "probe_hit"):
                    pair = stall_pair(load_rows(base), on_rows, kind)
                    if pair:
                        pair.update(requested_ext_gbs=w["ext_write_gbps"],
                                    achieved_ext_gbs=w["ext_bytes"] / w["seconds"] / 1e9)
                    stall[f"{name[:-5]}_{kind}"] = pair
    res["write_stall"] = stall
    res["write_load"] = write_load(d)
    for arm in ("both", "both_tc", "bothcpu", "cpuonly"):
        p = path(f"b6_{arm}_concurrency.json")
        if p:
            rows = load_rows(p)
            res[f"b6_{arm}"] = {
                n: {"read_gbs": summarize([r["valkey_out_bytes"] / 1e9 / r["wall_s"] for r in rows
                                           if r["inflight"] == n]),
                    # Hit bytes by cached tokens: counts CPU-tier hits too, which Valkey never sees.
                    "hit_gbs": summarize([sum(c or 0 for c in r["cached"]) * kvb / 1e9 / r["wall_s"]
                                          for r in rows if r["inflight"] == n]),
                    "wall_s": summarize([r["wall_s"] for r in rows if r["inflight"] == n])}
                for n in sorted({r["inflight"] for r in rows})}
    return res


def break_even_tokens(fetch_fit: Dict[str, float], b1: Optional[Dict[str, float]], kvb: int,
                      max_tokens: int = 131072) -> Optional[Dict[str, Any]]:
    """Shortest full-hit prompt (256-token steps) whose fetch (fit line) is faster than recompute (B1 fit)."""
    if not b1:
        return None
    for n in range(CHUNK_TOKENS, max_tokens + 1, CHUNK_TOKENS):
        fetch = fetch_fit["intercept"] + fetch_fit["slope"] * n * kvb / 1e9
        recompute = b1["a_s"] + b1["b_s_per_tok"] * n + b1["c_s_per_tok2"] * n * n
        if fetch < recompute:
            return {"tokens": n, "fetch_s": fetch, "recompute_s": recompute}
    return {"tokens": None, "note": f"recompute is faster up to {max_tokens} tokens"}


def _load_point(path: str, rate: float) -> Dict[str, Any]:
    """One write-load window: achieved rate, host and writer cost, probe TTFT per kind."""
    rows = load_rows(path)
    w = next(r for r in rows if r.get("kind") == "window")
    host = w.get("host") or {}
    writer = w.get("writer") or {}
    pt = {"requested_gbs": rate, "achieved_gbs": w["ext_bytes"] / w["seconds"] / 1e9,
          "valkey_in_gbs": w["valkey_in_bytes"] / w["seconds"] / 1e9,
          "busy_cores": host.get("busy_cores"), "system": host.get("system"),
          "softirq": host.get("softirq"), "irq": host.get("irq"), "tx_gbs": host.get("tx_gbs"),
          "rx_gbs": host.get("rx_gbs"), "max_core_busy": host.get("max_core_busy"),
          "writer_cores": writer.get("cpu_s", 0.0) / w["seconds"] if writer else 0.0,
          "writer_set_s": writer.get("set_s_median_of_procs"),
          "valkey": host.get("valkey"),
          "pcie_rx_mbs_per_gpu": host.get("pcie_rx_mbs_per_gpu", host.get("pcie_rx_mbs")),
          "writer_errors": writer.get("errors", 0) + writer.get("failed_threads", 0),
          "writer_procs_missing": (writer.get("procs", writer.get("procs_reporting", 0))
                                   - writer.get("procs_reporting", 0)) if writer else 0,
          "valkey_evicted_keys": w["valkey_evicted_keys"], "probes": {}}
    for kind in sorted({r["kind"] for r in rows if str(r.get("kind", "")).startswith("probe_")}):
        pt["probes"][kind] = load_ttft(rows, kind)
    return pt


def write_load(d: str) -> Dict[str, Any]:
    """B5 with an external writer: files ``w_<mode>_<GB/s>.json``, rate 0 is the baseline.

    Per mode and probe kind: probe TTFT change against the achieved write rate, fit as ms per
    GB/s; and host busy cores against the rate, whose slope is core-seconds per GB written.
    Points whose writer lost SETs or processes are reported but not fitted. ``w0end_<mode>.json``
    (rate 0 again, after the others) gives the drift of the baseline over the run.
    """
    out: Dict[str, Any] = {}
    by_mode: Dict[str, List[Tuple[float, str]]] = {}
    for name in os.listdir(d):
        if name.startswith("w_") and name.endswith(".json"):
            mode, _, rate = name[2:-5].rpartition("_")
            with contextlib.suppress(ValueError):
                by_mode.setdefault(mode, []).append((float(rate), name))
    for mode, files in sorted(by_mode.items()):
        points = [_load_point(os.path.join(d, name), rate) for rate, name in sorted(files)]
        clean = [p for p in points if not p["writer_errors"] and not p["writer_procs_missing"]]
        base = next((p for p in points if p["requested_gbs"] == 0), None)
        fits: Dict[str, Any] = {}
        drift: Dict[str, Optional[float]] = {}
        if base:
            for kind, s in base["probes"].items():
                xs, ys = [], []
                for p in points:
                    m = p["probes"].get(kind, {}).get("median")
                    if m is not None and s["median"] is not None:
                        p["probes"][kind]["delta_ms"] = (m - s["median"]) * 1000
                        if p in clean:
                            xs.append(p["achieved_gbs"])
                            ys.append(p["probes"][kind]["delta_ms"])
                if len(xs) >= 2:
                    fits[f"{kind}_ms_per_gbps"] = linfit(xs, ys)
            for key in ("busy_cores", "writer_cores"):
                xs = [p["achieved_gbs"] for p in clean if p[key] is not None]
                ys = [p[key] for p in clean if p[key] is not None]
                if len(xs) >= 2:
                    fits[f"{key}_core_s_per_gb"] = linfit(xs, ys)
            end_path = os.path.join(d, f"w0end_{mode}.json")
            if os.path.exists(end_path):
                end = _load_point(end_path, 0.0)
                for kind, s in base["probes"].items():
                    m = end["probes"].get(kind, {}).get("median")
                    ok = m is not None and s["median"] is not None
                    drift[kind] = (m - s["median"]) * 1000 if ok else None
        out[mode] = {"points": points, "fits": fits, "baseline_drift_ms": drift}
    return out


def quadfit(x1, x2, y) -> Dict[str, float]:
    """Least squares y = a + b x + c x^2 (3x3 normal equations)."""
    n = len(y)
    cols = [[1.0] * n, list(x1), list(x2)]
    ata = [[sum(ci[k] * cj[k] for k in range(n)) for cj in cols] for ci in cols]
    aty = [sum(ci[k] * y[k] for k in range(n)) for ci in cols]
    m = [row[:] + [v] for row, v in zip(ata, aty)]
    for i in range(3):
        piv = max(range(i, 3), key=lambda r: abs(m[r][i]))
        m[i], m[piv] = m[piv], m[i]
        for r in range(3):
            if r != i and m[i][i]:
                f = m[r][i] / m[i][i]
                m[r] = [a - f * b for a, b in zip(m[r], m[i])]
    a, b, c = (m[i][3] / m[i][i] if m[i][i] else 0.0 for i in range(3))
    p0 = 1 / b if b > 0 else None
    return {"a_s": a, "b_s_per_tok": b, "c_s_per_tok2": c, "P0": p0,
            "D_tokens": (b / (2 * c)) if c > 0 else None}


# --- CLI ------------------------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("bench", choices=sorted(BENCHES) + ["analyze"])
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--model", default="Qwen/Qwen3-8B")
    ap.add_argument("--valkey", default=None, help="valkey://127.0.0.1:6379 (omit for plain vLLM)")
    ap.add_argument("--out", help="result JSON file")
    ap.add_argument("--tag", default=str(int(time.time())), help="salt that makes prompts unique per run")
    ap.add_argument("--lengths", type=lambda s: [int(x) for x in s.split(",")], default=list(LENGTHS))
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--key-factor", type=float, default=1.0, help="Valkey keys per chunk (TP ranks)")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--duration", type=float, default=90.0)
    ap.add_argument("--bg-rate", type=float, default=0.0)
    ap.add_argument("--bg-closed", type=int, default=0,
                    help="load: keep this many background prompts in flight instead of --bg-rate")
    ap.add_argument("--bg-length", type=int, default=8192)
    ap.add_argument("--bg-max-tokens", type=int, default=1)
    ap.add_argument("--probe-rate", type=float, default=0.25)
    ap.add_argument("--probe-length", type=int, default=2048)
    ap.add_argument("--probe-kind", choices=("miss", "hit", "both"), default="both")
    ap.add_argument("--probe-delay", type=float, default=10.0)
    ap.add_argument("--ext-write-gbps", type=float, default=0.0,
                    help="load: also SET chunk-sized values into Valkey at this many GB/s from outside vLLM")
    ap.add_argument("--ext-threads", type=int, default=4,
                    help="resp: writer threads; glide: threads (one GLIDE client each) per writer process")
    ap.add_argument("--ext-writer", choices=("resp", "glide"), default="resp",
                    help="resp: this script's RESP client; glide: valkey-glide sync clients, as LMCache")
    ap.add_argument("--ext-procs", type=int, default=8, help="glide: writer processes (TP workers)")
    ap.add_argument("--ext-value-bytes", type=int, default=0,
                    help="bytes per SET (default: one chunk, 256 x --kv-bytes-per-token)")
    ap.add_argument("--probe-kinds", type=lambda s: s.split(","), default=None,
                    help="load: rotate probes over these kinds (miss, l2hit, cpuhit) instead of --probe-kind")
    ap.add_argument("--host-metrics", action="store_true",
                    help="load: sample host CPU, NIC and Valkey INFO each second during the window")
    ap.add_argument("--gpu-pcie", action="store_true", help="with --host-metrics: also nvidia-smi dmon PCIe")
    ap.add_argument("--cpu-clear-url", default=None,
                    help="LMCache internal API of the TP worker (http://127.0.0.1:9001): reset also clears "
                         "the CPU tier")
    ap.add_argument("--inflight", type=lambda s: [int(x) for x in s.split(",")], default=[1, 4, 16])
    ap.add_argument("--results", help="analyze: directory of result files")
    ap.add_argument("--kv-bytes-per-token", type=int, default=73728)
    args = ap.parse_args(argv)
    if args.bench == "analyze":
        if not args.results:
            ap.error("analyze needs --results DIR")
        out = analyze(args)
        text = json.dumps(out, indent=1, default=str)
        if args.out:
            with open(args.out, "w") as fh:
                fh.write(text)
        print(text)
        return 0
    eng = Engine(args.url, args.model, args.timeout, args.cpu_clear_url)
    valkey = Valkey(args.valkey) if args.valkey else None
    if args.probe_kinds and not set(args.probe_kinds) <= {"miss", "l2hit", "cpuhit"}:
        ap.error("--probe-kinds takes miss, l2hit and cpuhit")
    if args.probe_kinds and "l2hit" in args.probe_kinds and not args.cpu_clear_url:
        print("warning: l2hit probes without --cpu-clear-url are CPU-tier hits when the CPU tier is on",
              file=sys.stderr)
    if args.ext_procs < 1 or args.ext_threads < 1 or args.ext_value_bytes < 0:
        ap.error("--ext-procs and --ext-threads must be >= 1, --ext-value-bytes >= 0")
    if args.bench in ("hit", "load", "concurrency") and valkey is None:
        ap.error(f"{args.bench} needs --valkey")
    t0 = time.time()
    rows = asyncio.run(BENCHES[args.bench](eng, valkey, args))
    meta = {k: v for k, v in vars(args).items() if k not in ("results",)}
    meta.update(started=t0, seconds=time.time() - t0)
    with open(args.out, "w") as fh:
        json.dump({"meta": meta, "rows": rows}, fh, indent=0)
    bad = sum(1 for r in rows if r.get("error"))
    print(f"{args.bench}: {len(rows)} rows, {bad} errors, {time.time() - t0:.0f} s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
