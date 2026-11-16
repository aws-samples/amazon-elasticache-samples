"""Day-2 changes to a live link, without a stop-and-recreate.

THE PROBLEM THIS SOLVES. Adding a table to a running link by putting it in
`table.include.list` captures only changes made after the restart. Every row that
already existed is never sent, so the cache is permanently missing them -- and
nothing reports it, because from the pipeline's point of view everything is
working. The only recoveries are a full re-snapshot of the whole link, or a
separate bulk load.

Debezium's answer is an INCREMENTAL SNAPSHOT, requested by writing a row into a
signalling table on the source database. The connector reads it, backfills the
named tables in chunks, and keeps streaming throughout.

`data-plane/debezium-server/setup-signal-table.sql` has described this since the
v3 work, with MySQL-only DDL and commented-out example JSON -- but no properties
file ever set `signal.data.collection`, so Debezium never read the table. It was
documentation for a feature that was not enabled.

WHAT IS ENGINE-SPECIFIC HERE
  * how the collection is qualified (the same rule as table.include.list, which
    ENGINE_FACTS already encodes -- database for MySQL, schema for the rest)
  * the DDL, including Oracle's lack of IF NOT EXISTS and its VARCHAR2
  * SQL Server additionally needs the signalling table to be reachable by the
    connector's own user

WHAT IS REQUIRED
  * the table is PER LINK. Debezium requires the signalling collection to be
    unique to a connector instance; two links sharing one would read each other's
    signals and snapshot each other's tables.
"""

from __future__ import annotations

import json

from . import sqlident
from .resolve import ENGINE_FACTS, ResolutionError

# Debezium's own heartbeat topic. It is control traffic like the signalling table,
# and equally unconsumed, so it is dropped by the same predicate.
HEARTBEAT_TOPIC_PREFIX = '__debezium-heartbeat'

SIGNAL_TYPES = ('execute-snapshot', 'stop-snapshot', 'pause-snapshot',
                'resume-snapshot', 'log')


class SignalError(Exception):
    def __init__(self, message, hint=None):
        super(SignalError, self).__init__(message)
        self.hint = hint


def signal_table_name(link_name):
    """The signalling table for a link.

    Per link; the module docstring gives the reason. The link name is normalised into an identifier so a
    link called "checkout-cache" does not produce an unquotable table name.
    """
    safe = ''.join(c if (c.isalnum() or c == '_') else '_'
                   for c in link_name.lower())
    return 'ecdl_signal_{}'.format(safe)


def signal_collection(engine, link_name, database, schema=None, override=None):
    """The fully-qualified signalling collection, as Debezium wants it.

    Qualified by exactly the rule the engine uses for table.include.list, taken
    from ENGINE_FACTS rather than restated -- MySQL qualifies by database, the
    other three by schema. Getting this wrong is silent: the connector starts
    normally, never finds the table, and every signal is ignored for ever.
    """
    if override:
        return _checked_collection(override)
    facts = ENGINE_FACTS.get(engine)
    if not facts:
        raise SignalError('unsupported engine {!r}'.format(engine))
    table = signal_table_name(link_name)
    if facts['qualify_with'] == 'database':
        if not database:
            raise SignalError(
                'the signalling collection needs the source database name')
        return _checked_collection('{}.{}'.format(database, table))
    qualifier = schema or facts.get('default_schema')
    if not qualifier:
        raise SignalError(
            'the signalling collection needs a schema for {}'.format(engine),
            'Set source.schema in the link.')
    if engine == 'oracle':
        # Oracle folds unquoted identifiers to upper case, so the collection name
        # the connector matches on is upper case too.
        return _checked_collection('{}.{}'.format(qualifier.upper(), table.upper()))
    return _checked_collection('{}.{}'.format(qualifier, table))


def _checked_collection(collection):
    """The collection name, if every dotted part is a plain SQL identifier.

    It is written into DDL and an INSERT as text (names cannot be bound), and it
    comes from the link file -- advanced.signal_collection, source.database,
    source.schema. Left unquoted on purpose: Debezium matches the signalling
    collection by this exact text, and quoting would change case handling on
    PostgreSQL and Oracle. Validation is what makes leaving it unquoted safe.
    """
    try:
        return sqlident.validate_qualified(collection, 'signalling collection')
    except sqlident.UnsafeIdentifier as exc:
        raise SignalError(str(exc), 'Use letters, digits and _ in the database, '
                          'schema and advanced.signal_collection names.')


def topic_pattern(topic_prefix, collection):
    """Regex for the control traffic the Filter transform drops.

    Names the exact collections rather than matching a wildcard, so a customer
    table whose name happens to resemble ours is never silently dropped. Anchored
    at the end for the same reason.

    Only the signalling table's own inserts are matched. The HEARTBEAT IS NOT
    DROPPED, although it used to be: Debezium Server (3.0) commits an offset
    only for records that reach the sink, so with the heartbeat filtered an idle
    source never committed a position -- and the first deploy failed at
    "Debezium has not committed a log position", because pausing capture without
    one would skip every change made during the load. The heartbeat now reaches
    data:__debezium-heartbeat.<prefix>, and the sink keeps that stream trimmed
    (TRIM_STREAMS), so it neither grows nor reads as a backlog.

    With no signalling table there is nothing to drop; the pattern then matches
    no topic at all (`^$`, as a topic name is never empty).
    """
    table = collection.rsplit('.', 1)[-1] if collection else ''
    if not table:
        return '^$'
    return '(.*\\.{})'.format(_escape(table))


def _escape(text):
    out = []
    for ch in text or '':
        if ch in '.\\+*?[^]$(){}=!<>|:-#':
            out.append('\\' + ch)
        else:
            out.append(ch)
    return ''.join(out)


