"""List the tables and columns in a source, so they can be picked not typed.

This is the ONLY place in the control plane that reads a secret VALUE. Everything
else -- resolution, planning, discovery -- uses only describe calls, deliberately,
because a control plane that never holds a credential cannot leak one. Listing a
database's tables genuinely cannot be done that way: there is no AWS API for "what
is inside this database", so it takes a connection, and a connection takes the
password.

The rules that follow from that, all enforced below:

* The value is read, used for one catalogue query, and dropped. It is never
  returned, never logged, never stored, and never put in a config dict.
* Driver error text is scrubbed before it leaves this module. A connection failure
  from some drivers echoes the parameters it was given, and this is the one code
  path where those parameters include a password.
* Failure is a RESULT, not an exception. The control plane usually cannot reach a
  customer's database -- different VPC, no security group rule -- and that is an
  ordinary situation with a clear fallback: type the table names. So every
  failure comes back as `listed: false` with a reason and that fallback stated,
  rather than as an error that makes the form look broken.

Engine support differs by engine, and this module reports that accurately:
    MySQL / Aurora MySQL   pymysql        in backend/requirements.txt
    PostgreSQL / Aurora PG psycopg2       in backend/requirements.txt
    SQL Server             pymssql        NOT installed -- type the names
    Oracle                 oracledb       NOT installed -- type the names
Adding a driver is a one-line requirements change, but claiming support we have
not got would turn a clear "type the names" into a stack trace.
"""

import json

from .resolve import (
    ENGINE_FACTS,
    Clients,
    ResolutionError,
    describe_source,
    engine_family,
)

# Short on purpose. The common failure is no network path, which manifests as a
# hang, and a form that appears frozen for thirty seconds reads as broken. Eight
# seconds is long enough for a working in-VPC connection and short enough that a
# blocked one answers while someone is still looking at the screen.
CONNECT_TIMEOUT_SEC = 8

# Catalogue queries are cheap, but a source under load can still make one wait,
# and this runs inside a web request.
QUERY_TIMEOUT_SEC = 15

# Drivers per engine family. Absence is a supported outcome, not a bug.
_DRIVERS = {
    'mysql': ('pymysql', 'pymysql'),
    'postgresql': ('psycopg2', 'psycopg2-binary'),
    'sqlserver': ('pymssql', 'pymssql'),
    'oracle': ('oracledb', 'oracledb'),
}


def _import_driver(family):
    """The driver module for an engine, or None plus how to get it."""
    module_name, package = _DRIVERS[family]
    try:
        return __import__(module_name), None
    except ImportError:
        return None, package


def _scrub(text, password):
    """Remove a password from text before it is shown to anyone.

    An extra safeguard. Most drivers do not echo credentials, but this is the only
    module holding one, so this is the one place the safeguard is needed.
    """
    out = str(text)
    if password:
        out = out.replace(password, '********')
    return out


def read_credential(clients, secret_arn):
    """Username and password from a secret, accepting both shapes in use.

    RDS-managed secrets hold JSON with `username` and `password`. Manually created ones
    are often just the password as plaintext. The Go sink and the bulk loader make
    the same distinction the same way, on purpose: a credential that worked for
    one component and not another would be a half-failure, which is harder to
    diagnose than a clean one.
    """
    try:
        response = clients.secretsmanager.get_secret_value(SecretId=secret_arn)
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        text = str(exc)
        if 'AccessDenied' in name or 'AccessDenied' in text or 'not authorized' in text:
            raise ResolutionError(
                'not permitted to read the credential {}'.format(secret_arn),
                'Listing tables needs GetSecretValue on that secret. Without it '
                'you can still deploy -- enter the table names manually.')
        if 'ResourceNotFound' in name or 'ResourceNotFound' in text:
            raise ResolutionError(
                'the credential {} does not exist'.format(secret_arn))
        raise ResolutionError(
            'could not read the credential {}: {}'.format(secret_arn, text))

    raw = response.get('SecretString')
    if raw is None:
        # A binary secret cannot be a database password in any shape we support.
        raise ResolutionError(
            'the credential {} holds binary data, not a password'.format(
                secret_arn))

    raw = raw.strip()
    if raw.startswith('{'):
        try:
            document = json.loads(raw)
        except ValueError:
            # Looked like JSON and is not. Treating it as a plaintext password
            # that happens to start with a brace is the safer reading -- the same
            # judgement the Go sink makes about auth tokens.
            return None, raw
        if isinstance(document, dict):
            password = document.get('password')
            if not password:
                raise ResolutionError(
                    'the credential {} is JSON with no "password" field'.format(
                        secret_arn),
                    'A database credential must hold the password, either as '
                    'JSON with username and password or as plain text.')
            return document.get('username'), password
    return None, raw


