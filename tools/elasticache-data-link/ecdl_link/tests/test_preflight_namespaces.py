"""Jira story F131: a table qualified with another schema (or MySQL database) is checked
against that one, not by its bare name against the default one."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.preflight import (  # noqa: E402
    BLOCK, WARN, check_catalogue, estimate_pause, preflight)
from ecdl_link.tests.test_tables import (  # noqa: E402
    PG_CLUSTER, SECRET_ARN, SOURCE_ARN, Clients, FakeDriver, FakeRds)


def cat(namespace, *tables, kind='schema', listed=True):
    return {'listed': listed, 'namespace': namespace, 'namespace_kind': kind,
            'tables': [{'table': name, 'row_estimate': rows,
                        'columns': [{'name': c} for c in cols]}
                       for name, cols, rows in tables]}


PUBLIC = cat('public', ('orders', ['id', 'total'], 100))
SALES = cat('sales', ('invoices', ['id', 'amount', 'customer_id'], 50),
            ('customers', ['id', 'name'], 10))


def levels(findings):
    return [f.level for f in findings]


class CheckCatalogueTest(unittest.TestCase):
    def with_sales(self, sales=SALES):
        return dict(PUBLIC, other_namespaces={'sales': sales})

    def test_a_table_in_another_schema_is_found_there(self):
        self.assertEqual(check_catalogue({'tables': ['orders', 'sales.invoices']},
                                         self.with_sales()), [])

    def test_a_table_missing_from_its_schema_blocks_even_if_the_default_has_it(self):
        # public.orders exists; sales.orders does not.
        findings = check_catalogue({'tables': ['sales.orders']}, self.with_sales())
        self.assertEqual(levels(findings), [BLOCK])
        self.assertIn("'sales.orders' does not exist", findings[0].message)

    def test_an_unread_schema_is_a_warning_not_a_block(self):
        findings = check_catalogue({'tables': ['sales.invoices']}, PUBLIC)
        self.assertEqual(levels(findings), [WARN])
        self.assertIn("schema 'sales'", findings[0].message)
        failed = dict(PUBLIC, other_namespaces={'sales': {'listed': False,
                                                          'error': 'permission denied'}})
        findings = check_catalogue({'tables': ['sales.invoices']}, failed)
        self.assertEqual(levels(findings), [WARN])
        self.assertIn('permission denied', findings[0].message)

    def test_columns_are_checked_against_the_tables_own_schema(self):
        doc = {'tables': ['sales.invoices'],
               'transforms': {'tables': {'invoices': {'columns': ['amount', 'nope']}}}}
        findings = check_catalogue(doc, self.with_sales())
        self.assertEqual([(f.level, f.path) for f in findings],
                         [(BLOCK, 'transforms.tables.invoices.columns')])
        self.assertIn("'nope'", findings[0].message)

    def test_columns_of_an_unread_schema_are_not_guessed(self):
        doc = {'tables': ['sales.orders'],
               'transforms': {'tables': {'orders': {'columns': ['total', 'nope']}}}}
        # public.orders has 'total' but not 'nope'; sales was not read, so no
        # column finding at all -- only the table warning.
        findings = check_catalogue(doc, PUBLIC)
        self.assertEqual(levels(findings), [WARN])

    def test_denormalize_parent_in_another_schema(self):
        doc = {'tables': ['orders', 'sales.customers'],
               'transforms': {'tables': {'orders': {'denormalize': [{
                   'parent_table': 'customers', 'join_field': 'id',
                   'fields': ['name', 'missing']}]}}}}
        findings = check_catalogue(doc, self.with_sales())
        self.assertEqual(len(findings), 1)
        self.assertIn("'missing' from 'customers'", findings[0].message)

    def test_case_and_unqualified_names_are_unchanged(self):
        self.assertEqual(check_catalogue({'tables': ['PUBLIC.orders', 'orders']}, PUBLIC), [])
        no_ns = dict(PUBLIC, namespace=None)
        self.assertEqual(check_catalogue({'tables': ['sales.orders']}, no_ns), [])

    def test_mysql_databases_work_the_same_way(self):
        inventory = cat('Inventory', ('orders', ['id'], 1), kind='database')
        findings = check_catalogue({'tables': ['Archive.orders']}, inventory)
        self.assertIn("database 'Archive'", findings[0].message)

    def test_the_pause_estimate_uses_each_schemas_counts(self):
        est = estimate_pause(['orders', 'sales.invoices'], self.with_sales())
        self.assertEqual((est['rows'], est['unknown']), (150, []))
        est = estimate_pause(['orders', 'sales.invoices'], PUBLIC)
        self.assertEqual((est['rows'], est['unknown']), (100, ['sales.invoices']))


def pg_by_schema(schemas):
    """A PostgreSQL responder that answers per schema (the query parameter)."""
    def respond(sql, params):
        schema = params[0] if params else None
        tables = schemas.get(schema, [])
        if 'relreplident' in sql:
            return [(name, 'f', True) for name, _cols in tables]
        if 'attstorage' in sql:
            return []
        if 'pg_class' in sql:
            return [(name, 10) for name, _cols in tables]
        if 'information_schema.columns' in sql:
            return [(name, col, 'text', 'YES') for name, cols in tables for col in cols]
        if 'table_constraints' in sql:
            return [(name, cols[0]) for name, cols in tables]
        if 'wal_level' in sql:
            return [('logical',)]
        if 'pg_namespace' in sql:
            return [(s,) for s in schemas]
        return []
    return respond


class PreflightReadsEachSchemaTest(unittest.TestCase):
    def run_preflight(self, tables, schemas):
        driver = FakeDriver(pg_by_schema(schemas))
        doc = {'name': 'shop', 'source': {'kind': 'aws', 'arn': SOURCE_ARN,
                                          'secret_arn': SECRET_ARN, 'database': 'shop'},
               'tables': tables}
        return preflight(doc, clients=Clients(rds=FakeRds(PG_CLUSTER)), driver=driver,
                         publication='ecdl_shop_pub')

    def table_findings(self, out):
        return [(f['level'], f['message']) for f in out['findings'] if f['path'] == 'tables']

    def test_a_valid_link_across_two_schemas_is_not_blocked(self):
        out = self.run_preflight(['orders', 'sales.invoices'],
                                 {'public': [('orders', ['id'])],
                                  'sales': [('invoices', ['id'])]})
        self.assertEqual(self.table_findings(out), [])

    def test_a_missing_table_in_the_other_schema_blocks(self):
        out = self.run_preflight(['sales.orders'],
                                 {'public': [('orders', ['id'])], 'sales': []})
        found = self.table_findings(out)
        self.assertEqual([lvl for lvl, _m in found], [BLOCK])
        self.assertIn('sales.orders', found[0][1])


if __name__ == '__main__':
    unittest.main()
