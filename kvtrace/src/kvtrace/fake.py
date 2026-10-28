"""Synthetic KV-event publisher that speaks the vLLM / SGLang ZMQ protocol.

This is a TEST TOOL. Unlike the recorder it binds sockets (a PUB socket and a
replay ROUTER socket); by default it binds to 127.0.0.1 only. With
``connect=True`` the PUB socket connects instead, as vLLM's does for a
non-wildcard endpoint; the replay socket still binds.

The workload is a set of shared system prompts plus multi-turn conversations.
A simulated GPU prefix cache (LRU, leaf blocks evicted first) emits
BlockStored / BlockRemoved events. With ``host_tokens > 0`` it also models an
SGLang-HiCache-style host tier: GPU evictions are offloaded to the host tier,
and host hits are loaded back to the GPU.
"""

from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from array import array
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import msgpack
import zmq

from .wire import SEQ_END_MARKER, decode_seq, encode_seq

log = logging.getLogger(__name__)

# Present in every user turn so tests can prove raw tokens never reach a trace.
CANARY_TOKEN = 424242424


@dataclass
class _Conversation:
    conv_id: int
    tokens: List[int]
    digests: List[bytes] = field(default_factory=list)
    hashes: List[Any] = field(default_factory=list)
    turns: int = 0


