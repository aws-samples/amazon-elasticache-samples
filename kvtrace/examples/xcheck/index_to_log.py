#!/usr/bin/env python3
"""Rebuild a driver log (``synth_from_mooncake.py --requests`` input) from a synth index.

A benchmark run that keeps only the per-request index of its OFF run
(``bench-requests.jsonl``: ``line``, ``node``, ``ts`` = epoch +
send time + TTFT, ``ttft_s``). This writes ``line``, ``server``, ``t_send``,
``ttft_s`` and ``status`` per request, so synth reproduces the same routing and
event times. Standard library only.

    python examples/xcheck/index_to_log.py bench-requests.jsonl \\
        -o out/xcheck/bench-log.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("index")
    p.add_argument("--epoch", type=float, default=1.7e9, help="synth --epoch used for the index")
    p.add_argument("-o", "--out", required=True)
    a = p.parse_args(argv)
    n = 0
    with open(a.index) as fh, open(a.out, "w") as out:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            ttft = float(r.get("ttft_s") or 0.0)
            out.write(json.dumps({"line": r["line"], "server": r["node"], "status": "ok",
                                  "t_send": r["ts"] - a.epoch - ttft, "ttft_s": ttft}) + "\n")
            n += 1
    print(f"wrote {n} requests to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
