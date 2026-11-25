"""Tests for tables.py — catalogue listing, and the credential handling around it.

This module is the only one in the control plane that holds a database password,
so many of these tests check that the password is not exposed anywhere: not
into the response, not into an error message, not into a leaked connection.

Fake cursors return TUPLES, matching the positional access the queries use. That
is deliberate rather than incidental: MySQL 8 returns information_schema column
names in UPPERCASE, so a dictionary cursor plus `row['table_name']` raises
KeyError against a real server while looking correct in code review.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.resolve import ResolutionError  # noqa: E402
from ecdl_link.tables import (  # noqa: E402
    CONNECT_TIMEOUT_SEC,
    list_tables,
    read_credential,
)

PASSWORD = 'sup3rs3cr3t-do-not-leak'  # nosec B105 - fake test credential

CLUSTER = {
    'DBClusterArn': 'arn:aws:rds:us-east-2:000000000001:cluster:shop',
    'DBClusterIdentifier': 'shop',
    'Engine': 'aurora-mysql',
    'EngineMode': 'provisioned',
    'EngineVersion': '8.0.mysql_aurora.3.08.0',
    'Endpoint': 'shop.cluster-x.us-east-2.rds.amazonaws.com',
    'Port': 3306,
    'MasterUsername': 'admin',
    'Status': 'available',
    'DBSubnetGroup': 'shop-subnets',
}

PG_CLUSTER = dict(CLUSTER, Engine='aurora-postgresql', Port=5432,
                  MasterUsername='postgres')

SOURCE_ARN = CLUSTER['DBClusterArn']
SECRET_ARN = 'arn:aws:secretsmanager:us-east-2:000000000001:secret:shop-password'  # nosec B105 - fake test credential


class FakeRds(object):
    def __init__(self, cluster=CLUSTER):
        self._cluster = cluster

    def describe_db_clusters(self, **kwargs):
        return {'DBClusters': [self._cluster]}

    def describe_db_subnet_groups(self, **kwargs):
        return {'DBSubnetGroups': [{'VpcId': 'vpc-1', 'Subnets': []}]}


class FakeSecrets(object):
    def __init__(self, value=None, error=None):
        self._value = value
        self._error = error
        self.reads = 0

    def get_secret_value(self, **kwargs):
        self.reads += 1
        if self._error:
            raise self._error
        return {'SecretString': self._value}


class Clients(object):
    def __init__(self, rds=None, secretsmanager=None):
        self.rds = rds or FakeRds()
        self.elasticache = None
        self.secretsmanager = secretsmanager or FakeSecrets(PASSWORD)
        self.ec2 = None


class FakeCursor(object):
    def __init__(self, responder, log):
        self._responder = responder
        self._log = log
        self._rows = []
        self.closed = False

    def execute(self, sql, params=()):
        self._log.append((' '.join(sql.split()), params))
        self._rows = self._responder(sql, params)

    def fetchall(self):
        return self._rows

    def close(self):
        self.closed = True


class FakeConnection(object):
    def __init__(self, responder):
        self._responder = responder
        self.queries = []
        self.closed = False
        self.cursors = []

    def cursor(self):
        cursor = FakeCursor(self._responder, self.queries)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


class FakeDriver(object):
    """Stands in for pymysql/psycopg2. Records what it was asked to connect with."""

    def __init__(self, responder=None, raises=None):
        self._responder = responder or (lambda sql, params: [])
        self._raises = raises
        self.connect_kwargs = None
        self.connection = None

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self._raises:
            raise self._raises
        self.connection = FakeConnection(self._responder)
        return self.connection


def mysql_responder(tables=None, columns=None, settings=None):
    tables = tables if tables is not None else [('orders', 1200), ('items', 340)]
    columns = columns if columns is not None else [
        ('orders', 'id', 'bigint', 'NO', 'PRI'),
        ('orders', 'status', 'varchar', 'YES', ''),
        ('orders', 'amount', 'decimal', 'YES', ''),
        ('items', 'id', 'bigint', 'NO', 'PRI'),
        ('items', 'order_id', 'bigint', 'NO', 'MUL'),
    ]
    settings = settings or [('ROW', 'FULL', 'ON')]

    def respond(sql, params):
        if 'information_schema.TABLES' in sql:
            return tables
        if 'information_schema.COLUMNS' in sql:
            return columns
        if '@@binlog_format' in sql:
            return settings
        return []
    return respond


def pg_responder(tables=None, columns=None, keys=None, wal_level='logical',
                 identity=None, toastable=None):
    tables = tables if tables is not None else [('orders', 900)]
    columns = columns if columns is not None else [
        ('orders', 'id', 'bigint', 'NO'),
        ('orders', 'status', 'text', 'YES'),
    ]
    keys = keys if keys is not None else [('orders', 'id')]
    # (relname, relreplident, may INSERT); by default every table is DEFAULT.
    identity = identity if identity is not None else [
        (name, 'd', False) for name, _estimate in tables]
    toastable = toastable if toastable is not None else []

    def respond(sql, params):
        # Before the pg_class match below: these two queries join pg_class too.
        if 'relreplident' in sql:
            return identity
        if 'attstorage' in sql:
            return toastable
        if 'pg_class' in sql:
            return tables
        if 'information_schema.columns' in sql:
            return columns
        if 'table_constraints' in sql:
            return keys
        if 'wal_level' in sql:
            return [(wal_level,)]
        if 'pg_namespace' in sql:
            return [('public',), ('shop',)]
        return []
    return respond


# ── credential handling ────────────────────────────────────────────────────

class CredentialTest(unittest.TestCase):

    def test_plaintext_secret_is_a_password_with_no_username(self):
        clients = Clients(secretsmanager=FakeSecrets(PASSWORD))
        self.assertEqual(read_credential(clients, SECRET_ARN),
                         (None, PASSWORD))

    def test_json_secret_yields_username_and_password(self):
        clients = Clients(secretsmanager=FakeSecrets(
            json.dumps({'username': 'svc', 'password': PASSWORD})))
        self.assertEqual(read_credential(clients, SECRET_ARN), ('svc', PASSWORD))

    def test_json_without_a_password_is_refused_by_name(self):
        clients = Clients(secretsmanager=FakeSecrets(
            json.dumps({'username': 'svc'})))
        with self.assertRaises(ResolutionError) as caught:
            read_credential(clients, SECRET_ARN)
        self.assertIn('no "password" field', str(caught.exception))

    def test_password_starting_with_a_brace_stays_a_password(self):
        # Same judgement the Go sink makes about auth tokens: it only counts as
        # JSON if it actually parses.
        weird = '{not really json'
        clients = Clients(secretsmanager=FakeSecrets(weird))
        self.assertEqual(read_credential(clients, SECRET_ARN), (None, weird))

    def test_binary_secret_is_refused(self):
        clients = Clients(secretsmanager=FakeSecrets(None))
        with self.assertRaises(ResolutionError) as caught:
            read_credential(clients, SECRET_ARN)
        self.assertIn('binary', str(caught.exception))

    def test_access_denied_explains_the_fallback(self):
        clients = Clients(secretsmanager=FakeSecrets(
            error=Exception('User is not authorized (AccessDenied)')))
        with self.assertRaises(ResolutionError) as caught:
            read_credential(clients, SECRET_ARN)
        message = str(caught.exception)
        self.assertIn('GetSecretValue', message)
        self.assertIn('manually', message)


# ── MySQL listing ──────────────────────────────────────────────────────────

class MysqlListingTest(unittest.TestCase):

    def run_list(self, **kwargs):
        driver = kwargs.pop('driver', None) or FakeDriver(mysql_responder())
        clients = kwargs.pop('clients', None) or Clients()
        return list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                           clients=clients, driver=driver, **kwargs), driver

    def test_lists_tables_with_columns_and_primary_keys(self):
        result, _ = self.run_list()
        self.assertTrue(result['listed'])
        self.assertEqual(result['count'], 2)
        by_name = {t['table']: t for t in result['tables']}
        self.assertEqual(sorted(by_name), ['items', 'orders'])
        self.assertEqual(by_name['orders']['primary_key'], ['id'])
        self.assertEqual([c['name'] for c in by_name['orders']['columns']],
                         ['id', 'status', 'amount'])
        self.assertTrue(by_name['orders']['capturable'])

    def test_column_types_and_nullability_come_through(self):
        result, _ = self.run_list()
        orders = {t['table']: t for t in result['tables']}['orders']
        status = [c for c in orders['columns'] if c['name'] == 'status'][0]
        self.assertEqual(status['type'], 'varchar')
        self.assertTrue(status['nullable'])
        ident = [c for c in orders['columns'] if c['name'] == 'id'][0]
        self.assertFalse(ident['nullable'])

    def test_tables_are_sorted(self):
        result, _ = self.run_list()
        names = [t['table'] for t in result['tables']]
        self.assertEqual(names, sorted(names))

    def test_row_estimate_is_reported(self):
        result, _ = self.run_list()
        orders = {t['table']: t for t in result['tables']}['orders']
        self.assertEqual(orders['row_estimate'], 1200)

    def test_a_table_with_no_primary_key_is_flagged_not_hidden(self):
        driver = FakeDriver(mysql_responder(
            tables=[('audit', 10)],
            columns=[('audit', 'note', 'text', 'YES', '')]))
        result, _ = self.run_list(driver=driver)
        audit = result['tables'][0]
        self.assertFalse(audit['capturable'])
        self.assertIn('no primary key', audit['warning'])
        self.assertIn('initial load', audit['warning'])

    def test_the_database_is_the_catalogue_namespace_for_mysql(self):
        result, driver = self.run_list()
        self.assertEqual(result['namespace'], 'Inventory')
        self.assertEqual(result['namespace_kind'], 'database')
        self.assertEqual(result['qualify_with'], 'database')
        # And the query was scoped to it rather than listing the whole server.
        table_query = [q for q in driver.connection.queries
                       if 'information_schema.TABLES' in q[0]][0]
        self.assertEqual(table_query[1], ('Inventory',))

    def test_username_falls_back_to_the_rds_master_user(self):
        result, driver = self.run_list()
        self.assertEqual(driver.connect_kwargs['user'], 'admin')

    def test_username_in_the_secret_wins(self):
        clients = Clients(secretsmanager=FakeSecrets(
            json.dumps({'username': 'svc', 'password': PASSWORD})))
        _, driver = self.run_list(clients=clients)
        self.assertEqual(driver.connect_kwargs['user'], 'svc')

    def test_connect_uses_a_short_timeout(self):
        # The common failure is no network path, which manifests as a hang.
        _, driver = self.run_list()
        self.assertEqual(driver.connect_kwargs['connect_timeout'],
                         CONNECT_TIMEOUT_SEC)

    def test_the_connection_is_closed(self):
        _, driver = self.run_list()
        self.assertTrue(driver.connection.closed)

    def test_the_connection_is_closed_even_when_the_query_fails(self):
        def fail_query(sql, params):
            raise RuntimeError('catalogue unavailable')
        driver = FakeDriver(fail_query)
        result, _ = self.run_list(driver=driver)
        self.assertFalse(result['listed'])
        self.assertEqual(result['stage'], 'query')
        self.assertTrue(driver.connection.closed)

    def test_cursors_are_closed(self):
        _, driver = self.run_list()
        self.assertTrue(all(c.closed for c in driver.connection.cursors))


# ── PostgreSQL listing ─────────────────────────────────────────────────────

class PostgresListingTest(unittest.TestCase):

    def run_list(self, responder=None, schema=None):
        driver = FakeDriver(responder or pg_responder())
        clients = Clients(rds=FakeRds(PG_CLUSTER))
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='shop',
                             schema=schema, clients=clients, driver=driver)
        return result, driver

    def test_lists_tables_with_a_separate_primary_key_query(self):
        result, _ = self.run_list()
        self.assertTrue(result['listed'])
        orders = result['tables'][0]
        self.assertEqual(orders['table'], 'orders')
        self.assertEqual(orders['primary_key'], ['id'])
        self.assertTrue(orders['capturable'])

    def test_defaults_to_the_public_schema(self):
        result, _ = self.run_list()
        self.assertEqual(result['namespace'], 'public')
        self.assertEqual(result['namespace_kind'], 'schema')

    def test_an_explicit_schema_wins(self):
        result, driver = self.run_list(schema='shop')
        self.assertEqual(result['namespace'], 'shop')
        table_query = [q for q in driver.connection.queries if 'pg_class' in q[0]][0]
        self.assertEqual(table_query[1], ('shop',))

    def test_available_schemas_are_offered(self):
        result, _ = self.run_list()
        self.assertEqual(result['schemas'], ['public', 'shop'])

    def test_postgresql_without_a_database_is_refused_early(self):
        # A PostgreSQL connection requires a database, and the
        # catalogue is per-database, so there is nothing sensible to guess.
        driver = FakeDriver(pg_responder())
        result = list_tables(SOURCE_ARN, SECRET_ARN, database=None,
                             clients=Clients(rds=FakeRds(PG_CLUSTER)),
                             driver=driver)
        self.assertFalse(result['listed'])
        self.assertEqual(result['stage'], 'input')
        self.assertIsNone(driver.connect_kwargs)

    def test_negative_reltuples_becomes_none(self):
        # PostgreSQL reports -1 for a table that has never been analysed.
        result, _ = self.run_list(pg_responder(tables=[('fresh', None)],
                                               columns=[], keys=[]))
        self.assertIsNone(result['tables'][0]['row_estimate'])


# ── capture prerequisites ──────────────────────────────────────────────────

class M8ChecksTest(unittest.TestCase):
    """Review finding M8: replication grants, PARTIAL_JSON, free slots and WAL
    senders, partitioned tables and what a publication publishes."""
    def mysql(self, extra):
        base = mysql_responder(settings=[('ROW', 'FULL', 'ON')])

        def respond(sql, params):
            for needle, rows in extra.items():
                if needle in sql:
                    return rows
            return base(sql, params)
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                             clients=Clients(), driver=FakeDriver(respond))
        return {c['name']: c for c in result['prerequisites']}

    def test_mysql_partial_json_and_missing_grants_block(self):
        checks = self.mysql({'binlog_row_value_options': [('PARTIAL_JSON',)],
                             'SHOW GRANTS': [("GRANT SELECT ON `Inventory`.* TO `cdc`@`%`",)]})
        self.assertIs(checks['binlog_row_value_options is empty']['ok'], False)
        self.assertTrue(checks['binlog_row_value_options is empty']['block'])
        self.assertIs(checks['the user may read the binlog']['ok'], False)
        self.assertIn('REPLICATION SLAVE', checks['the user may read the binlog']['fix'])

    def test_mysql_good_settings_pass(self):
        checks = self.mysql({'binlog_row_value_options': [('',)],
                             'SHOW GRANTS': [('GRANT SELECT, REPLICATION SLAVE, REPLICATION CLIENT '
                                              'ON *.* TO `cdc`@`%`',)]})
        self.assertTrue(checks['binlog_row_value_options is empty']['ok'])
        self.assertTrue(checks['the user may read the binlog']['ok'])

    def pg(self, extra):
        base = pg_responder()

        def respond(sql, params):
            for needle, rows in extra.items():
                if needle in sql:
                    return rows
            return base(sql, params)
        return list_tables(SOURCE_ARN, SECRET_ARN, database='shop',
                           clients=Clients(rds=FakeRds(PG_CLUSTER)), driver=FakeDriver(respond))

    def test_pg_role_and_capacity(self):
        result = self.pg({'rolreplication': [(False,)],
                          'max_replication_slots': [(10, 10, 10, 3)]})
        checks = {c['name']: c for c in result['prerequisites']}
        self.assertIs(checks['the user may replicate']['ok'], False)
        self.assertIs(checks['a replication slot is free']['ok'], False)
        self.assertTrue(checks['a WAL sender is free']['ok'])

    def test_pg_partitioned_tables_are_listed_and_publication_options_read(self):
        result = self.pg({"relkind IN ('r', 'p')": [('orders', 100, 'p')],
                          'pubviaroot': [('ecdl_x_pub', True, True, False, False)]})
        orders = [t for t in result['tables'] if t['table'] == 'orders']
        self.assertTrue(orders and orders[0].get('partitioned'), result['tables'])
        self.assertEqual(result['publication_options']['ecdl_x_pub'],
                         {'insert': True, 'update': True, 'delete': False, 'via_root': False})


class PrerequisiteTest(unittest.TestCase):

    def check(self, settings):
        driver = FakeDriver(mysql_responder(settings=settings))
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                             clients=Clients(), driver=driver)
        return {c['name']: c for c in result['prerequisites']}

    def test_row_format_full_image_passes(self):
        checks = self.check([('ROW', 'FULL', 'ON')])
        for name in ('binary logging', 'binlog_format is ROW',
                     'binlog_row_image is FULL'):
            self.assertTrue(checks[name]['ok'], name)

    def _retention(self, rds_rows=None, expire=None, rds_error=False):
        def respond(sql, params):
            if 'rds_show_configuration' in sql:
                if rds_error:
                    raise RuntimeError('PROCEDURE does not exist')
                return rds_rows or []
            if 'binlog_expire_logs_seconds' in sql:
                return [] if expire is None else [(expire,)]
            return mysql_responder()(sql, params)
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                             clients=Clients(), driver=FakeDriver(respond))
        return [c for c in result['prerequisites']
                if c['name'].startswith('binary logs retained')][0]

    def test_rds_null_retention_fails_and_names_the_loss(self):
        entry = self._retention(rds_rows=[('binlog retention hours', None, '')])
        self.assertFalse(entry['ok'])
        self.assertIn("rds_set_configuration('binlog retention hours', 24)",
                      entry['fix'])
        self.assertIn('lost', entry['fix'])

    def test_rds_short_retention_fails_and_enough_passes(self):
        self.assertFalse(self._retention(
            rds_rows=[('binlog retention hours', '6', '')])['ok'])
        self.assertTrue(self._retention(
            rds_rows=[('binlog retention hours', '72', '')])['ok'])

    def test_self_managed_mysql_uses_the_server_setting(self):
        entry = self._retention(rds_error=True, expire=2592000)
        self.assertTrue(entry['ok'])
        self.assertIn('720h', entry['detail'])

    def test_unreadable_retention_is_unknown(self):
        self.assertIsNone(self._retention(rds_error=True)['ok'])
        self.assertIsNone(self._retention(rds_error=True)['retention_hours'])

    def test_retention_hours_are_reported_for_the_pause_check(self):  # Jira story F056
        entry = self._retention(rds_rows=[('binlog retention hours', '6', '')])
        self.assertEqual((entry['key'], entry['retention_hours'], entry['origin']),
                         ('log_retention', 6.0, 'rds'))
        self.assertEqual(self._retention(rds_error=True, expire=7200)['retention_hours'], 2.0)
        # RDS NULL purges at once; a server 0 may mean never.
        self.assertEqual(self._retention(
            rds_rows=[('binlog retention hours', None, '')])['retention_hours'], 0.0)
        self.assertIsNone(self._retention(rds_error=True, expire=0)['retention_hours'])

    def _pg(self, keep=None, idle=()):
        base = pg_responder()

        def respond(sql, params):
            if 'max_slot_wal_keep_size' in sql:
                return [] if keep is None else [(keep,)]
            if 'pg_replication_slots' in sql:
                return [(s,) for s in idle]
            return base(sql, params)
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='shop',
                             clients=Clients(rds=FakeRds(PG_CLUSTER)),
                             driver=FakeDriver(respond))
        return {c['name']: c for c in result['prerequisites']}

    def test_postgres_finite_slot_wal_limit_warns(self):
        checks = self._pg(keep='10GB')
        self.assertFalse(checks['replication slots never invalidated']['ok'])
        self.assertEqual(checks['replication slots never invalidated']['slot_wal_keep_size'],
                         '10GB')
        self.assertIsNone(self._pg(keep='-1')['replication slots never invalidated']
                          ['slot_wal_keep_size'])
        self.assertTrue(self._pg(keep='-1')['replication slots never invalidated']['ok'])

    def test_postgres_abandoned_slot_is_named(self):
        entry = self._pg(idle=['ecdl_old'])['no abandoned replication slots']
        self.assertFalse(entry['ok'])
        self.assertIn('ecdl_old', entry['detail'])

    def test_statement_format_fails_with_the_fix(self):
        checks = self.check([('STATEMENT', 'FULL', 'ON')])
        entry = checks['binlog_format is ROW']
        self.assertFalse(entry['ok'])
        self.assertIn('binlog_format=ROW', entry['fix'])

    def test_minimal_row_image_fails_and_says_why_it_matters(self):
        checks = self.check([('ROW', 'MINIMAL', 'ON')])
        entry = checks['binlog_row_image is FULL']
        self.assertFalse(entry['ok'])
        self.assertIn('missing fields', entry['fix'])

    def test_binary_logging_off_fails(self):
        checks = self.check([('ROW', 'FULL', 'OFF')])
        self.assertFalse(checks['binary logging']['ok'])

    def _gtid(self, rows=None, error=False):
        def respond(sql, params):
            if '@@gtid_mode' in sql:
                if error:
                    raise RuntimeError("Unknown system variable 'gtid_mode'")
                return rows or []
            return mysql_responder()(sql, params)
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                             clients=Clients(), driver=FakeDriver(respond))
        return {c['name']: c for c in result['prerequisites']}['GTID mode is ON']

    def test_gtid_on_passes(self):
        entry = self._gtid([('ON', 'ON')])
        self.assertTrue(entry['ok'])
        self.assertIsNone(entry['fix'])

    def test_gtid_off_fails_as_a_block_with_the_fix(self):
        # Jira story F008: without GTIDs a change of writer resumes at meaningless
        # binlog coordinates.
        entry = self._gtid([('OFF', 'OFF')])
        self.assertFalse(entry['ok'])
        self.assertTrue(entry['block'])
        self.assertIn('gtid_mode=OFF', entry['detail'])
        self.assertIn('gtid-mode=ON', entry['fix'])
        self.assertIn('enforce_gtid_consistency=ON', entry['fix'])
        self.assertIn('blue/green', entry['fix'])

    def test_gtid_half_on_fails(self):
        for mode, enforce in (('ON_PERMISSIVE', 'ON'), ('OFF_PERMISSIVE', 'ON'),
                              ('ON', 'OFF'), ('ON', 'WARN')):
            self.assertFalse(self._gtid([(mode, enforce)])['ok'], (mode, enforce))

    def test_gtid_enforce_as_a_number_passes(self):
        self.assertTrue(self._gtid([('ON', 1)])['ok'])

    def test_gtid_unreadable_is_unknown_not_a_block(self):
        # MariaDB has no gtid_mode; a user without access to it either.
        self.assertIsNone(self._gtid(error=True)['ok'])
        self.assertIsNone(self._gtid([])['ok'])

    def test_unreadable_settings_report_unknown_not_false(self):
        # Reporting a failed check as a failing prerequisite would misreport
        # the database.
        def respond(sql, params):
            if '@@binlog_format' in sql:
                raise RuntimeError('denied')
            return mysql_responder()(sql, params)
        driver = FakeDriver(respond)
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                             clients=Clients(), driver=driver)
        self.assertIsNone(result['prerequisites'][0]['ok'])

    def test_postgres_logical_wal_passes(self):
        driver = FakeDriver(pg_responder(wal_level='logical'))
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='shop',
                             clients=Clients(rds=FakeRds(PG_CLUSTER)),
                             driver=driver)
        self.assertTrue(result['prerequisites'][0]['ok'])

    def test_postgres_replica_wal_fails_with_the_fix(self):
        driver = FakeDriver(pg_responder(wal_level='replica'))
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='shop',
                             clients=Clients(rds=FakeRds(PG_CLUSTER)),
                             driver=driver)
        entry = result['prerequisites'][0]
        self.assertFalse(entry['ok'])
        self.assertIn('rds.logical_replication', entry['fix'])


class PostgresCaptureDetailTest(unittest.TestCase):
    """REPLICA IDENTITY, TOAST-able columns and INSERT rights per table (Jira story F035)."""

    def run_list(self, responder, driver=None):
        driver = driver or FakeDriver(responder)
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='shop',
                             clients=Clients(rds=FakeRds(PG_CLUSTER)),
                             driver=driver)
        return result, driver

    def test_identity_and_toastable_columns_are_attached(self):
        result, driver = self.run_list(pg_responder(
            tables=[('orders', 1), ('docs', 2)], keys=[('orders', 'id')],
            identity=[('orders', 'f', False), ('docs', 'd', True)],
            toastable=[('docs', 'body'), ('docs', 'img')]))
        tables = {t['table']: t for t in result['tables']}
        self.assertEqual(tables['orders']['replica_identity'], 'f')
        self.assertEqual(tables['docs']['replica_identity'], 'd')
        self.assertTrue(tables['docs']['insertable'])
        self.assertFalse(tables['orders']['insertable'])
        self.assertEqual(tables['docs']['toastable_columns'], ['body', 'img'])
        self.assertEqual(tables['orders']['toastable_columns'], [])
        # Both queries are scoped to the schema, by bind value.
        detail = [q for q in driver.connection.queries
                  if 'relreplident' in q[0] or 'attstorage' in q[0]]
        self.assertEqual([q[1] for q in detail], [('public',), ('public',)])

    def test_toast_query_excludes_bounded_and_plain_columns(self):
        from ecdl_link.tables import _PG_TOASTABLE
        self.assertIn("attstorage IN ('x', 'e')", _PG_TOASTABLE)
        self.assertIn('atttypmod - 4 > 2000', _PG_TOASTABLE)
        self.assertIn('NOT a.attisdropped', _PG_TOASTABLE)

    def test_an_unreadable_identity_leaves_the_fields_absent(self):
        base = pg_responder()

        def respond(sql, params):
            if 'relreplident' in sql:
                raise RuntimeError('permission denied for pg_class')
            return base(sql, params)
        result, _ = self.run_list(respond)
        self.assertTrue(result['listed'])
        self.assertNotIn('replica_identity', result['tables'][0])
        self.assertNotIn('toastable_columns', result['tables'][0])

    def test_an_unreadable_toast_query_rolls_back_and_keeps_identity(self):
        base = pg_responder()
        rolled = []

        def respond(sql, params):
            if 'attstorage' in sql:
                raise RuntimeError('boom')
            return base(sql, params)

        class RollbackConnection(FakeConnection):
            def rollback(self):
                rolled.append(1)

        class Driver(FakeDriver):
            def connect(self, **kwargs):
                self.connection = RollbackConnection(self._responder)
                return self.connection
        result, _ = self.run_list(None, driver=Driver(respond))
        entry = result['tables'][0]
        self.assertTrue(result['listed'])
        self.assertEqual(entry['replica_identity'], 'd')
        self.assertNotIn('toastable_columns', entry)
        # At least the TOAST query's; later checks the fake cannot answer roll
        # back too.
        self.assertTrue(rolled)


class PrerequisiteWarningTest(unittest.TestCase):
    def test_a_failed_blocking_prerequisite_is_a_block(self):
        from ecdl_link.preflight import prerequisite_warnings
        out = prerequisite_warnings({'prerequisites': [
            {'name': 'GTID mode is ON', 'ok': False, 'block': True,
             'detail': 'gtid_mode=OFF', 'fix': 'Set gtid-mode=ON'},
            {'name': 'GTID mode is ON', 'ok': None, 'block': True, 'detail': '?'},
            {'name': 'GTID mode is ON', 'ok': True, 'block': True, 'detail': 'ON'}]})
        self.assertEqual([f.level for f in out], ['block'])
        self.assertIn('Set gtid-mode=ON', out[0].message)
        self.assertEqual(out[0].path, 'source.prerequisites')

    def test_failed_prerequisites_become_deploy_warnings(self):
        from ecdl_link.preflight import prerequisite_warnings
        out = prerequisite_warnings({'prerequisites': [
            {'name': 'binary logs retained >= 24h', 'ok': False,
             'detail': 'binlog retention hours is NULL', 'fix': 'CALL x;'},
            {'name': 'binlog_format is ROW', 'ok': True, 'detail': 'ROW'},
            {'name': 'unknown', 'ok': None, 'detail': '?'}]})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].level, 'warn')
        self.assertIn('CALL x;', out[0].message)


# ── failure handling ───────────────────────────────────────────────────────

class DegradationTest(unittest.TestCase):

    def test_a_timeout_names_the_vpc_and_the_fallback(self):
        # The single most likely outcome: the control plane is not in the
        # customer's VPC. On its own "timed out" reads as "the database is down".
        driver = FakeDriver(raises=Exception(
            "(2003, \"Can't connect to MySQL server on 'shop' (timed out)\")"))
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                             clients=Clients(), driver=driver)
        self.assertFalse(result['listed'])
        self.assertEqual(result['stage'], 'connect')
        self.assertIn('security group', result['hint'])
        self.assertIn('manually', result['hint'])
        self.assertIn('does not block deployment', result['hint'].lower())

    def test_bad_credential_is_an_authentication_stage(self):
        driver = FakeDriver(raises=Exception(
            "(1045, \"Access denied for user 'admin'\")"))
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                             clients=Clients(), driver=driver)
        self.assertEqual(result['stage'], 'authenticate')
        self.assertIn('rejected the credential', result['error'])

    def test_missing_database_points_at_the_config_field(self):
        driver = FakeDriver(raises=Exception("Unknown database 'nope'"))
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='nope',
                             clients=Clients(), driver=driver)
        self.assertIn('source.database', result['hint'])

    def test_missing_driver_is_reported_as_a_missing_convenience(self):
        # Distinct from an unsupported engine: the pipeline handles SQL Server
        # fine, only this listing cannot be done from here.
        clients = Clients(rds=FakeRds(dict(CLUSTER, Engine='sqlserver-se')))
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                             clients=clients)
        self.assertFalse(result['listed'])
        self.assertEqual(result['stage'], 'driver')
        self.assertIn('pymssql', result['hint'])
        self.assertIn('Deployment is unaffected', result['hint'])

    def test_no_namespace_is_refused_before_connecting(self):
        cluster = dict(CLUSTER)
        cluster.pop('DBSubnetGroup', None)
        clients = Clients(rds=FakeRds(cluster))
        driver = FakeDriver(mysql_responder())
        result = list_tables(SOURCE_ARN, SECRET_ARN, database=None,
                             clients=clients, driver=driver)
        self.assertFalse(result['listed'])
        self.assertEqual(result['stage'], 'input')
        self.assertIsNone(driver.connect_kwargs)


class SecrecyTest(unittest.TestCase):
    """The password must not appear anywhere the caller can see."""

    def test_password_is_absent_from_a_successful_response(self):
        driver = FakeDriver(mysql_responder())
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                             clients=Clients(), driver=driver)
        self.assertNotIn(PASSWORD, json.dumps(result))

    def test_password_is_scrubbed_from_a_driver_error_that_echoes_it(self):
        # Some drivers include the parameters they were given. This is the only
        # code path where those parameters contain a password.
        driver = FakeDriver(raises=Exception(
            'connection failed: host=shop user=admin password={}'.format(PASSWORD)))
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                             clients=Clients(), driver=driver)
        blob = json.dumps(result)
        self.assertNotIn(PASSWORD, blob)
        self.assertIn('********', blob)

    def test_password_is_scrubbed_from_a_query_error(self):
        def fail_query(sql, params):
            raise RuntimeError('failed with password={}'.format(PASSWORD))
        driver = FakeDriver(fail_query)
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                             clients=Clients(), driver=driver)
        self.assertNotIn(PASSWORD, json.dumps(result))

    def test_the_secret_is_read_once_not_per_query(self):
        secrets = FakeSecrets(PASSWORD)
        driver = FakeDriver(mysql_responder())
        list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                    clients=Clients(secretsmanager=secrets), driver=driver)
        self.assertEqual(secrets.reads, 1)

    def test_no_secret_is_read_when_the_driver_is_missing(self):
        # Nothing should touch a credential it has no use for.
        secrets = FakeSecrets(PASSWORD)
        clients = Clients(rds=FakeRds(dict(CLUSTER, Engine='oracle-ee')),
                          secretsmanager=secrets)
        list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory', clients=clients)
        self.assertEqual(secrets.reads, 0)


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=0).result
    print('ok: {} table-listing tests passed'.format(result.testsRun)
          if result.wasSuccessful() else 'FAILED')
    raise SystemExit(0 if result.wasSuccessful() else 1)


class SourceTLSTest(unittest.TestCase):
    """Jira story F078: listing tables connects over verified TLS (RDS CA bundle)."""

    def test_mysql_verifies_the_certificate_and_host_name(self):
        import ssl
        driver = FakeDriver(mysql_responder())
        list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory', clients=Clients(),
                    driver=driver)
        ctx = driver.connect_kwargs['ssl']
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertGreaterEqual(len(ctx.get_ca_certs()), 100)

    def test_postgresql_is_verify_full_against_the_rds_bundle(self):
        from ecdl_link.sourcetls import RDS_CA_BUNDLE
        driver = FakeDriver(pg_responder())
        list_tables(SOURCE_ARN, SECRET_ARN, database='shop',
                    clients=Clients(rds=FakeRds(PG_CLUSTER)), driver=driver)
        self.assertEqual(driver.connect_kwargs['sslmode'], 'verify-full')
        self.assertEqual(driver.connect_kwargs['sslrootcert'], RDS_CA_BUNDLE)
        self.assertTrue(os.path.isfile(RDS_CA_BUNDLE))

    def test_a_certificate_failure_says_so(self):
        driver = FakeDriver(raises=Exception(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
        result = list_tables(SOURCE_ARN, SECRET_ARN, database='Inventory',
                             clients=Clients(), driver=driver)
        self.assertFalse(result['listed'])
        self.assertIn('TLS certificate', result['error'])
        self.assertIn('manually', result['hint'])

    def test_the_bundle_is_the_loaders(self):
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(os.path.dirname(here))
        with open(os.path.join(root, 'ecdl_link', 'rds-ca-bundle.pem'), 'rb') as a, \
                open(os.path.join(root, 'data-plane', 'bulk-loader', 'rds-ca-bundle.pem'),
                     'rb') as b:
            self.assertEqual(a.read(), b.read())
