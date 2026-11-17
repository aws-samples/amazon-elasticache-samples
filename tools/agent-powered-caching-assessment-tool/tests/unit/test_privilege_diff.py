"""Tests for scripts/scale/privilege_diff.py: how a role's answer is compared with a superuser's."""
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "privilege_diff",
    Path(__file__).resolve().parents[2] / "scripts" / "scale" / "privilege_diff.py")
pdiff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pdiff)


def col(table, name, default="nextval('s')"):
    return {"schema_name": "s", "table_name": table, "column_name": name,
            "column_default": default}


def test_identical_answers_have_no_gap():
    rows = [col("t", "id"), col("t", "n")]
    r = pdiff.compare_rows(list(rows), list(rows))
    assert r == {"reader_rows": 2, "superuser_rows": 2, "rows_missing": 0, "hidden_values": {}}


def test_rows_the_role_cannot_see_are_counted_missing():
    r = pdiff.compare_rows([col("t", "id")], [col("t", "id"), col("u", "id")])
    assert r["rows_missing"] == 1


def test_a_value_blanked_for_the_role_is_hidden_not_missing():
    r = pdiff.compare_rows([col("t", "id", default=None)], [col("t", "id")])
    assert r["rows_missing"] == 0
    assert r["hidden_values"] == {"column_default": 1}


def test_insufficient_privilege_marker_counts_as_hidden():
    s = [{"queryid": "1", "query": "SELECT 1"}]
    r = pdiff.compare_rows([{"queryid": "1", "query": "<insufficient privilege>"}], s)
    assert r["hidden_values"] == {"query": 1}


def test_a_counter_that_moved_is_not_a_gap():
    r = pdiff.compare_rows([{"relname": "t", "n_live_tup": "10"}],
                           [{"relname": "t", "n_live_tup": "12"}])
    assert r["rows_missing"] == 0 and r["hidden_values"] == {}


def test_rows_without_identity_columns_pair_by_position():
    r = pdiff.compare_rows([{"size_gb": "1.5"}], [{"size_gb": "1.5"}, {"size_gb": "2"}])
    assert r["rows_missing"] == 1


def test_null_for_both_is_not_hidden():
    r = pdiff.compare_rows([col("t", "id", default=None)], [col("t", "id", default=None)])
    assert r["hidden_values"] == {}


def test_merge_and_gaps_name_each_method():
    report: dict = {}
    pdiff.merge(report, "collect_columns_all",
                pdiff.compare_rows([col("t", "id", default=None)], [col("t", "id")]))
    pdiff.merge(report, "collect_tables", {"error": "permission denied for schema s"})
    pdiff.merge(report, "get_version", {"reader_rows": 1, "superuser_rows": 1,
                                        "rows_missing": 0, "hidden_values": {}})
    assert pdiff.gaps(report) == [
        "collect_columns_all: column column_default hidden in 1 rows",
        "collect_tables: error: permission denied for schema s",
    ]
    assert report["get_version"]["queries"] == 1


class FakeExec:
    def __init__(self, rows=None, fail=None):
        self.rows, self.fail = rows or [], fail

    def run_sql_json(self, host, port, database, sql="", timeout=300):
        if self.fail:
            raise RuntimeError(self.fail)
        return self.rows

    def run_command(self, command, timeout=300):
        return "ok"


def test_comparing_executor_returns_the_roles_rows_and_records_errors():
    pair = pdiff.ComparingExecutor(FakeExec([{"a": "1"}]), FakeExec([{"a": "1"}, {"a": "2"}]))
    assert pair.run_sql_json("h", 1, "d", "select") == [{"a": "1"}]
    assert pair.report["unknown"]["rows_missing"] == 1
    bad = pdiff.ComparingExecutor(FakeExec(fail="denied"), FakeExec([{"a": "1"}]))
    with pytest.raises(RuntimeError):
        bad.run_sql_json("h", 1, "d", "select")
    assert bad.report["unknown"]["errors"] == ["denied"]
    assert pair.run_command("true") == "ok"
