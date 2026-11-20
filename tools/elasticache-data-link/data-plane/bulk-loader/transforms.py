#!/usr/bin/env python3
"""
Shared transform engine (initial-load path). Mirrors tools/go-sink/transform.go.

Processing order per row: filter (on original fields) -> computed -> column
selection -> rename. Both engines must pass data-plane/transforms-fixtures.json
and tools/go-sink/testdata/transform_cases.json (numbers, expressions, filters).

apply(spec, table, row) -> (out_fields: dict|None, keep: bool)
  keep == False means the row must not be cached (loader skips it; the sink
  DELETEs the key). With no spec/table entry it's a passthrough (keep all).
"""
import json
import math
import re


def load_spec(path_or_bytes):
    """The spec from a file path or JSON text, checked (check_spec)."""
    if path_or_bytes is None:
        return None
    if isinstance(path_or_bytes, (bytes, str)) and str(path_or_bytes).strip().startswith("{"):
        spec = json.loads(path_or_bytes)
    else:
        with open(path_or_bytes) as f:
            spec = json.load(f)
    check_spec(spec)
    return spec


# ─────────────────────────────────────────────────────────────────────────────
# Numbers and computed-field expressions (Jira story F077). Identical to
# tools/go-sink/expr.go, which explains the rules; both test suites run
# tools/go-sink/testdata/transform_cases.json. This used to be Python's own
# expression parser and float(), which disagreed with the sink on `1e3`, `%`,
# NaN/Inf text, "1_000", Unicode digits and more.
#
#   expr    := term (('+' | '-') term)*
#   term    := unary (('*' | '/') unary)*
#   unary   := ('+' | '-') unary | primary
#   primary := number | name | '(' expr ')'
#   number  := (digits ['.' [digits]] | '.' digits) [('e' | 'E') ['+' | '-'] digits]
#   name    := [A-Za-z_][A-Za-z0-9_]*
# ─────────────────────────────────────────────────────────────────────────────
_ASCII_SPACE = " \t\n\r\f\v"
_DECIMAL = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_DEPTH = 64


def _decimal_value(text):
    # float() of a plain decimal is correctly rounded, as Go's ParseFloat; an
    # out-of-range value is inf (or 0) in both.
    return float(text)


def parse_number(s):
    """A field value as (float, True), or (0.0, False) when it is not a number."""
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    s = s.strip(_ASCII_SPACE)
    body, sign = s, 1.0
    if body[:1] in ("+", "-"):
        sign = -1.0 if body[0] == "-" else 1.0
        body = body[1:]
    low = body.lower()
    if low == "nan":
        return math.nan, True
    if low in ("inf", "infinity"):
        return sign * math.inf, True
    if not body or not _DECIMAL.fullmatch(body):
        return 0.0, False
    return sign * _decimal_value(body), True


def _to_float(s):
    return parse_number(s)[0]


def _format_num(v):
    """A computed value as text: %.6f without trailing zeros or dot, -0 as 0,
    NaN / Infinity / -Infinity as Java (and so Debezium) prints them."""
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "Infinity" if v > 0 else "-Infinity"
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


class _Parser(object):
    """Parses one expression and, with a row, evaluates it (expr.go exprParser)."""

    def __init__(self, text, row):
        self.s, self.pos, self.row, self.depth = text, 0, row, 0

    def enter(self):
        # Nesting is bounded (expr.go maxExprDepth), so a pathological
        # expression is refused alike, not by Python's recursion limit.
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise self.fail("nested more than {} deep".format(_MAX_DEPTH))

    def skip_space(self):
        while self.pos < len(self.s) and self.s[self.pos] in " \t\n\r":
            self.pos += 1

    def fail(self, what):
        if self.pos >= len(self.s):
            return ValueError("{} at the end of {!r}".format(what, self.s))
        return ValueError("{} at position {} of {!r} ({!r})".format(
            what, self.pos + 1, self.s, self.s[self.pos]))

    def peek(self):
        return self.s[self.pos] if self.pos < len(self.s) else ""

    def expr(self):
        v = self.term()
        while True:
            self.skip_space()
            op = self.peek()
            if op not in ("+", "-"):
                return v
            self.pos += 1
            r = self.term()
            v = v + r if op == "+" else v - r

    def term(self):
        v = self.unary()
        while True:
            self.skip_space()
            op = self.peek()
            if op not in ("*", "/"):
                return v
            self.pos += 1
            r = self.unary()
            if op == "*":
                v = v * r
            elif r == 0:
                v = 0.0
            else:
                v = v / r

    def unary(self):
        self.skip_space()
        op = self.peek()
        if op in ("+", "-"):
            self.pos += 1
            self.enter()
            v = self.unary()
            self.depth -= 1
            return -v if op == "-" else v
        return self.primary()

    def primary(self):
        self.skip_space()
        c = self.peek()
        if c == "(":
            self.pos += 1
            self.enter()
            v = self.expr()
            self.depth -= 1
            self.skip_space()
            if self.peek() != ")":
                raise self.fail("')' expected")
            self.pos += 1
            return v
        if c and (c in "0123456789."):
            m = _DECIMAL.match(self.s, self.pos)
            if not m:
                raise self.fail("a number expected")
            self.pos = m.end()
            return _decimal_value(m.group(0))
        m = _NAME.match(self.s, self.pos) if c else None
        if m:
            self.pos = m.end()
            return _to_float((self.row or {}).get(m.group(0), ""))
        raise self.fail("a number, a column name or '(' expected")


