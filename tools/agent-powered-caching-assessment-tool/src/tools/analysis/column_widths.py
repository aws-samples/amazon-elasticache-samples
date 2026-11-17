"""
Byte width of a column, for sizing cached values.

Deterministic, and read from statistics the collector already gathered. No row
contents are read anywhere.

Sources, in order of preference:

    pg_stats      avg_width, the measured average over the analyzed sample.
    declared      pg_attribute typlen > 0: a fixed-width type (int4 is 4).
    declared max  varchar(n) / char(n) from atttypmod, or numeric(p, s). An
                  upper bound, not an average: a varchar(255) holding 12
                  characters is sized at 255. Assumes one byte per character.
    type map      udt_name for a known fixed-width type, for collections made
                  before collector 0.3.0.

A column none of these resolve (text or jsonb on a never-analyzed table) has no
width, and callers must leave the dependent figure unsized rather than guess.
"""

from __future__ import annotations

import math

# Fixed widths by type name, used when pg_attribute widths were not collected.
TYPE_BYTES: dict[str, int] = {
    "bool": 1, "boolean": 1, "char": 1,
    "int2": 2, "smallint": 2,
    "int4": 4, "int": 4, "integer": 4, "date": 4, "float4": 4, "real": 4, "oid": 4,
    "int8": 8, "bigint": 8, "float8": 8, "double precision": 8, "money": 8,
    "timestamp": 8, "timestamptz": 8, "time": 8, "interval": 16,
    "uuid": 16, "inet": 19, "macaddr": 6,
}

SOURCE_STATS = "pg_stats"
SOURCE_DECLARED = "declared"
SOURCE_DECLARED_MAX = "declared max"
SOURCE_TYPE_MAP = "type map"


def _numeric_bytes(typmod: int) -> int | None:
    """Upper bound for numeric(p, s): 8 header bytes plus 2 per 4 digits."""
    if typmod is None or typmod < 4:
        return None
    precision = ((typmod - 4) >> 16) & 0xFFFF
    return 8 + 2 * math.ceil(precision / 4) if precision else None


def column_width(table: dict, column: str) -> tuple[float | None, str | None]:
    """Average or bounding byte width of one column.

    Args:
        table: One table record from the collection's database_schema.tables.
        column: Column name, any case.

    Returns:
        (bytes, source). (None, None) when no source resolves the width.
    """
    col = str(column).lower()
    stats = {str(k).lower(): v for k, v in (table.get("column_stats") or {}).items()}
    w = (stats.get(col) or {}).get("avg_width")
    if w is not None:
        return float(w), SOURCE_STATS

    widths = {str(k).lower(): v for k, v in (table.get("column_widths") or {}).items()}
    decl = widths.get(col) or {}
    typlen, typmod = decl.get("typlen"), decl.get("typmod")
    type_name = str(decl.get("type_name") or "").lower()
    if typlen is not None and typlen > 0:
        return float(typlen), SOURCE_DECLARED
    if typmod is not None and typmod > 4:
        if type_name.startswith("numeric"):
            nb = _numeric_bytes(typmod)
            if nb:
                return float(nb), SOURCE_DECLARED_MAX
        elif type_name.startswith(("character", "varchar", "char", "bpchar")):
            return float(typmod - 4), SOURCE_DECLARED_MAX

    for c in table.get("columns") or []:
        if str(c.get("column_name") or "").lower() == col:
            udt = str(c.get("udt_name") or "").lower()
            if udt in TYPE_BYTES:
                return float(TYPE_BYTES[udt]), SOURCE_TYPE_MAP
            ml = c.get("max_length")
            try:
                if ml not in (None, "", "None") and int(float(ml)) > 0:
                    return float(int(float(ml))), SOURCE_DECLARED_MAX
            except (TypeError, ValueError):
                pass
            break
    return None, None


def table_columns(table: dict) -> list[str]:
    """Column names of a table record in ordinal order, lowercased."""
    cols = [str(c.get("column_name")).lower() for c in (table.get("columns") or [])
            if c.get("column_name")]
    if not cols:
        cols = [str(c).lower() for c in (table.get("column_widths") or {})]
    if not cols:
        cols = [str(c).lower() for c in (table.get("column_stats") or {})]
    return cols


def tables_by_id(collector_output: dict) -> dict[str, dict]:
    """Index collected table records by lowercased schema.table id."""
    out: dict[str, dict] = {}
    for t in (collector_output.get("database_schema") or {}).get("tables") or []:
        tid = str(t.get("table_id") or t.get("table_name") or "").lower()
        if tid:
            out[tid] = t
    return out
