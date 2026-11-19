"""The psql output format and its parser."""

from __future__ import annotations

import pytest

from src.tools.database.tabular import (
    FIELD_SEP,
    PSQL_FORMAT_ARGS,
    RECORD_SEP,
    MalformedOutput,
    parse_tabular_output,
)


def out(*records: list[str], trailing_newline: bool = True) -> str:
    """Build psql output from records (the first is the header)."""
    text = RECORD_SEP.join(FIELD_SEP.join(r) for r in records)
    return text + ("\n" if trailing_newline else "")


def test_rows_become_dicts_with_lowercase_keys():
    rows = parse_tabular_output(out(["Name", "N"], ["a", "1"], ["b", "2.5"]))
    assert rows == [{"name": "a", "n": 1}, {"name": "b", "n": 2.5}]


def test_empty_output_and_header_only_give_no_rows():
    assert parse_tabular_output("") == []
    assert parse_tabular_output("\n") == []
    assert parse_tabular_output(out(["a", "b"])) == []


def test_newlines_and_tabs_inside_a_value_are_kept():
    """A view definition holds both. Under the old format the row was dropped."""
    definition = " SELECT a,\n\tb\n   FROM t;"
    rows = parse_tabular_output(out(["view_name", "definition"], ["v", definition]))
    assert rows == [{"view_name": "v", "definition": definition.strip()}]


def test_nulls_and_empty_values_are_none():
    rows = parse_tabular_output(out(["a", "b", "c"], ["NULL", "", "\\N"]))
    assert rows == [{"a": None, "b": None, "c": None}]


def test_a_blank_value_in_a_single_column_result_is_a_row():
    rows = parse_tabular_output(out(["a"], ["x"], [""], ["y"]))
    assert [r["a"] for r in rows] == ["x", None, "y"]


def test_large_integers_stay_exact():
    big = "1857227264912217097"
    assert parse_tabular_output(out(["q"], [big]))[0]["q"] == int(big)


def test_a_row_with_the_wrong_field_count_is_an_error_not_a_dropped_row():
    with pytest.raises(MalformedOutput, match="record 3"):
        parse_tabular_output(out(["a", "b"], ["1", "2"], ["only-one"]))


def test_no_trailing_newline_is_fine():
    assert parse_tabular_output(out(["a"], ["1"], trailing_newline=False)) == [{"a": 1}]


def test_format_args_set_both_separators_and_no_footer():
    args = list(PSQL_FORMAT_ARGS)
    assert args[args.index("-F") + 1] == FIELD_SEP
    assert args[args.index("-R") + 1] == RECORD_SEP
    assert "footer=off" in args and "-X" in args and "ON_ERROR_STOP=1" in args
