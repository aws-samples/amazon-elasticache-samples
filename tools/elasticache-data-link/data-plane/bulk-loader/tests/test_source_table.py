"""Jira story F061: the loader reads a table by the name the source knows it by.

The control plane used to pass the bare table name. PostgreSQL resolves a bare
name through search_path, so a table outside `public` failed, or a table of the
same name in `public` was loaded instead; MySQL looked the key up in the
connection's database whatever the name said. Now SQL and catalogue lookups use
the qualified name (schema.table, database.table) and the cache keys and
transform rules keep using the bare name, as the sink does.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bulk_loader  # noqa: E402
import sqlident  # noqa: E402
from tests.fake_source import (FakeSource, clean, real_valkey_modules,  # noqa: E402
                               run_main, target_from_env)


class _Cursor(object):
    def __init__(self, rows=(), one=None):
        self.calls, self.rows, self.one = [], list(rows), one

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.one

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn(object):
    def __init__(self, rows=(), one=None):
        self.cur = _Cursor(rows, one)

    def cursor(self, *args, **kwargs):
        return self.cur


def _adapter(engine, rows=(), one=None, cfg=None):
    """An adapter whose queries return `rows` (fetchall) and `one` (fetchone)."""
    a = bulk_loader.ADAPTERS[engine](cfg or {'database': 'shop'})
    a.conn = _Conn(rows, one)
    return a


class CatalogueLookupTest(unittest.TestCase):
    def test_mysql_primary_key_is_looked_up_in_the_named_database(self):
        a = _adapter('mysql', [{'COLUMN_NAME': 'id'}])
        self.assertEqual(a.primary_key('reporting.orders'), ['id'])
        self.assertEqual(a.conn.cur.calls[-1][1], ('reporting', 'orders'))

    def test_mysql_bare_name_uses_the_connection_database(self):
        a = _adapter('mysql', [{'COLUMN_NAME': 'id'}])
        a.primary_key('orders')
        self.assertEqual(a.conn.cur.calls[-1][1], ('shop', 'orders'))

    def test_mysql_columns_and_estimate_use_the_named_database(self):
        a = _adapter('mysql', [])
        a.column_types('reporting.orders')
        self.assertEqual(a.conn.cur.calls[-1][1], ('reporting', 'orders'))
        a.estimate_rows('reporting.orders')
        self.assertEqual(a.conn.cur.calls[-1][1], ('reporting', 'orders'))

    def test_mysql_queries_the_qualified_table(self):
        a = _adapter('mysql', one={'lo': 1, 'hi': 2})
        a.bounds('reporting.orders', 'id')
        self.assertIn('FROM `reporting`.`orders`', a.conn.cur.calls[-1][0])

    def test_postgresql_passes_the_schema_to_every_lookup(self):
        # Jira story F081: to_regclass() gets the name quoted, as the queries quote it.
        a = _adapter('postgresql', [], one=(1, 2))
        a.primary_key('sales.orders')
        self.assertEqual(a.conn.cur.calls[-1][1], ('"sales"."orders"',))
        a.column_types('sales.orders')
        self.assertEqual(set(a.conn.cur.calls[-1][1]), {'"sales"."orders"'})
        a.bounds('sales.orders', 'id')
        self.assertIn('FROM "sales"."orders"', a.conn.cur.calls[-1][0])

    def test_postgresql_mixed_case_names_keep_their_case_in_lookups(self):
        # Unquoted, to_regclass('Sales.Orders') looks up sales.orders, while
        # every query reads "Sales"."Orders".
        a = _adapter('postgresql', [], one=(7,))
        for lookup in (a.primary_key, a.column_types, a.estimate_rows):
            lookup('Sales.Orders')
            sql, params = a.conn.cur.calls[-1]
            self.assertIn('to_regclass(%s)', sql)
            self.assertEqual(set(params), {'"Sales"."Orders"'}, lookup.__name__)
        a.primary_key('Orders')
        self.assertEqual(a.conn.cur.calls[-1][1], ('"Orders"',))

    def test_postgresql_lookups_refuse_unsafe_names(self):
        a = _adapter('postgresql', [])
        with self.assertRaises(sqlident.UnsafeIdentifier):
            a.column_types('x"); DROP TABLE y; --')

    def test_oracle_lookups_are_scoped_to_the_owner(self):
        # Matching on the table name alone could answer for another schema's
        # table of the same name.
        a = _adapter('oracle', [('ID',)], one=(5,))
        a.primary_key('sales.orders')
        sql, binds = a.conn.cur.calls[-1]
        self.assertIn('ac.owner = :1 AND ac.table_name = :2', sql)
        self.assertEqual(binds, ['SALES', 'ORDERS'])
        a.estimate_rows('sales.orders')
        sql, binds = a.conn.cur.calls[-1]
        self.assertIn('owner = :1 AND table_name = :2', sql)
        self.assertEqual(binds, ['SALES', 'ORDERS'])

    def test_oracle_bare_name_uses_the_current_schema(self):
        a = _adapter('oracle', [('ID',)], one=(5,))
        a.primary_key('orders')
        sql, binds = a.conn.cur.calls[-1]
        self.assertIn("SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')", sql)
        self.assertEqual(binds, ['ORDERS'])
        a.estimate_rows('orders')
        sql, binds = a.conn.cur.calls[-1]
        self.assertIn("SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')", sql)
        self.assertEqual(binds, ['ORDERS'])


def _orders(schema_rows):
    return {'pk': ['id'], 'columns': [('id', 'int'), ('who', 'varchar')],
            'rows': [{'id': i, 'who': who} for i, who in schema_rows]}


class EndToEndTest(unittest.TestCase):
    """main() against a real Valkey, with two tables named orders."""

    def setUp(self):
        # Skip before touching the valkey modules: a skip in setUp runs no
        # tearDown.
        self.target = target_from_env(self)
        self._modules = real_valkey_modules()
        self._modules.__enter__()
        self.vk = bulk_loader.target_client(self.target)
        clean(self.vk, 'orders:*', 'ecdl:bulkload:*')
        FakeSource.reset({
            'public.orders': _orders([(1, 'public'), (2, 'public')]),
            'sales.orders': _orders([(1, 'sales'), (2, 'sales'), (3, 'sales')]),
        })

    def tearDown(self):
        clean(self.vk, 'orders:*', 'ecdl:bulkload:*')
        self.vk.close()
        self._modules.__exit__(None, None, None)

    def test_non_public_schema_is_the_one_loaded(self):
        run_main(self.target, 'sales.orders')
        self.assertTrue(FakeSource.seen)
        self.assertEqual(set(FakeSource.seen), {'sales.orders'})
        # Keys use the bare name, as the sink's source.table does.
        self.assertEqual(sorted(k.decode() for k in self.vk.scan_iter(match='orders:*')),
                         ['orders:1', 'orders:2', 'orders:3'])
        self.assertEqual(self.vk.hget('orders:3', 'who'), b'sales')

    def test_default_schema_unchanged(self):
        run_main(self.target, 'orders')
        self.assertEqual(set(FakeSource.seen), {'orders'})
        self.assertEqual(sorted(k.decode() for k in self.vk.scan_iter(match='orders:*')),
                         ['orders:1', 'orders:2'])
        self.assertEqual(self.vk.hget('orders:1', 'who'), b'public')

    def test_mysql_database_qualified(self):
        FakeSource.reset({'shop.orders': _orders([(5, 'shop')]),
                          'reporting.orders': _orders([(5, 'reporting')])},
                         default_schema='shop')
        run_main(self.target, 'reporting.orders')
        self.assertEqual(set(FakeSource.seen), {'reporting.orders'})
        self.assertEqual(self.vk.hget('orders:5', 'who'), b'reporting')

    def test_transforms_are_found_by_the_bare_name(self):
        spec = {'tables': {'orders': {'rename': {'who': 'owner'}}}}
        run_main(self.target, 'sales.orders', spec=spec)
        self.assertEqual(self.vk.hget('orders:1', 'owner'), b'sales')
        self.assertIsNone(self.vk.hget('orders:1', 'who'))

    def test_a_link_key_prefix_still_wins(self):
        clean(self.vk, 'v2:orders:*')
        try:
            run_main(self.target, 'sales.orders', key_prefix='v2:orders:')
            self.assertEqual(self.vk.hget('v2:orders:2', 'who'), b'sales')
            self.assertEqual(list(self.vk.scan_iter(match='orders:*')), [])
        finally:
            clean(self.vk, 'v2:orders:*', 'ecdl:bulkload:v2:*')

    def test_unsafe_qualified_names_are_still_refused(self):
        for bad in ('sales.orders; DROP TABLE x', 'sales."orders"', 'a.b.c.d'):
            with self.assertRaises(SystemExit) as caught:
                run_main(self.target, bad)
            self.assertIn('FATAL', str(caught.exception), bad)
        self.assertEqual(FakeSource.seen, [], 'refused before any query')
        self.assertTrue(sqlident.is_safe('orders'))


if __name__ == '__main__':
    unittest.main()
