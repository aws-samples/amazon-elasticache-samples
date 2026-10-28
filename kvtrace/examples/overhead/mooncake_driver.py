#!/usr/bin/env python3
"""Open-loop replay of the Kimi Mooncake FAST'25 traces against vLLM (OpenAI /v1/completions, token-ID prompts).

Trace rows: {"timestamp": ms, "input_length": n, "output_length": n, "hash_ids": [...]}. Each hash_id is one
512-token block (the last block can be partial); equal hash_ids mean an identical prefix block. No sessions.
  * Open loop: row i goes out (timestamp_i / 1000 - --offset-s) / --time-scale seconds after start, for
    --duration-s. Nothing waits for an earlier answer (0.25 = 4x slower than recorded, 2 = 2x faster).
    --loop repeats the trace (pass 1, 2, ...; pass p starts one trace span plus one mean gap after pass p-1)
    with a fresh hash namespace per pass, so a later pass cannot hit an earlier pass's cache.
  * Prompts: a block is 512 token IDs from SHAKE-128(seed | pass | hash_id) inside [vocab_lo, vocab_hi). The key
    has no request or session part, so equal hash_ids give byte-identical blocks across all requests. The prompt
    is cut to exactly input_length tokens (the last block truncated), clamped to --max-model-len - output_length.
    max_tokens = output_length, ignore_eos, temperature 0, streamed with include_usage: the body of driver.py.
  * Routing: single (first server), round_robin, hash_prefix (server from a hash of the pass and the first
    --prefix-blocks hash_ids), or cache_aware (SGLang-router style, the default for multi-server Kimi tests):
    the server that last received the longest leading chain of the request's blocks gets it when that chain
    covers >= --cache-threshold of the request's blocks and load is balanced; a request below the threshold goes
    to the server holding the fewest blocks; when the in-flight gap between servers exceeds --balance-abs and
    the busiest has more than --balance-rel x the idlest, the request goes to the server with the fewest in
    flight. hash_prefix with --prefix-blocks 1 sends a whole trace to one server when every row starts with the
    same block (conversation_trace does). A server the health loop marks down is skipped.
  * --max-inflight caps requests in flight. A request due while the cap is reached is NOT delayed: it is logged
    with status "shed" and its scheduled time. Failed requests are logged once, never retried (a retry would
    bend the arrival schedule).
  * The trace's own reuse in the replayed window is printed at start and stored in summary.json: the share of
    blocks seen before, and the share of prompt tokens an unbounded prefix cache would hit (ideal cache).
Outputs (in --out): requests.jsonl, events.jsonl, status.json, summary.json, shaped like driver.py's so
90_report/report.py reads them. --dry-run prints the first 5 scheduled requests and the window stats, sends nothing.
Only dependency: aiohttp. Self-contained on purpose (helpers copied from driver.py): edits to driver.py cannot
change this replay, and this file changes nothing the session driver uses.
"""
import argparse, asyncio, collections, hashlib, json, os, signal, struct, sys, time

try:
    import aiohttp
except ImportError:  # pragma: no cover
    print("ERROR: mooncake_driver needs aiohttp (pip install aiohttp)", file=sys.stderr)
    sys.exit(2)

VERSION = "kit-mooncake-driver 1.2"
BLOCK = 512


def stable_hash(s):
    return int.from_bytes(hashlib.sha1(s.encode()).digest()[:8], "big")


def now():
    return time.time()


async def sse_lines(content):
    """Yield newline-terminated lines from a streamed body with no line-length cap (copied from driver.py).

    aiohttp's own line iterator raises LineTooLong past 512 KB, which a few long completion chunks exceed.
    """
    buf = b""
    async for chunk in content.iter_any():
        buf += chunk
        while True:
            j = buf.find(b"\n")
            if j < 0:
                break
            yield buf[:j]
            buf = buf[j + 1:]
    if buf:
        yield buf


class JsonlWriter:
    def __init__(self, path):
        self.f = open(path, "a", buffering=1 << 16)
        self.n = 0

    def write(self, obj):
        self.f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self.n += 1

    def flush(self):
        self.f.flush()

    def close(self):
        self.f.flush()
        self.f.close()


class Server:
    def __init__(self, idx, addr, default_port):
        if ":" not in addr:
            addr = f"{addr}:{default_port}"
        self.idx, self.addr = idx, addr
        self.url = f"http://{addr}"
        self.healthy = True
        self.inflight = 0
        self.inflight_tokens = 0


