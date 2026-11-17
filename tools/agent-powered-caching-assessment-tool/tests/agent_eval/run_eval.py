"""Run the agent evaluation: real Claude Code sessions follow the assessment skill.

Needs scripts/local-pg running, `apcat` on PATH (uv tool install --editable .), the `claude`
CLI signed in, and the eval extra (pip install -e ".[eval]"). Costs model usage: a full
pass is seven sessions, two of them with a 5 minute assessment.

    .venv/bin/python -m tests.agent_eval.run_eval                     # every scenario once
    .venv/bin/python -m tests.agent_eval.run_eval happy_path -n 3     # one scenario, 3 times
    .venv/bin/python -m tests.agent_eval.run_eval --list

Writes tests/agent_eval/results/<time>/: report.md, results.json and each transcript.
Scenarios run one after another, because they share the test host.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from tests.agent_eval import checks, harness
from tests.agent_eval import scenarios as sc

RESULTS = Path(__file__).with_name("results")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenarios", nargs="*", help="Scenario ids (default: all)")
    p.add_argument("-n", "--repeat", type=int, default=1)
    p.add_argument("--model", help="The agent's model (default: your Claude settings)")
    p.add_argument("--user-model", default="sonnet", help="The simulated user's model")
    p.add_argument("--list", action="store_true")
    a = p.parse_args(argv)
    if a.list:
        for s in sc.SCENARIOS:
            print(f"{s.id:22} {s.summary}")
        return 0
    chosen = [sc.BY_ID[i] for i in a.scenarios] if a.scenarios else sc.SCENARIOS
    out_dir = RESULTS / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    fp = harness.fingerprint()
    rows = []
    for rep in range(a.repeat):
        for k, s in enumerate(chosen):
            t0 = time.time()
            print(f"[{rep + 1}/{a.repeat}] {s.id} ...", flush=True)
            run = harness.run_sync(s, index=k, model=a.model, user_model=a.user_model)
            verdicts = checks.judge(run, s, fp)
            passed = all(ok for ok, _ in verdicts.values())
            src = next((e.data["transcript"] for e in run.events if e.kind == "note" and "transcript" in e.data), None)
            name = f"{s.id}-{rep + 1}.json"
            if src:
                shutil.copy(src, out_dir / name)
                harness.cleanup(run)
            rows.append({"scenario": s.id, "repeat": rep + 1, "passed": passed,
                         "minutes": round((time.time() - t0) / 60, 1), "cost_usd": run.cost_usd,
                         "checks": {k2: {"ok": ok, "detail": d} for k2, (ok, d) in verdicts.items()},
                         "transcript": name})
            failed = [k2 for k2, (ok, _) in verdicts.items() if not ok]
            print(f"    {'PASS' if passed else 'FAIL'} in {rows[-1]['minutes']} min"
                  + (f"; failed: {', '.join(failed)}" if failed else ""), flush=True)
    (out_dir / "results.json").write_text(json.dumps(rows, indent=1))
    (out_dir / "report.md").write_text(report(rows))
    print(f"\n{sum(r['passed'] for r in rows)}/{len(rows)} passed. Report: {out_dir / 'report.md'}")
    return 0 if all(r["passed"] for r in rows) else 1


def report(rows: list[dict]) -> str:
    lines = ["# Agent evaluation", "", "| Scenario | Run | Result | Minutes | Cost (USD) | Failed checks |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        failed = [f"{k}: {v['detail']}" for k, v in r["checks"].items() if not v["ok"]]
        cost = f"{r['cost_usd']:.2f}" if r["cost_usd"] is not None else "-"
        lines.append(f"| {r['scenario']} | {r['repeat']} | {'pass' if r['passed'] else 'FAIL'} | "
                     f"{r['minutes']} | {cost} | {'<br>'.join(failed) or '-'} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
