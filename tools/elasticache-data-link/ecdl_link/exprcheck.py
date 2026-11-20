"""Syntax check of a computed field's expression, at link validation (Jira story F077).

The sink (tools/go-sink/expr.go) and the bulk loader
(data-plane/bulk-loader/transforms.py) refuse to start with an expression
outside their shared grammar. This checks the same grammar when the link is
validated, so the mistake is reported by `ecdl validate` / `plan` instead of
as a task that will not start. Only the syntax is checked; the engines
evaluate. All three are tested against
tools/go-sink/testdata/transform_cases.json.

    expr    := term (('+' | '-') term)*
    term    := unary (('*' | '/') unary)*
    unary   := ('+' | '-') unary | primary
    primary := number | name | '(' expr ')'
    number  := (digits ['.' [digits]] | '.' digits) [('e' | 'E') ['+' | '-'] digits]
    name    := [A-Za-z_][A-Za-z0-9_]*
"""
import re

_DECIMAL = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SPACE = " \t\n\r"
_MAX_DEPTH = 64  # nested parentheses and signs, as the engines bound them


class _Syntax(Exception):
    pass


def expr_problem(expr):
    """Why `expr` would be refused, as text, or None when it is valid."""
    if not isinstance(expr, str):
        return 'must be text'
    pos = [0]
    depth = [0]

    def enter():
        depth[0] += 1
        if depth[0] > _MAX_DEPTH:
            raise fail('nested more than {} deep'.format(_MAX_DEPTH))

    def skip():
        while pos[0] < len(expr) and expr[pos[0]] in _SPACE:
            pos[0] += 1

    def peek():
        return expr[pos[0]] if pos[0] < len(expr) else ''

    def fail(what):
        if pos[0] >= len(expr):
            return _Syntax('{} at the end'.format(what))
        return _Syntax('{} at position {} ({!r})'.format(what, pos[0] + 1, expr[pos[0]]))

    def primary():
        skip()
        c = peek()
        if c == '(':
            pos[0] += 1
            enter()
            additive()
            depth[0] -= 1
            skip()
            if peek() != ')':
                raise fail("')' expected")
            pos[0] += 1
            return
        m = None
        if c and c in '0123456789.':
            m = _DECIMAL.match(expr, pos[0])
            if not m:
                raise fail('a number expected')
        elif c:
            m = _NAME.match(expr, pos[0])
        if not m:
            raise fail("a number, a column name or '(' expected")
        pos[0] = m.end()

    def unary():
        skip()
        if peek() in ('+', '-') and peek():
            pos[0] += 1
            enter()
            unary()
            depth[0] -= 1
            return
        primary()

    def binary(operand, ops):
        operand()
        while True:
            skip()
            if not peek() or peek() not in ops:
                return
            pos[0] += 1
            operand()

    def additive():
        binary(lambda: binary(unary, '*/'), '+-')

    try:
        additive()
        skip()
        if pos[0] < len(expr):
            raise fail('an operator expected')
    except _Syntax as exc:
        return '{}. Expressions use + - * / ( ), numbers and column names'.format(exc)
    return None