def _connect(family, driver, host, port, user, password, database):
    """Open a connection, or raise the driver's own error for classification."""
    if family == 'mysql':
        return driver.connect(
            host=host, port=int(port or 3306), user=user, password=password,
            database=database or None, connect_timeout=CONNECT_TIMEOUT_SEC,
            read_timeout=QUERY_TIMEOUT_SEC, write_timeout=QUERY_TIMEOUT_SEC,
            # No autocommit needed: everything here is a read.
            charset='utf8mb4')
    if family == 'postgresql':
        return driver.connect(
            host=host, port=int(port or 5432), user=user, password=password,
            dbname=database, connect_timeout=CONNECT_TIMEOUT_SEC,
            # Stops a catalogue query from outliving the web request.
            options='-c statement_timeout={}s'.format(QUERY_TIMEOUT_SEC))
    raise ResolutionError(
        'listing tables is not implemented for {}'.format(
            ENGINE_FACTS[family]['label']))


def _classify(family, exc, host, port, user, password):
    """Turn a driver error into something with a fix in it.

    Each of these was chosen because the raw driver message does not say what to
    do. "timed out" in particular is the single most likely outcome when the
    control plane is in a different VPC from the database, and on its own it
    reads like the database is down.
    """
    text = _scrub(exc, password)
    lowered = text.lower()
    label = ENGINE_FACTS[family]['label']

    if 'timed out' in lowered or 'timeout' in lowered or "can't connect" in lowered \
            or 'could not connect' in lowered or 'connection refused' in lowered:
        return {
            'stage': 'connect',
            'error': 'could not reach {} at {}:{} within {}s'.format(
                label, host, port, CONNECT_TIMEOUT_SEC),
            'hint': 'The control plane needs a network path to the database: the '
                    'same VPC (or a peered one) and a security group rule '
                    'allowing it in on that port. This does not block '
                    'deployment -- the pipeline tasks run inside your VPC and '
                    'reach it directly. Enter the table names manually instead.',
            'detail': text,
        }
    if 'access denied' in lowered or 'password authentication failed' in lowered \
            or 'authentication failed' in lowered:
        return {
            'stage': 'authenticate',
            'error': '{} rejected the credential for user {!r}'.format(label, user),
            'hint': 'Check that the secret holds this database\'s password. The '
                    'username came from the cluster\'s RDS admin user; a secret for a '
                    'different user needs its own "username" field.',
            'detail': text,
        }
    if 'unknown database' in lowered or 'does not exist' in lowered:
        return {
            'stage': 'connect',
            'error': 'the database named in the link does not exist on this server',
            'hint': 'Check source.database. RDS reports no default database name '
                    'for most Aurora clusters, so it has to be given explicitly.',
            'detail': text,
        }
    return {
        'stage': 'connect',
        'error': 'could not list tables: {}'.format(text),
        'hint': 'You can still deploy -- enter the table names manually.',
        'detail': text,
    }


# ── catalogue queries ──────────────────────────────────────────────────────
#
# Tuple cursors throughout, never dictionary cursors. MySQL 8 returns
# information_schema column names in UPPERCASE (`TABLE_NAME`), so dictionary
# access written as `row['table_name']` raises KeyError against a real server
# while looking perfectly reasonable. Positional access cannot drift that way.

_MYSQL_TABLES = """
SELECT TABLE_NAME, TABLE_ROWS
  FROM information_schema.TABLES
 WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
 ORDER BY TABLE_NAME
"""

_MYSQL_COLUMNS = """
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = %s
 ORDER BY TABLE_NAME, ORDINAL_POSITION
"""

_PG_TABLES = """
SELECT c.relname,
       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %s AND c.relkind = 'r'
 ORDER BY c.relname
"""

_PG_COLUMNS = """
SELECT table_name, column_name, data_type, is_nullable
  FROM information_schema.columns
 WHERE table_schema = %s
 ORDER BY table_name, ordinal_position
"""

_PG_PRIMARY_KEYS = """
SELECT tc.table_name, kcu.column_name
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
   AND tc.table_schema = kcu.table_schema
 WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s
"""

_PG_SCHEMAS = """
SELECT nspname FROM pg_namespace
 WHERE nspname NOT LIKE 'pg_%%' AND nspname <> 'information_schema'
 ORDER BY nspname
"""


