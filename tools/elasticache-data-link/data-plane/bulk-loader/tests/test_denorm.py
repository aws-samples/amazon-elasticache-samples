#!/usr/bin/env python3
"""Denormalization on the initial-load path.

Two layers:
  * the pure helpers in transforms.py, which must mirror the Go sink byte for
    byte (idx:<child>:by:<parent>:<pk>, the embed prefix, the '<parent>:' default);
  * load_range() end to end, driven by a fake source and a fake cache, proving a
    child embeds its already-loaded parent and registers itself in the parent's
    reverse index -- including the missing-parent and default-prefix cases.

Run: python3 test_denorm.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LOADER = os.path.dirname(HERE)          # data-plane/bulk-loader
sys.path.insert(0, LOADER)

import transforms          # noqa: E402
import bulk_loader         # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeStore(object):
    """A tiny in-memory stand-in for the cache: hashes and sets."""

    def __init__(self):
        self.hashes = {}
        self.sets = {}

    def apply(self, op):
        kind = op[0]
        if kind == 'hset':
            _, key, mapping = op
            self.hashes.setdefault(key, {}).update(
                {k: str(v) for k, v in mapping.items()})
            return True
        if kind == 'hmget':
            _, key, fields = op
            # decode_responses is off in the real client, so values come back as
            # bytes; return bytes here to exercise the loader's own decoding.
            h = self.hashes.get(key, {})
            out = []
            for f in fields:
                v = h.get(f)
                out.append(None if v is None else v.encode('utf-8'))
            return out
        if kind == 'sadd':
            _, key, member = op
            s = self.sets.setdefault(key, set())
            before = len(s)
            s.add(str(member))
            return len(s) - before
        raise AssertionError('unknown op %r' % (op,))


class FakePipe(object):
    def __init__(self, store):
        self.store = store
        self.ops = []

    def hset(self, key, mapping=None):
        self.ops.append(('hset', key, dict(mapping or {})))
        return self

    def hmget(self, key, fields):
        self.ops.append(('hmget', key, list(fields)))
        return self

    def sadd(self, key, member):
        self.ops.append(('sadd', key, member))
        return self

    def execute(self):
        results = [self.store.apply(op) for op in self.ops]
        self.ops = []
        return results


class FakeValkey(object):
    def __init__(self, store):
        self.store = store

    def pipeline(self, transaction=False):
        return FakePipe(self.store)

    def hset(self, key, mapping=None):
        return self.store.apply(('hset', key, dict(mapping or {})))

    def delete(self, key):
        self.store.hashes.pop(key, None)


class FakeAdapter(object):
    def __init__(self, rows):
        self._rows = rows

    def connect(self):
        pass

    def fetch_iter(self, table, pk, lo, hi, batch=None):
        for r in self._rows:
            yield r


def run_load(store, rows, spec, table='orders', pk='id', key_prefix='orders:'):
    """Drive load_range() with fakes injected for the source and the cache."""
    saved_adapters = bulk_loader.ADAPTERS
    saved_target = bulk_loader.target_client
    bulk_loader.ADAPTERS = {'mysql': lambda cfg: FakeAdapter(rows)}
    bulk_loader.target_client = lambda cfg: FakeValkey(store)
    try:
        args = ('mysql', {}, {}, table, pk, key_prefix, 1, 10, spec, table, 0, '')
        return bulk_loader.load_range(args)
    finally:
        bulk_loader.ADAPTERS = saved_adapters
        bulk_loader.target_client = saved_target


ORDERS_SPEC = {'tables': {'orders': {'denormalize': [{
    'parent_table': 'customers', 'join_field': 'customer_id',
    'fields': ['name', 'tier'], 'prefix': 'cust_',
    'parent_key_prefix': 'customers:'}]}}}


# ── helper tests (must match the Go sink) ─────────────────────────────────────

class HelperTest(unittest.TestCase):
    def test_idx_key_format(self):
        self.assertEqual(transforms.idx_key('orders', 'customers', '7'),
                         'idx:orders:by:customers:7')

    def test_embed_prefixes_and_fills_missing(self):
        rule = {'fields': ['name', 'tier'], 'prefix': 'cust_'}
        out = transforms.embed_parent_fields(rule, {'name': 'Acme'})
        self.assertEqual(out, {'cust_name': 'Acme', 'cust_tier': ''})

    def test_parent_key_prefix_default_is_bare(self):
        self.assertEqual(
            transforms.parent_key_prefix({'parent_table': 'Inventory.customers'}),
            'customers:')

    def test_parent_key_prefix_explicit_wins(self):
        self.assertEqual(
            transforms.parent_key_prefix(
                {'parent_table': 'customers', 'parent_key_prefix': 'c:'}),
            'c:')

    def test_denorm_for_only_returns_child_rules(self):
        self.assertEqual(len(transforms.denorm_for(ORDERS_SPEC, 'orders')), 1)
        self.assertEqual(transforms.denorm_for(ORDERS_SPEC, 'customers'), [])
        self.assertEqual(transforms.denorm_for(None, 'orders'), [])


# ── load_range end to end ─────────────────────────────────────────────────────

class LoadRangeDenormTest(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        # Parent already materialised (parents load first).
        self.store.hashes['customers:7'] = {'name': 'Acme', 'tier': 'gold'}
        self.rows = [
            {'id': 1, 'customer_id': 7, 'total': 10},
            {'id': 2, 'customer_id': 7, 'total': 20},
            {'id': 3, 'customer_id': 99, 'total': 30},   # parent not in cache
        ]

    def test_child_embeds_parent_fields(self):
        run_load(self.store, self.rows, ORDERS_SPEC)
        o1 = self.store.hashes['orders:1']
        self.assertEqual(o1['cust_name'], 'Acme')
        self.assertEqual(o1['cust_tier'], 'gold')
        # Original columns are preserved alongside the embedded ones.
        self.assertEqual(o1['total'], '10')
        self.assertEqual(o1['customer_id'], '7')

    def test_missing_parent_embeds_empty(self):
        run_load(self.store, self.rows, ORDERS_SPEC)
        o3 = self.store.hashes['orders:3']
        self.assertEqual(o3['cust_name'], '')
        self.assertEqual(o3['cust_tier'], '')

    def test_reverse_index_is_built(self):
        run_load(self.store, self.rows, ORDERS_SPEC)
        self.assertEqual(self.store.sets['idx:orders:by:customers:7'], {'1', '2'})
        self.assertEqual(self.store.sets['idx:orders:by:customers:99'], {'3'})

    def test_default_prefix_still_finds_parent(self):
        # Same rule with parent_key_prefix omitted: the helper default 'customers:'
        # must resolve to the same parent key.
        spec = {'tables': {'orders': {'denormalize': [{
            'parent_table': 'customers', 'join_field': 'customer_id',
            'fields': ['name'], 'prefix': 'cust_'}]}}}
        run_load(self.store, self.rows, spec)
        self.assertEqual(self.store.hashes['orders:1']['cust_name'], 'Acme')

    def test_written_count(self):
        self.assertEqual(run_load(self.store, self.rows, ORDERS_SPEC), 3)

    def test_no_denorm_fast_path_unchanged(self):
        # A table with no denorm rules writes plain hashes and no index.
        run_load(self.store, self.rows, {'tables': {}})
        self.assertEqual(self.store.hashes['orders:1'],
                         {'id': '1', 'customer_id': '7', 'total': '10'})
        self.assertFalse(any(k.startswith('idx:') for k in self.store.sets))

    def test_filtered_child_is_skipped(self):
        spec = {'tables': {'orders': {
            'filter': {'field': 'total', 'op': '>', 'value': '15'},
            'denormalize': ORDERS_SPEC['tables']['orders']['denormalize']}}}
        run_load(self.store, self.rows, spec)
        # order 1 (total 10) filtered out; not written, not indexed.
        self.assertNotIn('orders:1', self.store.hashes)
        self.assertEqual(self.store.sets.get('idx:orders:by:customers:7'), {'2'})


if __name__ == '__main__':
    unittest.main()
