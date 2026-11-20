"""Tests for day-2 signalling. Stdlib only, no AWS, no database."""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.signals import (  # noqa: E402
    SignalError, describe_signal, insert_statement, log_signal,
    pause_snapshot_signal, resume_snapshot_signal, signal_collection,
    signal_table_ddl, signal_table_name, snapshot_signal, stop_snapshot_signal,
    topic_pattern,
)


class TableNameTest(unittest.TestCase):
    def test_named_per_link(self):
        # Debezium requires the signalling collection to be unique to a connector
        # instance: two links sharing one would read each other's signals and
        # snapshot each other's tables.
        self.assertNotEqual(signal_table_name('checkout'),
                            signal_table_name('catalog'))

    def test_link_name_normalised_to_an_identifier(self):
        # A link may be called "checkout-cache"; a table may not, unquoted.
        name = signal_table_name('Checkout-Cache 1')
        self.assertEqual(name, 'ecdl_signal_checkout_cache_1')
        self.assertTrue(re.match(r'^[a-z0-9_]+$', name), name)


class CollectionQualificationTest(unittest.TestCase):
    """Qualified by exactly the rule each engine uses for table.include.list.

    Getting this wrong is silent: the connector starts normally, never finds the
    table, and every signal is permanently ignored.
    """

    def test_mysql_qualifies_by_database(self):
        self.assertEqual(
            signal_collection('mysql', 'checkout', database='Inventory'),
            'Inventory.ecdl_signal_checkout')

    def test_postgresql_qualifies_by_schema(self):
        self.assertEqual(
            signal_collection('postgresql', 'checkout', database='shop'),
            'public.ecdl_signal_checkout')

    def test_sqlserver_defaults_to_dbo(self):
        self.assertEqual(
            signal_collection('sqlserver', 'checkout', database='Inventory'),
            'dbo.ecdl_signal_checkout')

    def test_oracle_is_uppercase(self):
        # Oracle folds unquoted identifiers, so the name the connector matches on
        # is upper case. A lower-case collection would simply never be found.
        self.assertEqual(
            signal_collection('oracle', 'checkout', database='ORCL',
                              schema='admin'),
            'ADMIN.ECDL_SIGNAL_CHECKOUT')

    def test_explicit_schema_wins(self):
        self.assertEqual(
            signal_collection('postgresql', 'checkout', database='shop',
                              schema='sales'),
            'sales.ecdl_signal_checkout')

    def test_override_used_verbatim(self):
        self.assertEqual(
            signal_collection('mysql', 'checkout', database='Inventory',
                              override='ops.dbz_signal'),
            'ops.dbz_signal')

    def test_mysql_without_a_database_is_refused(self):
        with self.assertRaises(SignalError):
            signal_collection('mysql', 'checkout', database=None)

    def test_oracle_without_a_schema_is_refused(self):
        # Oracle has no default schema to fall back on, unlike public and dbo.
        with self.assertRaises(SignalError):
            signal_collection('oracle', 'checkout', database='ORCL')

    def test_unknown_engine_refused(self):
        with self.assertRaises(SignalError):
            signal_collection('db2', 'checkout', database='x')


class TopicPatternTest(unittest.TestCase):
    def test_keeps_the_heartbeat(self):
        # Dropping it meant an idle source never committed an offset, and the
        # first deploy failed before the load. The sink trims its stream instead.
        pattern = topic_pattern('checkout', 'Inventory.ecdl_signal_checkout')
        self.assertIsNone(re.match(pattern + '$', '__debezium-heartbeat.checkout'))
    def test_matches_the_signal_table(self):
        pattern = topic_pattern('checkout', 'Inventory.ecdl_signal_checkout')
        self.assertTrue(
            re.match(pattern + '$', 'checkout.Inventory.ecdl_signal_checkout'))
    def test_does_not_match_a_captured_table(self):
        # The reason for naming the exact collections instead of a wildcard:
        # a customer table must never be silently dropped.
        pattern = topic_pattern('checkout', 'Inventory.ecdl_signal_checkout')
        for topic in ('checkout.Inventory.orders',
                      'checkout.Inventory.signals',
                      'checkout.Inventory.ecdl_signal_checkout_audit'):
            self.assertIsNone(re.match(pattern + '$', topic),
                              '%s must not be dropped' % topic)
    def test_dots_in_the_table_are_escaped(self):
        # An unescaped dot would match any character, so more topics would be dropped.
        pattern = topic_pattern('a', 'db.ecdl_signal_x')
        self.assertIsNone(re.match(pattern + '$', 'a.db.ecdl_signal_y'))
    def test_matches_nothing_with_no_signal_table(self):
        pattern = topic_pattern('checkout', '')
        for topic in ('__debezium-heartbeat.checkout', 'checkout.Inventory.orders'):
            self.assertIsNone(re.match(pattern, topic))


