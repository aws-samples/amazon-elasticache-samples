"""The loader's SQL text: names quoted per engine, hostile names refused.

No database is needed: the adapters' statements are built by _sql() and checked
as text, and a recording cursor shows the range bounds stay bind parameters.
"""
import unittest

import bulk_loader
import sqlident


class _Cursor:
    def __init__(self, row=None):
        self.calls = []
        self.row = row

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, row=None):
        self.cur = _Cursor(row)

    def cursor(self, *args, **kwargs):
        return self.cur


class AdapterSqlTest(unittest.TestCase):

    def test_every_adapter_declares_its_quoting(self):
        # An adapter that forgot QUOTE_STYLE would inherit 'bare' silently.
        self.assertEqual({k: v.QUOTE_STYLE for k, v in bulk_loader.ADAPTERS.items()},
                         {'mysql': 'mysql', 'postgresql': 'postgresql',
                          'sqlserver': 'sqlserver', 'oracle': 'bare'})

    def test_mysql_quotes_each_part_of_a_qualified_table(self):
        a = bulk_loader.MySQLAdapter({})
        self.assertEqual(a._sql(a.RANGE_SQL, 'Inventory.products', 'id'),
                         'SELECT * FROM `Inventory`.`products` '
                         'WHERE `id` >= %s AND `id` <= %s')

    def test_postgresql_quotes_each_part(self):
        a = bulk_loader.PostgreSQLAdapter({})
        self.assertEqual(a._sql(a.BOUNDS_SQL, 'public.orders', 'order_id'),
                         'SELECT MIN("order_id") AS lo, MAX("order_id") AS hi '
                         'FROM "public"."orders"')

    def test_sqlserver_brackets(self):
        a = bulk_loader.ADAPTERS['sqlserver']({})
        self.assertEqual(a._sql(a.RANGE_SQL, 'dbo.orders', 'id'),
                         'SELECT * FROM [dbo].[orders] WHERE [id] >= %s AND [id] <= %s')

    def test_oracle_is_validated_but_left_unquoted(self):
        # Unquoted, so Oracle folds `orders3w` to the ORDERS3W in its catalog.
        a = bulk_loader.ADAPTERS['oracle']({})
        self.assertEqual(a._sql(a.RANGE_SQL, 'ADMIN.orders3w', 'id'),
                         'SELECT * FROM ADMIN.orders3w WHERE id >= :1 AND id <= :2')

    def test_every_adapter_refuses_hostile_names(self):
        hostile = ['orders`; DROP TABLE users; --', 'orders"; DROP TABLE users; --',
                   'orders]; DROP TABLE users; --', 'orders WHERE 1=1', "o'x"]
        for engine, cls in bulk_loader.ADAPTERS.items():
            a = cls({})
            for bad in hostile:
                with self.assertRaises(sqlident.UnsafeIdentifier, msg=(engine, bad)):
                    a._sql(a.RANGE_SQL, bad, 'id')
                with self.assertRaises(sqlident.UnsafeIdentifier, msg=(engine, bad)):
                    a._sql(a.RANGE_SQL, 'orders', bad)

    def test_range_bounds_are_bound_not_formatted(self):
        a = bulk_loader.MySQLAdapter({})
        a.conn = _Conn()
        a.fetch('orders', 'id', 10, 20)
        sql, params = a.conn.cur.calls[-1]
        self.assertNotIn('10', sql)
        self.assertEqual(params, (10, 20))


if __name__ == '__main__':
    unittest.main()
