#!/usr/bin/env python3
"""
Run the Sizing & Cost (SAC) agent on an existing v2 analysis.

Deterministic. Sizes only what the analysis recommended, picks an ElastiCache
for Valkey node-based configuration, prices it from the AWS Pricing API, and
writes the analysis back with `sizing` attached.

Examples:
    python scripts/run_sizing.py --analysis analysis_v2.json \
        --collection collection.json --region us-east-1 -o analysis_v2.json

    # Offline: no Pricing API call, no cost
    python scripts/run_sizing.py --analysis a.json --collection c.json --no-price
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.sizing.handler import run_sizing  # noqa: E402


def _load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"error: not found: {p}")
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {p} is not valid JSON: {exc}") from exc


def _print(s: dict) -> None:
    t, n, c = s["totals"], s.get("node"), s.get("cost") or {}
    print(f"\nSizing: {s['status']}  (verdicts: {s['verdict_source']})")
    if s.get("status_reason"):
        print(f"  {s['status_reason']}")
    for kind in ("query_items", "object_items"):
        for i in s[kind]:
            mem = f"{i['memory_bytes'] / 1024 ** 2:,.2f} MB" if i["sized"] else "not sized"
            print(f"  {i['cache_type']:<6} {i['item_id'][:44]:<46} {mem:>14}  "
                  f"{i['total_ops_per_sec']:>9.1f} ops/s")
    print(f"  memory needed {t['memory_needed_gb']:.4f} GB, "
          f"{t['peak_ops_per_sec']:,.1f} ops/s, {t['peak_bandwidth_gbps']:.4f} Gbps")
    if n:
        print(f"  node {n['instance_type']} x {n['shards']} shard(s) x "
              f"{1 + n['replicas_per_shard']} = {n['total_nodes']} nodes "
              f"({n['memory_utilization_of_usable_pct']}% of usable memory)")
    if c.get("available"):
        print(f"  ${c['monthly_total']:,.2f}/month On-Demand in {c['region']} "
              "(estimate, not a quote)")
    elif c:
        print(f"  cost unavailable: {c.get('unavailable_reason')}")
    for w in s.get("warnings") or []:
        print(f"  ! {w}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Size and price recommended caching")
    ap.add_argument("--analysis", required=True, help="v2 analysis artifact JSON")
    ap.add_argument("--collection", required=True, help="Collection artifact JSON")
    ap.add_argument("--region", default="us-east-1", help="Region to price in")
    ap.add_argument("--no-price", action="store_true",
                    help="Skip the AWS Pricing API (no cost, fallback node specs)")
    ap.add_argument("-o", "--output", help="Write the analysis with sizing here")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    analysis = run_sizing(_load(args.analysis), _load(args.collection),
                          region=args.region, price=not args.no_price)
    if args.output:
        Path(args.output).write_text(json.dumps(analysis, indent=2, default=str))
        print(f"Analysis with sizing written to {args.output}")
    _print(analysis["sizing"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