class DdlTest(unittest.TestCase):
    def test_every_engine_produces_ddl(self):
        for engine, collection in (
                ('mysql', 'Inventory.ecdl_signal_x'),
                ('postgresql', 'public.ecdl_signal_x'),
                ('sqlserver', 'dbo.ecdl_signal_x'),
                ('oracle', 'ADMIN.ECDL_SIGNAL_X')):
            ddl = signal_table_ddl(engine, collection)
            self.assertIn('id', ddl)
            self.assertIn('type', ddl)
            self.assertIn('data', ddl)

    def test_column_widths_are_debeziums(self):
        ddl = signal_table_ddl('mysql', 'Inventory.ecdl_signal_x')
        self.assertIn('VARCHAR(42)', ddl)
        self.assertIn('VARCHAR(32)', ddl)
        self.assertIn('VARCHAR(2048)', ddl)

    def test_oracle_tolerates_an_existing_table(self):
        # Oracle has no CREATE TABLE IF NOT EXISTS, so re-running the DDL has to
        # be safe some other way: ORA-00955 is name-already-used.
        ddl = signal_table_ddl('oracle', 'ADMIN.ECDL_SIGNAL_X')
        self.assertIn('-955', ddl)
        self.assertIn('VARCHAR2', ddl)

    def test_every_engine_keeps_the_qualifier(self):  # Jira story F019
        # A bare MySQL name lands in whatever database the session is using.
        for engine, collection in (
                ('mysql', 'Inventory.ecdl_signal_x'),
                ('postgresql', 'public.ecdl_signal_x'),
                ('sqlserver', 'dbo.ecdl_signal_x'),
                ('oracle', 'ADMIN.ECDL_SIGNAL_X')):
            ddl = signal_table_ddl(engine, collection)
            self.assertIn(' {} '.format(collection), ddl.replace("'", ' ').replace('(', ' '),
                          engine)
        self.assertTrue(signal_table_ddl('mysql', 'Inventory.ecdl_signal_x').startswith(
            'CREATE TABLE IF NOT EXISTS Inventory.ecdl_signal_x ('))

    def test_mysql_grant_names_the_table_and_the_user(self):
        from ecdl_link.signals import mysql_signal_grant_sql
        self.assertEqual(mysql_signal_grant_sql('Inventory.ecdl_signal_x', 'cdc'),
                         "GRANT SELECT, INSERT ON Inventory.ecdl_signal_x TO 'cdc'@'%';")
        for bad_user in ("x'@'%", None, 'a b'):
            self.assertIn("'<the CDC user>'@'%'",
                          mysql_signal_grant_sql('Inventory.s', bad_user))
        with self.assertRaises(SignalError):
            mysql_signal_grant_sql('Inventory.s; DROP TABLE t', 'cdc')

    def test_mysql_and_postgres_are_idempotent(self):
        for engine, collection in (('mysql', 'db.ecdl_signal_x'),
                                   ('postgresql', 'public.ecdl_signal_x')):
            self.assertIn('IF NOT EXISTS', signal_table_ddl(engine, collection))

    def test_sqlserver_is_idempotent(self):
        self.assertIn('OBJECT_ID',
                      signal_table_ddl('sqlserver', 'dbo.ecdl_signal_x'))


class SnapshotSignalTest(unittest.TestCase):
    def test_incremental_type(self):
        # Debezium supports only 'incremental' for an ad hoc snapshot of this
        # kind, and it is what keeps streaming running during the backfill.
        signal = snapshot_signal(['Inventory.orders'])
        self.assertEqual(signal['type'], 'execute-snapshot')
        self.assertEqual(json.loads(signal['data'])['type'], 'incremental')

    def test_names_every_collection(self):
        signal = snapshot_signal(['Inventory.orders', 'Inventory.customers'])
        self.assertEqual(json.loads(signal['data'])['data-collections'],
                         ['Inventory.orders', 'Inventory.customers'])

    def test_empty_collection_list_refused(self):
        with self.assertRaises(SignalError):
            snapshot_signal([])

    def test_generated_id_fits_the_column(self):
        # The id column is VARCHAR(42); a longer id is truncated or rejected
        # depending on the engine, and a truncated id can collide.
        self.assertLessEqual(len(snapshot_signal(['a.b'])['id']), 42)

    def test_long_explicit_id_refused(self):
        with self.assertRaises(SignalError):
            snapshot_signal(['a.b'], signal_id='x' * 43)

    def test_ids_are_unique(self):
        ids = {snapshot_signal(['a.b'])['id'] for _ in range(20)}
        self.assertEqual(len(ids), 20)

    def test_additional_conditions_passed(self):
        signal = snapshot_signal(
            ['Inventory.orders'],
            additional_conditions=[{'data-collection': 'Inventory.orders',
                                    'filter': "status='active'"}])
        self.assertIn('additional-conditions', json.loads(signal['data']))

    def test_surrogate_key_passed(self):
        signal = snapshot_signal(['Inventory.orders'], surrogate_key='seq')
        self.assertEqual(json.loads(signal['data'])['surrogate-key'], 'seq')

    def test_payload_is_compact(self):
        # The data column holds 2048 characters, so a many-table request needs the
        # JSON not to waste them on whitespace.
        signal = snapshot_signal(['a.b', 'a.c'])
        self.assertNotIn(', ', signal['data'])


