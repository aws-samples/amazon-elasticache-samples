"""verify.py: the sampled check of the cache against the source (review
finding C1). Pure functions first, then whole runs over FakeSource against a
real Valkey, standalone and cluster: a table loaded by the loader, then broken
in each way the check must find (and the ways it must not report).
"""
import contextlib
import io
import json
import os
import random
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bulk_loader  # noqa: E402
import verify  # noqa: E402
from tests.fake_source import (FakeSource, clean, real_valkey_modules,  # noqa: E402
                               run_main, target_from_env)


class JudgeTest(unittest.TestCase):
    def test_a_row_the_filter_drops_must_be_absent(self):
        self.assertEqual(verify.judge(None, {}), 'ok')
        self.assertEqual(verify.judge(None, {'v': '1'}), 'extra')

    def test_a_kept_row(self):
        self.assertEqual(verify.judge({'v': '1'}, {}), 'missing')
        self.assertEqual(verify.judge({'v': '1'}, {'v': '1', 'parent_x': 'y', '_ecdl_v': '7-0'}), 'ok')
        self.assertEqual(verify.judge({'v': '1'}, {'v': '2'}), 'different')
        self.assertEqual(verify.judge({'v': '1', 'j': 'x'}, {'v': '1', 'j': 'y'}, skip={'j'}), 'ok')

    def test_keys_split_back_into_values(self):
        for values in (['a:b', 'c'], ['a', 'b:c'], ['a\\', ':c'], ['1', '2', '3']):
            encoded = dict(('k%d' % i, v) for i, v in enumerate(values))
            suffix = bulk_loader.sink_pk(encoded, list(encoded))
            self.assertEqual(verify.split_key(suffix, len(values)), values, suffix)
        self.assertEqual(verify.split_key('a:b', 1), ['a:b'])
        self.assertIsNone(verify.split_key('a:b:c', 2))

    def test_the_plan_follows_the_loader_stack(self):
        env = {'TABLES': 'orders,lines', 'SOURCE_TABLES': 'Shop.orders,Shop.lines',
               'PRIMARY_KEYS': ',line_no', 'LINK_KEY_PREFIX': 'v2'}
        self.assertEqual(verify.table_plan(env), [
            ('orders', 'Shop.orders', '', 'v2:orders:'),
            ('lines', 'Shop.lines', 'line_no', 'v2:lines:')])
        self.assertEqual(verify.table_plan({'TABLES': 'orders'}), [('orders', 'orders', '', 'orders:')])


class FromTheStart(random.Random):
    """Every window and every SCAN starts at the beginning, so a test sees
    every row; the real check starts each at a random point."""
    def getrandbits(self, k):
        return 0


class _Runs(object):
    CLUSTER = False
    ENV = 'ECDL_TEST_VALKEY'
    N = 300

    def setUp(self):
        self.target = target_from_env(self, self.ENV, self.CLUSTER)
        modules = real_valkey_modules()
        modules.__enter__()
        self.addCleanup(modules.__exit__, None, None, None)
        self.vk = bulk_loader.target_client(self.target)
        self.addCleanup(self.vk.close)
        clean(self.vk, 'c1v*', 'ecdl:c1:*')
        self.addCleanup(clean, self.vk, 'c1v*', 'ecdl:c1:*')
        self.rows = [{'id': i, 'status': 'active' if i % 10 else 'gone', 'v': 'v%d' % i}
                     for i in range(1, self.N + 1)]
        FakeSource.reset({'public.c1v': {
            'pk': ['id'], 'columns': [('id', 'int'), ('status', 'varchar'), ('v', 'varchar')],
            'rows': self.rows}})
        self.spec = {'tables': {'c1v': {'filter': {'field': 'status', 'op': '=', 'value': 'active'}}}}
        run_main(self.target, 'c1v', spec=self.spec,
                 env={'PROGRESS_KEY_BASE': 'ecdl:c1:bulkload', 'LOAD_VERSION': '5-0'})
        # Every row is sampled, so each break below is certainly found.
        self.saved = (verify.SOURCE_ROWS, verify.CACHE_KEYS, verify.RECHECK)
        verify.SOURCE_ROWS, verify.CACHE_KEYS, verify.RECHECK = 10 * self.N, 10 * self.N, [0, 0]
        self.addCleanup(self.restore)

    def restore(self):
        verify.SOURCE_ROWS, verify.CACHE_KEYS, verify.RECHECK = self.saved

    def run_verify(self, sleep=lambda s: None):
        env = {'SOURCE_ENGINE': 'mysql', 'TABLES': 'c1v', 'SOURCE_TABLES': 'public.c1v',
               'PROGRESS_KEY_BASE': 'ecdl:c1:bulkload', 'SOURCE_TLS': 'false',
               'TRANSFORMS_JSON': json.dumps(self.spec)}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = verify.main(env=env, adapters={'mysql': FakeSource}, target=self.vk, sleep=sleep,
                               rng=FromTheStart())
        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        tables = [json.loads(l) for l in lines if l.startswith('{') and '"table"' in l]
        return (tables[0] if tables else None), out.getvalue()

    def test_a_correct_cache_reports_nothing(self):
        found, out = self.run_verify()
        self.assertEqual((found['missing'], found['different'], found['extra']), (0, 0, 0), out)
        self.assertGreaterEqual(found['checked'], self.N)
        self.assertIn('sampled row(s) match', out)
        # Rows the filter drops are not in the cache, and that is not an error.
        self.assertFalse(self.vk.exists('c1v:10'))

    def test_each_kind_of_wrong_row_is_found(self):
        self.vk.delete('c1v:7')                                  # lost from the cache
        self.vk.hset('c1v:8', 'v', 'stale')                      # wrong value
        self.vk.hset('c1v:20', mapping={'v': 'x'})               # filtered row present
        self.vk.hset('c1v:99999', mapping={'v': 'x'})            # deleted from the source
        found, out = self.run_verify()
        self.assertEqual((found['missing'], found['different'], found['extra']), (1, 1, 2), out)
        self.assertIn('ERROR verify: table c1v', out)
        self.assertIn('c1v:8 different (v)', out)

    def test_a_row_fixed_before_the_recheck_is_not_reported(self):
        self.vk.delete('c1v:7')
        verify.RECHECK = [1]
        fixed = []

        def sink_catches_up(_seconds):
            self.vk.hset('c1v:7', mapping={'id': '7', 'status': 'active', 'v': 'v7'})
            fixed.append(True)
        found, out = self.run_verify(sleep=sink_catches_up)
        self.assertTrue(fixed)
        self.assertEqual(found['missing'], 0, out)

    def test_a_running_load_is_skipped(self):
        self.vk.hset('ecdl:c1:bulkload:c1v', mapping={'started_at_ms': '9999999999999'})
        self.vk.delete('c1v:7')
        found, out = self.run_verify()
        self.assertEqual(found.get('skipped'), 'load running or not finished', out)
        self.assertEqual(found['missing'], 0)


