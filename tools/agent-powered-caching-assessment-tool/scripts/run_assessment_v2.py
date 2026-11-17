#!/usr/bin/env python3
"""
Run the v2 (LLM-interpreted) analysis against a collection artifact.

Prints the model's verdict, with the deterministic scores shown as the evidence
it reasoned over. There is one verdict in a v2 run and it is the model's.

Examples:
    # With Bedrock
    python scripts/run_assessment_v2.py --file /tmp/lt2.json

    # Deterministic only, no model call. Score bands become the verdicts.
    python scripts/run_assessment_v2.py --file /tmp/lt2.json --no-llm

    # Different model or region
    python scripts/run_assessment_v2.py --file /tmp/lt2.json \
        --model-id global.anthropic.claude-sonnet-4-6 --region us-west-2
"""

import argparse
import json
import logging
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.analysis.handler_v2 import run_analysis_v2  # noqa: E402
from src.tools.analysis.caching_scoring import DEFAULT_THRESHOLDS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Run v2 LLM-interpreted analysis")
    ap.add_argument("--file", required=True, help="Collection artifact JSON")
    ap.add_argument("--model-id", default=None)
    ap.add_argument("--region", default=None)
    ap.add_argument("--no-llm", action="store_true",
                    help="Skip the model; use deterministic bands")
    ap.add_argument("--no-size", action="store_true",
                    help="Skip the Sizing & Cost agent")
    ap.add_argument("--no-price", action="store_true",
                    help="Size without calling the AWS Pricing API")
    ap.add_argument("-o", "--output", help="Write the v2 artifact here")
    ap.add_argument("--json", action="store_true", help="Emit raw JSON only")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    path = Path(args.file)
    if not path.exists():
        print(f"error: not found: {path}", file=sys.stderr)
        return 1

    try:
        collection = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"error: {path} is not valid JSON: {exc}", file=sys.stderr)
        return 1
    out, trace = run_analysis_v2(
        collection, thresholds=DEFAULT_THRESHOLDS, model_id=args.model_id,
        region=args.region, use_llm=not args.no_llm,
    )
    if not args.no_size:
        from src.agents.sizing.handler import run_sizing
        run_sizing(out, collection, region=args.region or "us-east-1",
                   price=not args.no_price)

    if args.output:
        Path(args.output).write_text(json.dumps(out, indent=2, default=str))
        print(f"Artifact written to {args.output}")

    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0

    _report(out, trace)
    return 0


VERDICT_ORDER = {
    "STRONG_CANDIDATE": 0, "MODERATE_CANDIDATE": 1,
    "WEAK_CANDIDATE": 2, "NOT_RECOMMENDED": 3,
}


