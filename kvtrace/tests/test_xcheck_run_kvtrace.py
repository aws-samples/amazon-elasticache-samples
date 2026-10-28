"""Smoke test for examples/xcheck/run_kvtrace.py on a tiny Mooncake trace."""

import importlib.util
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "examples", "xcheck", "run_kvtrace.py")


def _load():
    spec = importlib.util.spec_from_file_location("xcheck_run_kvtrace", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_kvtrace_counts_shared_prefix_hits(tmp_path):
    trace = tmp_path / "trace.jsonl"
    rows = [{"timestamp": 0, "input_length": 1024, "output_length": 1, "hash_ids": [1, 2]},
            {"timestamp": 1000, "input_length": 1024, "output_length": 1, "hash_ids": [1, 2]}]
    trace.write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = tmp_path / "out.json"
    for mode in ("store", "scheduled"):
        assert _load().main([str(trace), "--work", str(tmp_path / mode), "--nodes", "1",
                             "--gpu-tokens", "0", "--block-tokens", "256", "--capacities", "inf",
                             "--lookup-time", mode, "-o", str(out)]) == 0
        res = json.loads(out.read_text())
        assert res["runs_mapped"]
        # Synth input has no metrics, so scheduled falls back to store-event time.
        assert res["lookup_time"]["model"] == "store"
        assert res["results"][0]["all"]["hit_tokens"] == 1024
