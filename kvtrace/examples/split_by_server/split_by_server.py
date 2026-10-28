#!/usr/bin/env python3
"""Cut a Mooncake trace down to the requests a benchmark run routed to one server.

The benchmark driver logs one JSON line per request with the 0-based trace ``line`` and the
``server`` it was routed to. This keeps the trace rows of one server, in trace order, with their
original timestamps, so a replay at the same time scale reproduces that server's request stream.

    split_by_server.py --trace conversation_trace.jsonl --requests requests.jsonl.gz \
        --server 172.31.1.250:8000 --out server-a1.jsonl
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter
from typing import IO, Iterable


def open_text(path: str) -> IO[str]:
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def server_lines(rows: Iterable[dict], server: str, pass_no: int = 0) -> set[int]:
    """Trace lines routed to ``server`` in pass ``pass_no``, from driver request rows."""
    lines = set()
    for r in rows:
        if r.get("server") == server and r.get("pass", 0) == pass_no and r.get("line") is not None:
            lines.add(int(r["line"]))
    return lines


def route_counts(rows: Iterable[dict]) -> Counter:
    """Requests per (server, pass)."""
    return Counter((r.get("server"), r.get("pass", 0)) for r in rows)


def split(trace: IO[str], lines: set[int], out: IO[str]) -> int:
    n = 0
    for i, row in enumerate(trace):
        if i in lines:
            out.write(row if row.endswith("\n") else row + "\n")
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--trace", required=True)
    ap.add_argument("--requests", required=True, help="driver requests.jsonl[.gz]")
    ap.add_argument("--server", default="", help="host:port; empty lists the servers and exits")
    ap.add_argument("--pass-no", type=int, default=0)
    ap.add_argument("--out", default="-")
    a = ap.parse_args(argv)
    with open_text(a.requests) as f:
        rows = [json.loads(x) for x in f if x.strip()]
    if not a.server:
        for (s, p), n in sorted(route_counts(rows).items(), key=lambda kv: (-kv[1], str(kv[0]))):
            print(f"{s}\tpass {p}\t{n}")
        return 0
    lines = server_lines(rows, a.server, a.pass_no)
    if not lines:
        print(f"no requests for server {a.server}", file=sys.stderr)
        return 1
    with open_text(a.trace) as t:
        if a.out == "-":
            n = split(t, lines, sys.stdout)
        else:
            with open(a.out, "w", encoding="utf-8") as o:
                n = split(t, lines, o)
    if n != len(lines):
        print(f"wrote {n} rows but the driver log names {len(lines)} lines", file=sys.stderr)
        return 1
    print(f"wrote {n} rows for {a.server}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
