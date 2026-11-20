"""SQL identifiers: validate, then quote, in one place.

Values go through the driver as bind parameters (%s, ?, :1). Identifiers --
table, schema and column names -- cannot be bound, so they end up in the SQL
text. Every identifier that does is first checked against a strict pattern and
then quoted for its engine. A name that fails the check raises; it is never
"cleaned up" into something else, because silently querying a different table
than the one asked for is a separate bug.

The pattern admits ASCII letters, digits, '_' and '$', starting with a letter or
'_'. That excludes every quote character, whitespace, ';', '-', '/', '*' and
anything non-ASCII, so a valid name cannot terminate its quotes or start a
second statement. Quoting is then about reserved words and case, not escaping.

Styles:
  mysql       `name`        (MySQL, MariaDB, Aurora MySQL)
  postgresql  "name"        exact case
  sqlserver   [name]
  oracle      "name"        exact case -- Oracle catalog names are upper case
  bare        name          validated, not quoted. For places that rely on the
                            engine folding an unquoted name (Oracle callers that
                            pass lower-case names) or on the exact text Debezium
                            matches (a signalling collection).

This file is vendored, byte for byte, into data-plane/bulk-loader/ because the
loader image does not contain ecdl_link. A test fails if the copies differ.
"""

import re

__all__ = ['UnsafeIdentifier', 'is_safe', 'validate', 'validate_qualified',
           'quote', 'quote_qualified', 'render', 'binds']

# 128 covers every supported engine's limit (MySQL 64, PostgreSQL 63, Oracle 128,
# SQL Server 128); each engine still enforces its own.
_PART = re.compile(r'[A-Za-z_][A-Za-z0-9_$]{0,127}\Z')

_QUOTES = {
    'mysql': ('`', '`'),
    'postgresql': ('"', '"'),
    'sqlserver': ('[', ']'),
    'oracle': ('"', '"'),
    'bare': ('', ''),
}

_ALIASES = {
    'mariadb': 'mysql', 'aurora-mysql': 'mysql',
    'postgres': 'postgresql', 'aurora-postgresql': 'postgresql',
    'mssql': 'sqlserver',
}


class UnsafeIdentifier(ValueError):
    """A table, schema or column name that is not safe to put in SQL text."""


def _style(engine):
    key = _ALIASES.get(engine, engine)
    if key not in _QUOTES:
        raise ValueError('unknown SQL quoting style {!r}'.format(engine))
    return key


def is_safe(name):
    """True for a single identifier part that validate() would accept."""
    return isinstance(name, str) and bool(_PART.match(name))


def validate(name, what='identifier'):
    """Return `name` unchanged if it is a safe single identifier, else raise."""
    if not is_safe(name):
        raise UnsafeIdentifier(
            '{} {!r} is not a plain SQL identifier (letters, digits, _ and $, '
            'starting with a letter or _)'.format(what, name))
    return name


def validate_qualified(name, what='table', max_parts=3):
    """Return a dotted name (db.schema.table) unchanged if every part is safe."""
    if not isinstance(name, str) or not name:
        raise UnsafeIdentifier('{} {!r} is empty'.format(what, name))
    parts = name.split('.')
    if len(parts) > max_parts:
        raise UnsafeIdentifier('{} {!r} has more than {} parts'.format(
            what, name, max_parts))
    for part in parts:
        validate(part, what)
    return name


def quote(name, engine, what='identifier'):
    """One identifier, validated and quoted for `engine`."""
    left, right = _QUOTES[_style(engine)]
    return left + validate(name, what) + right


def quote_qualified(name, engine, what='table', max_parts=3):
    """A dotted name with each part validated and quoted separately.

    'Inventory.products' -> `Inventory`.`products`, not `Inventory.products`,
    which MySQL would read as one table whose name contains a dot.
    """
    validate_qualified(name, what, max_parts)
    return '.'.join(quote(part, engine, what) for part in name.split('.'))


class _Binds(str):
    """A run of bind markers, from binds(). The one non-identifier render() takes."""


_MARKERS = ('%s', '?', ':')


def binds(count, marker='%s'):
    """`count` bind markers for a VALUES list: '%s, %s, %s', or ':1, :2, :3'.

    marker is the driver's paramstyle: '%s' (pymysql, psycopg2, pymssql), '?'
    (sqlite3) or ':' for numbered Oracle binds. Pass the result to render() --
    it is the only thing render() puts into SQL without quoting it, and it can
    only ever contain markers.
    """
    if marker not in _MARKERS:
        raise ValueError('unknown bind marker {!r}'.format(marker))
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError('bind count must be a positive integer, got {!r}'.format(count))
    if marker == ':':
        return _Binds(', '.join(':{}'.format(i) for i in range(1, count + 1)))
    return _Binds(', '.join([marker] * count))


def render(engine, template, **identifiers):
    """Fill `template`'s {placeholders} with quoted identifiers.

    `template` is a constant in the calling code. Each keyword is a table or
    column name: a string is treated as a (possibly dotted) name, a list or
    tuple as a column list joined with ', '. Values never pass through here --
    leave %s / ? / :1 markers in the template for the driver to bind, or use
    binds() for a VALUES list whose length varies.

        render('mysql', 'SELECT {cols} FROM {t} WHERE {pk} = %s',
               cols=['id', 'price'], t='Inventory.products', pk='id')
        -> 'SELECT `id`, `price` FROM `Inventory`.`products` WHERE `id` = %s'

        render('mysql', 'INSERT INTO {t} ({cols}) VALUES ({marks})',
               t='orders', cols=['id', 'amount'], marks=binds(2))
        -> 'INSERT INTO `orders` (`id`, `amount`) VALUES (%s, %s)'
    """
    style = _style(engine)
    filled = {}
    for key, value in identifiers.items():
        if isinstance(value, _Binds):
            filled[key] = value
        elif isinstance(value, (list, tuple)):
            if not value:
                raise UnsafeIdentifier('column list {!r} is empty'.format(key))
            filled[key] = ', '.join(quote(v, style, key) for v in value)
        else:
            filled[key] = quote_qualified(value, style, key)
    return template.format(**filled)
