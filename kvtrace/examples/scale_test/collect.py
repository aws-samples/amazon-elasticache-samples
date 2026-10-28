#!/usr/bin/env python3
"""Summarize one scale-test run directory into stats.json.

Reads ``metrics/gpuK-{before,after}.prom`` (Prometheus text), ``valkey-{before,after}.txt``
(``INFO`` output) and ``summary.json`` (replayer). Writes counter deltas summed over the
fleet for the cache-related metrics, Valkey stat deltas and the replayer summary.

    python examples/scale_test/collect.py RUN_DIR
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import defaultdict
from typing import Dict

KEEP = re.compile(
    r"(prefix_cache|cached|prompt_tokens|generation_tokens|lmcache|num_requests|"
    r"kv_cache_usage|cache_usage|num_hit|retrieve|store|external|token_usage|"
    r"request_prefill_time|request_decode_time|time_to_first_token)",
    re.I,
)
SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eEnaINF]+)")


def parse_prom(path: str) -> Dict[str, float]:
    """Sum every sample of one metric name (labels dropped, histogram buckets skipped)."""
    out: Dict[str, float] = defaultdict(float)
    with open(path) as f:
        for line in f:
            m = SAMPLE.match(line)
            if not m or m.group(1).endswith("_bucket") or not KEEP.search(m.group(1)):
                continue
            try:
                out[m.group(1)] += float(m.group(3))
            except ValueError:
                continue
    return out


def parse_info(path: str) -> Dict[str, str]:
    out = {}
    with open(path) as f:
        for line in f:
            if ":" in line and not line.startswith("#"):
                k, v = line.strip().split(":", 1)
                out[k] = v
    return out


def collect(run: str) -> Dict:
    fleet_before: Dict[str, float] = defaultdict(float)
    fleet_after: Dict[str, float] = defaultdict(float)
    for after in sorted(glob.glob(os.path.join(run, "metrics", "*-after.prom"))):
        before = after.replace("-after.prom", "-before.prom")
        for k, v in parse_prom(after).items():
            fleet_after[k] += v
        if os.path.exists(before):
            for k, v in parse_prom(before).items():
                fleet_before[k] += v
    delta = {k: fleet_after[k] - fleet_before.get(k, 0.0) for k in sorted(fleet_after)}
    res: Dict = {"metrics_delta": delta}
    vb, va = os.path.join(run, "valkey-before.txt"), os.path.join(run, "valkey-after.txt")
    if os.path.exists(va):
        a = parse_info(va)
        b = parse_info(vb) if os.path.exists(vb) else {}
        num = (
            "keyspace_hits",
            "keyspace_misses",
            "evicted_keys",
            "expired_keys",
            "total_commands_processed",
            "total_net_input_bytes",
            "total_net_output_bytes",
        )
        res["valkey"] = {k: int(a.get(k, 0)) - int(b.get(k, 0)) for k in num}
        res["valkey"].update(
            {
                k: a.get(k)
                for k in (
                    "used_memory",
                    "used_memory_human",
                    "maxmemory",
                    "maxmemory_policy",
                    "valkey_version",
                    "db0",
                )
            }
        )
    sp = os.path.join(run, "summary.json")
    if os.path.exists(sp):
        with open(sp) as f:
            res["replay"] = json.load(f)
    return res


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    run = args[0]
    res = collect(run)
    with open(os.path.join(run, "stats.json"), "w") as f:
        json.dump(res, f, indent=2)
    print(json.dumps({k: v for k, v in res.items() if k != "replay"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
