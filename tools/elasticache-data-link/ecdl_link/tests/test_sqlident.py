"""Identifier validation and quoting: the one place names enter SQL text."""
import os
import unittest

from ecdl_link import sqlident
from ecdl_link.sqlident import UnsafeIdentifier

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Each of these must be refused. Several would end the quoted name early in one engine's
# quoting: a backtick for MySQL, a double quote for PostgreSQL/Oracle, ']' for
# SQL Server.
HOSTILE = [
    'products; DROP TABLE users',
    'products` WHERE 1=1; --',
    'products" OR "1"="1',
    'products] ; DROP TABLE x; --',
    "products' OR '1'='1",
    'products -- comment',
    'products/*x*/',
    'my table',
    'products\n',
    'products\t',
    'pro\x00ducts',
    'ürün',
    '1products',
    '',
    None,
    123,
    'a' * 129,
]


class ValidateTest(unittest.TestCase):

    def test_plain_names_pass_unchanged(self):
        for name in ('orders', 'Orders', '_tmp', 'order_items2', 'SYS$X', 'a' * 128):
            self.assertEqual(sqlident.validate(name), name)

    def test_hostile_names_are_refused(self):
        for name in HOSTILE:
            with self.assertRaises(UnsafeIdentifier, msg=repr(name)):
                sqlident.validate(name)
            self.assertFalse(sqlident.is_safe(name), repr(name))

    def test_unsafe_identifier_is_a_value_error(self):
        # Callers that already catch ValueError keep working.
        self.assertTrue(issubclass(UnsafeIdentifier, ValueError))

    def test_qualified_names(self):
        self.assertEqual(sqlident.validate_qualified('public.orders'), 'public.orders')
        self.assertEqual(sqlident.validate_qualified('db.dbo.orders'), 'db.dbo.orders')
        for bad in ('a.b.c.d', 'public.', '.orders', 'public..orders',
                    'public.orders; DROP TABLE x', 'pub lic.orders'):
            with self.assertRaises(UnsafeIdentifier, msg=bad):
                sqlident.validate_qualified(bad)


class QuoteTest(unittest.TestCase):

    def test_each_engine_quotes_its_own_way(self):
        self.assertEqual(sqlident.quote('order', 'mysql'), '`order`')
        self.assertEqual(sqlident.quote('order', 'postgresql'), '"order"')
        self.assertEqual(sqlident.quote('order', 'sqlserver'), '[order]')
        self.assertEqual(sqlident.quote('ORDER', 'oracle'), '"ORDER"')
        self.assertEqual(sqlident.quote('order', 'bare'), 'order')

    def test_aliases(self):
        self.assertEqual(sqlident.quote('t', 'postgres'), '"t"')
        self.assertEqual(sqlident.quote('t', 'mssql'), '[t]')
        self.assertEqual(sqlident.quote('t', 'aurora-mysql'), '`t`')

    def test_unknown_style_is_refused(self):
        with self.assertRaises(ValueError):
            sqlident.quote('t', 'sqlite')

    def test_each_dotted_part_is_quoted_separately(self):
        # The bug this replaces: `Inventory.products` is ONE table whose name
        # contains a dot, not products in Inventory.
        self.assertEqual(sqlident.quote_qualified('Inventory.products', 'mysql'),
                         '`Inventory`.`products`')
        self.assertEqual(sqlident.quote_qualified('public.orders', 'postgresql'),
                         '"public"."orders"')
        self.assertEqual(sqlident.quote_qualified('dbo.orders', 'sqlserver'),
                         '[dbo].[orders]')

    def test_quoting_never_happens_around_a_hostile_name(self):
        for engine in ('mysql', 'postgresql', 'sqlserver', 'oracle', 'bare'):
            for name in HOSTILE:
                with self.assertRaises(UnsafeIdentifier, msg=(engine, name)):
                    sqlident.quote_qualified(name, engine)


class RenderTest(unittest.TestCase):

    def test_render_quotes_names_and_leaves_bind_markers(self):
        sql = sqlident.render('mysql', 'SELECT {cols} FROM {t} WHERE {pk} = %s',
                              cols=['id', 'price'], t='Inventory.products', pk='id')
        self.assertEqual(
            sql, 'SELECT `id`, `price` FROM `Inventory`.`products` WHERE `id` = %s')

    def test_render_oracle_bind_markers_survive(self):
        sql = sqlident.render('bare', 'SELECT * FROM {t} WHERE {pk} >= :1', t='orders', pk='id')
        self.assertEqual(sql, 'SELECT * FROM orders WHERE id >= :1')

    def test_render_refuses_a_hostile_name_in_any_slot(self):
        with self.assertRaises(UnsafeIdentifier):
            sqlident.render('mysql', 'SELECT * FROM {t}', t='x`; DROP TABLE y; --')
        with self.assertRaises(UnsafeIdentifier):
            sqlident.render('postgresql', 'SELECT {cols} FROM t',
                            cols=['id', 'name" FROM pg_shadow --'])

    def test_binds(self):
        self.assertEqual(sqlident.binds(3), '%s, %s, %s')
        self.assertEqual(sqlident.binds(2, '?'), '?, ?')
        self.assertEqual(sqlident.binds(3, ':'), ':1, :2, :3')
        for bad in (0, -1, True, '3', 1.5):
            with self.assertRaises(ValueError, msg=bad):
                sqlident.binds(bad)
        with self.assertRaises(ValueError):
            sqlident.binds(2, "'; --")

    def test_render_passes_binds_through_and_quotes_everything_else(self):
        sql = sqlident.render('postgresql', 'INSERT INTO {t} ({cols}) VALUES ({marks})',
                              t='public.orders', cols=['id', 'amount'],
                              marks=sqlident.binds(2))
        self.assertEqual(sql, 'INSERT INTO "public"."orders" ("id", "amount") '
                              'VALUES (%s, %s)')
        # A plain string in the marks slot is an identifier, not raw SQL.
        with self.assertRaises(UnsafeIdentifier):
            sqlident.render('mysql', 'INSERT INTO t VALUES ({marks})',
                            marks='1); DROP TABLE t; --')

    def test_render_refuses_an_empty_column_list(self):
        with self.assertRaises(UnsafeIdentifier):
            sqlident.render('mysql', 'SELECT {cols} FROM t', cols=[])


class VendoredCopyTest(unittest.TestCase):

    def test_loader_copy_is_identical(self):
        # The loader image does not contain ecdl_link, so it carries a copy. A
        # drifted copy would mean the loader validates by different rules.
        with open(os.path.join(_REPO, 'ecdl_link', 'sqlident.py'), 'rb') as a, \
                open(os.path.join(_REPO, 'data-plane', 'bulk-loader', 'sqlident.py'),
                     'rb') as b:
            self.assertEqual(a.read(), b.read(),
                             'data-plane/bulk-loader/sqlident.py differs from '
                             'ecdl_link/sqlident.py; copy it across')


if __name__ == '__main__':
    unittest.main()
