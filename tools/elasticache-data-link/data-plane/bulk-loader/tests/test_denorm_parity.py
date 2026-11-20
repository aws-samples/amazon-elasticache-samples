"""Jira story F198: a loaded child embeds exactly what the sink embeds.

The cases are tools/go-sink/testdata/denorm_cases.json, which the Go sink's
tests also read (denorm_parity_integration_test.go): there the sink writes the
parent and child in both orders, here the loader loads the parent table and
then the child table. Against a real Valkey (ECDL_TEST_VALKEY, and
ECDL_TEST_VALKEY_CLUSTER for a cluster); skipped when unset.
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bulk_loader  # noqa: E402
import transforms  # noqa: E402
from tests.fake_source import (FakeSource, clean, real_valkey_modules,  # noqa: E402
                               run_main, target_from_env)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
CASES = os.path.join(REPO, 'tools', 'go-sink', 'testdata', 'denorm_cases.json')


def read_cases():
    with open(CASES) as handle:
        return json.load(handle)['cases']


def _columns(rows):
    names = []
    for r in rows:
        for k in r:
            if k not in names:
                names.append(k)
    return [(n, 'int' if n in ('id', 'cid') else 'varchar') for n in names]


class SharedCasesShape(unittest.TestCase):
    def test_there_are_cases(self):
        self.assertGreaterEqual(len(read_cases()), 5)

    def test_parent_field_name_follows_the_parent_rename(self):
        spec = {'tables': {'customers': {'rename': {'name': 'full_name'}}}}
        rule = {'parent_table': 'Inventory.customers'}
        self.assertEqual(transforms.parent_field_name(spec, rule, 'name'), 'full_name')
        self.assertEqual(transforms.parent_field_name(spec, rule, 'tier'), 'tier')
        self.assertEqual(transforms.parent_field_name(None, rule, 'name'), 'name')
        self.assertEqual(transforms.parent_field_name({'tables': {}}, rule, 'name'), 'name')


class _Parity(object):
    CLUSTER = False
    ENV = 'ECDL_TEST_VALKEY'

    def setUp(self):
        self.target = target_from_env(self, self.ENV, self.CLUSTER)
        modules = real_valkey_modules()
        modules.__enter__()
        self.addCleanup(modules.__exit__, None, None, None)
        self.vk = bulk_loader.target_client(self.target)
        self.addCleanup(self.vk.close)
        self.patterns = ('customers:*', 'orders:*', 'idx:*', 'v2:*', '{*', 'ecdl:bulkload:*')
        clean(self.vk, *self.patterns)
        self.addCleanup(clean, self.vk, *self.patterns)

    def test_the_loader_embeds_what_the_sink_embeds(self):
        for case in read_cases():
            for link in ('', 'v2'):
                with self.subTest(case=case['name'], prefix=link):
                    clean(self.vk, *self.patterns)
                    FakeSource.reset({
                        'public.customers': {'pk': ['id'], 'columns': _columns([case['parent']]),
                                             'rows': [dict(case['parent'])]},
                        'public.orders': {'pk': ['id'], 'columns': _columns([case['child']]),
                                          'rows': [dict(case['child'])]}})
                    pre = link + ':' if link else ''
                    env = {'LINK_KEY_PREFIX': link, 'LOAD_VERSION': '1000-0'}
                    for table in ('customers', 'orders'):
                        run_main(self.target, table, pk='id', key_prefix=pre + table + ':',
                                 workers=1, env=env, spec=case['spec'])
                    child = {k.decode(): v.decode() for k, v in self.vk.hgetall(
                        '{}orders:{}'.format(pre, case['child']['id'])).items()}
                    for field, want in case['embed'].items():
                        self.assertIn(field, child)
                        self.assertEqual(child[field], want, (field, child))


    def _load_orders(self, rows, spec, extra=(), link=''):
        FakeSource.reset({
            'public.customers': {'pk': ['id'], 'columns': [('id', 'int'), ('name', 'varchar')],
                                 'rows': [{'id': 7, 'name': 'Ann'}, {'id': 8, 'name': 'Bob'}]},
            'public.orders': {'pk': ['id'], 'columns': [('id', 'int'), ('cid', 'int'), ('qty', 'int')],
                              'rows': rows}})
        pre = link + ':' if link else ''
        env = {'LINK_KEY_PREFIX': link, 'LOAD_VERSION': '1000-0'}
        for table in ('customers', 'orders'):
            run_main(self.target, table, pk='id', key_prefix=pre + table + ':', workers=1,
                     env=env, spec=spec, extra=extra)
        return pre

    def members(self, pre, parent):
        return sorted(m.decode() for m in self.vk.smembers(
            '{}idx:orders:by:customers:{}'.format(pre, parent)))

    def test_a_replacing_load_that_drops_a_child_leaves_the_index(self):  # Jira story F082
        rule = {'parent_table': 'customers', 'join_field': 'cid', 'fields': ['name'], 'prefix': 'c_'}
        keep_all = {'tables': {'orders': {'denormalize': [rule]}, 'customers': {}}}
        big_only = {'tables': {'orders': {'denormalize': [rule],
                                          'filter': {'field': 'qty', 'op': '>', 'value': '1'}},
                               'customers': {}}}
        for link in ('', 'v2'):
            with self.subTest(prefix=link):
                clean(self.vk, *self.patterns)
                rows = [{'id': 1, 'cid': 7, 'qty': 5}, {'id': 2, 'cid': 7, 'qty': 1},
                        {'id': 3, 'cid': 8, 'qty': 1}]
                pre = self._load_orders(rows, keep_all, link=link)
                self.assertEqual(self.members(pre, 7), ['1', '2'])
                self.assertEqual(self.members(pre, 8), ['3'])
                # Order 3 moved to customer 7 in the source since the first
                # load, and both 2 and 3 are now filtered out.
                rows[2]['cid'] = 7
                self._load_orders(rows, big_only, extra=['--replace'], link=link)
                for pk in (2, 3):
                    self.assertFalse(self.vk.exists('{}orders:{}'.format(pre, pk)))
                self.assertEqual(self.members(pre, 7), ['1'])
                self.assertEqual(self.members(pre, 8), [], 'the cached row pointed at 8')
                self.assertEqual(self.vk.hget(pre + 'orders:1', 'c_name'), b'Ann')

    def test_a_merging_load_keeps_filtered_rows_and_their_index(self):  # Jira story F082
        # Without --replace a filtered-out row is skipped, not deleted, so its
        # index entry must stay with it.
        rule = {'parent_table': 'customers', 'join_field': 'cid', 'fields': ['name'], 'prefix': 'c_'}
        rows = [{'id': 1, 'cid': 7, 'qty': 5}, {'id': 2, 'cid': 7, 'qty': 1}]
        pre = self._load_orders(rows, {'tables': {'orders': {'denormalize': [rule]}, 'customers': {}}})
        self._load_orders(rows, {'tables': {'orders': {
            'denormalize': [rule], 'filter': {'field': 'qty', 'op': '>', 'value': '1'}}, 'customers': {}}})
        self.assertTrue(self.vk.exists(pre + 'orders:2'))
        self.assertEqual(self.members(pre, 7), ['1', '2'])


class ParityStandalone(_Parity, unittest.TestCase):
    pass


class ParityCluster(_Parity, unittest.TestCase):
    CLUSTER = True
    ENV = 'ECDL_TEST_VALKEY_CLUSTER'


if __name__ == '__main__':
    unittest.main()