class Workload:
    """Generate per-request event lists for one simulated engine instance."""

    def __init__(self, engine: str = "vllm", block_size: Optional[int] = None,
                 gpu_tokens: int = 32768, host_tokens: int = 0, seed: int = 1,
                 prefix_seed: int = 0, system_prompts: int = 4, system_prompt_tokens: int = 512,
                 turn_tokens: Tuple[int, int] = (32, 256), max_turns: int = 6,
                 max_active: int = 32, hash_format: str = "int"):
        if engine not in ("vllm", "sglang"):
            raise ValueError("engine must be vllm or sglang")
        self.engine = engine
        self.bs = block_size or (16 if engine == "vllm" else 1)
        self.gpu_blocks = max(1, gpu_tokens // self.bs)
        self.host_blocks = host_tokens // self.bs
        self.hash_format = hash_format
        self.rng = random.Random(seed)
        prefix_rng = random.Random(prefix_seed)
        self.system_prompts = [
            [prefix_rng.randint(1000, 32000) for _ in range(system_prompt_tokens)]
            for _ in range(system_prompts)
        ]
        self.turn_tokens = turn_tokens
        self.max_turns = max_turns
        self.max_active = max_active
        self.active: List[_Conversation] = []
        self.next_conv = 0
        self.gpu: OrderedDict[Any, None] = OrderedDict()
        self.host: OrderedDict[Any, None] = OrderedDict()
        self.parent_of: Dict[Any, Any] = {}
        self.block_tokens: Dict[Any, List[int]] = {}
        self.published_tokens: Set[int] = set()

    # ---- hashing ---------------------------------------------------------
    def _external(self, digest: bytes) -> Any:
        if self.hash_format == "bytes":
            return digest
        signed = self.engine == "sglang"  # SGLang: first 64 bits as int64
        return int.from_bytes(digest[:8], "big", signed=signed)

    def _extend_hashes(self, conv: _Conversation) -> None:
        full = len(conv.tokens) // self.bs
        while len(conv.digests) < full:
            i = len(conv.digests)
            prev = conv.digests[-1] if conv.digests else b""
            block = conv.tokens[i * self.bs:(i + 1) * self.bs]
            digest = hashlib.sha256(prev + array("I", block).tobytes()).digest()
            conv.digests.append(digest)
            conv.hashes.append(self._external(digest))
        # Blocks that left every tier were forgotten; restore what this request needs.
        for i, h in enumerate(conv.hashes):
            self.parent_of.setdefault(h, conv.hashes[i - 1] if i else None)
            self.block_tokens.setdefault(h, conv.tokens[i * self.bs:(i + 1) * self.bs])

    # ---- events ----------------------------------------------------------
    def _gpu_medium(self) -> Optional[str]:
        return "GPU" if self.engine == "vllm" else None

    def _stored(self, hashes: List[Any], parent: Any, medium: Optional[str],
                session: Optional[str]) -> list:
        tokens = [t for h in hashes for t in self.block_tokens[h]]
        self.published_tokens.update(tokens)
        event = ["BlockStored", hashes, parent, tokens, self.bs, None, medium]
        if self.engine == "sglang" and session:
            event += [None, session]  # cache_salt, session_id
        return event

    def _drop(self, h: Any) -> None:
        if h not in self.gpu and h not in self.host:
            self.block_tokens.pop(h, None)
            self.parent_of.pop(h, None)

    def _evict(self, events: list, protected: Set[Any]) -> None:
        removed_gpu: List[Any] = []
        for h in list(self.gpu):
            if len(self.gpu) <= self.gpu_blocks:
                break
            if h in protected:
                continue
            del self.gpu[h]
            if self.host_blocks:
                if h not in self.host:
                    events.append(self._stored([h], self.parent_of.get(h), "CPU_PINNED", None))
                self.host[h] = None
                self.host.move_to_end(h)
            removed_gpu.append(h)
        if removed_gpu:
            events.append(["BlockRemoved", removed_gpu, self._gpu_medium()])
        removed_host: List[Any] = []
        while self.host_blocks and len(self.host) > self.host_blocks:
            h, _ = self.host.popitem(last=False)
            removed_host.append(h)
        if removed_host:
            events.append(["BlockRemoved", removed_host, "CPU_PINNED"])
        for h in removed_gpu + removed_host:
            self._drop(h)

    def _pick_conversation(self) -> _Conversation:
        if self.active and (len(self.active) >= self.max_active or self.rng.random() < 0.6):
            return self.rng.choice(self.active)
        sp = self.rng.choice(self.system_prompts)
        conv = _Conversation(self.next_conv, list(sp))
        self.next_conv += 1
        self.active.append(conv)
        return conv

    def next_request(self) -> list:
        conv = self._pick_conversation()
        lo, hi = self.turn_tokens
        conv.tokens.append(CANARY_TOKEN)
        conv.tokens.extend(self.rng.randint(1000, 32000) for _ in range(self.rng.randint(lo, hi)))
        self._extend_hashes(conv)
        hashes = conv.hashes
        session = f"sess-{conv.conv_id}"
        events: list = []
        i = 0
        while i < len(hashes) and hashes[i] in self.gpu:
            i += 1
        load_start = i
        while i < len(hashes) and hashes[i] in self.host:
            i += 1
        if i > load_start:
            run = hashes[load_start:i]
            events.append(self._stored(run, hashes[load_start - 1] if load_start else None,
                                       self._gpu_medium(), session))
        # Store the rest in runs of blocks not yet on the GPU, each chained to its parent.
        run: List[Any] = []
        for j in range(i, len(hashes)):
            if hashes[j] in self.gpu:
                if run:
                    events.append(self._stored(run, self.parent_of.get(run[0]),
                                               self._gpu_medium(), session))
                    run = []
                continue
            run.append(hashes[j])
        if run:
            events.append(self._stored(run, self.parent_of.get(run[0]), self._gpu_medium(),
                                       session))
        # Touch leaf-first so that the LRU front holds the deepest, oldest blocks.
        for h in reversed(hashes):
            self.gpu[h] = None
            self.gpu.move_to_end(h)
        self._evict(events, set(hashes) if len(hashes) <= self.gpu_blocks else set())
        # The assistant reply joins the conversation (its KV is not published here).
        conv.tokens.extend(self.rng.randint(1000, 32000) for _ in range(self.rng.randint(lo, hi)))
        conv.turns += 1
        if conv.turns >= self.max_turns:
            self.active.remove(conv)
        return events


def _bind(sock: zmq.Socket, endpoint: str) -> str:
    if ":" in (urlparse(endpoint).hostname or ""):
        sock.setsockopt(zmq.IPV6, 1)  # e.g. tcp://[::1]:PORT
    host, _, port = endpoint.rpartition(":")
    if port in ("0", "*"):
        sock.bind_to_random_port(host)
    else:
        sock.bind(endpoint)
    return sock.getsockopt(zmq.LAST_ENDPOINT).decode()


class FakePublisher:
    """Publish a synthetic workload and serve the replay protocol from a ring buffer."""

    def __init__(self, engine: str = "vllm", bind: str = "tcp://127.0.0.1:5557",
                 replay_bind: Optional[str] = "tcp://127.0.0.1:5558", rate: float = 20.0,
                 duration: Optional[float] = None, max_batches: Optional[int] = None,
                 topic: str = "kv-events", dp_rank: Optional[int] = None, drop_every: int = 0,
                 replay_buffer: int = 10000, start_delay: float = 0.5, linger: float = 0.5,
                 wait_subscribers: int = 0, subscribe_timeout: float = 10.0,
                 legacy_replay: bool = False, connect: bool = False,
                 context: Optional[zmq.Context] = None, **workload_kwargs: Any):
        self.ctx = context or zmq.Context.instance()
        self.workload = Workload(engine=engine, **workload_kwargs)
        self.legacy_replay = legacy_replay
        self.rate = rate
        self.duration = duration
        self.max_batches = max_batches
        self.topic = topic.encode("utf-8")
        self.dp_rank = dp_rank
        self.drop_every = drop_every
        self.buffer: Deque[Tuple[int, bytes]] = deque(maxlen=max(1, replay_buffer))
        self.start_delay = start_delay
        self.linger = linger
        # When > 0, publishing starts once this many subscriptions arrived (instead of
        # after start_delay), so that no early message is lost to a slow subscriber.
        self.wait_subscribers = wait_subscribers
        self.subscribe_timeout = subscribe_timeout
        self.subscribers = 0
        self.sent = 0
        self.dropped = 0
        self.replays_served = 0
        self._stop = threading.Event()
        # XPUB behaves like PUB for subscribers and also reports their subscriptions.
        self.pub = self.ctx.socket(zmq.XPUB)
        self.pub.setsockopt(zmq.LINGER, 0)
        self.pub.setsockopt(zmq.XPUB_VERBOSE, 1)
        if connect:
            # Like vLLM with a non-wildcard endpoint: the PUB side connects to a
            # subscriber that binds (`kvtrace record --endpoint-mode bind`).
            if ":" in (urlparse(bind).hostname or ""):
                self.pub.setsockopt(zmq.IPV6, 1)  # e.g. tcp://[::1]:PORT
            self.pub.connect(bind)
            self.endpoint = bind
        else:
            self.endpoint = _bind(self.pub, bind)
        self.router: Optional[zmq.Socket] = None
        self.replay_endpoint: Optional[str] = None
        if replay_bind:
            self.router = self.ctx.socket(zmq.ROUTER)
            self.router.setsockopt(zmq.LINGER, 0)
            self.replay_endpoint = _bind(self.router, replay_bind)

    def stop(self) -> None:
        self._stop.set()

    def _serve_replay(self, timeout_ms: int) -> None:
        if self.router is None:
            self._stop.wait(timeout_ms / 1000.0)
            return
        if not self.router.poll(timeout=max(0, timeout_ms)):
            return
        frames = self.router.recv_multipart()
        identity, rest = frames[0], frames[1:]
        delimiter = [b""] if rest and rest[0] == b"" else []
        rest = rest[len(delimiter):]
        try:
            start = decode_seq(rest[0])
        except (IndexError, ValueError):
            log.warning("bad replay request")
            return
        # vLLM and SGLang put the topic before the sequence in replay answers and an
        # empty topic in the end marker; legacy_replay drops the topic frame.
        topic = [] if self.legacy_replay else [self.topic]
        end_topic = [] if self.legacy_replay else [b""]
        for seq, payload in list(self.buffer):
            if seq >= start:
                self.router.send_multipart([identity, *delimiter, *topic, encode_seq(seq), payload])
        self.router.send_multipart([identity, *delimiter, *end_topic, encode_seq(SEQ_END_MARKER), b""])
        self.replays_served += 1

    def _drain_subscriptions(self, timeout_ms: int = 0) -> None:
        while self.pub.poll(timeout=timeout_ms):
            message = self.pub.recv()
            if message[:1] == b"\x01":
                self.subscribers += 1
            timeout_ms = 0

    def _wait_for_start(self) -> None:
        if not self.wait_subscribers:
            self._stop.wait(self.start_delay)
            return
        deadline = time.monotonic() + self.subscribe_timeout
        while (self.subscribers < self.wait_subscribers and not self._stop.is_set()
               and time.monotonic() < deadline):
            self._drain_subscriptions(timeout_ms=50)
        if self.subscribers < self.wait_subscribers:
            log.warning("starting with %d of %d subscribers", self.subscribers, self.wait_subscribers)

    def run(self) -> None:
        t0 = time.monotonic()
        self._wait_for_start()
        seq = 0
        interval = 1.0 / self.rate if self.rate > 0 else 0.0
        next_send = time.monotonic()
        try:
            while not self._stop.is_set():
                if self.duration is not None and time.monotonic() - t0 >= self.duration:
                    break
                if self.max_batches is not None and seq >= self.max_batches:
                    break
                self._drain_subscriptions()
                wait = next_send - time.monotonic()
                if wait > 0:
                    self._serve_replay(int(wait * 1000))
                    continue
                events = self.workload.next_request()
                batch: List[Any] = [time.time(), events]
                if self.dp_rank is not None:
                    batch.append(self.dp_rank)
                payload = msgpack.packb(batch, use_bin_type=True)
                self.buffer.append((seq, payload))
                if self.drop_every and seq % self.drop_every == self.drop_every - 1:
                    self.dropped += 1
                else:
                    self.pub.send_multipart([self.topic, encode_seq(seq), payload])
                    self.sent += 1
                seq += 1
                next_send += interval
            end = time.monotonic() + self.linger
            while not self._stop.is_set() and time.monotonic() < end:
                self._serve_replay(50)
        finally:
            self.close()

    def close(self) -> None:
        self.pub.close(linger=0)
        if self.router is not None:
            self.router.close(linger=0)
            self.router = None

    def start_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="kvtrace-fake", daemon=True)
        thread.start()
        return thread
