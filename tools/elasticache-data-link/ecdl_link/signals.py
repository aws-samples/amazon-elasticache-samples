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

A `setup-signal-table.sql` next to the Debezium image used to describe this,
with one shared, MySQL-only `debezium_signal` table -- but no properties file
set `signal.data.collection` then, so Debezium never read it, and a shared
table is exactly what the per-link rule below forbids. It was removed (Jira story F020);
`ecdl signal ddl <link.yaml>` prints the statements for a link.

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
import re

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
    normally, never finds the table, and every signal is ignored indefinitely.
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
    # Qualified on every engine. MySQL's used to be cut to the bare table name
    # ("run it in the source database"), so run from a session in another
    # database it created the table there, Debezium never saw a signal, and a
    # backfill was silently ignored (Jira story F019).
    return sqlident.render('bare', template, qualified=collection)


def mysql_signal_grant_sql(collection, user):
    """The grant Debezium needs on a MySQL signalling table.

    It reads the table like any captured one, and an incremental snapshot
    writes its window markers into it, so the snapshot fails without INSERT.
    MySQL names a user with its host; '%' is any host, narrow it if the user
    was created for one.
    """
    collection = _checked_collection(collection)
    who = "'{}'@'%'".format(user) if sqlident.is_safe(user or '') else "'<the CDC user>'@'%'"
    return 'GRANT SELECT, INSERT ON {} TO {};'.format(collection, who)

# ── PostgreSQL: the publication and the signalling table's grant ──────────
#
# PostgreSQL sends a logical-replication client only the tables in the
# publication it names, so on PostgreSQL the publication is part of what is
# captured. A link table missing from it is never captured, and a signal
# inserted into a signalling table missing from it never reaches Debezium, so
# a backfill request is ignored with no error anywhere. Debezium does not
# create or alter it (publication.autocreate.mode=disabled), so these
# statements are emitted for the customer's DBA, like the table DDL.

# schema.table, each part a plain identifier or a double-quoted one (the
# preflight quotes a catalogue name PostgreSQL would otherwise fold).
_PG_PART = r'(?:[A-Za-z_][A-Za-z0-9_$]{0,62}|"(?:[^"]|""){1,63}")'
_PG_MEMBER = re.compile(r'{0}\.{0}\Z'.format(_PG_PART))


def pg_publication_sql(publication, tables, create=False):
    """CREATE PUBLICATION ... FOR TABLE, or ALTER PUBLICATION ... ADD TABLE.

    One statement for all `tables` (schema.table). ALTER fails on a table that
    is already a member, so callers that know the membership pass only the
    missing ones.
    """
    if not tables:
        raise SignalError('a publication statement needs at least one table')
    for table in tables:
        if not _PG_MEMBER.match(table):
            raise SignalError('{!r} is not a schema.table name that can be put in '
                              'SQL text'.format(table))
    template = ('CREATE PUBLICATION {pub} FOR TABLE {tables};' if create
                else 'ALTER PUBLICATION {pub} ADD TABLE {tables};')
    return template.format(
        pub=sqlident.validate(publication, 'publication'),
        tables=', '.join(tables))


def pg_signal_grant_sql(collection, user):
    """The grant Debezium needs on the signalling table.

    An incremental snapshot writes its window markers into the signalling
    table, so the connector's user needs INSERT on it; without it the snapshot
    fails as soon as it starts.
    """
    collection = _checked_collection(collection)
    who = user if sqlident.is_safe(user or '') else '<the CDC user>'
    return 'GRANT INSERT ON {} TO {};'.format(collection, who)


def pg_prerequisite_sql(publication, tables, collection, user):
    """Comment-annotated SQL for `ecdl signal ddl` on PostgreSQL: the
    publication (create it, or add to an existing one) and the grant."""
    members = list(tables) + [collection]
    return '\n'.join([
        '-- PostgreSQL sends Debezium only the tables in the publication {}. It must'.format(
            publication),
        '-- hold every table of this link and the signalling table: a table missing',
        '-- from it is never captured, and a signal written to a signalling table',
        '-- missing from it is never seen. If the publication does not exist yet:',
        pg_publication_sql(publication, members, create=True),
        '-- If it exists, add the tables it lacks instead (PostgreSQL refuses a table',
        '-- that is already a member):',
        '-- ' + pg_publication_sql(publication, members),
        '-- Debezium writes its snapshot window markers into the signalling table:',
        pg_signal_grant_sql(collection, user),
    ])


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



# ── did Debezium act on a snapshot signal? ────────────────────────────────
#
# The signal is an INSERT the operator runs on the source, so nothing used to
# say whether Debezium read it. Two things in the broker show that it did:
#
#   * Debezium's committed offset (the link's OffsetKey hash) carries the
#     running incremental snapshot: incremental_snapshot_collections lists the
#     tables, and incremental_snapshot_correlation_id is the signal id. Those
#     keys go when the snapshot completes, which is how "finished" is told.
#     (Field names from Debezium's AbstractIncrementalSnapshotContext.)
#   * The snapshot's rows reach the table streams as op "r" events. The sink
#     deletes entries as soon as they are applied, so seeing them is luck, not
#     a guarantee; it is used as evidence, never required.
#
# Neither seen before the timeout is reported as "unverified", with what was
# observed, not as a failure: an empty table produces no rows, and a snapshot
# can start and finish between two offset commits.

SNAPSHOT_COLLECTIONS_KEY = 'incremental_snapshot_collections'
SNAPSHOT_CORRELATION_KEY = 'incremental_snapshot_correlation_id'


def _text(value):
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else value


