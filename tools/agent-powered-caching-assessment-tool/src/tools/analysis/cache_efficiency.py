"""
Cache Efficiency v2: how efficiently would a cache serve this item's reads?

Spec: .kiro/specs/cache-efficiency-v2/

The cache entry is one query result. Key = hash of the query plus its parameter
values. Value = the full result set. Two factors:

    reuse        estimated hit rate over the observation window       75%
    result size  time saved per hit x bytes factor                    25%

REUSE
-----
N calls over the window, K distinct entries the query can create. Replaying the
N calls against an empty cache that holds every entry for the whole window, the
first request for an entry misses and every repeat hits. With every entry
equally popular, the expected number of distinct entries requested is
K x (1 - e^(-N/K)), so

    estimated hit rate = 1 - K x (1 - e^(-N/K)) / N

Full marks at 0.8 (the ElastiCache CacheHitRate guidance), linear below.

K comes from the parameters: 1 with none, reltuples for a primary-key lookup,
multiplied n_distinct for other equality filters. A range, IN, LIKE, OFFSET, or
any other parameter use has no K in the statistics and is deferred to the agent.

RESULT SIZE
-----------
Time saved per hit is PostgreSQL's mean execution time. The cache is assumed to
sit as close to the application as PostgreSQL, so the round trip and the result
transfer cancel. Log scale, 0 at 1 ms, 100 at 100 ms.

Bytes factor covers what is specific to a cache: holding and returning a large
value on a single-threaded engine. 1 up to 100 KB, log scale to 0 at 256 MB.

Writes are deliberately absent. Access and the Disqualifiers own them.

Every function returns a value or a reason. None raises on missing data, because
missing data must show as "not scored", never as 0.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace

from sqlglot import exp

logger = logging.getLogger(__name__)

# Defaults. The live values come from CacheScoringThresholds.
HIT_RATE_FULL_MARKS = 0.8
WEIGHT_REUSE = 0.75
WEIGHT_SIZE = 0.25
VALUE_SOFT_LIMIT_BYTES = 100 * 1024
VALUE_HARD_LIMIT_BYTES = 256 * 1024 * 1024
TIME_SAVED_ZERO_MS = 1.0
TIME_SAVED_FULL_MS = 100.0

# Parameter kinds. Only "eq" yields a K from statistics.
PARAM_KINDS = ("eq", "range", "in", "like", "offset", "other")
DEFERRED_KINDS = frozenset(PARAM_KINDS) - {"eq"}

STATUS_SCORED = "scored"
STATUS_PARTIAL = "partial"
STATUS_DEFERRED = "deferred"
STATUS_AGENT = "agent_estimated"
STATUS_NOT_SCORED = "not_scored"
K_SOURCE_AGENT = "agent estimate"

# Fixed byte widths by PostgreSQL type, for computed outputs.
_TYPE_BYTES: dict[str, int] = {
    "bool": 1, "boolean": 1,
    "int2": 2, "smallint": 2,
    "int4": 4, "int": 4, "integer": 4, "date": 4, "float4": 4, "real": 4,
    "int8": 8, "bigint": 8, "float8": 8, "double": 8, "double precision": 8,
    "timestamp": 8, "timestamptz": 8, "time": 8,
    "uuid": 16,
}

_RANGE_NODES = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)
_LIKE_NODES = tuple(
    t for t in (getattr(exp, n, None) for n in
                ("Like", "ILike", "SimilarTo", "RegexpLike", "RegexpILike"))
    if t is not None
)
# A parameter that reaches one of these without meeting a comparison sits in a
# filter as part of an expression: K cannot be read from statistics.
_FILTER_NODES = (exp.Where, exp.Join, exp.Having)
# A parameter that reaches one of these without meeting a comparison is outside
# any filter (select list, ORDER BY, GROUP BY, EXISTS (SELECT $n)): a constant
# the application does not vary, so it does not count toward K.
_OUTSIDE_FILTER_NODES = (exp.Select, exp.Group, exp.Order)
# Units and windows: date_trunc($n, ...), interval $n, make_interval(=> $n).
_UNIT_NODES = tuple(
    t for t in (getattr(exp, n, None) for n in
                ("Interval", "TimestampTrunc", "DateTrunc", "TimeTrunc"))
    if t is not None
)
_UNIT_FUNCS = frozenset({"make_interval", "date_trunc", "date_part"})
# The first of two arguments is the text-search configuration, e.g. 'english'.
_TSQUERY_FUNCS = frozenset({"to_tsquery", "plainto_tsquery", "phraseto_tsquery",
                            "websearch_to_tsquery"})
_PREDICATE_NODES = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is,
                    exp.Exists, exp.And, exp.Or, exp.Not, exp.Boolean, exp.In)


# ---------------------------------------------------------------------------
# Parser helpers: parameters and outputs
# ---------------------------------------------------------------------------


def build_alias_map(tree: exp.Expression, resolve) -> dict[str, str]:
    """Map every table alias or name in a statement to its qualified table.

    Args:
        tree: Parsed statement.
        resolve: Callable mapping a qualified-or-bare name to the canonical
            schema.table (query_cache_analysis._resolve_name with known names).

    Returns:
        Lowercased alias or name -> canonical table name. CTE names are left
        out, since a CTE is not a relation with statistics.
    """
    ctes = {str(c.alias).lower() for c in tree.find_all(exp.CTE) if c.alias}
    out: dict[str, str] = {}
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        if not t.db and name in ctes:
            continue
        q = f"{t.db}.{t.name}".lower() if t.db else name
        canon = resolve(q)
        out[t.alias_or_name.lower()] = canon
        out.setdefault(name, canon)
    return out


def _column_table(col: exp.Column, aliases: dict[str, str],
                  table_columns: dict[str, set[str]]) -> str | None:
    """Resolve the table a column belongs to, or None when it is ambiguous.

    Args:
        col: Column node.
        aliases: From build_alias_map().
        table_columns: Canonical table -> its column names, for unqualified
            columns in a join.

    Returns:
        Canonical table name, or None.
    """
    if col.table:
        return aliases.get(col.table.lower())
    # An unqualified column belongs to the nearest SELECT that reads it, so a
    # subquery's tables are tried before the whole statement's.
    scope = col.find_ancestor(exp.Select)
    candidates: list[list[str]] = []
    if scope is not None:
        local = {aliases.get(t.alias_or_name.lower()) for t in scope.find_all(exp.Table)
                 if t.find_ancestor(exp.Select) is scope}
        candidates.append(sorted(x for x in local if x))
    candidates.append(sorted(set(aliases.values())))
    for tables in candidates:
        if len(tables) == 1:
            return tables[0]
        owners = [t for t in tables if col.name.lower() in table_columns.get(t, set())]
        if len(owners) == 1:
            return owners[0]
    return None


def _first_column(node: exp.Expression | None) -> exp.Column | None:
    """The first Column inside an expression, or None."""
    if node is None:
        return None
    if isinstance(node, exp.Column):
        return node
    return next(node.find_all(exp.Column), None)


def extract_params(tree: exp.Expression, aliases: dict[str, str],
                   table_columns: dict[str, set[str]]) -> list[dict]:
    """Classify every $n parameter in a read by how it shapes the cache key.

    pg_stat_statements turns every literal into a placeholder, including values
    the application never varies. Those are constants and are not returned:
    LIMIT, anything outside a filter comparison (select list, coalesce defaults,
    EXISTS (SELECT $n), ORDER BY, GROUP BY), the text-search configuration of a
    two-argument tsquery function, and date or interval units. `column <> $n`
    counts as equality: one entry per value.

    Args:
        tree: Parsed statement.
        aliases: From build_alias_map().
        table_columns: Canonical table -> column names.

    Returns:
        One dict per distinct (placeholder, column, kind): {"param", "table",
        "column", "kind"}. kind is one of PARAM_KINDS. table or column may be
        None when they cannot be resolved.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for p in tree.find_all(exp.Parameter, exp.Placeholder):
        kind, col_node = "other", None
        child, cur = p, p.parent
        while cur is not None:
            # Constants: LIMIT, units and windows, text-search configuration.
            if isinstance(cur, exp.Limit) or (_UNIT_NODES and isinstance(cur, _UNIT_NODES)):
                kind = None
                break
            if isinstance(cur, exp.Anonymous):
                fname = str(cur.this).lower()
                if fname in _UNIT_FUNCS:
                    kind = None
                    break
                if (fname in _TSQUERY_FUNCS and len(cur.expressions) >= 2
                        and cur.expressions[0] is child):
                    kind = None
                    break
            if isinstance(cur, exp.Offset):
                kind = "offset"
                break
            if isinstance(cur, (exp.In, exp.Any, exp.All)):
                kind = "in"
                col_node = _first_column(cur.this) if isinstance(cur, exp.In) else None
                if isinstance(cur, (exp.Any, exp.All)) and isinstance(cur.parent, exp.EQ):
                    eq = cur.parent
                    other = eq.this if eq.expression is cur else eq.expression
                    col_node = _first_column(other)
                break
            if _LIKE_NODES and isinstance(cur, _LIKE_NODES):
                kind, col_node = "like", _first_column(cur.this)
                break
            if isinstance(cur, _RANGE_NODES):
                kind, col_node = "range", _first_column(cur.this)
                break
            # column = $n, and column <> $n: K from the column's distinct values.
            if isinstance(cur, (exp.EQ, exp.NEQ)):
                other = cur.expression if child is cur.this else cur.this
                col_node = _first_column(other)
                kind = "eq" if col_node is not None and not list(
                    other.find_all(exp.Parameter, exp.Placeholder)) else "other"
                break
            if isinstance(cur, _FILTER_NODES):
                break                       # in a filter, inside an expression
            if isinstance(cur, _OUTSIDE_FILTER_NODES):
                kind = None                 # outside any filter: a constant
                break
            child, cur = cur, cur.parent
        if kind is None:
            continue
        table = column = None
        if col_node is not None:
            column = col_node.name.lower()
            table = _column_table(col_node, aliases, table_columns)
        key = (p.sql(), table, column, kind)
        if key in seen:
            continue
        seen.add(key)
        out.append({"param": p.sql(), "table": table, "column": column, "kind": kind})
    return out


