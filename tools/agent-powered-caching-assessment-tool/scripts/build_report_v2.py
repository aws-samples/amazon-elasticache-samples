"""
Render a v2 (LLM-interpreted) analysis artifact as a self-contained HTML report.

v2 takes no recommendation artifact. The candidate set follows the model's
verdict, so sizing belongs to a downstream stage reading candidate_table_ids().

Examples:
    python scripts/build_report_v2.py \
        --collection artifacts/loadtest-002/collect/output.json \
        --analysis artifacts/loadtest-002/analyze-v2/analysis_v2.json \
        -o artifacts/loadtest-002/report-v2/assessment_v2.html
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tools.report.html_report_v2 import build_html_report_v2  # noqa: E402


def main() -> int:
    """Parse args, render the report, write it to disk.

    Returns:
        0 on success, 1 on any handled error.
    """
    ap = argparse.ArgumentParser(description="Build the v2 HTML report")
    ap.add_argument("--collection", required=True, help="Collector output JSON")
    ap.add_argument("--analysis", required=True, help="v2 analysis output JSON")
    ap.add_argument("-o", "--output", required=True, help="HTML file to write")
    ap.add_argument("--job-id", default=None, help="Identifier shown in the footer")
    args = ap.parse_args()

    for label, p in (("collection", args.collection), ("analysis", args.analysis)):
        if not Path(p).exists():
            print(f"error: {label} not found: {p}", file=sys.stderr)
            return 1

    try:
        collection = json.loads(Path(args.collection).read_text())
        analysis = json.loads(Path(args.analysis).read_text())
    except json.JSONDecodeError as exc:
        print(f"error: could not parse input JSON: {exc}", file=sys.stderr)
        return 1

    try:
        html = build_html_report_v2(collection, analysis, job_id=args.job_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    dest = Path(args.output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html)

    mode = analysis.get("interpretation_mode", "unknown")
    verdict = (
        (analysis.get("llm_interpretation") or {}).get("workload", {})
        .get("overall_recommendation")
    )
    print(f"wrote {dest} ({len(html):,} bytes)")
    print(f"  interpretation mode: {mode}")
    print(f"  workload verdict:    {verdict or 'none (deterministic fallback)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