def offset_snapshot(broker, offset_key):
    """The incremental snapshot in Debezium's committed offset.

    {'collections': [...], 'correlation_id': ...}; empty lists/None when no
    snapshot is running; None when the offset could not be read.
    """
    try:
        stored = broker.hgetall(offset_key) or {}
    except Exception:  # noqa: BLE001
        return None
    collections, correlation = [], None
    for raw in stored.values():
        try:
            offset = json.loads(_text(raw))
        except (TypeError, ValueError):
            continue
        if not isinstance(offset, dict):
            continue
        listed = offset.get(SNAPSHOT_COLLECTIONS_KEY)
        if isinstance(listed, str):
            try:
                listed = json.loads(listed)
            except ValueError:
                listed = [listed]
        for item in listed or []:
            name = item.get(SNAPSHOT_COLLECTIONS_KEY + '_id') if isinstance(item, dict) else item
            if isinstance(name, str) and name not in collections:
                collections.append(name)
        correlation = offset.get(SNAPSHOT_CORRELATION_KEY) or correlation
    return {'collections': collections, 'correlation_id': correlation}


def stream_positions(broker, streams):
    """{stream: last-generated-id, or None for a stream that does not exist}."""
    out = {}
    for stream in streams:
        try:
            out[stream] = _text(broker.xinfo_stream(stream).get('last-generated-id'))
        except Exception:  # noqa: BLE001
            out[stream] = None
    return out


def _snapshot_rows_after(broker, stream, after_id, count=500):
    """(newest id read, whether any op "r" entry was among them)."""
    start = '(' + after_id if after_id else '-'
    try:
        entries = broker.xrange(stream, min=start, max='+', count=count)
    except Exception:  # noqa: BLE001
        return after_id, False
    seen = False
    for entry_id, fields in entries:
        after_id = _text(entry_id)
        value = (fields or {}).get('value', (fields or {}).get(b'value'))
        try:
            seen = seen or json.loads(_text(value)).get('op') == 'r'
        except (TypeError, ValueError, AttributeError):
            continue
    return after_id, seen


def _lists_any(snapshot, collections):
    wanted = {c.lower() for c in collections}
    return any(c.lower() in wanted for c in (snapshot or {}).get('collections') or [])


def wait_for_snapshot(broker, signal_id, collections, streams, offset_key,
                      baseline, timeout=120.0, interval=1.0, settle=3.0,
                      clock=None, sleep=None):
    """Watch the broker after a snapshot signal was written.

    baseline is stream_positions() taken BEFORE the signal was inserted.
    Returns {'status': 'finished' | 'consumed' | 'unverified', 'detail': str,
    'evidence': [...]}:
      finished    Debezium took the signal and its offset no longer lists the
                  tables (seen `settle` seconds after the last snapshot row).
      consumed    Debezium took the signal; still running when the wait ended.
      unverified  no sign of it within `timeout`.
    """
    import time
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    before = offset_snapshot(broker, offset_key)
    already = _lists_any(before, collections)
    positions = dict(baseline)
    evidence = []
    consumed_at = None
    last_row_at = None
    deadline = clock() + timeout
    offset_readable = before is not None
    advanced = set()
    while True:
        now = clock()
        state = offset_snapshot(broker, offset_key)
        offset_readable = offset_readable or state is not None
        running = state is not None and (
            (signal_id and state.get('correlation_id') == signal_id) or
            (not already and _lists_any(state, collections)))
        if running and consumed_at is None:
            consumed_at = now
            evidence.append("Debezium's offset lists an incremental snapshot of {}".format(
                ', '.join(state['collections']) or 'these tables'))
        # The stream's last-generated-id moves even when the sink has
        # already deleted the entries, so it tells "something arrived".
        for stream, last in stream_positions(broker, streams).items():
            if last and last != baseline.get(stream):
                advanced.add(stream)
        for stream in streams:
            newest, rows = _snapshot_rows_after(broker, stream, positions.get(stream))
            positions[stream] = newest
            if rows:
                last_row_at = now
                if consumed_at is None:
                    consumed_at = now
                note = 'snapshot rows (op "r") arrived on {}'.format(stream)
                if note not in evidence:
                    evidence.append(note)
        if consumed_at is not None and state is not None and not running \
                and not _lists_any(state, collections) \
                and now - max(consumed_at, last_row_at or consumed_at) >= settle:
            return {'status': 'finished', 'evidence': evidence,
                    'detail': 'Debezium read the signal and the snapshot of {} has '
                              'finished'.format(', '.join(collections))}
        if now >= deadline:
            break
        sleep(interval)
    if consumed_at is not None:
        return {'status': 'consumed', 'evidence': evidence,
                'detail': 'Debezium read the signal; the snapshot was still running '
                          'after {:g}s. It continues in the background.'.format(timeout)}
    if not offset_readable:
        why = "Debezium's offset could not be read from the broker"
    elif advanced:
        why = ('{} received entries after the signal, but no snapshot row (op "r") '
               'was seen among them. The sink deletes entries once applied, so rows '
               'it applied before they were looked at cannot be checked'.format(
                   ', '.join(sorted(advanced))))
    else:
        why = 'no new entries reached {}'.format(', '.join(streams) or 'the table streams')
    hint = ('Check the Debezium log for "Requested \'INCREMENTAL\' snapshot" or an '
            'error naming the signalling table. On PostgreSQL the signalling '
            'table must be in the publication, or the signal is never seen.')
    if already:
        hint = ('A snapshot of these tables was already running before the '
                'signal. ' + hint)
    return {'status': 'unverified', 'evidence': evidence,
            'detail': 'no sign within {:g}s that Debezium read the signal: {}. '
                      '{}'.format(timeout, why, hint)}
