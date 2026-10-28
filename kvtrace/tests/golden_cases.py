"""Golden outputs with no phase 1 flag.

Each case builds one output text from a small fixture. ``capture()`` writes the texts
to ``tests/goldens``; ``tests/test_goldens.py`` checks that the current code gives the
same bytes. Run ``python tests/golden_cases.py`` only to accept a deliberate change.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import sys
from contextlib import redirect_stdout
from typing import Callable, Dict

sys.path.insert(0, os.path.dirname(__file__))

from conftest import workload_records  # noqa: E402

from kvtrace.cli import main  # noqa: E402
from kvtrace.contention import ContentionParams  # noqa: E402
from kvtrace.costmodel import CostInputs  # noqa: E402
from kvtrace.report import build_report, render_html, render_markdown  # noqa: E402
from kvtrace.simulator import SimParams, parse_capacities, simulate  # noqa: E402

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "goldens")
# Stands in for the temporary directory of a CLI case, so the text does not depend on it.
TMP = "<TMP>"


def _dump(sim: Dict) -> str:
    return json.dumps(sim, sort_keys=True, indent=1)


def _fixture_sim() -> Dict:
    """The ``sim_json`` fixture of conftest.py, before its metrics override."""
    recs = workload_records(nodes=2, requests=150, gpu_tokens=4096)
    for rec in recs:
        rec["model"] = "<script>alert(1)</script>"
    params = SimParams(chunk_tokens=64, capacities=parse_capacities("1MiB,16MiB,inf"),
                       kv_bytes_per_token=1024)
    return simulate(recs, params, sources=["fixture"])


def _fixture_report() -> Dict:
    sim = json.loads(json.dumps(_fixture_sim()))
    sim["metrics"] = {"per_node": {}, "fleet": {"counters": {
        "vllm:prompt_tokens_total": 1_000_000.0,
        "vllm:request_prefill_time_seconds_sum": 100.0,
        "vllm:request_decode_time_seconds_sum": 300.0,
    }, "gauges": {}}}
    inputs = CostInputs(prefill_tokens_per_sec=10000.0, prefill_tps_source="input",
                        replica_cost_per_hour=98.32, prefill_time_fraction=0.25,
                        prefill_fraction_source="input")
    return build_report(sim, inputs)


def _contention_store() -> Dict:
    p = SimParams(chunk_tokens=64, capacities=parse_capacities("16KiB,64KiB,inf"), kv_bytes_per_token=8)
    cp = ContentionParams(prefill_tokens_per_sec=5000.0, l2_read_gbps_per_node=1e-4,
                          l2_write_gbps_per_node=1e-4, l2_rtt_ms=2.0, l2_lookup_ms=1.0,
                          write_stall_ms_per_gb=1e6, write_load_ms_per_gbs=1e5)
    return simulate(workload_records(nodes=2, requests=150, gpu_tokens=4096), p, sources=["fixture"],
                    contention=cp, keep_run_deltas=True)


def _contention_cache_nodes_evict() -> Dict:
    p = SimParams(chunk_tokens=64, capacities=parse_capacities("16KiB,inf"), kv_bytes_per_token=8,
                  write_policy="evict")
    cp = ContentionParams(prefill_tokens_per_sec=5000.0, l2_read_gbps_per_node=1e-4,
                          l2_write_gbps_per_node=1e-4, l2_write_mode="blocking",
                          prefill_model="quadratic", prefill_quadratic_tokens=512.0,
                          cache_nodes=2, cache_node_gbps=5e-5)
    return simulate(workload_records(nodes=2, requests=150, gpu_tokens=4096), p, sources=["fixture"],
                    contention=cp, keep_run_deltas=True)


def _host_full_prompt_scheduled() -> Dict:
    p = SimParams(chunk_tokens=64, capacities=parse_capacities("16KiB,inf"), kv_bytes_per_token=8,
                  host_tier_bytes_per_node=64 * 8 * 40, host_tier_touch_l2=True,
                  l2_lookup="full-prompt", prefill_tokens_per_sec_per_request=2000.0)
    cp = ContentionParams(prefill_tokens_per_sec=5000.0, l2_read_gbps_per_node=1e-4)
    return simulate(workload_records(nodes=2, requests=150, gpu_tokens=4096, host_tokens=2048), p,
                    sources=["fixture"], contention=cp, keep_run_deltas=True)


def _cli_simulate() -> str:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        seg = os.path.join(tmp, "n0", "n0-0-0.kvt.jsonl.gz")
        os.makedirs(os.path.dirname(seg))
        with gzip.open(seg, "wt") as fh:
            for rec in workload_records(nodes=1, requests=80, gpu_tokens=2048):
                fh.write(json.dumps(rec) + "\n")
        sim_path, runs_path = os.path.join(tmp, "sim.json"), os.path.join(tmp, "runs.jsonl")
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["simulate", os.path.join(tmp, "n0"), "--chunk-tokens", "64",
                         "--kv-bytes-per-token", "8", "--capacity", "4KiB,inf", "--contention", "on",
                         "--prefill-tokens-per-sec", "5000", "--l2-read-gbps-per-node", "0.0001",
                         "--write-stall-ms-per-gb", "100000", "-o", sim_path,
                         "--contention-runs-out", runs_path])
        with open(sim_path, encoding="utf-8") as fh:
            sim_text = fh.read()
        with open(runs_path, encoding="utf-8") as fh:
            runs_text = fh.read()
        text = (f"exit {code}\n--- stdout\n{out.getvalue()}--- sim.json\n{sim_text}\n"
                f"--- runs.jsonl\n{runs_text}")
        return text.replace(tmp, TMP)


CASES: Dict[str, Callable[[], str]] = {
    "sim_fixture.json": lambda: _dump(_fixture_sim()),
    "report_fixture.html": lambda: render_html(_fixture_report()),
    "report_fixture.md": lambda: render_markdown(_fixture_report()),
    "sim_contention_store.json": lambda: _dump(_contention_store()),
    "report_contention_store.md": lambda: render_markdown(build_report(
        json.loads(json.dumps(_contention_store())), CostInputs(prefill_tokens_per_sec=5000.0))),
    "sim_contention_cache_nodes_evict.json": lambda: _dump(_contention_cache_nodes_evict()),
    "sim_host_full_prompt_scheduled.json": lambda: _dump(_host_full_prompt_scheduled()),
    "cli_simulate_contention.txt": _cli_simulate,
}


def capture() -> None:
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    for name, build in CASES.items():
        with gzip.open(os.path.join(GOLDEN_DIR, name + ".gz"), "wt", encoding="utf-8") as fh:
            fh.write(build())


def load(name: str) -> str:
    with gzip.open(os.path.join(GOLDEN_DIR, name + ".gz"), "rt", encoding="utf-8") as fh:
        return fh.read()


if __name__ == "__main__":
    capture()