def _rows(connection, sql, params=()):
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        cursor.close()


def _mysql_catalogue(connection, database):
    tables = {}
    for name, row_estimate in _rows(connection, _MYSQL_TABLES, (database,)):
        tables[name] = {'table': name, 'row_estimate': row_estimate,
                        'columns': [], 'primary_key': []}
    for name, column, data_type, nullable, key in _rows(
            connection, _MYSQL_COLUMNS, (database,)):
        entry = tables.get(name)
        if entry is None:
            continue
        entry['columns'].append({'name': column, 'type': data_type,
                                 'nullable': nullable == 'YES'})
        if key == 'PRI':
            entry['primary_key'].append(column)
    return tables


def _postgresql_catalogue(connection, schema):
    tables = {}
    for name, row_estimate in _rows(connection, _PG_TABLES, (schema,)):
        tables[name] = {'table': name, 'row_estimate': row_estimate,
                        'columns': [], 'primary_key': []}
    for name, column, data_type, nullable in _rows(
            connection, _PG_COLUMNS, (schema,)):
        entry = tables.get(name)
        if entry is None:
            continue
        entry['columns'].append({'name': column, 'type': data_type,
                                 'nullable': nullable == 'YES'})
    for name, column in _rows(connection, _PG_PRIMARY_KEYS, (schema,)):
        entry = tables.get(name)
        if entry is not None:
            entry['primary_key'].append(column)
    return tables


# ── capture prerequisites, checked while we are already connected ──────────
#
# These are the settings that decide whether change capture can work at all, and
# checking them here reports a problem on the form instead of in a deploy that
# fails twenty minutes later. Read-only; nothing is changed.

def _mysql_prerequisites(connection):
    checks = []
    try:
        row = _rows(connection,
                    'SELECT @@binlog_format, @@binlog_row_image, @@log_bin')[0]
        binlog_format, row_image, log_bin = row[0], row[1], row[2]
    except Exception:  # noqa: BLE001
        return [{'name': 'binary log', 'ok': None,
                 'detail': 'could not read the binary log settings'}]

    enabled = str(log_bin) in ('1', 'ON', 'True', 'true')
    checks.append({
        'name': 'binary logging',
        'ok': enabled,
        'detail': 'log_bin={}'.format(log_bin),
        'fix': None if enabled else
               'Enable binary logging. On Aurora MySQL set the cluster parameter '
               'binlog_format to ROW; on RDS MySQL set backup retention above 0.',
    })
    checks.append({
        'name': 'binlog_format is ROW',
        'ok': str(binlog_format).upper() == 'ROW',
        'detail': 'binlog_format={}'.format(binlog_format),
        'fix': None if str(binlog_format).upper() == 'ROW' else
               'Set binlog_format=ROW in the cluster parameter group. STATEMENT '
               'and MIXED do not carry the row values change capture needs.',
    })
    checks.append({
        'name': 'binlog_row_image is FULL',
        'ok': str(row_image).upper() == 'FULL',
        'detail': 'binlog_row_image={}'.format(row_image),
        'fix': None if str(row_image).upper() == 'FULL' else
               'Set binlog_row_image=FULL. MINIMAL omits unchanged columns, so '
               'cached rows would be written with missing fields.',
    })
    checks.append(_mysql_binlog_retention(connection))
    return checks


# How long the source must keep its change log for capture to survive a pause.
# The bulk load holds capture paused, and an outage or a failed deploy can leave
# it stopped; changes the connector has not read yet exist ONLY in the log, so a
# log purged before capture resumes is permanent loss with no error at the time.
MIN_LOG_RETENTION_HOURS = 24


