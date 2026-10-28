"""Analysis helpers of examples/knobs/measure_knobs.py (no engine, no Valkey)."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "examples", "knobs"))
import measure_knobs as mk  # noqa: E402


def _window(path, rate, probes, busy=1.0, errors=0, seconds=100.0):
    rows = [{"bench": "load", "kind": kind, "sent": 10.0 + i, "ttft": t}
            for kind, values in probes.items() for i, t in enumerate(values)]
    rows.append({"bench": "load", "kind": "window", "seconds": seconds, "ext_bytes": rate * 1e9 * seconds,
                 "valkey_in_bytes": rate * 1e9 * seconds, "valkey_evicted_keys": 0,
                 "host": {"busy_cores": busy}, "writer": {"procs": 8, "procs_reporting": 8, "errors": errors,
                                                          "cpu_s": 10.0 * rate}})
    with open(path, "w") as fh:
        json.dump({"meta": {}, "rows": rows}, fh)


def test_write_load_fits_ms_per_gbps_and_core_seconds(tmp_path):
    for rate in (0, 0.5, 1, 2):
        _window(tmp_path / f"w_bothcpu_{rate}.json", rate,
                {"probe_miss": [0.5 + 0.02 * rate] * 3, "probe_l2hit": [0.15 + 0.01 * rate] * 3},
                busy=0.2 + 1.5 * rate)
    _window(tmp_path / "w0end_bothcpu.json", 0, {"probe_miss": [0.503] * 3, "probe_l2hit": [0.15] * 3})
    out = mk.write_load(str(tmp_path))["bothcpu"]
    assert [p["requested_gbs"] for p in out["points"]] == [0, 0.5, 1, 2]
    assert out["fits"]["probe_miss_ms_per_gbps"]["slope"] == pytest.approx(20.0)
    assert out["fits"]["probe_l2hit_ms_per_gbps"]["slope"] == pytest.approx(10.0)
    assert out["fits"]["busy_cores_core_s_per_gb"]["slope"] == pytest.approx(1.5)
    assert out["baseline_drift_ms"]["probe_miss"] == pytest.approx(3.0)


def test_write_load_parses_modes_with_underscores_and_skips_bad_points(tmp_path):
    _window(tmp_path / "w_both_tc_0.json", 0, {"probe_miss": [0.5] * 3})
    _window(tmp_path / "w_both_tc_1.json", 1, {"probe_miss": [0.6] * 3})
    _window(tmp_path / "w_both_tc_2.json", 2, {"probe_miss": [9.0] * 3}, errors=5)
    _window(tmp_path / "w_both_tc_x.json", 0, {"probe_miss": [0.5] * 3})  # not a rate: ignored
    out = mk.write_load(str(tmp_path))
    assert list(out) == ["both_tc"]
    pts = out["both_tc"]["points"]
    assert len(pts) == 3 and pts[2]["writer_errors"] == 5
    # The point whose writer lost SETs is reported but not fitted.
    assert out["both_tc"]["fits"]["probe_miss_ms_per_gbps"]["slope"] == pytest.approx(100.0)


def test_write_load_without_baseline_has_no_fits(tmp_path):
    _window(tmp_path / "w_cpuonly_1.json", 1, {"probe_miss": [0.6] * 3})
    out = mk.write_load(str(tmp_path))["cpuonly"]
    assert out["fits"] == {} and out["baseline_drift_ms"] == {}


def test_break_even_tokens():
    b1 = {"a_s": 0.0, "b_s_per_tok": 1e-3, "c_s_per_tok2": 0.0}
    # Fetch 0.1 s + 1 ms per token (1 s/GB at 1e6 B/token) never beats 1 ms per token of recompute.
    assert mk.break_even_tokens({"intercept": 0.1, "slope": 1.0}, b1, 1_000_000)["tokens"] is None
    # Fetch 100 ms + 0.1 ms per token beats 1 ms per token above 111 tokens: first 256-step is 256.
    got = mk.break_even_tokens({"intercept": 0.1, "slope": 0.1}, b1, 1_000_000)
    assert got["tokens"] == 256 and got["fetch_s"] < got["recompute_s"]
    # Fixed cost 0.5 s: 0.5 + 0.1e-3 n < 1e-3 n above 556 tokens, so 768.
    assert mk.break_even_tokens({"intercept": 0.5, "slope": 0.1}, b1, 1_000_000)["tokens"] == 768
    assert mk.break_even_tokens({"intercept": 0.1, "slope": 0.1}, None, 1) is None