def _widest(node: exp.Expression, aliases: dict[str, str],
            table_columns: dict[str, set[str]]) -> dict:
    """Output spec for a numeric aggregate: the widest column inside it.

    Args:
        node: The aggregate expression.
        aliases: From build_alias_map().
        table_columns: Canonical table -> column names.

    Returns:
        {"kind": "widest", "refs": [(table, column), ...]}, or
        {"kind": "variable"} when it reads no column.
    """
    refs = [(_column_table(c, aliases, table_columns), c.name.lower())
            for c in node.find_all(exp.Column)]
    return {"kind": "widest", "refs": refs} if refs else {"kind": "variable"}


def _cast_bytes(node: exp.Cast) -> int | None:
    """Fixed width of a cast's target type, or None when variable."""
    to = node.args.get("to")
    name = to.sql(dialect="postgres").lower() if to is not None else ""
    return _TYPE_BYTES.get(name)


def extract_outputs(tree: exp.Expression, aliases: dict[str, str],
                    table_columns: dict[str, set[str]],
                    column_types: dict[str, dict[str, str]]) -> list[dict]:
    """Describe each projected expression so its byte width can be looked up.

    Args:
        tree: Parsed statement. Only a top-level SELECT has outputs; anything
            else (a UNION, for example) yields one variable output.
        aliases: From build_alias_map().
        table_columns: Canonical table -> column names.
        column_types: Canonical table -> column -> udt_name, for aggregates.

    Returns:
        One dict per output: {"kind": "column" | "star" | "fixed" | "variable",
        "table", "column", "bytes", "expr"}.
    """
    if not isinstance(tree, exp.Select):
        return [{"kind": "variable", "table": None, "column": None, "bytes": None,
                 "expr": type(tree).__name__}]
    outs: list[dict] = []
    for e in tree.expressions:
        node = e.this if isinstance(e, exp.Alias) else e
        # coalesce(x, $n): the value returned is x, the default is a constant.
        while isinstance(node, (exp.Coalesce, exp.Paren)):
            node = node.this
        text = e.sql(dialect="postgres")[:80]
        rec = {"kind": "variable", "table": None, "column": None, "bytes": None, "expr": text}
        if isinstance(node, exp.Star):
            rec["kind"] = "star"
        elif isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
            rec.update(kind="star", table=aliases.get(node.table.lower()) if node.table else None)
        elif isinstance(node, exp.Column):
            rec.update(kind="column", column=node.name.lower(),
                       table=_column_table(node, aliases, table_columns))
        elif isinstance(node, exp.Count):
            rec.update(kind="fixed", bytes=8)
        elif isinstance(node, (exp.Min, exp.Max)) and isinstance(node.this, exp.Column):
            c = node.this
            rec.update(kind="column", column=c.name.lower(),
                       table=_column_table(c, aliases, table_columns))
        elif isinstance(node, (exp.Sum, exp.Avg)):
            c = node.this if isinstance(node.this, exp.Column) else None
            t = _column_table(c, aliases, table_columns) if c is not None else None
            udt = column_types.get(t or "", {}).get(c.name.lower(), "") if c is not None else ""
            if isinstance(node, exp.Sum) and udt in ("int2", "int4"):
                rec.update(kind="fixed", bytes=8)      # sum(int2/int4) -> bigint
            elif udt in ("float4", "float8"):
                rec.update(kind="fixed", bytes=8 if isinstance(node, exp.Avg) or udt == "float8" else 4)
            else:
                # Returns numeric (sum(int8), avg of integers, numeric columns,
                # arithmetic). Width: the widest column inside it.
                rec.update(_widest(node, aliases, table_columns))
        elif isinstance(node, (exp.Min, exp.Max)):
            rec.update(_widest(node, aliases, table_columns))
        elif isinstance(node, exp.Cast):
            b = _cast_bytes(node)
            if b is not None:
                rec.update(kind="fixed", bytes=b)
        elif isinstance(node, _PREDICATE_NODES):
            rec.update(kind="fixed", bytes=1)
        outs.append(rec)
    if any(o["kind"] == "star" and o["table"] is None for o in outs):
        # Bare * expands to every table read.
        expanded = []
        for o in outs:
            if o["kind"] == "star" and o["table"] is None:
                for t in sorted(set(aliases.values())):
                    expanded.append({**o, "table": t})
            else:
                expanded.append(o)
        outs = expanded
    return outs


