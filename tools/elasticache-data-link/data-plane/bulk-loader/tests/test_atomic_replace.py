"""Jira story F087: a replacing load rebuilds each row in one atomic script call, and a
source column named like the version field never becomes the row's version.

As DEL then HSET, a sink write between the two found no row, wrote an older
change in full, and the load's HSET merged into it. Against a real Valkey,
standalone and cluster.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bulk_loader  # noqa: E402
from tests.fake_source import (FakeSource, clean, real_valkey_modules,  # noqa: E402
                               run_main, target_from_env)

N = 2500


class _Replace(object):
    CLUSTER = False
    ENV = 'ECDL_TEST_VALKEY'

    def setUp(self):
        self.target = target_from_env(self, self.ENV, self.CLUSTER)
        modules = real_valkey_modules()
        modules.__enter__()
        self.addCleanup(modules.__exit__, None, None, None)
        self.vk = bulk_loader.target_client(self.target)
        self.addCleanup(self.vk.close)
        clean(self.vk, 'f087*', 'ecdl:bulkload:f087*')
        self.addCleanup(clean, self.vk, 'f087*', 'ecdl:bulkload:f087*')

    def table(self, rows, columns=(('id', 'int'), ('v', 'varchar'))):
        FakeSource.reset({'public.f087': {'pk': ['id'], 'columns': list(columns),
                                          'rows': rows}})

    def test_replace_removes_stale_fields_in_one_command_per_row(self):
        self.table([{'id': i, 'v': 'new%d' % i} for i in range(1, N + 1)])
        for i in range(1, N + 1):
            self.vk.hset('f087:%d' % i, mapping={'v': 'old', 'gone': 'x'})
        before = self.commandstats()
        out = run_main(self.target, 'f087', extra=['--replace'],
                       env={'LOAD_VERSION': '7-0'})
        self.assertIn('Loaded 2,500 rows', out)
        after = self.commandstats()
        for i in (1, 1000, 1001, N):
            self.assertEqual(self.vk.hgetall('f087:%d' % i),
                             {b'id': str(i).encode(), b'v': ('new%d' % i).encode(),
                              b'_ecdl_v': b'7-0'})
        if not self.CLUSTER:
            # Commands a script calls count too, so DEL and HSET are not zero.
            self.assertGreaterEqual(after.get('evalsha', 0) - before.get('evalsha', 0), N)

    def test_each_row_is_one_script_call(self):
        class Recorder(object):
            def __init__(self):
                self.calls = []

            def execute_command(self, *args):
                self.calls.append(args)

            def hset(self, *a, **k):
                self.calls.append(('HSET',) + a)

            def delete(self, *a):
                self.calls.append(('DEL',) + a)
        pipe = Recorder()
        bulk_loader.write_row(pipe, 'k', {'a': '1', 'b': '2'}, 'sha1')
        self.assertEqual(pipe.calls, [('EVALSHA', 'sha1', 1, 'k', 'a', '1', 'b', '2')])
        pipe.calls = []
        bulk_loader.write_row(pipe, 'k', {'a': '1'})
        self.assertEqual(pipe.calls, [('HSET', 'k')])

    def test_a_row_wider_than_one_hset_slice(self):
        cols = [('id', 'int')] + [('c%d' % j, 'varchar') for j in range(4100)]
        row = dict({'id': 1}, **{'c%d' % j: 'v%d' % j for j in range(4100)})
        self.table([row], cols)
        self.vk.hset('f087:1', mapping={'stale': '1'})
        run_main(self.target, 'f087', extra=['--replace'], env={'LOAD_VERSION': '7-0'})
        got = self.vk.hgetall('f087:1')
        self.assertEqual(len(got), 4102)         # id, 4100 columns, the version
        self.assertNotIn(b'stale', got)
        self.assertEqual(got[b'c4099'], b'v4099')

    def test_a_flushed_script_cache_is_retried_not_fatal(self):
        # A primary promoted after a failover may lack the script (NOSCRIPT).
        self.table([{'id': i, 'v': 'n'} for i in range(1, N + 1)])
        vk = self.vk
        cluster = self.CLUSTER

        class Flushing(FakeSource):
            flushed = []

            def fetch_iter(self, table, pk, lo, hi, batch=None):
                for i, row in enumerate(FakeSource.fetch_iter(self, table, pk, lo, hi, batch)):
                    if i == 1500 and not Flushing.flushed:
                        Flushing.flushed.append(1)
                        if cluster:
                            for node in vk.get_primaries():
                                node.valkey_connection.script_flush()
                        else:
                            vk.script_flush()
                    yield row
        saved = bulk_loader.ADAPTERS
        out = run_main(self.target, 'f087', workers=1, extra=['--replace'],
                       env={'LOAD_VERSION': '7-0'}, adapter=Flushing)
        bulk_loader.ADAPTERS = saved
        self.assertIn('NoScriptError', out)
        self.assertIn('Loaded 2,500 rows', out)
        self.assertEqual(len(list(self.vk.scan_iter(match='f087:*', count=1000))), N)

    def test_merge_mode_still_merges(self):
        self.table([{'id': 1, 'v': 'new'}])
        self.vk.hset('f087:1', mapping={'kept': 'x'})
        run_main(self.target, 'f087')
        self.assertEqual(self.vk.hget('f087:1', 'kept'), b'x')

    def test_a_source_column_named_like_the_version_field_is_not_the_version(self):
        self.table([{'id': 1, 'v': 'a', '_ecdl_v': '99999999999999-0'}],
                   [('id', 'int'), ('v', 'varchar'), ('_ecdl_v', 'varchar')])
        out = run_main(self.target, 'f087', env={'LOAD_VERSION': '7-0'})
        self.assertEqual(self.vk.hget('f087:1', '_ecdl_v'), b'7-0')
        self.assertIn('column _ecdl_v is not stored', out)
        # Without a load version the column is still not stored.
        self.vk.delete('f087:1')
        run_main(self.target, 'f087', env={'LOAD_VERSION': None})
        self.assertIsNone(self.vk.hget('f087:1', '_ecdl_v'))
        self.assertEqual(self.vk.hget('f087:1', 'v'), b'a')

    def commandstats(self):
        stats = self.vk.info('commandstats')
        return {k[len('cmdstat_'):]: v['calls'] for k, v in stats.items()
                if k.startswith('cmdstat_')}


class ReplaceStandalone(_Replace, unittest.TestCase):
    pass


class ReplaceCluster(_Replace, unittest.TestCase):
    CLUSTER = True
    ENV = 'ECDL_TEST_VALKEY_CLUSTER'


if __name__ == '__main__':
    unittest.main()