class PrefixRouter:
    """cache_aware routing state: owner of each leading block chain (pass, hash_ids[0..i]) and blocks per server."""

    def __init__(self, n, threshold, bal_abs, bal_rel):
        self.owner, self.blocks = {}, [0] * n
        self.threshold, self.bal_abs, self.bal_rel = threshold, bal_abs, bal_rel
        self.counts = collections.Counter()      # why each request went where it went

    @staticmethod
    def chains(it):
        c, out = it.pass_no * 1000003 + 17, []
        for h in it.hids[:it.n_blocks]:
            c = hash((c, h))                     # ints only: deterministic across processes
            out.append(c)
        return out

    def pick(self, it, servers):
        """(server, reason). servers: all servers; down ones are never picked unless all are down."""
        live = [s for s in servers if s.healthy] or servers
        keys = self.chains(it)
        best, matched = None, 0
        for i, k in enumerate(keys):
            o = self.owner.get(k)
            if o is None:
                break
            best, matched = o, i + 1
        loads = [s.inflight for s in live]
        if max(loads) - min(loads) > self.bal_abs and max(loads) > self.bal_rel * min(loads):
            srv, why = min(live, key=lambda s: (s.inflight, self.blocks[s.idx], s.idx)), "balance"
        elif best is not None and keys and matched / len(keys) >= self.threshold and servers[best] in live:
            srv, why = servers[best], "prefix"
        elif best is not None and keys and matched / len(keys) >= self.threshold:
            srv, why = min(live, key=lambda s: (s.inflight, self.blocks[s.idx], s.idx)), "owner_down"
        else:
            srv, why = min(live, key=lambda s: (self.blocks[s.idx], s.inflight, s.idx)), "new"
        for k in keys:
            o = self.owner.get(k)
            if o != srv.idx:
                if o is not None:
                    self.blocks[o] -= 1
                self.owner[k] = srv.idx
                self.blocks[srv.idx] += 1
        self.counts[why] += 1
        return srv, why


class BlockTokens:
    """Deterministic 512-token blocks keyed by (seed, pass namespace, hash_id), shared by every request."""

    def __init__(self, seed, lo, hi, cache_blocks=16384):
        self.seed, self.lo, self.span = seed, lo, hi - lo
        self.fmt = "<%dI" % BLOCK
        self.cache = collections.OrderedDict()   # (ns, hid) -> comma-joined text of the full block
        self.cache_blocks = max(0, cache_blocks)

    def block_ids(self, ns, hid):
        raw = hashlib.shake_128(f"{self.seed}|{ns}|{hid}".encode()).digest(4 * BLOCK)
        return [self.lo + v % self.span for v in struct.unpack(self.fmt, raw)]

    def block_text(self, ns, hid):
        key = (ns, hid)
        t = self.cache.get(key)
        if t is None:
            t = ",".join(map(str, self.block_ids(ns, hid)))
            if self.cache_blocks:
                self.cache[key] = t
                if len(self.cache) > self.cache_blocks:
                    self.cache.popitem(last=False)
        else:
            self.cache.move_to_end(key)
        return t

    def prompt_json(self, ns, hash_ids, n_tokens, pad_key):
        """JSON array text of exactly n_tokens token IDs: full blocks, then the head of the next block."""
        full, rem = divmod(n_tokens, BLOCK)
        need = full + (1 if rem else 0)
        hs = list(hash_ids[:need])
        k = 0
        while len(hs) < need:          # trace quirk: fewer hash_ids than blocks; pad with request-unique blocks
            hs.append(f"pad:{pad_key}:{k}")
            k += 1
        parts = [self.block_text(ns, hs[i]) for i in range(full)]
        if rem:
            parts.append(",".join(map(str, self.block_ids(ns, hs[full])[:rem])))
        return "[" + ",".join(parts) + "]"

    def prompt_ids(self, ns, hash_ids, n_tokens, pad_key="x"):
        return json.loads(self.prompt_json(ns, hash_ids, n_tokens, pad_key))


def load_trace(path):
    """[(timestamp_ms, input_length, output_length, hash_ids, line_no)] sorted by (timestamp, line)."""
    recs = []
    with open(path) as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            recs.append((int(r["timestamp"]), int(r["input_length"]), int(r["output_length"]),
                         list(r.get("hash_ids") or []), i))
    if not recs:
        raise SystemExit(f"ERROR: no rows in {path}")
    recs.sort(key=lambda x: (x[0], x[4]))
    return recs


def clamp(n_in, n_out, max_model_len, max_out_tokens=0):
    """(prompt tokens to send, max_tokens, clamped): prompt cut so prompt + output_length <= max_model_len."""
    mt = max(1, min(n_out, max_out_tokens) if max_out_tokens else n_out)
    n = max(1, n_in)
    clamped = False
    if n + mt > max_model_len:
        clamped = True
        if mt >= max_model_len:
            mt = max(1, max_model_len - 1)
        n = max(1, min(n, max_model_len - mt))
    return n, mt, clamped


class Item:
    __slots__ = ("t_wall", "pass_no", "ts_ms", "line", "n_in", "n_out", "hids", "n_sent", "max_tokens",
                 "clamped", "n_blocks", "reused_blocks", "hit_tokens")


