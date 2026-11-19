"""
Per-query cache analysis: shape classification and cache economics.

WHY THIS EXISTS
---------------
`caching_scoring.py` aggregates everything to the table. That rollup is lossy in
a way that matters for a customer conversation. The per-table view reports
`retail.category` as "78.9 calls/sec at a 100% buffer hit ratio". The per-query
view reports that one four-table revenue aggregate consumes 1,336,272 ms of
database time, which is 33% of all query time in the workload. The second is the
fact a customer reacts to, and the first buries it by averaging one query across
four tables.

`pg_stat_statements` is per-query. The table rollup was always the lossy step.

OBJECT vs QUERY CACHING
-----------------------
These are different products with different keys, different invalidation, and
different failure modes. Conflating them produces advice that cannot be acted on.

    OBJECT   one row or entity, keyed by a primary or unique key.
             Invalidation is precise: only a write to that key matters.
             Example: SELECT ... FROM customer WHERE customer_id = $1

    QUERY    one result set, keyed by query identity plus parameter values.
             Invalidation is broad: any write to any table it reads.
             Example: a four-table GROUP BY revenue aggregate

The distinction is decided by the parser, not by a model and not by a threshold.

WHY A PARSER AND NOT A MODEL
----------------------------
Classification is a structural property of the SQL, so it is cheap, exact, and
reproducible to derive it from a parse tree. A model would be slower, more
expensive, and non-deterministic for no gain. The model's job is judgement about
whether caching is worth it, not recovering facts already present in the text.

Regular expressions were tried and rejected. They fail on `retail."order"`, on
`now()` nested inside `make_interval(days => $2)`, and on `WITH x AS (INSERT ...)`
which parses as a SELECT at the root and so reads as a query rather than a write.
That last one was a real defect: CTE-wrapped writes counted as neither read nor
write and understated the invalidation rate.

WRITES AND CLOCKS
-----------------
Read dominance is reads / (reads + measured writes on every table read), used
as-is for key lookups and result sets. A view counts through its base tables,
resolved from its pg_views definition. A clock function is acceptable only at
hourly or coarser granularity; anything finer changes the result without a write.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

# How many queries to analyse. Ranked by total database time, so the cap keeps
# the highest-value candidates by construction.
DEFAULT_TOP_N = 100

# Functions whose value changes independently of the data. A cached result is
# wrong when the clock moves, not when a row changes.
NON_DETERMINISTIC: set[str] = {
    "now", "current_timestamp", "current_date", "current_time",
    "clock_timestamp", "statement_timestamp", "timeofday",
    "random", "gen_random_uuid", "uuid_generate_v4",
    "current_user", "session_user", "user", "current_setting",
    "txid_current", "pg_backend_pid", "nextval",
}

# These return a different value on every call, so a query using them can never
# be cached.
UNCACHEABLE_FUNCS: set[str] = {
    "random", "gen_random_uuid", "uuid_generate_v4", "nextval",
}

# Clock functions. A query using one is a candidate only when every clock use
# resolves to hourly or coarser: date_trunc('day', now()), now()::date,
# current_date, extract(hour FROM now()). A raw now(), anything finer than an
# hour, or a truncation unit hidden behind a $n placeholder excludes it.
# pg_stat_statements replaces literals with placeholders, so date_trunc('day',
# now()) usually arrives as date_trunc($1, now()) and cannot be shown hourly.
CLOCK_ANON: set[str] = {
    "now", "clock_timestamp", "statement_timestamp", "transaction_timestamp",
    "timeofday", "localtimestamp", "current_timestamp",
}
HOURLY_OR_COARSER: set[str] = {
    "hour", "hours", "day", "days", "week", "weeks", "month", "months",
    "quarter", "year", "years", "decade", "century", "millennium",
    "dow", "doy", "isodow", "isoyear", "julian",
}
# Expressions a clock value passes through without being reduced to a unit.
_CLOCK_PASS_THROUGH = (exp.Add, exp.Sub, exp.Paren, exp.Interval, exp.Neg)

AGG_FUNCS: set[str] = {
    "sum", "count", "avg", "min", "max", "array_agg", "string_agg",
    "jsonb_agg", "json_agg", "bool_and", "bool_or", "stddev", "variance",
    "percentile_cont", "percentile_disc",
}

# Catalog and introspection schemas. Queries against these are this tool's own
# traffic or an ORM reading metadata, never customer workload, and must never be
# offered as cache candidates. Eight of the 38 patterns in the reference
# collection were exactly this, and the per-table scorer counted them as load.
SYSTEM_SCHEMAS: set[str] = {
    "pg_catalog", "information_schema", "pg_toast",
    "pg_temp_1", "pg_toast_temp_1",
}
SYSTEM_TABLE_PREFIXES: tuple[str, ...] = (
    "pg_stat", "pg_settings", "pg_locks", "pg_class", "pg_namespace",
    "pg_index", "pg_attribute", "pg_database",
)

CACHE_TYPES = ("OBJECT", "QUERY", "NOT_CACHEABLE", "UNKNOWN")
ACCESS_SHAPES = (
    "POINT_LOOKUP", "BOUNDED_RANGE", "FULL_SCAN", "AGGREGATE", "REPORT",
    "WRITE", "SYSTEM", "CONTROL", "UNKNOWN",
)
WRITE_QUERY_TYPES = {"INSERT", "UPDATE", "DELETE", "MERGE"}


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


@dataclass
class QueryShape:
    """Structural classification of one normalized query.

    Everything here is derived from the parse tree. No measurement, no
    judgement, no thresholds.
    """

    cache_type: str = "UNKNOWN"
    access_shape: str = "UNKNOWN"

    tables_read: list[str] = field(default_factory=list)
    tables_written: list[str] = field(default_factory=list)
    unresolved_relations: list[str] = field(default_factory=list)
    # Views read by the query, replaced in tables_read by their base tables.
    views_resolved: list[str] = field(default_factory=list)

    # Each clock-function use and the granularity it resolves to, for example
    # "now: raw" or "now: day". None when no clock function is used.
    clock_uses: list[str] = field(default_factory=list)
    clock_hourly_or_coarser: bool | None = None

    key_columns: list[str] = field(default_factory=list)
    equality_columns: list[str] = field(default_factory=list)
    range_columns: list[str] = field(default_factory=list)
    key_is_primary_key: bool = False
    caches_collection: bool = False

    non_deterministic: list[str] = field(default_factory=list)
    is_aggregate: bool = False
    has_join: bool = False
    has_group_by: bool = False
    has_window: bool = False
    has_limit: bool = False
    has_subquery: bool = False

    parse_ok: bool = True
    is_system_query: bool = False
    looks_truncated: bool = False
    reasons: list[str] = field(default_factory=list)

    # Cache Efficiency inputs, filled for cacheable reads when schema columns
    # are supplied. See cache_efficiency.extract_params / extract_outputs.
    params: list[dict] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)

    @property
    def is_cacheable(self) -> bool:
        """True when this query could be cached in some form."""
        return self.cache_type in ("OBJECT", "QUERY")


def _unit_of(node: exp.Expression) -> str | None:
    """The literal unit of a truncation or extract, or None when it is not literal.

    Args:
        node: A TimestampTrunc, DateTrunc, TimeTrunc, or Extract node.
    """
    unit = node.args.get("unit")
    if unit is None and isinstance(node, exp.Extract):
        unit = node.this
    if isinstance(unit, (exp.Var, exp.Literal, exp.Identifier)):
        return str(unit.name).strip("'").lower()
    if isinstance(unit, exp.Column) and not unit.table:
        return unit.name.lower()
    return None


def _clock_nodes(tree: exp.Expression) -> list[tuple[str, exp.Expression]]:
    """Every clock-function use in a statement, labelled by function."""
    out: list[tuple[str, exp.Expression]] = []
    for n in tree.find_all(exp.CurrentTimestamp):
        out.append(("now", n))
    for n in tree.find_all(exp.CurrentTime):
        out.append(("current_time", n))
    for n in tree.find_all(exp.CurrentDate):
        out.append(("current_date", n))
    for n in tree.find_all(exp.Anonymous):
        if isinstance(n.this, str) and n.this.lower() in CLOCK_ANON:
            out.append((n.this.lower(), n))
    return out


def clock_granularity(tree: exp.Expression) -> list[tuple[str, str]]:
    """Resolve each clock-function use to the granularity it is used at.

    Walks up from each clock value through arithmetic until something reduces it
    to a unit: a cast to date, date_trunc, or extract. Nothing reducing it means
    the value is used at full precision.

    Args:
        tree: Parsed statement.

    Returns:
        (function, granularity) per clock use. Granularity is a unit name such as
        "hour" or "day", "raw" at full precision, or "unknown" when a truncation
        unit is a placeholder.
    """
    result: list[tuple[str, str]] = []
    for label, node in _clock_nodes(tree):
        if label == "current_date":
            result.append((label, "day"))
            continue
        if label == "current_time":
            result.append((label, "raw"))
            continue
        gran = "raw"
        cur = node
        while cur.parent is not None:
            parent = cur.parent
            if isinstance(parent, (exp.Cast, exp.TryCast)):
                to = parent.args.get("to")
                if to is not None and to.is_type(exp.DataType.Type.DATE):
                    gran = "day"
                break
            if isinstance(parent, (exp.TimestampTrunc, exp.DateTrunc, exp.TimeTrunc,
                                   exp.Extract)):
                gran = _unit_of(parent) or "unknown"
                break
            if isinstance(parent, exp.Anonymous) and str(parent.this).lower() in (
                    "date_trunc", "date_part"):
                first = parent.expressions[0] if parent.expressions else None
                gran = (first.name.lower() if isinstance(first, exp.Literal)
                        and first.is_string else "unknown")
                break
            if not isinstance(parent, _CLOCK_PASS_THROUGH):
                break
            cur = parent
        result.append((label, gran))
    return result


def _qualify(table: exp.Table) -> str:
    """Render a table node as schema.table, lowercased, quotes stripped."""
    return f"{table.db}.{table.name}".lower() if table.db else table.name.lower()


def _resolve_name(name: str, known_tables: set[str]) -> str:
    """Map an unqualified table name onto a unique collected schema.table.

    Args:
        name: Name as parsed, qualified or not.
        known_tables: Qualified names of collected tables.

    Returns:
        The qualified name when exactly one collected table matches, otherwise
        the name unchanged. Ambiguity is left visible rather than guessed.
    """
    if name in known_tables or "." in name:
        return name
    matches = [k for k in known_tables if k.split(".", 1)[-1] == name]
    return matches[0] if len(matches) == 1 else name


def _looks_like_system_query(lowered_sql: str) -> bool:
    """Detect catalog and introspection traffic from raw text.

    Text-based rather than parse-based on purpose. These are exactly the queries
    sqlglot is most likely to reject, and an unparseable monitoring query must
    still be excluded. Running this check after the parse left this tool's own
    `pg_stat_statements` query classified as UNKNOWN instead of SYSTEM.

    Args:
        lowered_sql: Query text, already lowercased.

    Returns:
        True when the statement reads system catalogs or introspection views.
    """
    for schema in SYSTEM_SCHEMAS:
        if f"{schema}." in lowered_sql:
            return True
    for prefix in SYSTEM_TABLE_PREFIXES:
        # Require a FROM or JOIN position so a customer column merely named
        # something like pg_status_code does not trip this.
        if f"from {prefix}" in lowered_sql or f"join {prefix}" in lowered_sql:
            return True
    return False


def classify_query(
    sql: str,
    primary_keys: dict[str, list[str]] | None = None,
    known_tables: set[str] | None = None,
    views: dict[str, list[str]] | None = None,
    schema_columns: dict[str, dict[str, str]] | None = None,
) -> QueryShape:
    """Classify a normalized SQL statement for caching purposes.

    Args:
        sql: Query text from `pg_stat_statements`, with literals already replaced
            by $1-style placeholders.
        primary_keys: Qualified table name to primary key columns. Used to tell a
            unique-row lookup from a one-to-many collection read, which changes
            both the cached value size and the invalidation scope.
        known_tables: Qualified names of tables present in the collection. A
            relation absent from this set, and not a resolvable view, has an
            unknowable invalidation rate.
        views: View name to its base tables, from resolve_views(). A view in
            this map is replaced in tables_read by its base tables, so the
            query is charged with their writes. Shape classification still
            reflects the SQL as written.
        schema_columns: Qualified table -> column -> udt_name. When given, a
            cacheable read also gets `params` and `outputs` for Cache
            Efficiency. Omitted by callers that only need the shape.

    Returns:
        A populated QueryShape. When `parse_ok` is False, `cache_type` is UNKNOWN
        and callers MUST NOT treat the result as a classification.
    """
    primary_keys = primary_keys or {}
    shape = QueryShape()

    if not sql or not sql.strip():
        shape.parse_ok = False
        shape.reasons.append("empty query text")
        return shape

    lowered = sql.lower()

    # pg_stat_statements truncates at track_activity_query_size, default 1024
    # bytes. Recorded as a fact about the input. It is NOT used as the
    # explanation for parse failures, because most failures are not truncation.
    if len(sql) >= 1020 or sql.rstrip().endswith((",", "(", " and", " or", " =")):
        shape.looks_truncated = True

    if _looks_like_system_query(lowered):
        shape.is_system_query = True
        shape.cache_type = "NOT_CACHEABLE"
        shape.access_shape = "SYSTEM"
        shape.reasons.append(
            "reads system catalogs or introspection views, so this is tooling "
            "or ORM metadata traffic rather than customer workload"
        )
        return shape

    tree = None
    parse_error: Exception | None = None
    # Postgres dialect first. The generic dialect sometimes succeeds where the
    # Postgres tokenizer mis-pairs $n placeholders as dollar-quote delimiters.
    for dialect in ("postgres", None):
        try:
            tree = sqlglot.parse_one(sql, dialect=dialect)
            if tree is not None:
                break
        except Exception as exc:  # noqa: BLE001 - any parse error falls through
            parse_error = exc
            tree = None

    if tree is None:
        shape.parse_ok = False
        shape.cache_type = "UNKNOWN"
        kind = type(parse_error).__name__ if parse_error else "no tree"
        shape.reasons.append(
            f"could not be parsed ({kind}), so it is left unclassified rather "
            "than guessed at"
        )
        if shape.looks_truncated:
            shape.reasons.append(
                "query text may be truncated by track_activity_query_size"
            )
        return shape

    # --- statement kind -------------------------------------------------
    # A write can be nested. WITH x AS (INSERT ...) SELECT ... parses with a
    # Select at the root, so testing the root node alone reports it as a read.
    write_nodes = (
        list(tree.find_all(exp.Insert))
        + list(tree.find_all(exp.Update))
        + list(tree.find_all(exp.Delete))
        + list(tree.find_all(exp.Merge))
    )

    write_targets: set[str] = set()
    for node in write_nodes:
        target = node.this
        # UPDATE and DELETE put the table in .this; INSERT wraps it in a Schema.
        if isinstance(target, exp.Schema):
            target = target.this
        if isinstance(target, exp.Table):
            write_targets.add(_qualify(target))

    # CTE names appear as Table nodes but are not relations. Without dropping
    # them, WITH n AS (...) SELECT ... FROM n reads a "table" called n, which
    # then looks like an unresolved view.
    cte_names = {
        str(c.alias).lower() for c in tree.find_all(exp.CTE) if c.alias
    }
    all_tables = {
        _qualify(t) for t in tree.find_all(exp.Table)
        if not (not t.db and t.name.lower() in cte_names)
    }

    # Unqualified names are resolved by search_path at run time. Map a bare
    # name onto the one collected table with that name, so writes are charged
    # to the right table and the table is not mistaken for an unresolved view.
    views = views or {}
    if known_tables or views:
        names = (known_tables or set()) | set(views)
        all_tables = {_resolve_name(t, names) for t in all_tables}
        write_targets = {_resolve_name(t, names) for t in write_targets}

    shape.tables_written = sorted(write_targets)
    # A CTE-wrapped INSERT reads from its SELECT and writes to one target. Both
    # sets matter: reads for the cache key, writes for invalidation.
    shape.tables_read = sorted(all_tables - write_targets)

    # Second system pass, for catalog relations without a recognisable prefix.
    for t in all_tables | write_targets:
        schema = t.split(".", 1)[0] if "." in t else ""
        bare = t.split(".", 1)[-1]
        if schema in SYSTEM_SCHEMAS or bare.startswith(SYSTEM_TABLE_PREFIXES):
            shape.is_system_query = True
            shape.cache_type = "NOT_CACHEABLE"
            shape.access_shape = "SYSTEM"
            shape.reasons.append(
                "reads system catalogs, so this is tooling or ORM metadata "
                "traffic rather than customer workload"
            )
            return shape

    # --- non-deterministic functions ------------------------------------
    found_nd: set[str] = set()
    for node in tree.find_all(exp.Anonymous):
        if isinstance(node.this, str) and node.this.lower() in NON_DETERMINISTIC:
            found_nd.add(node.this.lower())
    # sqlglot normalises several of these into dedicated node types, so they
    # never appear as Anonymous and a name-only scan misses them entirely.
    for node_type, label in (
        (exp.CurrentTimestamp, "now" if "now(" in lowered else "current_timestamp"),
        (exp.CurrentDate, "current_date"),
        (exp.CurrentTime, "current_time"),
        (exp.CurrentUser, "current_user"),
        (exp.SessionUser, "session_user"),
        (exp.Rand, "random"),
        # gen_random_uuid() normalises to Uuid, so it has no function name to
        # match. Missing it would let a per-call-unique value be cached.
        (exp.Uuid, "gen_random_uuid"),
    ):
        if list(tree.find_all(node_type)):
            found_nd.add(label)
    shape.non_deterministic = sorted(found_nd)

    grans = clock_granularity(tree)
    if grans:
        shape.clock_uses = [f"{fn}: {g}" for fn, g in grans]
        shape.clock_hourly_or_coarser = all(g in HOURLY_OR_COARSER for _fn, g in grans)

    # --- shape signals --------------------------------------------------
    for node in tree.find_all(exp.Anonymous):
        if isinstance(node.this, str) and node.this.lower() in AGG_FUNCS:
            shape.is_aggregate = True
    for agg_type in (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max,
                     exp.ArrayAgg, exp.GroupConcat):
        if list(tree.find_all(agg_type)):
            shape.is_aggregate = True

    shape.has_group_by = bool(list(tree.find_all(exp.Group)))
    shape.has_window = bool(list(tree.find_all(exp.Window)))
    shape.has_join = bool(list(tree.find_all(exp.Join)))
    shape.has_limit = bool(list(tree.find_all(exp.Limit)))
    shape.has_subquery = bool(list(tree.find_all(exp.Subquery))) or bool(
        list(tree.find_all(exp.Exists))
    )
    if shape.has_group_by or shape.has_window:
        shape.is_aggregate = True

    # --- predicates -----------------------------------------------------
    eq_cols: list[str] = []
    range_cols: list[str] = []
    for where in tree.find_all(exp.Where):
        for eq in where.find_all(exp.EQ):
            col = eq.this if isinstance(eq.this, exp.Column) else (
                eq.expression if isinstance(eq.expression, exp.Column) else None
            )
            other = eq.expression if col is eq.this else eq.this
            # Only count column = placeholder. column = column is a join
            # predicate and is not a cache key.
            if col is not None and isinstance(
                other, (exp.Placeholder, exp.Parameter, exp.Literal)
            ):
                eq_cols.append(col.name.lower())
        for op in (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In):
            for node in where.find_all(op):
                if isinstance(node.this, exp.Column):
                    range_cols.append(node.this.name.lower())
    shape.equality_columns = sorted(set(eq_cols))
    shape.range_columns = sorted(set(range_cols))

    # --- classify -------------------------------------------------------
    if write_nodes:
        shape.cache_type = "NOT_CACHEABLE"
        shape.access_shape = "WRITE"
        shape.reasons.append(
            "write statement. Not a cache candidate itself, but it invalidates "
            "cached entries for the tables it writes"
        )
        return shape

    # Not a read at all: BEGIN, COMMIT, ROLLBACK, SET, SHOW. These parse to
    # their own node types, and without this check fell through to the read
    # branches and scored as query-cache candidates.
    if not list(tree.find_all(exp.Select)):
        shape.cache_type = "NOT_CACHEABLE"
        shape.access_shape = "CONTROL"
        shape.reasons.append(
            "transaction control or session statement, not a read that returns data"
        )
        return shape

    # A SELECT that reads no table, such as SELECT version() or a function call.
    # There is no data behind it to cache and nothing a write could invalidate.
    if not shape.tables_read:
        shape.cache_type = "NOT_CACHEABLE"
        shape.access_shape = "CONTROL"
        shape.reasons.append(
            "reads no table, so there is no stored data to cache"
        )
        return shape

    if found_nd & UNCACHEABLE_FUNCS:
        shape.cache_type = "NOT_CACHEABLE"
        shape.access_shape = "REPORT" if shape.is_aggregate else "FULL_SCAN"
        shape.reasons.append(
            f"uses {', '.join(sorted(found_nd & UNCACHEABLE_FUNCS))}, which "
            "returns a different value on every call"
        )
        return shape

    n_tables = len(shape.tables_read)

    if shape.is_aggregate:
        shape.access_shape = "REPORT" if (shape.has_join or n_tables > 1) else "AGGREGATE"
        shape.cache_type = "QUERY"
        shape.reasons.append(
            "aggregate result, so the cacheable unit is the computed result set "
            "keyed by its parameters, not any single row"
        )
    elif n_tables > 1 or shape.has_join:
        shape.access_shape = "BOUNDED_RANGE" if shape.equality_columns else "FULL_SCAN"
        shape.cache_type = "QUERY"
        shape.reasons.append(
            f"joins {n_tables} tables, so the cacheable unit is the joined "
            "result, not a row from any one table"
        )
    elif shape.equality_columns and not shape.range_columns and not shape.has_group_by:
        shape.cache_type = "OBJECT"
        shape.access_shape = "POINT_LOOKUP"
        shape.key_columns = shape.equality_columns

        table = shape.tables_read[0] if shape.tables_read else ""
        pk = [c.lower() for c in primary_keys.get(table, [])]
        shape.key_is_primary_key = bool(pk) and set(shape.equality_columns) == set(pk)
        shape.caches_collection = bool(pk) and not shape.key_is_primary_key

        if shape.key_is_primary_key:
            shape.reasons.append(
                f"single-table lookup on the primary key "
                f"({', '.join(shape.equality_columns)}), so it caches as one "
                "object per key and only a write to that same key invalidates it"
            )
        elif shape.caches_collection:
            shape.reasons.append(
                f"single-table lookup on {', '.join(shape.equality_columns)}, "
                f"which is not the primary key ({', '.join(pk)}), so it returns "
                "several rows and caches as a collection. The value size varies "
                "and any insert or update for that key invalidates the entry"
            )
        else:
            shape.reasons.append(
                f"single-table equality lookup on "
                f"{', '.join(shape.equality_columns)}, but the primary key is "
                "unknown so uniqueness could not be confirmed"
            )
    elif shape.range_columns:
        shape.cache_type = "QUERY"
        shape.access_shape = "BOUNDED_RANGE"
        shape.reasons.append(
            f"single table filtered by a range on "
            f"{', '.join(shape.range_columns)}, so the result set is the unit"
        )
    else:
        shape.cache_type = "QUERY"
        shape.access_shape = "FULL_SCAN"
        shape.reasons.append(
            "unfiltered single-table read, so the whole result set is the unit"
        )

    if shape.cache_type != "OBJECT" and shape.equality_columns:
        shape.key_columns = shape.equality_columns

    if shape.clock_uses:
        if shape.clock_hourly_or_coarser:
            shape.reasons.append(
                f"uses the clock at hourly or coarser granularity "
                f"({'; '.join(shape.clock_uses)}), so the result only changes at "
                "that boundary"
            )
        else:
            shape.reasons.append(
                f"uses the clock finer than hourly ({'; '.join(shape.clock_uses)}), "
                "so the result changes continuously without any write"
            )
    other_nd = sorted(set(found_nd) - {"now", "current_timestamp", "current_date",
                                       "current_time", "clock_timestamp",
                                       "statement_timestamp", "timeofday"})
    if other_nd:
        shape.reasons.append(
            f"depends on {', '.join(other_nd)}, which varies by session"
        )

    # Views are charged with their base tables' writes. Done after shape
    # classification so the shape reflects the SQL as written, while
    # invalidation and attribution use the tables that actually change.
    if views:
        expanded: set[str] = set()
        for t in shape.tables_read:
            if t in views:
                shape.views_resolved.append(t)
                expanded.update(views[t])
            else:
                expanded.add(t)
        if shape.views_resolved:
            shape.tables_read = sorted(expanded)
            for v in shape.views_resolved:
                shape.reasons.append(
                    f"reads view {v}, resolved from its definition to "
                    f"{', '.join(views[v])}, so it is invalidated by writes to those"
                )

    if known_tables is not None:
        shape.unresolved_relations = sorted(
            t for t in shape.tables_read if t not in known_tables
        )
        if shape.unresolved_relations:
            shape.reasons.append(
                f"reads {', '.join(shape.unresolved_relations)}, which is neither a "
                "collected table nor a view with a collected definition, so its "
                "invalidation rate cannot be computed"
            )

    if schema_columns is not None and shape.is_cacheable:
        _attach_efficiency_inputs(shape, tree, known_tables, views, schema_columns)

    return shape


def _attach_efficiency_inputs(
    shape: QueryShape,
    tree: exp.Expression,
    known_tables: set[str] | None,
    views: dict[str, list[str]] | None,
    schema_columns: dict[str, dict[str, str]],
) -> None:
    """Fill shape.params and shape.outputs for Cache Efficiency.

    A failure here leaves both empty and logs it, so classification itself is
    never lost to an efficiency-input problem.

    Args:
        shape: The shape being built, updated in place.
        tree: Parsed statement.
        known_tables: Qualified collected table names.
        views: View -> base tables.
        schema_columns: Qualified table -> column -> udt_name.
    """
    from src.tools.analysis.cache_efficiency import (
        build_alias_map,
        extract_outputs,
        extract_params,
    )

    names = (known_tables or set()) | set(views or {})
    try:
        aliases = build_alias_map(tree, lambda n: _resolve_name(n, names))
        table_columns = {t: set(cols) for t, cols in schema_columns.items()}
        shape.params = extract_params(tree, aliases, table_columns)
        shape.outputs = extract_outputs(tree, aliases, table_columns, schema_columns)
    except Exception as exc:  # noqa: BLE001 - never lose the classification
        logger.warning("efficiency inputs failed for query: %s", exc)
        shape.params, shape.outputs = [], []


def resolve_views(views: list[dict], known_tables: set[str]) -> dict[str, list[str]]:
    """Map each view to the base tables it reads, following nested views.

    Each pg_views definition is parsed with the same classifier as queries, so
    CTE names, quoting, and unqualified names are handled identically.

    Args:
        views: Rows from pg_views: schema_name, view_name, definition.
        known_tables: Qualified names of collected tables.

    Returns:
        view -> sorted base tables. A view whose definition cannot be parsed,
        that reads no table, or that sits in a cycle is left out, so callers
        still treat it as unresolved.
    """
    defs: dict[str, str] = {}
    for v in views or []:
        schema = str(v.get("schema_name") or "").lower()
        name = str(v.get("view_name") or "").lower()
        if schema and name and v.get("definition"):
            defs[f"{schema}.{name}"] = str(v["definition"])
    names = set(known_tables) | set(defs)

    direct: dict[str, list[str] | None] = {}
    for view, sql in defs.items():
        try:
            shape = classify_query(sql, known_tables=names)
        except Exception as exc:  # noqa: BLE001 - never fail analysis on one view
            logger.warning("resolve_views: could not classify %s: %s", view, exc)
            direct[view] = None
            continue
        direct[view] = shape.tables_read if shape.parse_ok and shape.tables_read else None

    def expand(view: str, seen: frozenset) -> set[str] | None:
        if view in seen or direct.get(view) is None:
            return None
        out: set[str] = set()
        for t in direct[view]:
            if t in defs:
                sub = expand(t, seen | {view})
                if sub is None:
                    return None
                out |= sub
            else:
                out.add(t)
        return out

    resolved: dict[str, list[str]] = {}
    for view in defs:
        base = expand(view, frozenset())
        if base:
            resolved[view] = sorted(base)
        else:
            logger.warning("resolve_views: %s left unresolved", view)
    return resolved


def build_view_map(collector_output: dict, known_tables: set[str]) -> dict[str, list[str]]:
    """Resolve the collection's view definitions to base tables.

    Args:
        collector_output: Raw collector output.
        known_tables: Qualified names of collected tables.

    Returns:
        view -> base tables. Empty when the collection carries no views, which
        is true of artifacts written before views were collected.
    """
    views = (collector_output.get("database_schema") or {}).get("views") or []
    return resolve_views(views, known_tables)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@dataclass
class QueryCacheProfile:
    """Per-query aggregate and cache economics.

    Two figures carry the customer conversation:

        db_time_ms_per_sec       database time you stop paying at a 100% hit
                                 rate. An explicit upper bound, not a forecast.
        invalidating_write_rate  writes per second against the tables this query
                                 reads. Answers whether a cache would hold.
    """

    queryid: str = ""
    query_text: str = ""
    query_type: str = ""
    shape: QueryShape = field(default_factory=QueryShape)

    calls_per_second: float = 0.0
    avg_exec_ms: float = 0.0
    total_time_ms: float = 0.0
    rows_returned_avg: float = 0.0
    buffer_hit_ratio_pct: float | None = None
    io_read_time_share: float | None = None
    temp_blocks_written: int = 0

    db_time_ms_per_sec: float = 0.0
    db_time_share_pct: float = 0.0
    invalidating_write_rate: float = 0.0
    invalidation_ratio: float | None = None
    write_sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # --- The four dimensions, same scorer and thresholds as tables ---
    # reads / (reads + measured writes on every table read).
    read_dominance: float | None = None
    breakdown: object | None = None          # CacheScoreBreakdown
    deterministic_verdict: str | None = None

    # --- Cache Efficiency inputs and result ---
    calls_in_window: float | None = None     # N: calls since the last stats reset
    window_seconds: float | None = None
    efficiency: object | None = None         # cache_efficiency.EfficiencyResult


def build_schema_maps(collector_output: dict) -> tuple[dict[str, list[str]], set[str]]:
    """Extract primary keys and known table names from a collection.

    Args:
        collector_output: Raw collector output.

    Returns:
        (primary_keys_by_table, known_table_names), both lowercased.
    """
    pks: dict[str, list[str]] = {}
    known: set[str] = set()
    for t in (collector_output.get("database_schema") or {}).get("tables") or []:
        tid = str(t.get("table_id") or t.get("table_name") or "").lower()
        if not tid:
            continue
        known.add(tid)
        pk = t.get("primary_key") or []
        if pk:
            pks[tid] = [str(c).lower() for c in pk]
    return pks, known


def build_write_rate_map(
    queries: list[dict],
    primary_keys: dict[str, list[str]] | None = None,
    known_tables: set[str] | None = None,
) -> dict[str, float]:
    """Writes per second per table, derived from the workload's write statements.

    Uses parsed write targets rather than `pg_stat_user_tables` counters, because
    those counters are cumulative tuple counts while this needs a statement rate
    directly comparable to a read call rate.

    Args:
        queries: All collected query patterns.
        primary_keys: Passed through to the classifier.
        known_tables: Passed through to the classifier.

    Returns:
        Qualified table name to writes per second.
    """
    rates: dict[str, float] = {}
    for q in queries:
        shape = classify_query(
            q.get("query_text") or "", primary_keys, known_tables
        )
        if not shape.tables_written:
            continue
        cps = float(q.get("calls_per_second") or 0.0)
        for t in shape.tables_written:
            rates[t] = rates.get(t, 0.0) + cps
    return {k: round(v, 4) for k, v in rates.items()}


def build_solo_read_map(
    queries: list[dict],
    primary_keys: dict[str, list[str]] | None = None,
    known_tables: set[str] | None = None,
    views: dict[str, list[str]] | None = None,
) -> dict[str, dict]:
    """Single-table reads of each table: what object caching would serve.

    A table's efficiency is judged on the reads a row cache would replace, the
    cacheable reads of that table alone. Joined reports return rows of several
    tables, so their row counts are not this table's cache unit.

    Args:
        queries: All collected query patterns.
        primary_keys: Passed through to the classifier.
        known_tables: Passed through to the classifier.
        views: View to base tables, so a view over two tables is not a solo read.

    Returns:
        table -> {"rows_per_read": call-weighted rows per read (a plain mean when
        no call rate is known), "keyable": True when any such read filters by
        equality or not at all, "reads": number of distinct read patterns}.
        Tables with no single-table read are absent.
    """
    acc: dict[str, dict] = {}
    for q in queries:
        sh = classify_query(q.get("query_text") or "", primary_keys, known_tables, views)
        if not sh.is_cacheable or len(sh.tables_read) != 1 or sh.tables_written:
            continue
        t = sh.tables_read[0]
        cps = float(q.get("calls_per_second") or 0.0)
        rows = float(q.get("rows_returned_avg") or 0.0)
        a = acc.setdefault(t, {"w": 0.0, "wrows": 0.0, "n": 0, "rows": 0.0,
                               "keyable": False})
        a["w"] += cps
        a["wrows"] += cps * rows
        a["n"] += 1
        a["rows"] += rows
        a["keyable"] = a["keyable"] or not sh.range_columns
    return {
        t: {"rows_per_read": round(a["wrows"] / a["w"] if a["w"] > 0 else a["rows"] / a["n"], 2),
            "keyable": a["keyable"], "reads": a["n"]}
        for t, a in acc.items()
    }


def build_query_profiles(
    collector_output: dict,
    top_n: int = DEFAULT_TOP_N,
    thresholds=None,
) -> tuple[list[QueryCacheProfile], dict]:
    """Build per-query profiles for the top N queries by database time.

    Ranked by `total_time_ms`, which is total database time consumed. That is the
    same ordering as `db_time_ms_per_sec`, so the cap retains the highest-value
    candidates by construction rather than by luck.

    Cache Efficiency is computed here for every cacheable read, because it
    needs the column statistics in the collection.

    Args:
        collector_output: Raw collector output.
        top_n: How many queries to analyse.
        thresholds: CacheScoringThresholds for Efficiency, or None for defaults.

    Returns:
        (profiles, summary). The summary records coverage and what the cap
        dropped, so a reader can tell whether the top N is the whole story.
    """
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    if not queries:
        return [], {
            "patterns_collected": 0,
            "analysed": 0,
            "dropped_by_cap": 0,
            "db_time_covered_pct": 0.0,
            "note": "No query patterns collected; pg_stat_statements was "
                    "unavailable, so per-query analysis is not possible",
        }

    pks, known = build_schema_maps(collector_output)
    view_map = build_view_map(collector_output, known)
    write_rates = build_write_rate_map(queries, pks, known)
    total_db_time = sum(float(q.get("total_time_ms") or 0.0) for q in queries)

    ranked = sorted(queries, key=lambda q: -(float(q.get("total_time_ms") or 0.0)))
    selected = ranked[:top_n]

    from src.tools.analysis.cache_efficiency import build_table_stats, score_query_efficiency
    table_stats = build_table_stats(collector_output)
    schema_columns = {t: s.column_types or dict.fromkeys(s.columns, "")
                      for t, s in table_stats.items()}

    profiles: list[QueryCacheProfile] = []
    for q in selected:
        shape = classify_query(q.get("query_text") or "", pks, known, view_map,
                               schema_columns)
        cps = float(q.get("calls_per_second") or 0.0)
        ms = float(q.get("execution_time_ms_avg") or 0.0)
        tt = float(q.get("total_time_ms") or 0.0)

        p = QueryCacheProfile(
            queryid=resolve_query_id(q),
            query_text=q.get("query_text") or "",
            query_type=str(q.get("query_type") or ""),
            shape=shape,
            calls_per_second=round(cps, 4),
            avg_exec_ms=round(ms, 4),
            total_time_ms=round(tt, 2),
            rows_returned_avg=float(q.get("rows_returned_avg") or 0.0),
            buffer_hit_ratio_pct=q.get("cache_hit_ratio_pct"),
            temp_blocks_written=int(q.get("temp_blocks_written") or 0),
        )
        io_ms = float(q.get("io_read_time_ms") or 0.0)
        p.io_read_time_share = round(min(io_ms / tt, 1.0), 4) if tt > 0 else None

        # Cache Efficiency: N is the measured call count; fall back to rate x
        # window when only the rate is present. Over a collection window it uses
        # the counts since the last reset (`since_reset`): an estimated hit rate needs
        # a long run of calls, and the result size does not depend on the window.
        eff_src = q.get("since_reset") or q
        window = eff_src.get("stats_window_seconds")
        p.window_seconds = float(window) if window else None
        calls = eff_src.get("execution_count")
        eff_cps = float(eff_src.get("calls_per_second") or cps)
        if calls is None and p.window_seconds:
            calls = eff_cps * p.window_seconds
        p.calls_in_window = float(calls) if calls is not None else None
        if shape.is_cacheable and not shape.tables_written:
            p.efficiency = score_query_efficiency(
                calls_in_window=p.calls_in_window, window_seconds=p.window_seconds,
                exec_ms=float(eff_src.get("execution_time_ms_avg") or ms),
                rows_per_call=float(eff_src.get("rows_returned_avg")
                                    if eff_src.get("rows_returned_avg") is not None
                                    else p.rows_returned_avg),
                params=shape.params, outputs=shape.outputs, stats=table_stats,
                thresholds=thresholds)

        p.db_time_ms_per_sec = round(cps * ms, 2)
        p.db_time_share_pct = (
            round(tt / total_db_time * 100, 2) if total_db_time else 0.0
        )

        # Every write to a table this query reads can make the cached result
        # wrong. Sum their statement rates.
        inval = 0.0
        for t in shape.tables_read:
            if t in write_rates:
                inval += write_rates[t]
                p.write_sources.append(f"{t} at {write_rates[t]:.2f} writes/s")
        p.invalidating_write_rate = round(inval, 4)

        if cps > 0 and shape.is_cacheable:
            p.invalidation_ratio = round(inval / cps, 4)

        for reason in shape.reasons:
            if reason.startswith("reads view ") or "neither a collected table" in reason:
                p.notes.append(reason)

        profiles.append(p)

    # Identifiers must be present and unique, or per-query verdicts cannot be
    # matched back to their measurements. This previously failed silently.
    ids = [p.queryid for p in profiles]
    blank = sum(1 for i in ids if not i)
    duplicates = len(ids) - len(set(ids))
    if blank or duplicates:
        logger.error(
            "Query identifiers are not usable: %d blank, %d duplicate out of %d. "
            "Per-query verdicts cannot be matched to measurements.",
            blank, duplicates, len(ids),
        )

    cacheable = [p for p in profiles if p.shape.is_cacheable]
    summary = {
        "queryid_blank": blank,
        "queryid_duplicated": duplicates,
        "patterns_collected": len(queries),
        "analysed": len(selected),
        "dropped_by_cap": max(0, len(queries) - top_n),
        "top_n": top_n,
        "total_db_time_ms": round(total_db_time, 2),
        "db_time_covered_pct": round(
            sum(p.total_time_ms for p in profiles) / total_db_time * 100, 2
        ) if total_db_time else 0.0,
        "cache_type_counts": _counts(p.shape.cache_type for p in profiles),
        "access_shape_counts": _counts(p.shape.access_shape for p in profiles),
        "object_candidates": sum(
            1 for p in profiles if p.shape.cache_type == "OBJECT"),
        "query_candidates": sum(
            1 for p in profiles if p.shape.cache_type == "QUERY"),
        "system_queries_excluded": sum(
            1 for p in profiles if p.shape.is_system_query),
        "unparseable": sum(1 for p in profiles if not p.shape.parse_ok),
        "views_resolved": sorted(view_map),
        "queries_on_resolved_views": sum(1 for p in profiles if p.shape.views_resolved),
        "queries_on_unresolved_relations": sum(
            1 for p in profiles if p.shape.unresolved_relations),
        "total_db_time_ms_per_sec": round(
            sum(p.db_time_ms_per_sec for p in profiles), 2),
        "cacheable_db_time_ms_per_sec": round(
            sum(p.db_time_ms_per_sec for p in cacheable), 2),
        "write_rate_by_table": write_rates,
    }
    return profiles, summary


# ---------------------------------------------------------------------------
# Scoring: the same four dimensions and thresholds as object caching
# ---------------------------------------------------------------------------


def _query_gate(p: QueryCacheProfile) -> str | None:
    """Reasons a query cannot be cached at all, checked before scoring.

    Args:
        p: The query profile.

    Returns:
        An exclusion reason, or None when the query goes on to be scored.
    """
    sh = p.shape
    if sh.cache_type in ("NOT_CACHEABLE", "UNKNOWN"):
        return (sh.reasons[0] if sh.reasons else "not cacheable").capitalize()
    # A clock used finer than hourly changes the result without any write.
    if sh.clock_uses and not sh.clock_hourly_or_coarser:
        detail = "; ".join(sh.clock_uses)
        hint = (" A truncation unit given as a placeholder cannot be shown to be "
                "hourly or coarser." if "unknown" in detail else "")
        return (
            f"Time-dependent: uses the clock finer than hourly ({detail}), so the "
            f"result changes continuously without any write.{hint}"
        )
    # Views are resolved to base tables from pg_views. Anything still unresolved
    # has no measured write rate, and there is no substitute for one, so it is
    # excluded rather than scored against zero.
    if sh.unresolved_relations:
        return (
            f"Insufficient data: reads {', '.join(sh.unresolved_relations)}, which "
            "is neither a collected table nor a view with a collected definition, "
            "so its write rate is unknown"
        )
    return None


def score_query_candidacy(p: QueryCacheProfile, context, thresholds,
                          table_profiles: dict | None = None) -> None:
    """Score one query on the four dimensions, in place.

    Expresses the query in the table-profile shape and runs the unchanged
    table scorer, so the thresholds, weights, bands, and hard exclusions are the
    same by construction rather than by copy.

    Mapping:
      read dominance   calls / (calls + measured writes on every table read)
      frequency        the query's calls/s
      pressure         the query's own duration, hit ratio, disk share, spill
      efficiency       p.efficiency from build_query_profiles: estimated hit
                       rate and result size (cache_efficiency)
      churn, locks     the worst of the tables it reads

    Writes are measured and used as-is, for key lookups and result sets alike.
    Views count through their base tables, resolved from pg_views.

    Args:
        p: The query profile. Scores and verdict are written onto it.
        context: WorkloadContext from caching_scoring.
        thresholds: CacheScoringThresholds.
        table_profiles: Table profiles by table id, for churn and lock signals.
            Optional; without them a query carries no churn penalty.
    """
    from src.tools.analysis.caching_scoring import (
        CacheScoreBreakdown,
        CacheTableProfile,
        classify_verdict,
        score_cache_candidacy,
    )

    gate = _query_gate(p)
    if gate:
        # A parser gate, not a disqualifier: the query cannot be cached
        # correctly at all. Still blocks recommendation (spec DQ-9).
        b = CacheScoreBreakdown(hard_excluded=True, exclusion_reason=gate,
                                exclusion_reasons=[gate],
                                exclusion_kind="not_cacheable")
        b.concerns.append(gate)
        p.breakdown = b
        p.deterministic_verdict = "NOT_RECOMMENDED"
        return

    sh = p.shape
    writes = p.invalidating_write_rate
    cps = p.calls_per_second
    rd = cps / (cps + writes) if (cps + writes) > 0 else 0.0
    p.read_dominance = round(rd, 4)

    # Churn and lock contention belong to tables. A query inherits the worst of
    # the tables it reads, so a query over a churning table is penalised too.
    by_name = {str(k).lower(): v for k, v in (table_profiles or {}).items()}
    read_tables = [by_name[t] for t in sh.tables_read if t in by_name]
    tp = CacheTableProfile(
        table_id=p.queryid,
        total_calls_per_second=cps,
        read_calls_per_second=cps,
        write_calls_per_second=writes,
        read_ratio_weighted=round(rd, 4),
        read_ratio_known=(cps + writes) > 0,
        query_count=1,
        weighted_exec_time_ms=p.avg_exec_ms,
        buffer_hit_ratio_pct=p.buffer_hit_ratio_pct,
        io_read_time_share=p.io_read_time_share,
        temp_blocks_written=p.temp_blocks_written,
        max_rows_returned=p.rows_returned_avg,
        rows_per_read=p.rows_returned_avg,
        # Every distinct range is its own entry, so a range filter is hard to key.
        keyable=not sh.range_columns,
        size_mb=None,
        dead_tuple_pct=max((t.dead_tuple_pct for t in read_tables), default=0.0),
        is_lock_contended=any(t.is_lock_contended for t in read_tables),
        db_ms_per_sec=p.db_time_ms_per_sec,
    )
    b = score_cache_candidacy(tp, context, thresholds, efficiency=p.efficiency)

    # Say what read dominance means for a query, so the generic evidence wording
    # ("of calls are reads") is not misread.
    basis = (
        f"Read dominance {rd:.0%}: {cps:.2f} calls/s against {writes:.2f} "
        "measured writes/s on the tables it reads"
    )
    if sh.views_resolved:
        basis += f"; views resolved to base tables: {', '.join(sh.views_resolved)}"
    b.evidence.insert(0, basis)

    p.breakdown = b
    p.deterministic_verdict = classify_verdict(b)


def table_db_time_incl_joins(profiles: list[QueryCacheProfile]) -> dict[str, float]:
    """Sum the database time of every analysed read that reads each table.

    A query joining four tables counts toward all four, so the totals overlap
    and must never be summed across tables. Write statements are skipped: their
    time is not cacheable read time.

    Args:
        profiles: Query profiles from build_query_profiles().

    Returns:
        table_id -> ms of database time per second, rounded to 0.01.
    """
    totals: dict[str, float] = {}
    for p in profiles:
        if p.shape.tables_written:
            continue
        for t in set(p.shape.tables_read):
            totals[t] = totals.get(t, 0.0) + (p.db_time_ms_per_sec or 0.0)
    return {t: round(v, 2) for t, v in totals.items()}


def table_efficiency_rollup(profiles: list[QueryCacheProfile],
                            thresholds=None) -> dict[str, object]:
    """Table-level Cache Efficiency from the read queries that read each table.

    Every cacheable read that passes the query gates counts toward every table
    it reads, joins included (spec R6.1). Write statements and gated queries
    (not cacheable, time-dependent, insufficient data) are left out.

    Args:
        profiles: Query profiles from build_query_profiles(), with efficiency.
        thresholds: CacheScoringThresholds, or None for defaults.

    Returns:
        table id (lowercased) -> cache_efficiency.EfficiencyResult.
    """
    from src.tools.analysis.cache_efficiency import rollup_table_efficiency

    by_table: dict[str, list[tuple[float, object]]] = {}
    for p in profiles:
        if p.efficiency is None or _query_gate(p):
            continue
        for t in set(p.shape.tables_read):
            by_table.setdefault(t, []).append((p.calls_in_window or 0.0, p.efficiency))
    return {t: rollup_table_efficiency(rs, thresholds) for t, rs in by_table.items()}


def score_query_profiles(profiles: list[QueryCacheProfile], context,
                         thresholds, table_profiles: dict | None = None) -> None:
    """Score every profile in place with the shared four-dimension scorer.

    Args:
        profiles: Query profiles from build_query_profiles().
        context: WorkloadContext from caching_scoring.
        thresholds: CacheScoringThresholds.
        table_profiles: Table profiles by table id, for churn and lock signals.
    """
    for p in profiles:
        score_query_candidacy(p, context, thresholds, table_profiles)


def resolve_query_id(q: dict) -> str:
    """Stable identifier for one query pattern.

    The collector emits `query_id`. An earlier version of this module read
    `queryid`, which is absent, so every record received an empty string. They
    then collapsed to a single dict key and the model's per-query verdicts could
    not be matched back to any record. The failure was silent because an empty
    id is still a valid key.

    Falls back to a hash of the normalised query text when no id is supplied, so
    matching never depends on a collector field being present.

    Args:
        q: One query pattern from the collector.

    Returns:
        A non-empty identifier.
    """
    for key in ("query_id", "queryid"):
        val = q.get(key)
        if val not in (None, "", "None"):
            return str(val)
    text = (q.get("query_text") or "").strip()
    if text:
        # Stable across runs, unlike hash(), which is salted per process.
        # An identifier, not a security control. SHA-1 is kept so ids in saved analyses
        # still match; usedforsecurity=False says so to the runtime.
        return "h" + hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]  # noqa: S324  # nosec B324  # nosemgrep: insecure-hash-algorithm-sha1
    return ""


def _counts(values) -> dict[str, int]:
    """Tally occurrences into a plain dict."""
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out
