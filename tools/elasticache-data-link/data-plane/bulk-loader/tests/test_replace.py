"""Tests for the loader's merge (default) and replace modes.

The default load merges into an existing key with HSET. That keeps fields a
previous transform spec produced and leaves keys for rows the filter now drops,
so re-loading after a spec change does not make the cache match the new spec.
--replace rebuilds each key: DEL then HSET for kept rows, DEL for dropped ones.

These run load_range() end to end with an in-memory cache.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bulk_loader  # noqa: E402
from tests import test_denorm as fakes  # noqa: E402


class Store(fakes.FakeStore):
    def apply(self, op):
        if op[0] == 'del':
            self.hashes.pop(op[1], None)
            return 1
        return super(Store, self).apply(op)


class Pipe(fakes.FakePipe):
    def delete(self, key):
        self.ops.append(('del', key))
        return self


class Valkey(fakes.FakeValkey):
    def pipeline(self, transaction=False):
        return Pipe(self.store)


def load(store, rows, spec, replace, table='products'):
    saved = (bulk_loader.ADAPTERS, bulk_loader.target_client)
    bulk_loader.ADAPTERS = {'mysql': lambda cfg: fakes.FakeAdapter(rows)}
    bulk_loader.target_client = lambda cfg: Valkey(store)
    try:
        args = ('mysql', {}, {}, table, 'id', table + ':', 1, 10, spec, table,
                0, '', replace)
        return bulk_loader.load_range(args)
    finally:
        bulk_loader.ADAPTERS, bulk_loader.target_client = saved


SPEC = {'tables': {'products': {
    'filter': {'field': 'status', 'op': '=', 'value': 'active'},
    'computed': [{'name': 'price_cents', 'expr': 'price * 100'}],
}}}

ROWS = [
    {'id': 1, 'price': '2.50', 'status': 'active'},
    {'id': 2, 'price': '9.99', 'status': 'inactive'},
]


def stale_store():
    """A cache as an earlier load left it: no price_cents, an extra field that
    the current spec no longer produces, and a key for a row now filtered out."""
    store = Store()
    store.hashes['products:1'] = {'id': '1', 'price': '2.50', 'status': 'active',
                                  'old_field': 'x'}
    store.hashes['products:2'] = {'id': '2', 'price': '9.99', 'status': 'active'}
    return store


class MergeModeTest(unittest.TestCase):
    """The default. These check what it does NOT clean, so the difference
    from --replace is visible rather than assumed."""

    def test_new_fields_are_added_to_an_existing_key(self):
        store = stale_store()
        load(store, ROWS, SPEC, replace=False)
        self.assertEqual(store.hashes['products:1']['price_cents'], '250')

    def test_fields_from_an_earlier_spec_remain(self):
        store = stale_store()
        load(store, ROWS, SPEC, replace=False)
        self.assertIn('old_field', store.hashes['products:1'])

    def test_a_key_for_a_now_filtered_row_remains(self):
        store = stale_store()
        load(store, ROWS, SPEC, replace=False)
        self.assertIn('products:2', store.hashes)

    def test_a_twelve_element_task_still_means_merge(self):
        # Callers that predate the flag build a 12-tuple; it must keep working.
        store = stale_store()
        saved = (bulk_loader.ADAPTERS, bulk_loader.target_client)
        bulk_loader.ADAPTERS = {'mysql': lambda cfg: fakes.FakeAdapter(ROWS)}
        bulk_loader.target_client = lambda cfg: Valkey(store)
        try:
            bulk_loader.load_range(('mysql', {}, {}, 'products', 'id', 'products:',
                                    1, 10, SPEC, 'products', 0, ''))
        finally:
            bulk_loader.ADAPTERS, bulk_loader.target_client = saved
        self.assertIn('old_field', store.hashes['products:1'])


class ReplaceModeTest(unittest.TestCase):

    def test_kept_rows_are_rebuilt_without_stale_fields(self):
        store = stale_store()
        load(store, ROWS, SPEC, replace=True)
        self.assertEqual(store.hashes['products:1'],
                         {'id': '1', 'price': '2.50', 'status': 'active',
                          'price_cents': '250'})

    def test_rows_the_filter_drops_lose_their_key(self):
        store = stale_store()
        load(store, ROWS, SPEC, replace=True)
        self.assertNotIn('products:2', store.hashes)

    def test_written_counts_only_kept_rows(self):
        self.assertEqual(load(stale_store(), ROWS, SPEC, replace=True), 1)

    def test_many_filtered_rows_are_flushed_in_batches(self):
        # 2,500 dropped rows and no kept row: the deletes must still be sent.
        store = Store()
        rows = [{'id': i, 'price': '1', 'status': 'inactive'} for i in range(1, 2501)]
        for r in rows:
            store.hashes['products:%d' % r['id']] = {'id': str(r['id'])}
        load(store, rows, SPEC, replace=True)
        self.assertEqual(store.hashes, {})


class DenormReplaceTest(unittest.TestCase):
    """The denormalisation path writes through its own pipelines."""

    SPEC = {'tables': {'orders': {
        'filter': {'field': 'status', 'op': '=', 'value': 'open'},
        'denormalize': [{'parent_table': 'customers', 'join_field': 'customer_id',
                         'fields': ['name'], 'prefix': 'cust_'}],
    }}}

    def test_replace_rebuilds_children_and_drops_filtered_ones(self):
        store = Store()
        store.hashes['customers:7'] = {'name': 'Ada'}
        store.hashes['orders:1'] = {'id': '1', 'old_field': 'x'}
        store.hashes['orders:2'] = {'id': '2'}
        store.sets['idx:orders:by:customers:7'] = {'2'}
        rows = [{'id': 1, 'customer_id': 7, 'status': 'open'},
                {'id': 2, 'customer_id': 7, 'status': 'closed'}]
        load(store, rows, self.SPEC, replace=True, table='orders')
        self.assertNotIn('old_field', store.hashes['orders:1'])
        self.assertEqual(store.hashes['orders:1']['cust_name'], 'Ada')
        self.assertNotIn('orders:2', store.hashes)
        # Jira story F082: the dropped child also leaves its parent's reverse index.
        self.assertEqual(store.sets['idx:orders:by:customers:7'], {'1'})


if __name__ == '__main__':
    unittest.main()
