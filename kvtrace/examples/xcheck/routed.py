#!/usr/bin/env python3
"""Routed request stream for ``blis_l1``: synth's routing and order, the trace's hash ids.

Reads a synth index (``run_kvtrace.py`` writes it as ``<work>/index.jsonl``, in
serving order) and the Mooncake trace, and writes ``line``, ``node``,
``input_length`` (the prompt tokens synth served, after any clamp) and
``hash_ids`` per request, so BLIS serves the same requests on the same nodes in
the same order. Standard library only.

    python examples/xcheck/routed.py conversation_trace.jsonl /tmp/xcheck/kvt-c2/index.jsonl \\
        -o /tmp/xcheck/c2.routed.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mooncake_replay import load_trace  # noqa: E402


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("trace")
    p.add_argument("index")
    p.add_argument("-o", "--out", required=True)
    a = p.parse_args(argv)
    by_line = {r.index: r for r in load_trace(a.trace)}
    n = 0
    with open(a.index) as fh, open(a.out, "w") as out:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            req = by_line[int(row["line"])]
            out.write(json.dumps({"line": req.index, "node": row["node"],
                                  "input_length": row["input_tokens"], "hash_ids": req.hash_ids},
                                 separators=(",", ":")) + "\n")
            n += 1
    print(f"wrote {n} requests to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