# ---------------------------------------------------------------------------
# Statistics lookup
# ---------------------------------------------------------------------------


@dataclass
class TableStats:
    """Planner statistics for one table, from the collector."""

    reltuples: float | None = None
    primary_key: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    column_types: dict[str, str] = field(default_factory=dict)
    column_stats: dict[str, dict] = field(default_factory=dict)
    # False when the collection never asked for reltuples or pg_stats (made
    # before collector 0.2.0), so "missing" means not collected, not unanalyzed.
    collected: bool = True


def build_table_stats(collector_output: dict) -> dict[str, TableStats]:
    """Index the collector's per-table statistics by lowercased table id.

    Args:
        collector_output: Raw collector output.

    Returns:
        table id -> TableStats. A collection made before contract 0.2.0 has no
        reltuples or column_stats, so those stay empty and the dependent factors
        are not scored.
    """
    out: dict[str, TableStats] = {}
    for t in (collector_output.get("database_schema") or {}).get("tables") or []:
        tid = str(t.get("table_id") or t.get("table_name") or "").lower()
        if not tid:
            continue
        cols = t.get("columns") or []
        out[tid] = TableStats(
            reltuples=t.get("reltuples"),
            primary_key=[str(c).lower() for c in (t.get("primary_key") or [])],
            columns=[str(c.get("column_name")).lower() for c in cols if c.get("column_name")],
            column_types={str(c.get("column_name")).lower(): str(c.get("udt_name") or "").lower()
                          for c in cols if c.get("column_name")},
            column_stats={str(k).lower(): v for k, v in (t.get("column_stats") or {}).items()},
            collected="reltuples" in t or "column_stats" in t,
        )
    return out


