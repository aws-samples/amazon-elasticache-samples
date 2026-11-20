"""Loaded rows carry the version the deployer passes in LOAD_VERSION (D4').

Every change captured before capture was paused is older than the loaded rows,
every change after the resume is newer. The sink's guard compares the row's
_ecdl_v with each change's stream id, so the loader must write it on every row,
on both write paths, and must not let a source column of that name replace it.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bulk_loader  # noqa: E402
from tests import test_denorm as fakes  # noqa: E402
from tests import test_replace as replace_fakes  # noqa: E402


def load(rows, spec=None, version=None, table='orders', replace=False):
    store = replace_fakes.Store()
    saved = bulk_loader.ADAPTERS, bulk_loader.target_client, os.environ.get('LOAD_VERSION')
    bulk_loader.ADAPTERS = {'mysql': lambda cfg: fakes.FakeAdapter(rows)}
    bulk_loader.target_client = lambda cfg: replace_fakes.Valkey(store)
    if version is None:
        os.environ.pop('LOAD_VERSION', None)
    else:
        os.environ['LOAD_VERSION'] = version
    try:
        bulk_loader.load_range(('mysql', {}, {}, table, 'id', table + ':', 1, 10, spec,
                                table, 0, '', replace, [{'name': 'id', 'type': 'int'}]))
    finally:
        bulk_loader.ADAPTERS, bulk_loader.target_client = saved[0], saved[1]
        if saved[2] is None:
            os.environ.pop('LOAD_VERSION', None)
        else:
            os.environ['LOAD_VERSION'] = saved[2]
    return store


class LoadVersionTest(unittest.TestCase):
    ROWS = [{'id': 1, 'v': 'a'}, {'id': 2, 'v': 'b'}]

    def test_every_row_carries_the_version(self):
        store = load(self.ROWS, version='1794882952638-0')
        for k in ('orders:1', 'orders:2'):
            self.assertEqual(store.hashes[k]['_ecdl_v'], '1794882952638-0')
            self.assertIn('v', store.hashes[k])

    def test_replace_mode_carries_it_too(self):
        store = load(self.ROWS, version='5-0', replace=True)
        self.assertEqual(store.hashes['orders:1']['_ecdl_v'], '5-0')

    def test_no_version_when_unset(self):
        store = load(self.ROWS)
        self.assertNotIn('_ecdl_v', store.hashes['orders:1'])

    def test_a_source_column_cannot_set_the_version(self):
        rows = [{'id': 1, '_ecdl_v': '99999999999999-0'}]
        self.assertEqual(load(rows, version='7-0').hashes['orders:1']['_ecdl_v'], '7-0')
        self.assertNotIn('_ecdl_v', load(rows).hashes['orders:1'])

    def test_denormalized_rows_carry_it(self):
        store = fakes.FakeStore()
        store.hashes['customers:7'] = {'name': 'Acme', 'tier': 'gold'}
        saved = bulk_loader.ADAPTERS, bulk_loader.target_client
        bulk_loader.ADAPTERS = {'mysql': lambda cfg: fakes.FakeAdapter(
            [{'id': 1, 'customer_id': 7}])}
        bulk_loader.target_client = lambda cfg: fakes.FakeValkey(store)
        os.environ['LOAD_VERSION'] = '42-0'
        try:
            bulk_loader.load_range(('mysql', {}, {}, 'orders', 'id', 'orders:', 1, 10,
                                    fakes.ORDERS_SPEC, 'orders', 0, '', False,
                                    [{'name': 'id', 'type': 'int'}]))
        finally:
            bulk_loader.ADAPTERS, bulk_loader.target_client = saved
            os.environ.pop('LOAD_VERSION', None)
        self.assertEqual(store.hashes['orders:1']['_ecdl_v'], '42-0')
        self.assertEqual(store.hashes['orders:1']['cust_name'], 'Acme')

    def test_validation(self):
        for good in ('1-0', '1794882952638-0', '12-345'):
            os.environ['LOAD_VERSION'] = good
            self.assertEqual(bulk_loader.load_version_from_env(), good)
        for bad in ('abc', '12', '12-', '-0', '1.5-0', '12-0x', '1 -0'):
            os.environ['LOAD_VERSION'] = bad
            with self.assertRaises(SystemExit):
                bulk_loader.load_version_from_env()
        os.environ['LOAD_VERSION'] = '  '
        self.assertIsNone(bulk_loader.load_version_from_env())
        os.environ.pop('LOAD_VERSION', None)
        self.assertIsNone(bulk_loader.load_version_from_env())


if __name__ == '__main__':
    unittest.main()
