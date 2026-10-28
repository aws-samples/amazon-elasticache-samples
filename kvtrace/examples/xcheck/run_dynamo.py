#!/usr/bin/env python3
"""Dynamo mocker replay of a Mooncake trace: prefix-cache reuse per engine setup.

Run it with the Python of an environment that has ``ai-dynamo`` (its wheels need
glibc >= 2.28; on older hosts run it in a container). It calls
``dynamo.replay.run_trace_replay`` (offline mode, Mooncake format, 512-token
trace blocks) and prints the report summary as JSON. Standard library plus Dynamo.

    python run_dynamo.py conversation_trace.jsonl --workers 8 --gpu-blocks 16384 \\
        --block-size 16 --router round_robin --speedup 1.0 -o dyn.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("trace")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--gpu-blocks", type=int, required=True)
    p.add_argument("--block-size", type=int, default=16)
    p.add_argument("--router", choices=("round_robin", "kv_router"), default="round_robin")
    p.add_argument("--speedup", type=float, default=1.0, help="arrival_speedup_ratio")
    p.add_argument("-o", "--out", required=True)
    a = p.parse_args(argv)

    from dynamo.replay import run_trace_replay  # type: ignore
    from dynamo.replay.config import load_engine_args  # type: ignore

    engine = load_engine_args({"num_gpu_blocks": a.gpu_blocks, "block_size": a.block_size,
                               "enable_prefix_caching": True})
    rep = run_trace_replay(a.trace, extra_engine_args=engine, num_workers=a.workers,
                           router_mode=a.router, trace_block_size=512, trace_format="mooncake",
                           arrival_speedup_ratio=a.speedup)
    d = rep.to_dict() if hasattr(rep, "to_dict") else dict(rep)
    out = {"inputs": vars(a), "summary": d.get("summary")}
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    s = out["summary"] or {}
    print(json.dumps({k: s.get(k) for k in ("num_requests", "total_input_tokens",
                                            "prefix_cache_reused_ratio")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