def has_column_stats(stats: dict[str, TableStats]) -> bool:
    """True when the collection carries any column statistics at all."""
    return any(s.column_stats for s in stats.values())


# ---------------------------------------------------------------------------
# Reuse
# ---------------------------------------------------------------------------


def convert_n_distinct(raw: float | None, reltuples: float | None) -> float | None:
    """Turn a pg_stats n_distinct into an absolute distinct-value count.

    Args:
        raw: n_distinct as stored. Positive is a count. Negative is minus the
            fraction of rows that are distinct. Zero or None is unknown.
        reltuples: Table row estimate, needed for a negative value.

    Returns:
        Distinct values, at least 1, or None when it cannot be known.
    """
    if raw is None or raw == 0:
        return None
    if raw > 0:
        return max(float(raw), 1.0)
    if reltuples is None or reltuples <= 0:
        return None
    return max(-float(raw) * float(reltuples), 1.0)


def resolve_k(params: list[dict], stats: dict[str, TableStats]
              ) -> tuple[float | None, str | None, str | None, str | None]:
    """Number of distinct cache entries a query can create.

    Args:
        params: From extract_params().
        stats: From build_table_stats().

    Returns:
        (k, source, deferred_reason, missing_reason). Exactly one of k or a
        reason is set. deferred_reason means the agent judges reuse; missing
        reason means it is not scored.
    """
    if not params:
        return 1.0, "no parameters", None, None

    deferred = [p for p in params if p["kind"] in DEFERRED_KINDS]
    if deferred:
        p = deferred[0]
        label = {"range": "a range", "in": "an IN list", "like": "a pattern match",
                 "offset": "an OFFSET", "other": "a parameter used inside an expression"}
        where = (f" on {p['table'] + '.' if p['table'] else ''}{p['column']}"
                 if p["column"] else "")
        return None, None, (
            f"{label[p['kind']]}{where}, so the number of distinct requests "
            "depends on what the application sends"), None

    by_table: dict[str, set[str]] = {}
    for p in params:
        if not p["table"] or not p["column"]:
            return None, None, None, (
                f"parameter column {p['column'] or '?'} could not be tied to a table")
        by_table.setdefault(p["table"], set()).add(p["column"])

    k = 1.0
    sources: list[str] = []
    caps: list[float] = []
    for table, cols in sorted(by_table.items()):
        ts = stats.get(table)
        if ts is None:
            return None, None, None, f"{table} has no collected statistics"
        if not ts.collected:
            return None, None, None, (f"{table}: planner statistics were not collected "
                                      "(collection predates collector 0.2.0)")
        if ts.reltuples is not None:
            caps.append(ts.reltuples)
        if ts.primary_key and set(ts.primary_key) <= cols:
            if ts.reltuples is None:
                return None, None, None, f"{table} has no row estimate (never analyzed)"
            k *= max(ts.reltuples, 1.0)
            sources.append("primary key")
            continue
        for c in sorted(cols):
            cs = ts.column_stats.get(c)
            nd = convert_n_distinct(cs.get("n_distinct") if cs else None, ts.reltuples)
            if nd is None:
                return None, None, None, f"{table}.{c} has no distinct-value statistics"
            k *= nd
        sources.append("n_distinct")
    if caps:
        k = min(k, max(max(caps), 1.0))
    source = sources[0] if len(set(sources)) == 1 else "primary key and n_distinct"
    return max(k, 1.0), source, None, None