def _report(out: dict, trace: dict) -> None:
    W = 78
    llm = out.get("llm_interpretation") or {}
    mode = out.get("interpretation_mode")
    is_llm = mode == "llm"

    print("=" * W)
    print("POSTGRESQL CACHING ASSESSMENT".center(W))
    print("=" * W)

    if not is_llm:
        print("\n  DETERMINISTIC ANALYSIS ONLY, no LLM judgement")
        print(f"  reason: {out.get('fallback_reason')}")
        print("\n  Verdicts below are threshold bands applied to measured scores.")
        print("  They are a weaker product than a reviewed judgement. The bands")
        print("  are uncalibrated defaults; treat the result as indicative.")
    else:
        wl = llm.get("workload") or {}
        print(f"\n  VERDICT: {wl.get('overall_recommendation')}")
        print()
        print(textwrap.fill(wl.get("executive_summary", ""), W - 2,
                            initial_indent="  ", subsequent_indent="  "))
        if wl.get("key_finding"):
            print("\n  Key finding")
            print(textwrap.fill(wl["key_finding"], W - 4,
                                initial_indent="    ", subsequent_indent="    "))

    wl = llm.get("workload") or {}
    if is_llm:
        for label, key in (("Workload archetype", "workload_archetype"),
                           ("Primary bottleneck", "primary_bottleneck")):
            if wl.get(key):
                print(f"\n  {label}")
                print(textwrap.fill(wl[key], W - 4,
                                    initial_indent="    ", subsequent_indent="    "))

    # Queries first: the primary dimension.
    qrecs = sorted(out.get("query_recommendations") or [],
                   key=lambda r: -(r.get("db_time_ms_per_sec") or 0))
    qv = {q["queryid"]: q for q in (llm.get("queries") or [])}
    scored = [r for r in qrecs if r.get("cache_type") in ("OBJECT", "QUERY")]
    if scored:
        print("\n" + "-" * W)
        print("QUERIES")
        print("-" * W)
        print(f"{'db ms/s':>8} {'type':<6} {'RD':>5} {'A/P/E':>11} {'score':>5}  "
              f"{'band':<9} {'verdict':<9} query")
        print("-" * W)
        short = {"STRONG_CANDIDATE": "STRONG", "MODERATE_CANDIDATE": "MODERATE",
                 "WEAK_CANDIDATE": "WEAK", "NOT_RECOMMENDED": "NOT"}
        for r in scored[:15]:
            rd = r.get("read_dominance")
            rd_s = f"{rd:.2f}" if rd is not None else "-"
            ape = (f"{r.get('access_suitability_score', 0)}/"
                   f"{r.get('pressure_evidence_score', 0)}/"
                   f"{r.get('cache_efficiency_score', 0)}")
            band = short.get(r.get("verdict"), "-")
            final = "NOT" if r.get("hard_excluded") else short.get(
                (qv.get(r["queryid"]) or {}).get("verdict") or r.get("verdict"), "-")
            print(f"{r.get('db_time_ms_per_sec', 0):8.1f} {r.get('cache_type'):<6} "
                  f"{rd_s:>5} {ape:>11} {r.get('confidence_score', 0):>5}  "
                  f"{band:<9} {final:<9} {' '.join(r.get('query_text', '').split())[:22]}")

    # Measured signals, presented as the evidence they are.
    det = {r["table_id"]: r for r in out.get("table_recommendations") or []}
    llm_v = {t["table_id"]: t for t in (llm.get("tables") or [])}

    def verdict_of(tid: str) -> str:
        if tid in llm_v:
            return llm_v[tid].get("verdict", "-")
        return det.get(tid, {}).get("verdict", "-")

    print("\n" + "-" * W)
    print("TABLES")
    print("-" * W)
    print(f"{'table':<28}{'verdict':<21}{'reads':>7}{'calls/s':>9}{'hit%':>7}{'MB':>7}")
    print("-" * W)
    for tid in sorted(det, key=lambda t: (VERDICT_ORDER.get(verdict_of(t), 9), t)):
        r = det[tid]
        # Formatted separately: nested quotes inside an f-string expression are a
        # syntax error, and the conditionals are easier to read unpacked anyway.
        rr = r.get("read_ratio_weighted")
        reads = f"{rr:.0%}" if rr is not None else "-"
        hit = r.get("buffer_hit_ratio_pct")
        hit_s = f"{hit:.0f}" if hit is not None else "-"
        cps = r.get("calls_per_second") or 0.0
        size = r.get("size_mb") or 0.0
        print(f"{tid[:27]:<28}{verdict_of(tid):<21}{reads:>7}"
              f"{cps:>9.2f}{hit_s:>7}{size:>7.1f}")

    # Candidates first, with the model's reasoning.
    candidates = [t for t in VERDICT_ORDER
                  if t in ("STRONG_CANDIDATE", "MODERATE_CANDIDATE")]
    shown = [tid for tid in det if verdict_of(tid) in candidates]

    if shown and is_llm:
        print("\n" + "-" * W)
        print(f"RECOMMENDED FOR CACHING ({len(shown)})")
        print("-" * W)
        for tid in shown:
            t = llm_v.get(tid, {})
            print(f"\n  {tid}  [{t.get('verdict')}]")
            if t.get("primary_signal"):
                print(f"    decisive: {t['primary_signal']}")
            print(textwrap.fill(t.get("reasoning", ""), W - 4,
                                initial_indent="    ", subsequent_indent="    "))
            for label, key in (("strategy", "caching_strategy"),
                               ("invalidation", "invalidation_concern")):
                if t.get(key):
                    print(textwrap.fill(f"{label}: {t[key]}", W - 6,
                                        initial_indent="      ", subsequent_indent="        "))
    elif is_llm:
        print("\n" + "-" * W)
        print("NO TABLES RECOMMENDED FOR CACHING")
        print("-" * W)
        print(textwrap.fill(
            "The model found no table where caching would deliver measurable "
            "benefit. Reasoning for the closest cases follows.", W - 2,
            initial_indent="  ", subsequent_indent="  "))
        near = sorted(det, key=lambda t: -(det[t].get("confidence_score") or 0))[:3]
        for tid in near:
            t = llm_v.get(tid, {})
            if not t:
                continue
            print(f"\n  {tid}  [{t.get('verdict')}]  "
                  f"score {det[tid].get('confidence_score')}")
            print(textwrap.fill(t.get("reasoning", ""), W - 4,
                                initial_indent="    ", subsequent_indent="    "))

    if is_llm:
        for label, key in (("What to do instead", "cheaper_alternatives"),
                           ("Risks if proceeding", "risks_if_proceeding"),
                           ("Next steps", "recommended_next_steps")):
            items = wl.get(key) or []
            if items:
                print("\n" + "-" * W)
                print(label.upper())
                print("-" * W)
                for i in items:
                    print(textwrap.fill(f"- {i}", W - 2,
                                        initial_indent="  ", subsequent_indent="    "))

    if is_llm:
        print("\n" + "-" * W)
        print("PROVENANCE")
        print("-" * W)
        print(f"  model          : {llm.get('model_id')}")
        print(f"  prompt version : {llm.get('prompt_version')}")
        print(f"  tool calls     : {llm.get('tool_calls_made')} "
              f"across {len(llm.get('tools_used') or [])} tools")
        print(f"  duration       : {llm.get('interpretation_duration_seconds')}s")
        print(f"  assessment mode: {str(out.get('assessment_mode')).upper()}")
        if not llm.get("tool_calls_made"):
            print("\n  WARNING: the model made no tool calls, so its judgement is")
            print("           not grounded in the measured data.")
        diffs = trace.get("threshold_band_differences") or []
        if diffs:
            print(f"\n  Note: {len(diffs)} verdict(s) differ from the uncalibrated")
            print("  threshold bands. See decision_trace for calibration review.")

    print("\n" + "=" * W)


if __name__ == "__main__":
    raise SystemExit(main())