class StandaloneRuns(_Runs, unittest.TestCase):
    pass


class ClusterRuns(_Runs, unittest.TestCase):
    CLUSTER = True
    ENV = 'ECDL_TEST_VALKEY_CLUSTER'


class Rows(object):
    """An adapter whose every query returns `rows`, or raises for `fail` SQL."""
    def __init__(self, rows, fail=None):
        self.rows, self.fail, self.sql = rows, fail, []

    def query_rows(self, sql, params):
        self.sql.append(sql)
        if self.fail and self.fail in sql:
            raise RuntimeError('column "wal_status" does not exist')
        return self.rows


class SourceHealthTest(unittest.TestCase):
    """Review findings H6 (PostgreSQL slot) and H11 (SQL Server capture job)."""
    PG = {'PG_SLOT_NAME': 'ecdl_shop'}

    def test_pg_slot_lag_is_reported(self):
        rec, problem = verify.source_health(Rows([{'wal_status': 'reserved', 'active': True,
                                                   'safe_wal_size': 1000, 'lag_bytes': 4096}]),
                                            'postgresql', self.PG)
        self.assertEqual((rec['slot_lag_bytes'], rec['wal_status'], rec['slot_active']), (4096, 'reserved', 1))
        self.assertIsNone(problem)

    def test_pg_lost_unreserved_and_missing_slots_are_problems(self):
        for rows, words in (([{'wal_status': 'lost', 'lag_bytes': 9}], 'invalidated'),
                            ([{'wal_status': 'unreserved', 'lag_bytes': 9}], 'max_slot_wal_keep_size'),
                            ([], 'does not exist')):
            _, problem = verify.source_health(Rows(rows), 'postgresql', self.PG)
            self.assertIn(words, problem or '')

    def test_pg_12_has_no_wal_status(self):
        rows = Rows([{'active': False, 'lag_bytes': 10}], fail='wal_status')
        rec, problem = verify.source_health(rows, 'postgresql', self.PG)
        self.assertEqual(rec['slot_lag_bytes'], 10)
        self.assertEqual(len(rows.sql), 2)
        self.assertIsNone(problem)

    def test_sqlserver_capture_age(self):
        self.assertIsNone(verify.source_health(Rows([{'age': 200}]), 'sqlserver', {})[1])
        rec, problem = verify.source_health(Rows([{'age': 3600}]), 'sqlserver', {})
        self.assertEqual(rec['capture_age_seconds'], 3600)
        self.assertIn('capture job is probably stopped', problem)
        self.assertIn('SQL Server Agent', verify.source_health(Rows([{'age': None}]), 'sqlserver', {})[1])

    def test_other_engines_and_no_slot_report_nothing(self):
        self.assertEqual(verify.source_health(Rows([]), 'mysql', {}), (None, None))
        self.assertEqual(verify.source_health(Rows([]), 'postgresql', {}), (None, None))


class SamplingTest(unittest.TestCase):
    def test_windows_start_at_random_keys_and_stay_small(self):
        rows = [{'id': i, 'v': str(i)} for i in range(1, 100001, 7)]
        FakeSource.reset({'public.big': {'pk': ['id'], 'columns': [('id', 'int'), ('v', 'varchar')],
                                         'rows': rows}})
        check = verify.TableCheck(FakeSource({}), None, 'mysql', 'big', 'big', 'big:', None,
                                  rng=random.Random(3))
        with contextlib.redirect_stdout(io.StringIO()):
            check.prepare()
        got = check.source_sample()
        self.assertLessEqual(len(got), verify.SOURCE_ROWS)
        self.assertGreater(len(got), verify.SOURCE_ROWS // 2)
        self.assertNotEqual(got[0]['id'], 1, 'starts at a random key, not the first')


if __name__ == '__main__':
    unittest.main()