def estimate_hit_rate(n: float, k: float) -> float:
    """Hit rate a cache holding every entry would have reached over N calls.

    Args:
        n: Calls during the window. Must be > 0.
        k: Distinct entries. Must be >= 1.

    Returns:
        Estimated hit rate, 0 to 1.
    """
    distinct = k * -math.expm1(-n / k)
    return max(0.0, min(1.0, 1.0 - distinct / n))


def estimated_hits(n: float, k: float) -> float:
    """Expected hits over N calls with K equally popular entries."""
    return max(0.0, n - k * -math.expm1(-n / k))


def observed_hit_rate(n: float, d: float) -> float:
    """Hit rate when D distinct requests were sent over N calls.

    Used for the agent's estimate, which counts the distinct requests the
    application actually sent in the window, not a pool of possible keys. A
    cache that kept every entry misses once per distinct request and hits on
    every repeat, so the hit rate is 1 - D/N.

    Args:
        n: Calls during the window. Must be > 0.
        d: Distinct requests sent during the window, >= 1. Capped at n.

    Returns:
        Hit rate, 0 to 1. Returns 0.0 for unusable input.
    """
    try:
        n, d = float(n), float(d)
    except (TypeError, ValueError):
        logger.warning("observed_hit_rate: unusable input n=%r d=%r", n, d)
        return 0.0
    if not n > 0 or math.isnan(d):
        return 0.0
    d = min(max(d, 1.0), n)
    return max(0.0, min(1.0, 1.0 - d / n))


