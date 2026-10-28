#!/usr/bin/env python3
"""Trace-only ground truth for a Mooncake trace: the reusable-prefix ceiling.

For each request, its hash ids are prefix-matched against every earlier request
(an infinite cache shared by all engines, no eviction). The matched blocks give
the tokens an ideal fleet-wide cache could reuse. This bounds L1 plus L2 together
and does not depend on any engine.

    python examples/scale_test/ground_truth.py conversation_trace.jsonl --max-model-len 32768
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from typing import Any, Dict, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mooncake_replay import BLOCK_TOKENS, Request, load_trace, select  # noqa: E402


def ground_truth(
    requests: Sequence[Request], block: int = BLOCK_TOKENS, chunk: Optional[int] = None
) -> Dict[str, Any]:
    """Reusable tokens with an infinite shared prefix cache.

    ``chunk`` rounds each request's reusable prefix down to a multiple of ``chunk``
    tokens (an L2 that stores whole chunks only, like LMCache)."""
    seen: set = set()
    total = reusable = 0
    hit_requests = 0
    for r in requests:
        matched = 0
        for h in r.hash_ids:
            if h not in seen:
                break
            matched += 1
        tokens = min(r.input_length, matched * block)
        if chunk:
            tokens -= tokens % chunk
        total += r.input_length
        reusable += tokens
        hit_requests += tokens > 0
        seen.update(r.hash_ids)
    return {
        "requests": len(requests),
        "input_tokens": total,
        "reusable_tokens": reusable,
        "reusable_fraction": reusable / total if total else 0.0,
        "requests_with_reuse": hit_requests,
        "chunk_tokens": chunk,
    }


def trace_stats(requests: Sequence[Request]) -> Dict[str, Any]:
    def q(vals, p):
        s = sorted(vals)
        return s[min(len(s) - 1, int(p * len(s)))] if s else None

    il = [r.input_length for r in requests]
    ol = [r.output_length for r in requests]
    ts = [r.timestamp_ms for r in requests]
    return {
        "requests": len(requests),
        "span_seconds": (max(ts) - min(ts)) / 1000 if ts else 0,
        "input_mean": statistics.mean(il) if il else 0,
        "input_p50": q(il, 0.5),
        "input_p90": q(il, 0.9),
        "input_p99": q(il, 0.99),
        "input_max": max(il, default=0),
        "output_mean": statistics.mean(ol) if ol else 0,
        "output_p50": q(ol, 0.5),
        "output_p99": q(ol, 0.99),
        "distinct_hash_ids": len({h for r in requests for h in r.hash_ids}),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("trace")
    p.add_argument("--max-model-len", type=int, default=32768)
    p.add_argument("--limit", type=int)
    p.add_argument("--max-seconds", type=float)
    p.add_argument("-o", "--out")
    a = p.parse_args(argv)
    raw = load_trace(a.trace)
    kept, dropped = select(raw, a.max_model_len, a.limit, a.max_seconds)
    res = {
        "raw": trace_stats(raw),
        "raw_over_max_model_len_fraction": sum(
            r.input_length + r.output_length > a.max_model_len for r in raw
        )
        / max(1, len(raw)),
        "selected": trace_stats(kept),
        "dropped_too_long": dropped,
        "max_model_len": a.max_model_len,
        "ground_truth": ground_truth(kept),
        "ground_truth_chunk256": ground_truth(kept, chunk=256),
    }
    text = json.dumps(res, indent=2)
    if a.out:
        with open(a.out, "w") as f:
            f.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
