#!/usr/bin/env python3
"""Synthetic chat workload for testing kvtrace against real vLLM / SGLang servers.

Sends concurrent OpenAI-compatible ``/v1/chat/completions`` requests that mix:

- a few long shared system prompts (prefix reuse across users and servers),
- multi-turn conversations that re-send their growing history, and
- unique one-off prompts (no reuse).

Standard library only. The text is generated from a fixed vocabulary, so it
holds no real user data.

Example::

    python examples/workload.py --url http://127.0.0.1:8000 --url http://127.0.0.1:8001 \\
        --model Qwen/Qwen2.5-1.5B-Instruct --duration 600 --concurrency 8
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import Dict, List, Optional, Tuple

WORDS = (
    "cache tensor prefix block shard replica latency token decode prefill memory bandwidth "
    "cluster node request batch kernel stream queue vector index segment tier offload "
    "router policy budget window chunk hash parent eviction capacity region zone network "
    "engine scheduler adapter context history session system user assistant answer"
).split()


def text(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words))


class Workload:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        rng = random.Random(args.seed)
        # Roughly 1.3 tokens per word for this vocabulary with Qwen2.5's tokenizer.
        self.system_prompts = [
            f"You are assistant #{i}. Policy: " + text(rng, rng.randint(800, 2300))
            for i in range(args.system_prompts)
        ]
        self.lock = threading.Lock()
        self.conversations: List[Dict] = []
        self.stats: Counter = Counter()
        self.stop_at = time.time() + args.duration

    def _pick(self, rng: random.Random) -> Tuple[Optional[Dict], List[Dict[str, str]]]:
        roll = rng.random()
        if roll < self.args.oneoff_fraction:
            self.stats["oneoff"] += 1
            return None, [{"role": "user",
                           "content": f"[{rng.random()}] " + text(rng, rng.randint(200, 900))}]
        with self.lock:
            if self.conversations and rng.random() < 0.6:
                conv = rng.choice(self.conversations)
                if conv["turns"] >= self.args.max_turns:
                    self.conversations.remove(conv)
                    conv = None
            else:
                conv = None
            if conv is None:
                conv = {"messages": [{"role": "system", "content": rng.choice(self.system_prompts)}],
                        "turns": 0}
                self.conversations.append(conv)
                if len(self.conversations) > self.args.max_conversations:
                    self.conversations.pop(0)
                self.stats["new_conversation"] += 1
            else:
                self.stats["followup"] += 1
            conv["messages"].append({"role": "user", "content": text(rng, rng.randint(20, 120))})
            conv["turns"] += 1
            return conv, list(conv["messages"])

    def _post(self, url: str, messages: List[Dict[str, str]]) -> str:
        body = json.dumps({
            "model": self.args.model,
            "messages": messages,
            "max_tokens": self.args.max_tokens,
            "temperature": 0.7,
        }).encode("utf-8")
        req = urllib.request.Request(url + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.args.timeout) as resp:  # noqa: S310
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"] or ""

    def worker(self, idx: int) -> None:
        rng = random.Random(self.args.seed * 1000 + idx)
        while time.time() < self.stop_at:
            conv, messages = self._pick(rng)
            # Sticky routing per conversation would hide cross-node reuse; pick at random.
            url = rng.choice(self.args.url)
            try:
                answer = self._post(url, messages)
                self.stats["ok"] += 1
            except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
                self.stats["error"] += 1
                print(f"worker {idx}: {type(exc).__name__}: {exc}", file=sys.stderr)
                time.sleep(1)
                continue
            if conv is not None:
                with self.lock:
                    conv["messages"].append({"role": "assistant", "content": answer})

    def run(self) -> Counter:
        threads = [threading.Thread(target=self.worker, args=(i,), daemon=True)
                   for i in range(self.args.concurrency)]
        for t in threads:
            t.start()
        last = time.time()
        while any(t.is_alive() for t in threads):
            time.sleep(1)
            if time.time() - last >= 30:
                last = time.time()
                print(f"progress: {dict(self.stats)}", flush=True)
        return self.stats


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", action="append", required=True, help="server base URL (repeatable)")
    p.add_argument("--model", required=True)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--system-prompts", type=int, default=4)
    p.add_argument("--max-turns", type=int, default=6)
    p.add_argument("--max-conversations", type=int, default=40)
    p.add_argument("--oneoff-fraction", type=float, default=0.2)
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--seed", type=int, default=1)
    args = p.parse_args()
    stats = Workload(args).run()
    print(f"done: {dict(stats)}")
    return 0 if stats["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