def observed_hits(n: float, d: float) -> float:
    """Hits when D distinct requests were sent over N calls (N - D, floor 0)."""
    try:
        n, d = float(n), float(d)
    except (TypeError, ValueError):
        logger.warning("observed_hits: unusable input n=%r d=%r", n, d)
        return 0.0
    if not n > 0 or math.isnan(d):
        return 0.0
    return max(0.0, n - min(max(d, 1.0), n))


def reuse_score(h: float, full_marks: float = HIT_RATE_FULL_MARKS) -> int:
    """Full marks at or above full_marks, linear below."""
    return int(round(100 * min(h / full_marks, 1.0)))


# ---------------------------------------------------------------------------
# Result size
# ---------------------------------------------------------------------------


def row_width(outputs: list[dict], stats: dict[str, TableStats]
              ) -> tuple[float | None, str | None]:
    """Average bytes in one returned row.

    Args:
        outputs: From extract_outputs().
        stats: From build_table_stats().

    Returns:
        (bytes, missing_reason).
    """
    if not outputs:
        return None, "no output columns"
    total = 0.0
    for o in outputs:
        if o["kind"] == "fixed":
            total += o["bytes"]
            continue
        if o["kind"] == "variable":
            return None, f"output {o['expr']} has no fixed width or column statistics"
        if o["kind"] == "widest":
            widths = []
            for t, c in o["refs"]:
                ts = stats.get(t or "")
                if ts is None or not ts.collected:
                    return None, (f"output {o['expr']}: column widths for "
                                  f"{t or c} were not collected")
                w = (ts.column_stats.get(c) or {}).get("avg_width")
                if w is None:
                    return None, f"{t}.{c} has no width statistics"
                widths.append(w)
            total += max(widths)
            continue
        ts = stats.get(o["table"] or "")
        if ts is None:
            return None, f"output {o['expr']} could not be tied to a table with statistics"
        if not ts.collected:
            return None, (f"{o['table']}: column widths were not collected "
                          "(collection predates collector 0.2.0)")
        if o["kind"] == "column":
            w = (ts.column_stats.get(o["column"]) or {}).get("avg_width")
            if w is None:
                return None, f"{o['table']}.{o['column']} has no width statistics"
            total += w
        else:  # star
            cols = ts.columns or list(ts.column_stats)
            widths = [(ts.column_stats.get(c) or {}).get("avg_width") for c in cols]
            if not cols or any(w is None for w in widths):
                return None, f"{o['table']} has columns without width statistics"
            total += sum(widths)
    return total, None


def time_saved_score(exec_ms: float, zero_ms: float = TIME_SAVED_ZERO_MS,
                     full_ms: float = TIME_SAVED_FULL_MS) -> int:
    """0 at zero_ms or less, 100 at full_ms or more, log scale between."""
    if exec_ms <= zero_ms:
        return 0
    if exec_ms >= full_ms:
        return 100
    return int(round(100 * math.log(exec_ms / zero_ms) / math.log(full_ms / zero_ms)))


def bytes_factor(nbytes: float, soft: float = VALUE_SOFT_LIMIT_BYTES,
                 hard: float = VALUE_HARD_LIMIT_BYTES) -> float:
    """1 up to soft, 0 at hard or more, log scale between."""
    if nbytes <= soft:
        return 1.0
    if nbytes >= hard:
        return 0.0
    return 1.0 - math.log(nbytes / soft) / math.log(hard / soft)


def result_size_score(time_score: int, factor: float) -> int:
    """Time saved score scaled by the bytes factor."""
    return int(round(time_score * factor))


# ---------------------------------------------------------------------------
# Pillar
# ---------------------------------------------------------------------------


def efficiency_score(reuse: int | None, size: int | None,
                     w_reuse: float = WEIGHT_REUSE, w_size: float = WEIGHT_SIZE) -> int | None:
    """Weighted Efficiency, rescaled when one factor is not scored.

    Returns:
        0 to 100, or None when neither factor could be scored.
    """
    parts = [(v, w) for v, w in ((reuse, w_reuse), (size, w_size)) if v is not None]
    if not parts:
        return None
    total_w = sum(w for _v, w in parts)
    return int(round(sum(v * w for v, w in parts) / total_w))