# ── prerequisite DDL ──────────────────────────────────────────────────────
# The column widths are Debezium's documented ones. `data` holds the JSON payload,
# which for a many-table snapshot request can be long, hence 2048.

_MYSQL_DDL = """CREATE TABLE IF NOT EXISTS {qualified} (
  id   VARCHAR(42)   NOT NULL,
  type VARCHAR(32)   NOT NULL,
  data VARCHAR(2048) NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB;"""

_POSTGRES_DDL = """CREATE TABLE IF NOT EXISTS {qualified} (
  id   VARCHAR(42)   NOT NULL,
  type VARCHAR(32)   NOT NULL,
  data VARCHAR(2048) NULL,
  PRIMARY KEY (id)
);"""

_SQLSERVER_DDL = """IF OBJECT_ID(N'{qualified}', N'U') IS NULL
CREATE TABLE {qualified} (
  id   VARCHAR(42)   NOT NULL,
  type VARCHAR(32)   NOT NULL,
  data VARCHAR(2048) NULL,
  PRIMARY KEY (id)
);"""

# Oracle has no CREATE TABLE IF NOT EXISTS, so an existing table has to be
# tolerated by catching ORA-00955 (name already used).
_ORACLE_DDL = """BEGIN
  EXECUTE IMMEDIATE 'CREATE TABLE {qualified} (
    id   VARCHAR2(42)   NOT NULL,
    type VARCHAR2(32)   NOT NULL,
    data VARCHAR2(2048) NULL,
    PRIMARY KEY (id)
  )';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;"""


def signal_table_ddl(engine, collection):
    """DDL a customer runs once on the source, before deploying the link.

    Emitted rather than executed: this is a change to the customer's own database
    and it needs review by their DBA, not our credentials. It is also the one
    prerequisite that cannot be derived from an ARN.
    """
    if engine not in ENGINE_FACTS:
        raise SignalError('unsupported engine {!r}'.format(engine))
    collection = _checked_collection(collection)
    template = {'mysql': _MYSQL_DDL, 'postgresql': _POSTGRES_DDL,
                'sqlserver': _SQLSERVER_DDL}.get(engine, _ORACLE_DDL)
    if engine == 'mysql':
        # Run in the source database, so the table is named bare.
        collection = collection.rsplit('.', 1)[-1]
    return sqlident.render('bare', template, qualified=collection)


# ── building signals ──────────────────────────────────────────────────────

def snapshot_signal(collections, signal_id=None, additional_conditions=None,
                    surrogate_key=None):
    """An incremental-snapshot request for one or more tables.

    `collections` are fully-qualified names in the SAME form the connector's
    table.include.list uses, which is what the resolver's `qualified` field
    already holds.
    """
    if not collections:
        raise SignalError('an incremental snapshot needs at least one table')
    payload = {'data-collections': list(collections), 'type': 'incremental'}
    if additional_conditions:
        payload['additional-conditions'] = additional_conditions
    if surrogate_key:
        # For a table whose primary key is unsuitable for chunking (a UUID, say),
        # a monotonic column can be named instead.
        payload['surrogate-key'] = surrogate_key
    return _signal('execute-snapshot', payload, signal_id)


def stop_snapshot_signal(collections=None, signal_id=None):
    """Stop an in-progress snapshot, or exclude specific tables from it.

    An empty collection list stops the whole snapshot, which is the documented
    behaviour and the reason the field is omitted rather than sent empty.
    """
    payload = {}
    if collections:
        payload['data-collections'] = list(collections)
    return _signal('stop-snapshot', payload, signal_id)


def pause_snapshot_signal(signal_id=None):
    # No data field: a pause takes effect wherever the snapshot has reached, so
    # naming a collection would be meaningless.
    return _signal('pause-snapshot', {}, signal_id)


def resume_snapshot_signal(signal_id=None):
    return _signal('resume-snapshot', {}, signal_id)


def log_signal(message, signal_id=None):
    """Ask the connector to log a line.

    Useful as a liveness probe: it proves the connector is reading the signalling
    table at all, without changing any data.
    """
    return _signal('log', {'message': message}, signal_id)


def _signal(signal_type, payload, signal_id):
    if signal_type not in SIGNAL_TYPES:
        raise SignalError('unknown signal type {!r}'.format(signal_type))
    if signal_id is None:
        import uuid
        signal_id = '{}-{}'.format(signal_type, uuid.uuid4().hex[:12])
    if len(signal_id) > 42:
        # The id column is VARCHAR(42); a longer id would be truncated or rejected
        # depending on the engine's strictness, and a truncated id can collide.
        raise SignalError(
            'signal id {!r} is longer than the 42 characters the id column '
            'holds'.format(signal_id))
    return {
        'id': signal_id,
        'type': signal_type,
        # separators keep the JSON compact, which matters against the 2048-char
        # data column when a snapshot names many tables.
        'data': json.dumps(payload, separators=(',', ':')) if payload else None,
    }


def insert_statement(engine, collection, signal):
    """Parameterised INSERT for a signal row, with its bind values.

    Parameterised rather than interpolated because the `data` field is JSON full
    of quotes and braces, and because a table name that came from a link file is
    not something to concatenate into SQL.
    """
    marks = sqlident.binds(3, ':' if engine == 'oracle' else '%s')
    sql = sqlident.render('bare', 'INSERT INTO {t} (id, type, data) VALUES ({marks})',
                          t=_checked_collection(collection), marks=marks)
    return sql, (signal['id'], signal['type'], signal['data'])


def describe_signal(signal):
    """One line, for a CLI or an audit log."""
    payload = signal.get('data')
    if not payload:
        return '{} [{}]'.format(signal['type'], signal['id'])
    return '{} [{}] {}'.format(signal['type'], signal['id'], payload)