def trace_span_s(a):
    """Trace seconds replayed in --duration-s wall seconds (piecewise with --schedule)."""
    if not a.segments:
        return a.duration_s * a.time_scale
    return sum((min(a.duration_s, w1) - w0) * sc for w0, w1, sc, _ in a.segments if w0 < a.duration_s)


def wall_of(a, x):
    """Wall seconds after start at which trace second x (past the offset) goes out, for a --schedule."""
    for k, (w0, w1, sc, t0) in enumerate(a.segments):
        if x < t0 + (w1 - w0) * sc or k == len(a.segments) - 1:
            return w0 + (x - t0) / sc
    return x


def build_schedule(recs, a):
    """Every request of the replayed window in send order, with the trace's own reuse per request."""
    first_ms, last_ms = recs[0][0], recs[-1][0]
    gap_ms = (last_ms - first_ms) / max(1, len(recs) - 1)
    period_ms = max(1.0, last_ms + gap_ms)
    off_ms = a.offset_s * 1000.0
    end_ms = off_ms + trace_span_s(a) * 1000.0
    items = []
    seen_blocks, seen_chain = set(), set()
    p, done = 0, False
    while not done:
        base = p * period_ms
        if base >= end_ms:
            break
        for ts, n_in, n_out, hids, line in recs:
            v = base + ts
            if v < off_ms:
                continue
            if v >= end_ms:
                done = True
                break
            it = Item()
            it.t_wall = wall_of(a, (v - off_ms) / 1000.0) if a.segments else (v - off_ms) / 1000.0 / a.time_scale
            it.pass_no, it.ts_ms, it.line, it.n_in, it.n_out, it.hids = p, ts, line, n_in, n_out, hids
            it.n_sent, it.max_tokens, it.clamped = clamp(n_in, n_out, a.max_model_len, a.max_out_tokens)
            it.n_blocks = -(-it.n_sent // BLOCK)
            reused = lead = 0
            leading, chain = True, ("ns", p)
            for h in hids[:it.n_blocks]:
                kb = (p, h)
                if kb in seen_blocks:
                    reused += 1
                else:
                    seen_blocks.add(kb)
                chain = hash((chain, h))          # prefix-chained key: a block hits only after the same prefix
                if leading and chain in seen_chain:
                    lead += 1
                else:
                    leading = False
                    seen_chain.add(chain)
            it.reused_blocks = reused
            it.hit_tokens = min(it.n_sent, lead * BLOCK)
            items.append(it)
        if not a.loop:
            break
        p += 1
    return items, window_stats(items, a, period_ms)


def window_stats(items, a, period_ms):
    tok = sum(i.n_sent for i in items)
    blocks = sum(i.n_blocks for i in items)
    span = items[-1].t_wall if items else 0.0
    return {"requests": len(items), "passes": (items[-1].pass_no + 1) if items else 0,
            "wall_span_s": round(span, 3), "trace_period_s": round(period_ms / 1000.0, 3),
            "req_per_s": round(len(items) / span, 3) if span > 0 else None,
            "prompt_tokens": tok, "output_tokens": sum(i.max_tokens for i in items), "blocks": blocks,
            "clamped": sum(1 for i in items if i.clamped),
            "block_reuse_share": round(sum(i.reused_blocks for i in items) / blocks, 4) if blocks else None,
            "prefix_hit_token_share": round(sum(i.hit_tokens for i in items) / tok, 4) if tok else None}


class MooncakeDriver:
    def __init__(self, a, recs):
        self.a = a
        servers = a.servers or ("server0" if a.dry_run else "")
        self.servers = [Server(i, s.strip(), a.port) for i, s in enumerate(servers.split(",")) if s.strip()]
        if not self.servers:
            raise SystemExit("ERROR: --servers is empty")
        self.tok = BlockTokens(a.seed if a.salt == "" else f"{a.seed}:{a.salt}", a.vocab_lo, a.vocab_hi,
                               a.block_cache)
        self.items, self.window = build_schedule(recs, a)
        self.trace_name = a.trace_name or os.path.splitext(os.path.basename(a.trace))[0]
        self.rr = 0
        self.router = PrefixRouter(len(self.servers), a.cache_threshold, a.balance_abs, a.balance_rel)
        self.stats = collections.Counter()
        self.route_share = collections.Counter()
        self.recent = collections.deque()     # (t_end, ttft, prompt_tokens, cached, output_tokens, ideal_hit)
        self.inflight_n = 0
        self.tasks = set()
        self.cur_pass = None
        self.t0 = None
        self.stop = None
        self.req_log = self.evt_log = None

    # ---------------------------------------------------------------- helpers
    def event(self, kind, quiet=False, **kw):
        row = {"ts": now(), "t_rel": round(now() - self.t0, 3) if self.t0 else 0.0, "event": kind,
               "run_id": self.a.run_id}
        row.update(kw)
        self.evt_log.write(row)
        self.evt_log.flush()
        if not quiet:
            print(f"[mooncake] event {kind} {json.dumps(kw)}", file=sys.stderr, flush=True)

    def route(self, it):
        """(server, rerouted) for one request."""
        a, n = self.a, len(self.servers)
        if a.routing == "single" or n == 1:
            return self.servers[0], False
        if a.routing == "cache_aware":
            srv, why = self.router.pick(it, self.servers)
            return srv, why == "owner_down"
        if a.routing == "hash_prefix" and it.hids:
            key = f"{it.pass_no}|" + ",".join(map(str, it.hids[:a.prefix_blocks]))
            idx = stable_hash(key) % n
        else:
            idx = self.rr % n
            self.rr += 1
        srv = self.servers[idx]
        if not srv.healthy:                     # skip a down server, keep the order stable for the rest
            for k in range(1, n):
                s2 = self.servers[(idx + k) % n]
                if s2.healthy:
                    return s2, True
        return srv, False

    def base_row(self, it, t_sched):
        a = self.a
        return {"run_id": a.run_id, "test_id": a.test_id, "valkey": a.valkey, "phase": a.phase,
                "session": f"{self.trace_name}:{it.line}", "pass": it.pass_no, "home": None, "kind": "trace",
                "turn": None, "group": None, "inner": None, "think_s": None, "cold_start": False,
                "trace": self.trace_name, "line": it.line, "trace_ts_ms": it.ts_ms, "input_length": it.n_in,
                "output_length": it.n_out, "n_hash_ids": len(it.hids),
                "first_hash_id": it.hids[0] if it.hids else None, "prompt_tokens_sent": it.n_sent,
                "max_tokens": it.max_tokens, "clamped": it.clamped, "routing": a.routing,
                "t_sched": t_sched, "trace_reused_blocks": it.reused_blocks, "trace_blocks": it.n_blocks,
                "trace_prefix_hit_tokens": it.hit_tokens, "attempt": 1, "retries": 0}

    @staticmethod
    def new_out():
        return {"t_send": None, "ttft_s": None, "t_first": None, "t_end": None, "e2e_s": None, "output_tokens": 0,
                "prompt_tokens": None, "cached_tokens": None, "itl_mean_s": None, "itl_max_s": None,
                "tpot_s": None, "chunks": 0, "http_status": None, "status": "error", "error": None}

    # ---------------------------------------------------------------- HTTP request
    async def one_request(self, http, it, srv, rerouted, t_sched):
        a = self.a
        row = self.base_row(it, t_sched)
        row.update(server=srv.addr, rerouted=rerouted)
        out = self.new_out()
        try:
            prompt = self.tok.prompt_json(it.pass_no, it.hids, it.n_sent, f"{it.line}:{it.pass_no}")
            body = ('{"model":%s,"prompt":%s,"max_tokens":%d,"stream":true,'
                    '"stream_options":{"include_usage":true,"continuous_usage_stats":false},'
                    '"ignore_eos":true,"temperature":0}') % (json.dumps(a.model), prompt, it.max_tokens)
            await self.stream(http, srv, body.encode(), it.n_sent, out)
        except asyncio.CancelledError:
            out["status"] = "cancelled"
            if out["t_end"] is None:
                out["t_end"] = now()
            row.update(out)
            row["send_lag_s"] = (out["t_send"] - t_sched) if out["t_send"] else None
            self.req_log.write(row)
            self.stats["cancelled"] += 1
            raise
        finally:
            self.inflight_n -= 1
        row.update(out)
        row["send_lag_s"] = out["t_send"] - t_sched if out["t_send"] else None
        self.req_log.write(row)
        self.stats["requests"] += 1
        if out["status"] == "ok":
            self.stats["ok"] += 1
            self.recent.append((out["t_end"], out["ttft_s"], out.get("prompt_tokens") or it.n_sent,
                                out.get("cached_tokens") or 0, out.get("output_tokens") or 0, it.hit_tokens))
        else:
            self.stats["errors"] += 1
            self.stats["err_" + out["status"]] += 1

    async def stream(self, http, srv, body_b, n_tok, out):
        """POST one streamed completion and fill `out` (driver.py's stream(), `out` passed in for cancels)."""
        t_send = now()
        out["t_send"] = t_send
        srv.inflight += 1
        srv.inflight_tokens += n_tok
        try:
            async with http.post(srv.url + "/v1/completions", data=body_b,
                                 headers={"Content-Type": "application/json"}) as resp:
                out["http_status"] = resp.status
                if resp.status != 200:
                    out["status"] = f"http_{resp.status}"
                    out["error"] = (await resp.text())[:300]
                    return out
                t_last = None
                gaps_sum, gaps_max, n_gaps, chunks = 0.0, 0.0, 0, 0
                usage = finish = None
                async for line in sse_lines(resp.content):
                    line = line.strip()
                    if not line or not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    if obj.get("error"):
                        out["status"] = "stream_error"
                        out["error"] = str(obj.get("error"))[:300]
                        return out
                    if obj.get("usage"):
                        usage = obj["usage"]
                    ch = obj.get("choices") or []
                    if ch:
                        c0 = ch[0]
                        if c0.get("finish_reason"):
                            finish = c0["finish_reason"]
                        if c0.get("text") is not None and (c0.get("text") != "" or c0.get("finish_reason") is None):
                            t = now()
                            chunks += 1
                            if out["t_first"] is None:
                                out["t_first"] = t
                            elif t_last is not None:
                                g = t - t_last
                                gaps_sum += g
                                n_gaps += 1
                                if g > gaps_max:
                                    gaps_max = g
                            t_last = t
                t_end = now()
                out["t_end"] = t_end
                out["e2e_s"] = t_end - t_send
                out["chunks"] = chunks
                out["finish_reason"] = finish
                if out["t_first"] is not None:
                    out["ttft_s"] = out["t_first"] - t_send
                if usage:
                    out["prompt_tokens"] = usage.get("prompt_tokens")
                    out["output_tokens"] = usage.get("completion_tokens") or 0
                    out["cached_tokens"] = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                else:
                    out["output_tokens"] = chunks
                if n_gaps:
                    out["itl_mean_s"] = gaps_sum / n_gaps
                    out["itl_max_s"] = gaps_max
                if out["t_first"] is not None and (out["output_tokens"] or 0) > 1:
                    out["tpot_s"] = (t_end - out["t_first"]) / (out["output_tokens"] - 1)
                out["status"] = "ok" if out["t_first"] is not None else "no_tokens"
                return out
        except asyncio.CancelledError:
            out["status"] = "cancelled"
            out["t_end"] = now()
            raise
        except asyncio.TimeoutError:
            out["status"] = "timeout"
        except aiohttp.ClientConnectorError as e:
            out["status"] = "conn_error"
            out["error"] = str(e)[:200]
        except (aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError, aiohttp.ClientOSError) as e:
            out["status"] = "disconnected"
            out["error"] = str(e)[:200]
        except aiohttp.ClientError as e:
            out["status"] = "client_error"
            out["error"] = str(e)[:200]
        finally:
            srv.inflight -= 1
            srv.inflight_tokens -= n_tok
            if out["t_end"] is None:
                out["t_end"] = now()
                out["e2e_s"] = out["t_end"] - t_send
        return out

    # ---------------------------------------------------------------- open-loop dispatch
    async def dispatch_loop(self, http):
        a = self.a
        for it in self.items:
            if self.stop.is_set():
                return
            t_sched = self.t0 + it.t_wall
            d = t_sched - now()
            if d > 0:
                await asyncio.sleep(d)
            if self.stop.is_set():
                return
            if it.pass_no != self.cur_pass:
                self.cur_pass = it.pass_no
                self.event("pass_start", quiet=it.pass_no == 0, **{"pass": it.pass_no}, line=it.line)
            self.stats["scheduled"] += 1
            if self.inflight_n >= a.max_inflight:
                row = self.base_row(it, t_sched)
                row.update(self.new_out())
                row.update(status="shed", server=None, rerouted=False, send_lag_s=None,
                           error=f"max_inflight {a.max_inflight} reached")
                self.req_log.write(row)
                self.stats["shed"] += 1
                continue
            srv, rer = self.route(it)
            self.route_share[srv.addr] += 1
            self.inflight_n += 1
            task = asyncio.ensure_future(self.one_request(http, it, srv, rer, t_sched))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        self.event("trace_exhausted", scheduled=self.stats["scheduled"], loop=a.loop)
        while self.tasks and not self.stop.is_set():
            await asyncio.sleep(0.1)
        self.stop.set()

    # ---------------------------------------------------------------- background loops
    async def health_loop(self, http):
        to = aiohttp.ClientTimeout(total=self.a.health_timeout_s)
        while not self.stop.is_set():
            for s in self.servers:
                try:
                    async with http.get(s.url + "/health", timeout=to) as r:
                        ok = r.status == 200
                except Exception:
                    ok = False
                if ok != s.healthy:
                    self.event("server_health", server=s.addr, healthy=ok)
                s.healthy = ok
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.a.health_interval_s)
            except asyncio.TimeoutError:
                pass

    async def duration_loop(self):
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=max(0.0, self.t0 + self.a.duration_s - now()))
        except asyncio.TimeoutError:
            self.event("duration_reached", duration_s=self.a.duration_s)
            self.stop.set()

    def rolling(self, window=60.0):
        t = now()
        while self.recent and self.recent[0][0] < t - window:
            self.recent.popleft()
        vals = sorted(x[1] for x in self.recent if x[1] is not None)
        p50 = vals[len(vals) // 2] if vals else None
        pt = sum(x[2] for x in self.recent)
        ca = sum(x[3] for x in self.recent)
        ot = sum(x[4] for x in self.recent)
        ih = sum(x[5] for x in self.recent)
        return p50, pt / window, (ca / pt if pt else None), ot / window, len(self.recent), (ih / pt if pt else None)

    def status_obj(self):
        p50, pts, cshare, ots, n, ideal = self.rolling()
        return {"ts": now(), "t_rel": round(now() - self.t0, 1) if self.t0 else 0, "run_id": self.a.run_id,
                "phase": self.a.phase, "arrival": "open", "trace": self.trace_name, "time_scale": self.cur_scale(),
                "routing": self.a.routing, "pass": self.cur_pass, "max_inflight": self.a.max_inflight,
                "active_sessions": self.inflight_n,
                "inflight": {s.addr: s.inflight for s in self.servers},
                "inflight_prompt_tokens": {s.addr: s.inflight_tokens for s in self.servers},
                "servers": {s.addr: ("up" if s.healthy else "down") for s in self.servers},
                "stats": dict(self.stats), "ttft_p50_60s": p50, "prompt_tok_s_60s": round(pts, 1),
                "output_tok_s_60s": round(ots, 1), "cached_share_60s": cshare, "req_60s": n,
                "trace_prefix_share_60s": ideal}

    def cur_scale(self):
        if not self.a.segments or self.t0 is None:
            return self.a.time_scale
        t = now() - self.t0
        return next((sc for w0, w1, sc, _ in reversed(self.a.segments) if t >= w0), self.a.segments[0][2])

    async def schedule_loop(self):
        """--schedule: a "target" event with the new time_scale at every step (the report splits steps on them)."""
        for w0, _, sc, _ in self.a.segments or []:
            d = self.t0 + w0 - now()
            if d > 0:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=d)
                    return
                except asyncio.TimeoutError:
                    pass
            self.event("target", time_scale=sc, t_s=w0)

    def write_status(self):
        s = self.status_obj()
        path = os.path.join(self.a.out, self.a.prefix + "status.json")
        with open(path + ".tmp", "w") as f:
            json.dump(s, f)
        os.replace(path + ".tmp", path)
        return s

    async def status_loop(self):
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.a.status_interval_s)
            except asyncio.TimeoutError:
                pass
            s = self.write_status()
            self.req_log.flush()
            p50 = f"{s['ttft_p50_60s']:.2f}s" if s["ttft_p50_60s"] is not None else "-"
            cs = f"{100 * s['cached_share_60s']:.0f}%" if s["cached_share_60s"] is not None else "-"
            tp = f"{100 * s['trace_prefix_share_60s']:.0f}%" if s["trace_prefix_share_60s"] is not None else "-"
            print(f"[{time.strftime('%H:%M:%S')}] {self.a.phase} t={s['t_rel']:.0f}s "
                  f"sched={self.stats['scheduled']} shed={self.stats['shed']} pass={self.cur_pass} "
                  f"active={s['active_sessions']} inflight={sum(s['inflight'].values())} ok={self.stats['ok']} "
                  f"err={self.stats['errors']} ttft_p50(60s)={p50} prompt_tok/s={s['prompt_tok_s_60s']:.0f} "
                  f"cached={cs} servers={','.join(v for v in s['servers'].values())} trace_prefix(60s)={tp}",
                  file=sys.stderr, flush=True)

    # ---------------------------------------------------------------- dry run and main
    def window_line(self):
        w, a = self.window, self.a
        pct = lambda x: "-" if x is None else f"{100 * x:.1f}%"
        return (f"{self.trace_name}: {w['requests']} requests in {w['wall_span_s']:.1f} s of replay "
                f"(trace {a.offset_s:g} s + {trace_span_s(a):g} s at time-scale {a.time_scale:g}{' (stepped)' if a.segments else ''}, "
                f"passes {w['passes']}, {w['req_per_s']} req/s, {w['clamped']} clamped); trace reuse in window: "
                f"blocks {pct(w['block_reuse_share'])}, prefix tokens {pct(w['prefix_hit_token_share'])} (ideal cache)")

    def dry_run(self, n=5):
        print(f"[mooncake] DRY RUN, nothing sent. {self.window_line()}")
        print(f"{'#':>2} {'t_send_s':>9} {'pass':>4} {'line':>6} {'trace_s':>8} {'input':>6} {'sent':>6} "
              f"{'output':>6}  {'route':<22} hash_ids")
        routes = collections.Counter()
        for k, it in enumerate(self.items):
            srv, _ = self.route(it)
            routes[srv.addr] += 1
            if k < n:
                h = it.hids
                hs = ", ".join(map(str, h[:12])) + (f", ... +{len(h) - 12} more" if len(h) > 12 else "")
                print(f"{k + 1:>2} {it.t_wall:>9.3f} {it.pass_no:>4} {it.line:>6} {it.ts_ms / 1000:>8.3f} "
                      f"{it.n_in:>6} {it.n_sent:>6} {it.max_tokens:>6}  {srv.addr:<22} [{hs}] ({len(h)} ids)")
        print(f"[mooncake] route share over the window ({self.a.routing}): "
              + ", ".join(f"{k}={v} ({100 * v / max(1, len(self.items)):.0f}%)" for k, v in routes.most_common())
              + (f"; reasons {dict(self.router.counts)} (dry run: no load, so no balance moves)"
                 if self.a.routing == "cache_aware" else ""))
        return 0

    async def run(self):
        a = self.a
        os.makedirs(a.out, exist_ok=True)
        self.req_log = JsonlWriter(os.path.join(a.out, a.prefix + "requests.jsonl"))
        self.evt_log = JsonlWriter(os.path.join(a.out, a.prefix + "events.jsonl"))
        self.stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: (self.event("signal", sig=int(s)), self.stop.set()))
            except NotImplementedError:
                pass
        print(f"[mooncake] {self.window_line()}", file=sys.stderr, flush=True)
        timeout = aiohttp.ClientTimeout(total=a.request_timeout_s, sock_connect=10)
        # vLLM closes idle HTTP connections after 5 s (VLLM_HTTP_TIMEOUT_KEEP_ALIVE); aiohttp keeps them 15 s, so a
        # request could go out on a connection the server was closing ("Server disconnected" in about 20 ms
        # in one test run). Dropping idle connections first removes that race.
        conn = aiohttp.TCPConnector(limit=0, force_close=False, keepalive_timeout=a.keepalive_s)
        async with aiohttp.ClientSession(timeout=timeout, connector=conn) as http:
            self.t0 = now()
            self.event("start", version=VERSION, args=vars(a), servers=[s.addr for s in self.servers],
                       trace=self.trace_name, window=self.window)
            bg = [asyncio.ensure_future(x) for x in (self.health_loop(http), self.status_loop(), self.duration_loop())]
            if a.segments:
                bg.append(asyncio.ensure_future(self.schedule_loop()))
            disp = asyncio.ensure_future(self.dispatch_loop(http))
            await self.stop.wait()
            disp.cancel()
            await asyncio.gather(disp, return_exceptions=True)
            if a.drain_s > 0:
                end = now() + a.drain_s
                while self.tasks and now() < end:
                    await asyncio.sleep(0.2)
            tasks = list(self.tasks)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for t in bg:
                t.cancel()
            await asyncio.gather(*bg, return_exceptions=True)
        t1 = now()
        self.write_status()
        summary = {"run_id": a.run_id, "test_id": a.test_id, "valkey": a.valkey, "phase": a.phase,
                   "version": VERSION, "t_start": self.t0, "t_end": t1, "duration_s": t1 - self.t0,
                   "stats": dict(self.stats), "servers": [s.addr for s in self.servers], "failed": [],
                   "routing": a.routing, "prefix_blocks": a.prefix_blocks, "seed": a.seed, "salt": a.salt,
                   "cache_threshold": a.cache_threshold, "balance_abs": a.balance_abs, "balance_rel": a.balance_rel,
                   "route_reasons": dict(self.router.counts), "route_share": dict(self.route_share),
                   "arrival": "open", "trace": self.trace_name, "trace_path": a.trace, "time_scale": a.time_scale,
                   "offset_s": a.offset_s, "loop": a.loop, "passes": (self.cur_pass + 1) if self.cur_pass is not None else 0,
                   "max_inflight": a.max_inflight, "max_model_len": a.max_model_len, "window": self.window,
                   "keepalive_s": a.keepalive_s}
        if a.segments:
            summary["schedule"] = [{"t_s": w0, "time_scale": sc} for w0, _, sc, _ in a.segments]
        self.event("end", **{k: v for k, v in summary.items() if k in ("duration_s", "stats")})
        for w in (self.req_log, self.evt_log):
            w.close()
        with open(os.path.join(a.out, a.prefix + "summary.json"), "w") as f:
            json.dump(summary, f, indent=1)
        print(f"[mooncake] done: {json.dumps(summary['stats'])}", file=sys.stderr, flush=True)
        return 0 if self.stats["ok"] > 0 else 3


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True, help="Mooncake trace .jsonl (conversation_trace, toolagent_trace, ...)")
    ap.add_argument("--trace-name", default="", help="label for rows (default: trace file name)")
    ap.add_argument("--servers", default="", help="comma list of host[:port] (port default --port)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("VLLM_PORT", 8000)))
    ap.add_argument("--model", default=os.environ.get("SERVED_MODEL_NAME", "qwen3-235b"))
    ap.add_argument("--max-model-len", type=int, default=int(os.environ.get("MAX_MODEL_LEN", 262144)))
    ap.add_argument("--max-out-tokens", type=int, default=0, help="cap on max_tokens per request (0 = trace value)")
    ap.add_argument("--time-scale", type=float, default=1.0,
                    help="send at trace time / this (0.25 = 4x slower, 2 = 2x faster)")
    ap.add_argument("--duration-s", type=float, default=5400, help="wall seconds of replay")
    ap.add_argument("--offset-s", type=float, default=0.0, help="start this many trace seconds into the trace")
    ap.add_argument("--loop", action="store_true", help="repeat the trace, fresh hash namespace per pass")
    ap.add_argument("--routing", choices=["single", "round_robin", "hash_prefix", "cache_aware"], default="single")
    ap.add_argument("--cache-threshold", type=float, default=0.5,
                    help="cache_aware: share of a request's blocks the best server's leading match must cover")
    ap.add_argument("--balance-abs", type=int, default=32, help="cache_aware: in-flight gap that counts as imbalance")
    ap.add_argument("--balance-rel", type=float, default=1.5, help="cache_aware: and busiest > this x idlest")
    ap.add_argument("--prefix-blocks", type=int, default=1, help="hash_prefix: leading hash_ids hashed for the route")
    ap.add_argument("--max-inflight", type=int, default=256, help="requests over this in flight are shed, not delayed")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--salt", default="", help="extra token-generator key (for warmup: different tokens)")
    ap.add_argument("--vocab-lo", type=int, default=1000)
    ap.add_argument("--vocab-hi", type=int, default=150000)
    ap.add_argument("--block-cache", type=int, default=16384, help="rendered 512-token blocks kept in memory")
    ap.add_argument("--request-timeout-s", type=float, default=1800)
    ap.add_argument("--keepalive-s", type=float, default=4.0,
                    help="close client connections idle this long (below vLLM's 5 s server keep-alive)")
    ap.add_argument("--health-interval-s", type=float, default=5.0)
    # vLLM's /health waits on the engine loop and took 20-57 s under load in one test run while the server
    # kept serving; 30 s (as in driver.py) still catches a dead server.
    ap.add_argument("--health-timeout-s", type=float, default=30.0)
    ap.add_argument("--status-interval-s", type=float, default=10.0)
    ap.add_argument("--drain-s", type=float, default=0.0, help="at the end, wait up to N s for in-flight requests")
    ap.add_argument("--out", "--out-dir", dest="out", default="", help="output directory")
    ap.add_argument("--prefix", default="", help="file name prefix inside --out (e.g. warmup_)")
    ap.add_argument("--run-id", default="adhoc")
    ap.add_argument("--test-id", default="adhoc")
    ap.add_argument("--valkey", default="unknown", help="label only: on/off")
    ap.add_argument("--phase", default="measure", help="label only: warmup/measure")
    ap.add_argument("--dry-run", action="store_true", help="print the first 5 scheduled requests, send nothing")
    ap.add_argument("--schedule", default="", help='JSON file [{"t_s": wall s, "time_scale": x}, ...] (first t_s 0): '
                    "the time scale steps over wall time, for calibration; --time-scale becomes the first step's")
    a = ap.parse_args(argv)
    if a.vocab_hi <= a.vocab_lo:
        ap.error("--vocab-hi must exceed --vocab-lo")
    if a.time_scale <= 0 or a.duration_s <= 0 or a.offset_s < 0:
        ap.error("--time-scale and --duration-s must be > 0, --offset-s >= 0")
    a.segments = None
    if a.schedule:
        rows = sorted(json.load(open(a.schedule)), key=lambda r: float(r["t_s"]))
        if not rows or float(rows[0]["t_s"]) != 0 or any(float(r["time_scale"]) <= 0 for r in rows):
            ap.error("--schedule needs rows with time_scale > 0 and the first at t_s 0")
        segs, t0 = [], 0.0
        for k, r in enumerate(rows):
            w0 = float(r["t_s"]); w1 = float(rows[k + 1]["t_s"]) if k + 1 < len(rows) else 1e12
            segs.append((w0, w1, float(r["time_scale"]), t0))
            if w1 != 1e12:
                t0 += (w1 - w0) * float(r["time_scale"])
        a.segments = segs
        a.time_scale = segs[0][2]
    if not 0 < a.cache_threshold <= 1 or a.balance_abs < 0 or a.balance_rel < 1:
        ap.error("--cache-threshold must be in (0, 1], --balance-abs >= 0, --balance-rel >= 1")
    if a.max_inflight < 1 or a.prefix_blocks < 1:
        ap.error("--max-inflight and --prefix-blocks must be >= 1")
    if not a.dry_run and (not a.servers or not a.out):
        ap.error("--servers and --out are required (except with --dry-run)")
    return a


def main(argv=None):
    a = parse_args(argv)
    d = MooncakeDriver(a, load_trace(a.trace))
    if a.dry_run:
        return d.dry_run()
    return asyncio.run(d.run())


if __name__ == "__main__":
    sys.exit(main())