def _parse_expr(expr, row):
    if not isinstance(expr, str):
        raise ValueError("an expression must be text, not {!r}".format(expr))
    p = _Parser(expr, row)
    v = p.expr()
    p.skip_space()
    if p.pos < len(p.s):
        raise p.fail("an operator expected")
    return v


def check_expr(expr):
    """Raise ValueError saying why an expression would be refused."""
    _parse_expr(expr, None)


def _eval_expr(expr, row):
    """A computed field over a row. Specs are checked when loaded, so an
    invalid expression only arrives from a spec built in code; it gives 0."""
    try:
        return _parse_expr(expr, row)
    except ValueError:
        return 0.0


_FILTER_OPS = ("=", "!=", ">", ">=", "<", "<=")


def check_spec(spec):
    """Raise ValueError for a spec the two engines would not apply the same
    way. Mirrors checkSpec (Go)."""
    for table, ts in sorted(((spec or {}).get("tables") or {}).items()):
        ts = ts or {}
        f = ts.get("filter")
        if f is not None:
            if not f.get("field"):
                raise ValueError("transforms.tables.{}.filter: field is required".format(table))
            if f.get("op") not in _FILTER_OPS:
                raise ValueError("transforms.tables.{}.filter: op {!r} is not one of "
                                 "= != > >= < <=".format(table, f.get("op")))
            if not isinstance(f.get("value", ""), str):
                raise ValueError("transforms.tables.{}.filter: value must be text, "
                                 "not {!r}".format(table, f.get("value")))
        for i, cf in enumerate(ts.get("computed") or []):
            if not cf.get("name"):
                raise ValueError("transforms.tables.{}.computed[{}]: name is required".format(table, i))
            try:
                check_expr(cf.get("expr"))
            except ValueError as exc:
                raise ValueError("transforms.tables.{}.computed[{}] ({}): {}".format(
                    table, i, cf["name"], exc))
        seen = {}
        for src, dst in sorted((ts.get("rename") or {}).items()):
            if dst in seen:
                raise ValueError("transforms.tables.{}.rename: {} and {} are both renamed "
                                 "to {}".format(table, seen[dst], src, dst))
            seen[dst] = src


def _eval_filter(f, row):
    got = row.get(f["field"], "")
    op, val = f["op"], f["value"]
    if op == "=":
        return str(got) == val
    if op == "!=":
        return str(got) != val
    if op == ">":
        return _to_float(got) > _to_float(val)
    if op == ">=":
        return _to_float(got) >= _to_float(val)
    if op == "<":
        return _to_float(got) < _to_float(val)
    if op == "<=":
        return _to_float(got) <= _to_float(val)
    return True


def apply(spec, table, row):
    """row: dict[str,str]. Returns (out_fields|None, keep)."""
    if not spec:
        return dict(row), True
    ts = spec.get("tables", {}).get(table)
    if not ts:
        return dict(row), True

    if ts.get("filter") and not _eval_filter(ts["filter"], row):
        return None, False

    computed = {}
    for cf in ts.get("computed", []):
        computed[cf["name"]] = _format_num(_eval_expr(cf["expr"], row))

    cols = ts.get("columns")
    if cols:
        out = {c: row[c] for c in cols if c in row}
    else:
        out = dict(row)
    out.update(computed)

    rename = ts.get("rename")
    if rename:
        # A renamed field replaces a field that already has the new name,
        # whatever the column order (as in Go, where map order is random).
        renamed = {k: v for k, v in out.items() if k not in rename}
        renamed.update({rename[k]: v for k, v in out.items() if k in rename})
        out = renamed
    return out, True


