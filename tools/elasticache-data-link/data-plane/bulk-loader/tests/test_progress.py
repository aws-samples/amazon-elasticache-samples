"""Jira story F060: one progress hash per link and table, and resume only the same run.

Every link and table on a cache used to share one progress hash
(ecdl:metadata:bulkload), so a load could resume from another link's or table's
progress and skip chunks it never loaded. The hash is now
<PROGRESS_KEY_BASE>:<key prefix> (ecdl:<link>:bulkload:<table>), and its header
records the run's identity (table, key prefix, transform-spec hash, key range,
chunk count, replace mode, chunk and key columns); a run resumes only when all
of it matches. Against a real Valkey, standalone and cluster.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bulk_loader  # noqa: E402
from tests.fake_source import (FakeSource, clean, real_valkey_modules,  # noqa: E402
                               run_main, target_from_env)

N = 3000          # rows; two chunks: 1..1500 and 1501..3000


def _orders():
    return {'pk': ['id'], 'columns': [('id', 'int'), ('who', 'varchar')],
            'rows': [{'id': i, 'who': 'w%d' % i} for i in range(1, N + 1)]}


class _Progress(object):
    CLUSTER = False
    ENV = 'ECDL_TEST_VALKEY'

    def setUp(self):
        self.target = target_from_env(self, self.ENV, self.CLUSTER)
        modules = real_valkey_modules()
        modules.__enter__()
        self.addCleanup(modules.__exit__, None, None, None)
        self.vk = bulk_loader.target_client(self.target)
        self.addCleanup(self.vk.close)
        self.addCleanup(clean, self.vk, 'pt_*', 'a:pt_*', 'b:pt_*', 'ecdl:pt_*',
                        'ecdl:bulkload:pt_*')
        self.addCleanup(os.environ.pop, 'LOADER_STACK_ID', None)
        clean(self.vk, 'pt_*', 'a:pt_*', 'b:pt_*', 'ecdl:pt_*', 'ecdl:bulkload:pt_*')
        FakeSource.reset({'public.pt_orders': _orders(), 'public.pt_items': _orders()})
        saved = bulk_loader.LOAD_RETRIES
        self.addCleanup(setattr, bulk_loader, 'LOAD_RETRIES', saved)

    def load(self, link, key_prefix=None, table='pt_orders', **kw):
        env = dict({'PROGRESS_KEY_BASE': 'ecdl:pt_{}:bulkload'.format(link),
                    'PROGRESS_KEY': None}, **kw.pop('env', {}))
        return run_main(self.target, table, key_prefix=key_prefix, workers=2,
                        env=env, **kw)

    def fail_second_chunk(self, link, key_prefix=None, **kw):
        """A run whose second chunk fails for good after 1,000 confirmed rows."""
        FakeSource.fail = {'lo': 1501, 'fetch': 1200}
        bulk_loader.LOAD_RETRIES = 0
        # One worker, so chunk 0 has finished before chunk 1 fails: a failed
        # chunk stops the chunks still running (Jira story F079).
        import concurrent.futures
        with self.assertRaises(ConnectionError):
            self.load(link, key_prefix,
                      pool=lambda max_workers: concurrent.futures.ThreadPoolExecutor(1), **kw)
        bulk_loader.LOAD_RETRIES = 5
        FakeSource.fetches = []

    def progress(self, key):
        return {k.decode(): v.decode() for k, v in self.vk.hgetall(key).items()}

    def keys(self, pattern):
        return [k.decode() for k in self.vk.scan_iter(match=pattern, count=1000)]

    # ── the key ──────────────────────────────────────────────────────────────

    def test_the_key_names_the_link_and_the_table(self):
        self.vk.delete('ecdl:metadata:bulkload')     # the old shared hash
        out = self.load('a')
        self.assertIn('progress=ecdl:pt_a:bulkload:pt_orders', out)
        head = self.progress('ecdl:pt_a:bulkload:pt_orders')
        self.assertEqual(head['table'], 'pt_orders')
        self.assertIn('completed_at_ms', head)
        self.assertEqual(self.keys('ecdl:metadata:bulkload'), [])

    def test_the_link_key_prefix_is_part_of_the_key(self):
        self.load('a', key_prefix='v9:pt_orders:')
        self.assertTrue(self.progress('ecdl:pt_a:bulkload:v9:pt_orders'))
        clean(self.vk, 'v9:pt_*', 'ecdl:pt_a:bulkload:v9:*')

    def test_the_header_records_the_run_identity(self):
        spec = {'tables': {'pt_orders': {'rename': {'who': 'owner'}}}}
        self.load('a', spec=spec, extra=['--replace'])
        head = self.progress('ecdl:pt_a:bulkload:pt_orders')
        self.assertEqual({k: head[k] for k in (
            'table', 'key_prefix', 'spec_hash', 'pk_lo', 'pk_hi', 'workers', 'replace',
            'chunk_column', 'key_columns', 'scope', 'stack', 'chunks')},
            {'table': 'pt_orders', 'key_prefix': 'pt_orders:',
             'spec_hash': bulk_loader.spec_hash(spec), 'pk_lo': '1', 'pk_hi': str(N),
             'workers': '2', 'replace': '1', 'chunk_column': 'id', 'key_columns': 'id',
             'scope': 'None..None', 'stack': '',
             'chunks': '[[1, 1500], [1501, %d]]' % N})

    def test_progress_key_can_still_be_set_or_turned_off(self):
        self.load('a', env={'PROGRESS_KEY': 'ecdl:pt_custom'})
        self.assertTrue(self.progress('ecdl:pt_custom'))
        clean(self.vk, 'ecdl:pt_*')
        self.load('a', extra=['--progress-key', ''])
        self.assertEqual(self.keys('ecdl:pt_*'), [])

    # ── two links, one table, one cache ──────────────────────────────────────

    def test_two_links_do_not_share_progress(self):
        # Link a stops with chunk 0 done and chunk 1 part-way.
        self.fail_second_chunk('a', key_prefix='a:pt_orders:')
        a_before = self.progress('ecdl:pt_a:bulkload:a:pt_orders')
        self.assertEqual(a_before.get('w0_done'), '1')
        # Link b, same table and cache, loads everything from the first row.
        out = self.load('b', key_prefix='b:pt_orders:')
        self.assertNotIn('resuming', out)
        self.assertEqual(sorted(FakeSource.fetches), [(1, 1500), (1501, N)])
        self.assertEqual(len(self.keys('b:pt_orders:*')), N)
        # Link a's progress is untouched, and a resumes its own run.
        self.assertEqual(self.progress('ecdl:pt_a:bulkload:a:pt_orders'), a_before)
        FakeSource.fetches = []
        out = self.load('a', key_prefix='a:pt_orders:')
        self.assertIn('resuming', out)
        self.assertEqual(FakeSource.fetches, [(2501, N)])
        self.assertEqual(len(self.keys('a:pt_orders:*')), N)

    def test_two_links_without_a_key_prefix_do_not_share_progress(self):
        # Refused by plan as a key conflict, but run by hand it must not
        # resume the other link's load either.
        self.fail_second_chunk('a')
        out = self.load('b')
        self.assertNotIn('resuming', out)
        self.assertEqual(sorted(FakeSource.fetches), [(1, 1500), (1501, N)])

    def test_two_tables_of_one_link_do_not_share_progress(self):
        self.fail_second_chunk('a')
        out = self.load('a', table='pt_items')
        self.assertNotIn('resuming', out)
        self.assertEqual(len(self.keys('pt_items:*')), N)
        self.assertEqual(self.progress('ecdl:pt_a:bulkload:pt_orders').get('w0_done'), '1')

    # ── same run resumes; a different one starts fresh ───────────────────────

    def test_the_same_run_resumes(self):
        spec = {'tables': {'pt_orders': {'rename': {'who': 'owner'}}}}
        self.fail_second_chunk('a', spec=spec)
        out = self.load('a', spec=spec)
        self.assertIn('resuming an incomplete load of pt_orders: 1 of 2', out)
        self.assertEqual(FakeSource.fetches, [(2501, N)])
        self.assertEqual(len(self.keys('pt_orders:*')), N)
        self.assertIn('completed_at_ms', self.progress('ecdl:pt_a:bulkload:pt_orders'))

    def test_a_spec_change_starts_fresh(self):
        self.fail_second_chunk('a')
        spec = {'tables': {'pt_orders': {'rename': {'who': 'owner'}}}}
        out = self.load('a', spec=spec)
        self.assertIn('differs in spec_hash', out)
        self.assertNotIn('resuming', out)
        self.assertEqual(sorted(FakeSource.fetches), [(1, 1500), (1501, N)])
        # Chunk 0, which the first run finished, is written under the new spec.
        self.assertEqual(self.vk.hget('pt_orders:1', 'owner'), b'w1')

    def test_a_replace_mode_change_starts_fresh(self):
        self.fail_second_chunk('a')
        out = self.load('a', extra=['--replace'])
        self.assertIn('differs in replace', out)
        self.assertEqual(sorted(FakeSource.fetches), [(1, 1500), (1501, N)])

    def test_a_key_prefix_change_starts_fresh(self):
        # Same progress key (given explicitly), different keys written.
        env = {'PROGRESS_KEY': 'ecdl:pt_fixed'}
        self.fail_second_chunk('a', env=env)
        out = self.load('a', key_prefix='a:pt_orders:', env=env)
        self.assertIn('differs in key_prefix', out)
        self.assertEqual(len(self.keys('a:pt_orders:*')), N)

    def test_a_finished_run_is_not_resumed(self):
        self.load('a')
        FakeSource.fetches = []
        out = self.load('a')
        self.assertNotIn('resuming', out)
        self.assertEqual(sorted(FakeSource.fetches), [(1, 1500), (1501, N)])

    # ── Jira story F067: the stored chunks are reused ──────────────────────────────────

    def test_rows_inserted_after_a_failure_still_resume(self):
        self.fail_second_chunk('a')
        rows = FakeSource.TABLES['public.pt_orders']['rows']
        rows.extend({'id': i, 'who': 'w%d' % i} for i in range(N + 1, N + 501))
        out = self.load('a')
        self.assertIn('resuming an incomplete load of pt_orders: 1 of 3', out)
        self.assertIn('1 new chunk(s) for keys added since', out)
        self.assertEqual(sorted(FakeSource.fetches), [(2501, N), (N + 1, N + 500)])
        self.assertEqual(len(self.keys('pt_orders:*')), N + 500)
        head = self.progress('ecdl:pt_a:bulkload:pt_orders')
        self.assertEqual(head['chunks'], '[[1, 1500], [1501, %d], [%d, %d]]' % (N, N + 1, N + 500))
        self.assertIn('completed_at_ms', head)

    def test_a_resumed_run_that_fails_again_resumes_with_its_new_chunk(self):
        self.fail_second_chunk('a')
        rows = FakeSource.TABLES['public.pt_orders']['rows']
        rows.extend({'id': i, 'who': 'w'} for i in range(N + 1, N + 1201))
        FakeSource.fail = {'lo': N + 1, 'fetch': 1100}
        bulk_loader.LOAD_RETRIES = 0
        import concurrent.futures
        with self.assertRaises(ConnectionError):
            self.load('a', pool=lambda max_workers: concurrent.futures.ThreadPoolExecutor(1))
        bulk_loader.LOAD_RETRIES = 5
        FakeSource.fetches = []
        out = self.load('a')
        self.assertIn('2 of 3 chunk(s) already done', out)
        self.assertEqual(FakeSource.fetches, [(N + 1001, N + 1200)])
        self.assertEqual(len(self.keys('pt_orders:*')), N + 1200)

    def test_another_worker_count_still_resumes(self):
        self.fail_second_chunk('a')
        out = run_main(self.target, 'pt_orders', workers=5,
                       env={'PROGRESS_KEY_BASE': 'ecdl:pt_a:bulkload', 'PROGRESS_KEY': None})
        self.assertIn('resuming an incomplete load of pt_orders: 1 of 2', out)
        self.assertEqual(FakeSource.fetches, [(2501, N)])

    # ── Jira story F137: a link created again under the same name ─────────────────────

    def test_a_new_loader_stack_does_not_resume_the_old_ones_load(self):
        self.fail_second_chunk('a', env={'LOADER_STACK_ID': 'arn:aws:cloudformation:x:stack/old'})
        out = self.load('a', env={'LOADER_STACK_ID': 'arn:aws:cloudformation:x:stack/new'})
        self.assertIn('differs in stack', out)
        self.assertNotIn('resuming', out)
        self.assertEqual(sorted(FakeSource.fetches), [(1, 1500), (1501, N)])

    def test_the_same_loader_stack_resumes(self):
        env = {'LOADER_STACK_ID': 'arn:aws:cloudformation:x:stack/same'}
        self.fail_second_chunk('a', env=env)
        out = self.load('a', env=env)
        self.assertIn('resuming', out)

    def test_existing_keys_of_a_fresh_load_are_named(self):
        for i in range(1, N + 1):
            self.vk.hset('pt_orders:%d' % i, mapping={'ghost': '1'})
        out = self.load('a')
        self.assertIn('100% of the sampled rows already have keys under pt_orders:', out)
        self.assertIn('fields it does not write stay', out)
        clean(self.vk, 'pt_*')
        out = self.load('a')
        self.assertNotIn('already have keys', out)

    # ── Jira story F073: sparse keys split by quantile ────────────────────────────────

    def test_sparse_keys_give_every_worker_rows(self):
        rows = ([{'id': i, 'who': 'a'} for i in range(1, 1001)]
                + [{'id': 10 ** 9 + i, 'who': 'b'} for i in range(1000)])
        FakeSource.reset({'public.pt_sparse': {'pk': ['id'], 'columns': _orders()['columns'],
                                               'rows': rows}})
        out = self.load('a', table='pt_sparse')
        self.assertIn('split by quantiles of id', out)
        head = self.progress('ecdl:pt_a:bulkload:pt_sparse')
        self.assertEqual(head['w0_rows'], '1000')
        self.assertEqual(head['w1_rows'], '1000')
        self.assertEqual(len(self.keys('pt_sparse:*')), 2000)

    def test_fresh_ignores_a_resumable_run(self):
        self.fail_second_chunk('a')
        out = self.load('a', extra=['--fresh'])
        self.assertNotIn('resuming', out)
        self.assertEqual(sorted(FakeSource.fetches), [(1, 1500), (1501, N)])


class ProgressStandalone(_Progress, unittest.TestCase):
    pass


class ProgressCluster(_Progress, unittest.TestCase):
    CLUSTER = True
    ENV = 'ECDL_TEST_VALKEY_CLUSTER'


if __name__ == '__main__':
    unittest.main()