class OtherSignalTest(unittest.TestCase):
    def test_stop_without_collections_omits_the_field(self):
        # An absent data-collections stops the whole snapshot; sending it empty
        # is a different request.
        signal = stop_snapshot_signal()
        self.assertIsNone(signal['data'])

    def test_stop_with_collections_excludes_them(self):
        signal = stop_snapshot_signal(['Inventory.orders'])
        self.assertEqual(json.loads(signal['data'])['data-collections'],
                         ['Inventory.orders'])

    def test_pause_and_resume_carry_no_data(self):
        # A pause takes effect wherever the snapshot has reached, so naming a
        # collection would be meaningless.
        self.assertIsNone(pause_snapshot_signal()['data'])
        self.assertIsNone(resume_snapshot_signal()['data'])

    def test_log_signal_carries_the_message(self):
        signal = log_signal('ecdl liveness check')
        self.assertEqual(json.loads(signal['data'])['message'],
                         'ecdl liveness check')


class InsertStatementTest(unittest.TestCase):
    def test_parameterised_not_interpolated(self):
        # The data field is JSON full of quotes and braces, and the table name came
        # from a link file. Neither belongs in concatenated SQL.
        sql, values = insert_statement('mysql', 'Inventory.ecdl_signal_x',
                                       snapshot_signal(['Inventory.orders']))
        self.assertIn('%s', sql)
        self.assertNotIn('execute-snapshot', sql)
        self.assertEqual(len(values), 3)
        self.assertEqual(values[1], 'execute-snapshot')

    def test_oracle_uses_numbered_binds(self):
        sql, _ = insert_statement('oracle', 'ADMIN.ECDL_SIGNAL_X',
                                  snapshot_signal(['ADMIN.ORDERS']))
        self.assertIn(':1', sql)
        self.assertNotIn('%s', sql)

    def test_targets_the_signal_collection(self):
        sql, _ = insert_statement('postgresql', 'public.ecdl_signal_x',
                                  log_signal('hi'))
        self.assertIn('public.ecdl_signal_x', sql)


class DescribeTest(unittest.TestCase):
    def test_includes_type_and_id(self):
        signal = snapshot_signal(['Inventory.orders'], signal_id='snap-1')
        text = describe_signal(signal)
        self.assertIn('execute-snapshot', text)
        self.assertIn('snap-1', text)
        self.assertIn('Inventory.orders', text)

    def test_handles_a_signal_with_no_payload(self):
        self.assertIn('pause-snapshot', describe_signal(pause_snapshot_signal()))


class InjectionTest(unittest.TestCase):
    """The collection comes from the link file and is written into SQL text."""

    HOSTILE = ("Inventory.x; DROP TABLE orders; --",
               "public.sig\" (id) VALUES ('a'); --",
               "dbo.sig]; DROP TABLE t; --",
               "Inventory.sig' OR '1'='1")

    def test_hostile_override_refused(self):
        for bad in self.HOSTILE:
            with self.assertRaises(SignalError, msg=bad):
                signal_collection('mysql', 'checkout', 'Inventory', override=bad)

    def test_hostile_database_or_schema_refused(self):
        with self.assertRaises(SignalError):
            signal_collection('mysql', 'checkout', 'Inventory; DROP DATABASE x')
        with self.assertRaises(SignalError):
            signal_collection('postgresql', 'checkout', 'shop',
                              schema='public" CASCADE; --')

    def test_ddl_and_insert_refuse_hostile_collections(self):
        for engine in ('mysql', 'postgresql', 'sqlserver', 'oracle'):
            for bad in self.HOSTILE:
                with self.assertRaises(SignalError, msg=(engine, bad)):
                    signal_table_ddl(engine, bad)
                with self.assertRaises(SignalError, msg=(engine, bad)):
                    insert_statement(engine, bad, log_signal('x'))

    def test_values_are_still_bound(self):
        sql, values = insert_statement('postgresql', 'public.ecdl_signal_c',
                                       log_signal("it's {quoted}"))
        self.assertEqual(sql, 'INSERT INTO public.ecdl_signal_c (id, type, data) '
                              'VALUES (%s, %s, %s)')
        self.assertIn("it's {quoted}", values[2])


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=0).result
    print('ok: {} signal tests passed'.format(result.testsRun)
          if result.wasSuccessful() else 'FAILED')
    sys.exit(0 if result.wasSuccessful() else 1)