@dataclass
class EfficiencyResult:
    """Everything R8.1 asks a record to keep."""

    score: int | None = None
    status: str = STATUS_NOT_SCORED
    calls_in_window: float | None = None
    window_seconds: float | None = None
    k: float | None = None
    k_source: str | None = None
    estimated_hit_rate: float | None = None
    estimated_hits: float | None = None
    reuse_score: int | None = None
    reuse_deferred_reason: str | None = None
    reuse_missing_reason: str | None = None
    row_width_bytes: float | None = None
    result_bytes: float | None = None
    time_saved_ms: float | None = None
    time_saved_score: int | None = None
    bytes_factor: float | None = None
    result_size_score: int | None = None
    size_missing_reason: str | None = None
    queries_rolled_up: int = 0
    queries_deferred: int = 0

    def as_dict(self) -> dict:
        """Plain dict for contracts and tools."""
        return dict(self.__dict__)


def _status(r: EfficiencyResult) -> str:
    """Derive the status from which factors were scored."""
    if r.reuse_deferred_reason or (r.queries_deferred and r.reuse_score is None):
        return STATUS_DEFERRED
    if r.reuse_score is not None and r.result_size_score is not None:
        return STATUS_SCORED
    if r.reuse_score is None and r.result_size_score is None:
        return STATUS_NOT_SCORED
    return STATUS_PARTIAL


def score_query_efficiency(
    *,
    calls_in_window: float | None,
    window_seconds: float | None,
    exec_ms: float,
    rows_per_call: float,
    params: list[dict],
    outputs: list[dict],
    stats: dict[str, TableStats],
    thresholds=None,
) -> EfficiencyResult:
    """Score one read query's Cache Efficiency.

    Args:
        calls_in_window: N, calls since the last stats reset.
        window_seconds: Length of the window.
        exec_ms: PostgreSQL mean execution time.
        rows_per_call: Average rows returned.
        params: From extract_params().
        outputs: From extract_outputs().
        stats: From build_table_stats().
        thresholds: CacheScoringThresholds, or None for module defaults.

    Returns:
        Populated EfficiencyResult.
    """
    t = thresholds
    full = getattr(t, "efficiency_hit_rate_full", HIT_RATE_FULL_MARKS)
    r = EfficiencyResult(calls_in_window=calls_in_window, window_seconds=window_seconds)

    k, source, deferred, missing = resolve_k(params, stats)
    r.k, r.k_source, r.reuse_deferred_reason, r.reuse_missing_reason = k, source, deferred, missing
    if k is not None:
        if not calls_in_window:
            r.reuse_missing_reason = "no calls measured in the window"
        else:
            h = estimate_hit_rate(calls_in_window, k)
            r.estimated_hit_rate = round(h, 4)
            r.estimated_hits = round(estimated_hits(calls_in_window, k), 1)
            r.reuse_score = reuse_score(h, full)

    width, wmiss = row_width(outputs, stats)
    r.time_saved_ms = round(exec_ms, 4)
    r.time_saved_score = time_saved_score(
        exec_ms, getattr(t, "time_saved_zero_ms", TIME_SAVED_ZERO_MS),
        getattr(t, "time_saved_full_ms", TIME_SAVED_FULL_MS))
    if width is None:
        r.size_missing_reason = wmiss
    else:
        r.row_width_bytes = round(width, 1)
        r.result_bytes = round(width * max(rows_per_call, 0.0), 1)
        r.bytes_factor = round(bytes_factor(
            r.result_bytes,
            getattr(t, "value_soft_limit_kb", VALUE_SOFT_LIMIT_BYTES / 1024) * 1024,
            getattr(t, "value_hard_limit_mb", VALUE_HARD_LIMIT_BYTES / 1024 / 1024) * 1024 * 1024,
        ), 4)
        r.result_size_score = result_size_score(r.time_saved_score, r.bytes_factor)

    r.score = efficiency_score(
        r.reuse_score, r.result_size_score,
        getattr(t, "efficiency_weight_reuse", WEIGHT_REUSE),
        getattr(t, "efficiency_weight_size", WEIGHT_SIZE))
    r.status = _status(r)
    return r


