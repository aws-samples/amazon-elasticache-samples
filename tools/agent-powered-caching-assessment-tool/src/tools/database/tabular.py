"""
psql output format shared by every executor, and its parser.

The collector reads query results from the standard output of `psql`. Two things about that
output used to lose data without any sign:

  * Rows were separated by newlines and fields by tabs. A value that holds a newline or a tab
    (a view definition always does) split into several lines, the line had the wrong number of
    fields, and the row was dropped.
  * A row with the wrong number of fields was dropped quietly.

The format here uses the ASCII unit separator (0x1F) between fields and the record separator
(0x1E) between rows. Neither occurs in catalog text, so newlines and tabs inside values are
safe. A row that still has the wrong number of fields is an error, never a dropped row.
"""

import re
from typing import Any

FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"

# Arguments every executor puts on the psql command line, after the connection arguments.
# -X: ignore ~/.psqlrc. -q: no notices. -A: unaligned. footer=off: no "(N rows)" line.
PSQL_FORMAT_ARGS = (
    "-X", "-q", "-A", "-v", "ON_ERROR_STOP=1",
    "-F", FIELD_SEP, "-R", RECORD_SEP, "-P", "footer=off",
)

_NULLS = ("NULL", "\\N", "")


class MalformedOutput(ValueError):
    """psql output that does not match its own header."""


def parse_tabular_output(raw: str) -> list[dict]:
    """Parse psql output in the format above into a list of dicts.

    Args:
        raw: Standard output of psql run with PSQL_FORMAT_ARGS. The first record is the header.

    Returns:
        One dict per row, keyed by the lower-cased column names. Numbers become int or float,
        and an empty value or NULL becomes None. An empty result gives an empty list.

    Raises:
        MalformedOutput: a row has a different number of fields than the header. Nothing is
            returned in that case, so a damaged result can never look like a short one.
    """
    text = raw.rstrip("\r\n")
    if not text.strip():
        return []
    records = text.split(RECORD_SEP)
    headers = [h.strip().lower() for h in records[0].split(FIELD_SEP)]
    rows: list[dict] = []
    for number, record in enumerate(records[1:], start=2):
        if record.strip("\r\n") == "" and len(headers) > 1:
            continue
        values = record.split(FIELD_SEP)
        if len(values) != len(headers):
            raise MalformedOutput(
                f"record {number} has {len(values)} fields, the header has {len(headers)}")
        rows.append({h: _coerce_value(v.strip()) for h, v in zip(headers, values, strict=True)})
    return rows


def _coerce_value(val: str) -> Any:
    """Convert a field to None, int or float where it reads as one, else keep the text."""
    if val in _NULLS:
        return None
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    try:
        return float(val)
    except ValueError:
        return val