# ─────────────────────────────────────────────────────────────────────────────
# Denormalization (fan-out) — mirrors tools/go-sink/transform.go + denorm.go.
#
# A denorm rule embeds selected columns of a PARENT row onto a CHILD record, and
# maintains a reverse index (parent -> its children) so a later parent change can
# fan out to every child. The initial-load path only ever acts on the CHILD side:
# with parents loaded before children (see loader ordering), the parent is
# already in the cache when a child is loaded, so the child embeds it directly
# and adds itself to the reverse index. No parent-side fan-out is needed on the
# initial load because a parent has no children yet at the moment it is written.
# ─────────────────────────────────────────────────────────────────────────────

def bare_table(name):
    """'Inventory.customers' -> 'customers'. Rows are keyed by the bare name."""
    return str(name or "").split(".")[-1]


def normalize_key_prefix(raw):
    """advanced.key_prefix as '<prefix>:' or ''. Mirrors normalizeKeyPrefix (Go)."""
    raw = str(raw or "").strip().rstrip(":")
    return raw + ":" if raw else ""


def parent_key_prefix(rule, link_prefix=""):
    """The cache-key prefix for a rule's parent, defaulting to
    '<link prefix><parent_table>:' -- the key the sink writes the parent under.

    The schema documents this default but does not write it into the file, so both
    engines apply it themselves. Keeping it here means a hand-written transforms
    file behaves the same as one emitted by the control plane.
    """
    pref = rule.get("parent_key_prefix")
    if pref:
        return pref
    # Bare table name: rows are keyed '<bare table>:<pk>', so a schema-qualified
    # parent_table ('Inventory.customers') still resolves to 'customers:'.
    return "{}{}:".format(link_prefix, bare_table(rule.get("parent_table", "")))


def _rule_tag(rule):
    return "{}:{}".format(bare_table(rule.get("parent_table", "")),
                          rule.get("join_field", ""))


def fk_field(rule):
    """The child field naming the parent it points at. Mirrors fkField (Go).

    The sink applies a parent's fan-out only to a child whose fk field matches,
    so a loaded child must carry it or later parent changes skip it.
    """
    return "_ecdl_fk:" + _rule_tag(rule)


def pv_field(rule):
    """The child field holding the version of the embedded parent (pvField, Go)."""
    return "_ecdl_pv:" + _rule_tag(rule)


def denorm_for(spec, table):
    """Denorm rules for which `table` is the CHILD (Phase 1 embed lookups)."""
    if not spec:
        return []
    ts = spec.get("tables", {}).get(table)
    if not ts:
        return []
    return ts.get("denormalize", []) or []


def idx_key(child_table, parent_table, parent_pk, link_prefix=""):
    """Reverse-index set key: <link prefix>idx:<child>:by:<bare parent>:<parent_pk>.

    Byte-for-byte identical to idxKey() in the Go sink (denorm.go), so the two
    write paths maintain the same index and either can serve a parent fan-out.
    It used to ignore the link's key prefix and keep a schema-qualified parent
    name, and the sink then never found the children the loader indexed.
    """
    return "{}idx:{}:by:{}:{}".format(link_prefix, child_table,
                                      bare_table(parent_table), parent_pk)


def parent_field_name(spec, rule, field):
    """The field a rule's parent column has in the parent's cached hash: the
    parent table's rename of it, or the column itself. Mirrors
    parentFieldName (Go).

    A child reads its parent from the cache, which holds the parent row after
    the parent table's own transform, and the sink's parent fan-out now reads
    the same transformed row (Jira story F198), so both look a column up under this name.
    """
    ts = ((spec or {}).get("tables") or {}).get(bare_table(rule.get("parent_table", ""))) or {}
    return (ts.get("rename") or {}).get(field, field)


def embed_parent_fields(rule, parent):
    """Prefixed parent fields to merge onto a child, given the parent row.

    Mirrors EmbedParentFields() in the Go sink: every listed field is emitted
    (prefixed), missing values become empty string, so the child always carries
    the expected keys.
    """
    prefix = rule.get("prefix", "") or ""
    out = {}
    for f in rule.get("fields", []) or []:
        out[prefix + f] = parent.get(f, "")
    return out
