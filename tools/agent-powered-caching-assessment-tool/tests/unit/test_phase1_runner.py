"""The pure helpers of the Phase 1 scale runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[2] / "scripts" / "scale"
sys.path.insert(0, str(_DIR))
_spec = importlib.util.spec_from_file_location("phase1_runner", _DIR / "phase1_runner.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def test_percentile_nearest_rank():
    v = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert runner.percentile(v, 50) == 5
    assert runner.percentile(v, 95) == 10
    assert runner.percentile(v, 0) == 1
    assert runner.percentile([7], 95) == 7
    assert runner.percentile([3, 1, 2], 100) == 3


@pytest.mark.parametrize("values,pct", [([], 50), ([1], -1), ([1], 101)])
def test_percentile_refuses_bad_input(values, pct):
    with pytest.raises(ValueError):
        runner.percentile(values, pct)


@pytest.mark.parametrize("text,want", [("  123456\n", 123456), ("", None), ("abc", None),
                                       ("12 34", None), ("-5", None)])
def test_parse_rss(text, want):
    assert runner.parse_rss_kb(text) == want


def test_brief_drops_body_and_keeps_error_detail():
    ok = runner.brief({"status": 200, "seconds": 1.0, "bytes": 9, "json": {"a": 1}})
    assert "json" not in ok and ok["bytes"] == 9
    bad = runner.brief({"status": 429, "seconds": 0.1, "bytes": 5, "json": {"code": "queue_full"}})
    assert bad["detail"] == {"code": "queue_full"}


def test_call_reports_connection_failure_as_error():
    r = runner.call(runner.free_port(), "GET", "/health")  # nothing listens there
    assert "error" in r and "status" not in r


def test_table_survives_a_failed_tier():
    text = runner.table([{"tier": "L0", "error": "boom"}, {"tier": "L2", "peak_rss_mb": 12.5,
                                                          "submit": {"seconds": 3.1}}])
    assert "boom" in text and "12.5" in text and "3.1" in text