def apply_estimated_k(result: EfficiencyResult, k_estimate: float,
                      thresholds=None) -> EfficiencyResult:
    """Score deferred reuse from the agent's estimate of distinct requests.

    The estimate is the number of distinct requests the application sent in
    the window, so the hit rate is 1 - D/N (observed_hit_rate), not the
    uniform-draw formula used for statistics-derived K, which treats K as a
    pool of possible keys and credits repeats even when every request is
    unique (spec R5.5). The deferred reason is kept so the record shows why
    the agent was asked.

    Args:
        result: A deferred EfficiencyResult from score_query_efficiency().
        k_estimate: Agent's estimated number of distinct requests, >= 1.
        thresholds: CacheScoringThresholds, or None for module defaults.

    Returns:
        A new EfficiencyResult with reuse and score filled, status
        agent_estimated. The input is returned unchanged when the estimate is
        unusable or no calls were measured.
    """
    try:
        k = max(float(k_estimate), 1.0)
    except (TypeError, ValueError):
        logger.warning("apply_estimated_k: unusable estimate %r", k_estimate)
        return result
    if not result.calls_in_window:
        return result
    t = thresholds
    r = replace(result)
    h = observed_hit_rate(result.calls_in_window, k)
    r.k, r.k_source = round(k, 1), K_SOURCE_AGENT
    r.estimated_hit_rate = round(h, 4)
    r.estimated_hits = round(observed_hits(result.calls_in_window, k), 1)
    r.reuse_score = reuse_score(h, getattr(t, "efficiency_hit_rate_full", HIT_RATE_FULL_MARKS))
    r.reuse_missing_reason = None
    r.score = efficiency_score(
        r.reuse_score, r.result_size_score,
        getattr(t, "efficiency_weight_reuse", WEIGHT_REUSE),
        getattr(t, "efficiency_weight_size", WEIGHT_SIZE))
    r.status = STATUS_AGENT
    return r


def rollup_table_efficiency(results: list[tuple[float, EfficiencyResult]],
                            thresholds=None) -> EfficiencyResult:
    """Score a table from the read queries that read it, joins included.

    Reuse: hits summed over the queries with a K, divided by their summed
    calls. Result size: call-weighted average of the queries' result size
    scores. Deferred queries are left out of reuse and counted.

    Args:
        results: (calls_in_window, EfficiencyResult) per read query.
        thresholds: CacheScoringThresholds, or None for module defaults.

    Returns:
        EfficiencyResult for the table.
    """
    t = thresholds
    full = getattr(t, "efficiency_hit_rate_full", HIT_RATE_FULL_MARKS)
    r = EfficiencyResult(queries_rolled_up=len(results))
    r.queries_deferred = sum(1 for _n, q in results if q.reuse_deferred_reason)
    windows = [q.window_seconds for _n, q in results if q.window_seconds]
    r.window_seconds = max(windows) if windows else None

    reuse = [(n, q) for n, q in results if q.estimated_hits is not None and n]
    if reuse:
        n_total = sum(n for n, _q in reuse)
        hits = sum(q.estimated_hits for _n, q in reuse)
        h = hits / n_total
        r.calls_in_window = round(n_total, 1)
        r.estimated_hits = round(hits, 1)
        r.estimated_hit_rate = round(h, 4)
        r.reuse_score = reuse_score(h, full)
        r.k_source = f"rollup of {len(reuse)} quer{'y' if len(reuse) == 1 else 'ies'}"
    elif not results:
        r.reuse_missing_reason = "no read query reads this table"
    elif r.queries_deferred:
        r.reuse_deferred_reason = (f"all {r.queries_deferred} read queries with a measurable "
                                   "shape are deferred to the agent")
    else:
        r.reuse_missing_reason = "no read query of this table has a measurable K"

    sized = [(n, q) for n, q in results if q.result_size_score is not None and n]
    if sized:
        w = sum(n for n, _q in sized)
        r.result_size_score = int(round(sum(n * q.result_size_score for n, q in sized) / w))
    elif results:
        r.size_missing_reason = "no read query of this table has a measurable result size"

    r.score = efficiency_score(
        r.reuse_score, r.result_size_score,
        getattr(t, "efficiency_weight_reuse", WEIGHT_REUSE),
        getattr(t, "efficiency_weight_size", WEIGHT_SIZE))
    r.status = _status(r)
    return r
