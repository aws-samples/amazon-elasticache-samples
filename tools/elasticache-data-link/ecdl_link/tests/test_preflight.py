"""Tests for the source-schema preflight.

The pure check (`check_catalogue`) is exercised against hand-built catalogues so
the comparison logic is tested without a database. The I/O wrapper (`preflight`)
reuses the fake driver/clients from test_tables so the end-to-end path -- read
the catalogue, compare, summarise -- is covered too.

The invariant that matters most: a source we could NOT read is a WARN and never a
BLOCK, because a control plane that cannot reach the customer's database is the
normal case, not a defect.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.preflight import (  # noqa: E402
    BLOCK,
    WARN,
    check_catalogue,
    preflight,
    summarize,
)
from ecdl_link.tests.test_tables import (  # noqa: E402
    Clients,
    FakeDriver,
    FakeRds,
    SECRET_ARN,
    SOURCE_ARN,
    mysql_responder,
)


def catalogue(*tables):
    """A listed catalogue from (name, [cols]) pairs."""
    return {
        'listed': True,
        'tables': [
            {'table': name,
             'columns': [{'name': c} for c in cols],
             'primary_key': [cols[0]] if cols else []}
            for name, cols in tables
        ],
    }


CAT = catalogue(
    ('catalog', ['id', 'sku', 'name', 'category', 'price', 'status']),
    ('merchants', ['merchant_id', 'name', 'country', 'risk_tier']),
)


def levels(findings):
    return [f.level for f in findings]


def paths(findings):
    return [f.path for f in findings]


class TableExistence(unittest.TestCase):

    def test_all_tables_present_is_clean(self):
        findings = check_catalogue({'tables': ['catalog', 'merchants']}, CAT)
        self.assertEqual(findings, [])

    def test_missing_table_blocks(self):
        findings = check_catalogue({'tables': ['catalog', 'customers']}, CAT)
        self.assertEqual(levels(findings), [BLOCK])
        self.assertIn('customers', findings[0].message)
        self.assertEqual(findings[0].path, 'tables')

    def test_missing_table_suggests_a_close_match(self):
        # 'catalogg' is one edit from the real 'catalog'.
        findings = check_catalogue({'tables': ['catalogg']}, CAT)
        self.assertEqual(levels(findings), [BLOCK])
        self.assertIn("did you mean 'catalog'", findings[0].message)

    def test_qualified_name_is_matched_by_bare_name(self):
        findings = check_catalogue({'tables': ['Inventory.catalog']}, CAT)
        self.assertEqual(findings, [])

    def test_case_insensitive_match(self):
        findings = check_catalogue({'tables': ['CATALOG']}, CAT)
        self.assertEqual(findings, [])


class ColumnReferences(unittest.TestCase):

    def _doc(self, spec):
        return {'tables': ['catalog', 'merchants'],
                'transforms': {'tables': {'catalog': spec}}}

    def test_selected_column_missing_blocks(self):
        findings = check_catalogue(self._doc({'columns': ['id', 'ghost']}), CAT)
        self.assertEqual(levels(findings), [BLOCK])
        self.assertEqual(paths(findings), ['transforms.tables.catalog.columns'])
        self.assertIn('ghost', findings[0].message)

    def test_selected_columns_all_present_is_clean(self):
        findings = check_catalogue(self._doc({'columns': ['id', 'price']}), CAT)
        self.assertEqual(findings, [])

    def test_filter_on_missing_column_blocks(self):
        findings = check_catalogue(
            self._doc({'filter': {'field': 'ghost', 'op': '=', 'value': 'x'}}), CAT)
        self.assertEqual(levels(findings), [BLOCK])
        self.assertEqual(paths(findings), ['transforms.tables.catalog.filter.field'])

    def test_filter_on_computed_field_is_allowed(self):
        # A filter may reference a field this transform creates, not only a
        # source column.
        findings = check_catalogue(self._doc({
            'computed': [{'name': 'price_cents', 'expr': 'price*100'}],
            'filter': {'field': 'price_cents', 'op': '=', 'value': '1'},
        }), CAT)
        self.assertEqual(findings, [])

    def test_rename_of_missing_column_blocks(self):
        findings = check_catalogue(self._doc({'rename': {'ghost': 'x'}}), CAT)
        self.assertEqual(levels(findings), [BLOCK])
        self.assertIn('ghost', findings[0].message)

    def test_rename_of_real_column_is_clean(self):
        findings = check_catalogue(self._doc({'rename': {'price': 'cost'}}), CAT)
        self.assertEqual(findings, [])

    def test_computed_expression_is_not_parsed_for_columns(self):
        # A made-up column inside an expression must NOT block: guessing wrong
        # would reject a valid config.
        findings = check_catalogue(self._doc({
            'computed': [{'name': 'x', 'expr': 'nonexistent_col * 2'}],
        }), CAT)
        self.assertEqual(findings, [])


class Denormalize(unittest.TestCase):

    def _doc(self, rule):
        return {'tables': ['catalog', 'merchants'],
                'transforms': {'tables': {'catalog': {'denormalize': [rule]}}}}

    def test_join_field_missing_on_child_blocks(self):
        findings = check_catalogue(self._doc({
            'parent_table': 'merchants', 'join_field': 'zzz',
            'fields': ['name'],
        }), CAT)
        self.assertTrue(any(f.level == BLOCK and 'join_field' in f.path
                            for f in findings))

    def test_embedded_field_missing_on_parent_blocks(self):
        findings = check_catalogue(self._doc({
            'parent_table': 'merchants', 'join_field': 'category',
            'fields': ['name', 'ghost'],
        }), CAT)
        self.assertTrue(any(f.level == BLOCK and 'fields' in f.path
                            for f in findings))
        self.assertTrue(any('ghost' in f.message for f in findings))

    def test_valid_denormalize_is_clean(self):
        findings = check_catalogue(self._doc({
            'parent_table': 'merchants', 'join_field': 'category',
            'fields': ['name', 'country'],
        }), CAT)
        self.assertEqual(findings, [])


class Unreachable(unittest.TestCase):

    def test_unlisted_catalogue_warns_never_blocks(self):
        cat = {'listed': False, 'error': 'no network path',
               'hint': 'type the names'}
        findings = check_catalogue({'tables': ['anything']}, cat)
        self.assertEqual(levels(findings), [WARN])
        self.assertIn('could not verify', findings[0].message)

    def test_empty_catalogue_is_a_warning(self):
        findings = check_catalogue({'tables': ['x']}, None)
        self.assertEqual(levels(findings), [WARN])

    def test_missing_table_does_not_add_column_errors(self):
        # A missing table is one block, not a block plus every column it would
        # have had.
        doc = {'tables': ['ghost'],
               'transforms': {'tables': {'ghost': {'columns': ['a', 'b', 'c']}}}}
        findings = check_catalogue(doc, CAT)
        self.assertEqual(levels(findings), [BLOCK])
        self.assertEqual(findings[0].path, 'tables')


class Summarize(unittest.TestCase):

    def test_counts_and_shape(self):
        findings = check_catalogue({'tables': ['catalog', 'nope']}, CAT)
        out = summarize(findings, checked=True)
        self.assertEqual(out['checked'], True)
        self.assertEqual(out['blocks'], 1)
        self.assertEqual(out['warnings'], 0)
        self.assertEqual(out['findings'][0]['level'], BLOCK)
        self.assertIn('message', out['findings'][0])


class PreflightIO(unittest.TestCase):
    """The wrapper that actually reads the source."""

    def _doc(self, tables, database='Inventory'):
        return {
            'source': {'kind': 'aws', 'arn': SOURCE_ARN,
                       'secret_arn': SECRET_ARN, 'database': database},
            'tables': tables,
        }

    def _clients_driver(self):
        return Clients(rds=FakeRds()), FakeDriver(mysql_responder())

    def test_no_secret_is_a_warning_not_a_block(self):
        out = preflight({'source': {'kind': 'aws', 'arn': SOURCE_ARN},
                         'tables': ['orders']})
        self.assertFalse(out['checked'])
        self.assertEqual(out['blocks'], 0)
        self.assertEqual(out['warnings'], 1)

    def test_present_table_passes_end_to_end(self):
        clients, driver = self._clients_driver()
        out = preflight(self._doc(['orders']), clients=clients, driver=driver)
        self.assertTrue(out['checked'])
        self.assertEqual(out['blocks'], 0)

    def test_missing_table_blocks_end_to_end(self):
        clients, driver = self._clients_driver()
        out = preflight(self._doc(['ghost_table']), clients=clients, driver=driver)
        self.assertTrue(out['checked'])
        self.assertEqual(out['blocks'], 1)
        self.assertIn('ghost_table', out['findings'][0]['message'])


if __name__ == '__main__':
    unittest.main()
