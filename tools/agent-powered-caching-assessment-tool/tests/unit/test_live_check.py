"""The pure helpers of the live check."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parents[2] / "scripts" / "scale"
sys.path.insert(0, str(_DIR))
_spec = importlib.util.spec_from_file_location("live_check", _DIR / "live_check.py")
live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live)

from synth_collection import build_collection  # noqa: E402


def test_summarize_counts_a_full_collection():
    doc = build_collection(10, 20)
    s = live.summarize(doc)
    assert s["tables"] == 10 and s["tables_with_columns"] == 10
    assert s["tables_with_primary_key"] == 10
    assert s["column_stats_rows"] == sum(
        len(t["column_stats"]) for t in doc["database_schema"]["tables"])
    assert s["query_patterns"] == 20
    assert s["probe_available"] is True
    assert s["failed_stages"] == [] and s["warnings"] == []


def test_summarize_reports_failed_stages_warnings_and_the_query_limit():
    doc = build_collection(3, 3)
    doc["metadata"]["stages"] = {"2_tables": {"status": "ok"},
                                 "2_views": {"status": "failed", "error": "RuntimeError"}}
    doc["metadata"]["warnings"] = ["2_views failed (RuntimeError): boom"]
    doc["queries"].update({"available_after_filter": 9, "dropped_by_limit": 6})
    s = live.summarize(doc)
    assert s["failed_stages"] == ["2_views"]
    assert s["warnings"] == ["2_views failed (RuntimeError): boom"]
    assert s["query_patterns_available_after_filter"] == 9
    assert s["query_patterns_dropped_by_limit"] == 6


def test_summarize_survives_an_empty_and_a_partial_collection():
    assert live.summarize({})["tables"] == 0
    part = {"database_schema": {"tables": [{"table_id": "a.b"}]}, "queries": None}
    s = live.summarize(part)
    assert s["tables"] == 1 and s["tables_with_columns"] == 0 and s["query_patterns"] == 0


def test_verdict_reports_each_mismatch_and_skips_unset_expectations():
    found = {"tables": 5, "tables_with_columns": 4, "tables_with_primary_key": 5,
             "column_stats_rows": 90}
    assert live.verdict(found, None, None) == []
    out = live.verdict(found, 5, 100)
    assert any("tables_with_columns" in x for x in out)
    assert any("column_stats_rows" in x for x in out)
    assert len(out) == 2
    only_tables = live.verdict(found, 5, 90)
    assert len(only_tables) == 1 and "tables_with_columns" in only_tables[0]


def test_verdict_clean_when_all_match():
    found = {"tables": 5, "tables_with_columns": 5, "tables_with_primary_key": 5,
             "column_stats_rows": 60}
    assert live.verdict(found, 5, 60) == []


def test_call_reports_unreachable_api_as_error():
    r = live.call(1, "GET", "/health", timeout=2)  # nothing listens on port 1
    assert "error" in r