def _mysql_binlog_retention(connection):
    """Binary log retention: RDS/Aurora's setting, else the server's own."""
    name = 'binary logs retained >= {}h'.format(MIN_LOG_RETENTION_HOURS)
    hours, origin = None, None
    try:
        for row in _rows(connection, 'CALL mysql.rds_show_configuration'):
            if str(row[0]).lower() == 'binlog retention hours':
                hours, origin = row[1], 'rds'
                break
    except Exception:  # noqa: BLE001
        pass   # not RDS/Aurora, or no permission: fall back to the server setting
    if origin is None:
        try:
            rows = _rows(connection, 'SELECT @@binlog_expire_logs_seconds')
        except Exception:  # noqa: BLE001
            rows = []
        if not rows:
            return {'name': name, 'ok': None,
                    'detail': 'could not read the binary log retention'}
        seconds = rows[0][0]
        hours, origin = (int(seconds) / 3600.0 if seconds else None), 'server'

    fix = ("CALL mysql.rds_set_configuration('binlog retention hours', {});"
           .format(MIN_LOG_RETENTION_HOURS) if origin == 'rds' else
           'SET PERSIST binlog_expire_logs_seconds = {};'.format(
               MIN_LOG_RETENTION_HOURS * 3600))
    if hours in (None, '', 'NULL'):
        return {'name': name, 'ok': False,
                'detail': 'binlog retention hours is NULL' if origin == 'rds'
                          else 'binlog_expire_logs_seconds is 0',
                'fix': ('{} With no retention, RDS and Aurora purge binary logs '
                        'as soon as nothing else needs them, so changes made while '
                        'capture is paused for the bulk load or stopped by an '
                        'outage can be gone before it resumes -- lost, with no '
                        'error at the time.'.format(fix))
                       if origin == 'rds' else
                       ('{} 0 can mean the logs are never expired (disk grows) on '
                        'MySQL 8 servers configured with expire_logs_days; check '
                        'both.'.format(fix))}
    hours = float(hours)
    ok = hours >= MIN_LOG_RETENTION_HOURS
    return {'name': name, 'ok': ok,
            'detail': 'binary logs retained {:g}h'.format(hours),
            'fix': None if ok else
                   ('{} Capture that is paused or down for longer than {:g}h '
                    'loses the changes from that window.'.format(fix, hours))}


def _postgresql_prerequisites(connection):
    checks = []
    try:
        wal_level = _rows(connection, "SELECT current_setting('wal_level')")[0][0]
    except Exception:  # noqa: BLE001
        return [{'name': 'wal_level', 'ok': None,
                 'detail': 'could not read wal_level'}]
    ok = str(wal_level).lower() == 'logical'
    checks.append({
        'name': 'wal_level is logical',
        'ok': ok,
        'detail': 'wal_level={}'.format(wal_level),
        'fix': None if ok else
               'Set rds.logical_replication=1 in the parameter group and reboot. '
               'Without it PostgreSQL emits no logical decoding stream.',
    })
    checks.extend(_postgresql_slot_checks(connection))
    return checks


def _postgresql_slot_checks(connection):
    """WAL retention for replication slots, and slots that no connector is reading.

    A slot makes PostgreSQL keep WAL until the slot's reader confirms it, which
    is what lets capture survive a pause. Two ways that goes wrong:
      * max_slot_wal_keep_size is finite: if capture falls further behind than
        that, PostgreSQL INVALIDATES the slot and the changes are lost.
      * a slot with no reader (a deleted link, a stopped connector) keeps WAL
        forever and eventually fills the disk.
    """
    checks = []
    try:
        keep = _rows(connection, "SELECT current_setting('max_slot_wal_keep_size')")
        keep = keep[0][0] if keep else None
    except Exception:  # noqa: BLE001
        keep = None    # PostgreSQL 12 and older have no such setting
    if keep is not None:
        unlimited = str(keep).strip() in ('-1', '-1MB')
        checks.append({
            'name': 'replication slots never invalidated',
            'ok': unlimited,
            'detail': 'max_slot_wal_keep_size={}'.format(keep),
            'fix': None if unlimited else
                   ('Set max_slot_wal_keep_size=-1 (or large enough for the '
                    'longest pause you expect). When capture falls behind by '
                    'more than {}, PostgreSQL invalidates the slot and the '
                    'changes it held are lost; the link then needs a full '
                    'reload.'.format(keep)),
        })
    try:
        idle = [r[0] for r in _rows(
            connection,
            "SELECT slot_name FROM pg_replication_slots "
            "WHERE NOT active AND slot_type = 'logical'")]
    except Exception:  # noqa: BLE001
        idle = None
    if idle is not None:
        checks.append({
            'name': 'no abandoned replication slots',
            'ok': not idle,
            'detail': ('inactive logical slots: ' + ', '.join(idle)) if idle
                      else 'no inactive logical slots',
            'fix': None if not idle else
                   ('An inactive slot keeps WAL until the disk fills. If no link '
                    'uses it any more: SELECT pg_drop_replication_slot(\'<name>\'); '
                    '(a slot is inactive briefly while its connector restarts, so '
                    'check before dropping)'),
        })
    return checks


# ── entry point ────────────────────────────────────────────────────────────

