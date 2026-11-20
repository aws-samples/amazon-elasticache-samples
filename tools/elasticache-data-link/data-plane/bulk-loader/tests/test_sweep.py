"""Jira story F072: a replacing load removes the keys of rows the source no longer has.

Reloads merged and never swept: fields a transform removed, rows it now drops
and rows deleted at the source stayed in the cache. The deployer now loads
with REPLACE_EXISTING=true, and after a complete replacing load every hash
under the table's prefix with a version older than the load's is deleted.
Against a real Valkey, standalone and cluster.
"""
import concurrent.futures
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bulk_loader  # noqa: E402
from tests.fake_source import (FakeSource, clean, real_valkey_modules,  # noqa: E402
                               run_main, target_from_env)

V_OLD, V_LOAD, V_NEW = '1000-0', '5000-0', '9000-0'


class _Sweep(object):
    CLUSTER = False
    ENV = 'ECDL_TEST_VALKEY'

    def setUp(self):
        self.target = target_from_env(self, self.ENV, self.CLUSTER)
        modules = real_valkey_modules()
        modules.__enter__()
        self.addCleanup(modules.__exit__, None, None, None)
        self.vk = bulk_loader.target_client(self.target)
        self.addCleanup(self.vk.close)
        patterns = ('sw_*', '{sw_*', 'idx:sw_*', 'ecdl:sw*', 'ecdl:bulkload:sw*')
        clean(self.vk, *patterns)
        self.addCleanup(clean, self.vk, *patterns)
        rows = [{'id': i, 'v': 'r%d' % i} for i in range(1, 2001)]
        FakeSource.reset({'public.sw_t': {'pk': ['id'], 'columns': [('id', 'int'), ('v', 'text')],
                                          'rows': rows}})

    def load(self, version=V_LOAD, extra=('--replace',), **kw):
        env = dict({'LOAD_VERSION': version, 'PROGRESS_KEY_BASE': 'ecdl:sw:bulkload',
                    'PROGRESS_KEY': None}, **kw.pop('env', {}))
        return run_main(self.target, 'sw_t', workers=2, extra=list(extra), env=env, **kw)

    def ghosts(self):
        """Keys an earlier load left: rows 5001..5100 no longer in the source."""
        pipe = self.vk.pipeline(transaction=False)
        for i in range(5001, 5101):
            pipe.hset('sw_t:%d' % i, mapping={'v': 'gone', '_ecdl_v': V_OLD})
        pipe.execute()

    def keys(self, pattern):
        return sorted(k.decode() for k in self.vk.scan_iter(match=pattern, count=1000))

    def test_keys_of_rows_the_source_no_longer_has_are_swept(self):
        self.ghosts()
        self.vk.hset('sw_t:9999', mapping={'v': 'newer change', '_ecdl_v': V_NEW})
        self.vk.hset('sw_t:7777', mapping={'v': 'no version'})
        self.vk.set('sw_t:string', 'not a hash')
        self.vk.hset('sw_t_archive:5001', mapping={'_ecdl_v': V_OLD})
        self.vk.hset('{sw_t:5002}:ecdl-del', mapping={'_ecdl_v': V_OLD})
        out = self.load()
        self.assertIn('sweep: 100 key(s) under sw_t: older than this load deleted', out)
        self.assertIn('1 key(s) without a version left', out)
        left = self.keys('sw_t:*')
        self.assertEqual(len(left), 2000 + 3)
        for kept in ('sw_t:9999', 'sw_t:7777', 'sw_t:string', 'sw_t:1', 'sw_t:2000'):
            self.assertIn(kept, left)
        self.assertNotIn('sw_t:5001', left)
        self.assertTrue(self.vk.exists('sw_t_archive:5001'), 'another table')
        self.assertTrue(self.vk.exists('{sw_t:5002}:ecdl-del'), 'a tombstone')

    def test_a_merging_load_does_not_sweep(self):
        self.ghosts()
        out = self.load(extra=())
        self.assertNotIn('sweep', out)
        self.assertEqual(len(self.keys('sw_t:*')), 2100)

    def test_without_a_version_nothing_is_swept(self):
        self.ghosts()
        out = self.load(version=None)
        self.assertIn('sweep skipped: the rows of this load carry no version', out)
        self.assertEqual(len(self.keys('sw_t:*')), 2100)

    def test_a_partial_key_range_is_not_swept(self):
        self.ghosts()
        out = self.load(extra=('--replace', '--max-pk', '1500'))
        self.assertIn('sweep skipped: the load covered part of the key range', out)
        self.assertEqual(len(self.keys('sw_t:*')), 1500 + 100)

    def test_turned_off(self):
        self.ghosts()
        saved = bulk_loader.SWEEP
        bulk_loader.SWEEP = False
        try:
            out = self.load()
        finally:
            bulk_loader.SWEEP = saved
        self.assertNotIn('sweep', out)
        self.assertEqual(len(self.keys('sw_t:*')), 2100)

    def test_a_resumed_load_keeps_the_rows_of_its_first_run(self):
        # Run 1 (version 5000-0) finishes chunk 0, fails in chunk 1. Run 2
        # (a later reload, version 7000-0) resumes chunk 1. Chunk 0's rows carry
        # 5000-0 and must survive the sweep; the ghosts (1000-0) must not.
        self.ghosts()
        FakeSource.fail = {'lo': 1001, 'fetch': 700}
        bulk_loader.LOAD_RETRIES, saved = 0, bulk_loader.LOAD_RETRIES
        try:
            with self.assertRaises(ConnectionError):
                self.load(pool=lambda max_workers: concurrent.futures.ThreadPoolExecutor(1))
        finally:
            bulk_loader.LOAD_RETRIES = saved
        out = self.load(version='7000-0')
        self.assertIn('resuming', out)
        self.assertIn('sweep: 100 key(s)', out)
        self.assertEqual(self.vk.hget('sw_t:1', '_ecdl_v'), b'5000-0')
        self.assertEqual(self.vk.hget('sw_t:2000', '_ecdl_v'), b'7000-0')
        self.assertEqual(len(self.keys('sw_t:*')), 2000)

    def test_a_swept_child_leaves_its_parents_index(self):
        spec = {'tables': {'sw_t': {'denormalize': [{
            'parent_table': 'sw_p', 'join_field': 'pid', 'fields': ['name']}]}}}
        rows = [{'id': i, 'pid': '1'} for i in range(1, 11)]
        FakeSource.reset({'public.sw_t': {'pk': ['id'], 'columns': [('id', 'int'),
                                                                   ('pid', 'int')],
                                          'rows': rows}})
        self.vk.hset('sw_p:1', mapping={'name': 'P'})
        # A child the source deleted, still indexed under parent 1.
        self.vk.hset('sw_t:99', mapping={'pid': '1', '_ecdl_v': V_OLD})
        import transforms
        rule = spec['tables']['sw_t']['denormalize'][0]
        fk = transforms.fk_field(rule)
        self.vk.hset('sw_t:99', fk, '1')
        idx = transforms.idx_key('sw_t', 'sw_p', '1', '')
        self.vk.sadd(idx, '99')
        out = run_main(self.target, 'sw_t', workers=1, extra=['--replace'], spec=spec,
                       env={'LOAD_VERSION': V_LOAD, 'PROGRESS_KEY': ''})
        self.assertIn('sweep: 1 key(s)', out)
        self.assertFalse(self.vk.exists('sw_t:99'))
        members = {m.decode() for m in self.vk.smembers(idx)}
        self.assertNotIn('99', members)
        self.assertEqual(members, {str(i) for i in range(1, 11)})
        clean(self.vk, 'sw_p*', 'idx:*sw_*')


class SweepStandalone(_Sweep, unittest.TestCase):
    pass


class SweepCluster(_Sweep, unittest.TestCase):
    CLUSTER = True
    ENV = 'ECDL_TEST_VALKEY_CLUSTER'


class GlobTest(unittest.TestCase):
    def test_a_prefix_is_matched_literally(self):
        self.assertEqual(bulk_loader._glob_escape('a*b?[c]\\:'), 'a\\*b\\?\\[c\\]\\\\:')


if __name__ == '__main__':
    unittest.main()