def list_tables(source_arn, secret_arn, database=None, schema=None, region=None,
                clients=None, driver=None):
    """Tables, columns, primary keys and capture prerequisites for a source.

    Always returns a dict. `listed` says whether the catalogue was read; when it
    is false, `error` and `hint` say why and what to do instead, because the
    fallback (type the names) is always available and the deployment does not
    depend on this working.
    """
    if clients is None:
        clients = Clients(region=region)

    source = describe_source(clients, source_arn)
    family = engine_family(source.get('engine_raw'))
    facts = ENGINE_FACTS[family]

    # Which name the catalogue is keyed on differs by engine, and the link schema
    # already carries both.
    if facts['qualify_with'] == 'database':
        namespace = database or source.get('database_name')
        namespace_kind = 'database'
    else:
        namespace = schema or facts['default_schema']
        namespace_kind = 'schema'

    base = {
        'listed': False,
        'engine': family,
        'engine_label': facts['label'],
        'qualify_with': facts['qualify_with'],
        'namespace': namespace,
        'namespace_kind': namespace_kind,
        'endpoint': source.get('endpoint'),
        'port': source.get('port'),
        'tables': [],
        'schemas': [],
        'prerequisites': [],
    }

    if driver is None:
        driver, package = _import_driver(family)
        if driver is None:
            # A supported engine we cannot inspect from here. Distinguished from
            # "unsupported engine" because the pipeline itself handles this
            # engine -- only this convenience is missing.
            return dict(base, stage='driver',
                        error='the control plane has no {} driver installed, so '
                              'it cannot list this database\'s tables'.format(
                                  facts['label']),
                        hint='Enter the table names manually. Deployment is '
                             'unaffected: the pipeline connects with its own '
                             'driver. To enable listing, add {} to '
                             'backend/requirements.txt.'.format(package))

    if not namespace:
        return dict(base, stage='input',
                    error='no {} to inspect'.format(namespace_kind),
                    hint='Set source.{} in the link. RDS reports no default '
                         'database name for most Aurora clusters, which is why '
                         'it has to be given.'.format(namespace_kind))

    username, password = read_credential(clients, secret_arn)
    # A secret holding only a password is the common shape, so the username falls
    # back to the admin user RDS reported. Resolution already surfaces that name.
    username = username or source.get('username')
    if not username:
        return dict(base, stage='input',
                    error='no database username is known',
                    hint='Either the secret needs a "username" field or '
                         'source.username must be set in the link.')

    connect_database = database or source.get('database_name')
    if family == 'postgresql' and not connect_database:
        # PostgreSQL has no concept of connecting without a database, and the
        # catalogue is per-database, so there is nothing sensible to guess.
        return dict(base, stage='input',
                    error='PostgreSQL needs a database name to connect to',
                    hint='Set source.database in the link.')

    connection = None
    try:
        connection = _connect(family, driver, source.get('endpoint'),
                              source.get('port'), username, password,
                              connect_database)
    except ResolutionError:
        raise
    except Exception as exc:  # noqa: BLE001
        return dict(base, **_classify(family, exc, source.get('endpoint'),
                                      source.get('port'), username, password))

    try:
        if family == 'mysql':
            tables = _mysql_catalogue(connection, namespace)
            prerequisites = _mysql_prerequisites(connection)
            schemas = []
        else:
            tables = _postgresql_catalogue(connection, namespace)
            prerequisites = _postgresql_prerequisites(connection)
            schemas = [row[0] for row in _rows(connection, _PG_SCHEMAS)]
    except Exception as exc:  # noqa: BLE001
        return dict(base, stage='query',
                    error='connected, but could not read the catalogue: {}'.format(
                        _scrub(exc, password)),
                    hint='The database user needs read access to the system '
                         'catalogue.')
    finally:
        # The password goes out of scope with the function; the connection must be
        # closed explicitly or a failed request leaks a session.
        try:
            if connection is not None:
                connection.close()
        except Exception:  # noqa: BLE001
            pass

    listed = []
    for entry in sorted(tables.values(), key=lambda t: t['table']):
        # A missing primary key is worth reporting now rather than discovering later:
        # the sink keys every cached row on the primary key, and the bulk loader
        # splits work by primary-key range. Neither has anything to use without
        # one.
        entry['capturable'] = bool(entry['primary_key'])
        if not entry['primary_key']:
            entry['warning'] = (
                'no primary key, so there is no key to cache each row under and '
                'no range for the initial load to split on')
        listed.append(entry)

    return dict(base, listed=True, tables=listed, schemas=schemas,
                prerequisites=prerequisites,
                count=len(listed))
