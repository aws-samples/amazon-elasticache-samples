"""The loader retries a failed chunk from its last confirmed key, and a new run
resumes an incomplete previous one instead of starting from row one."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bulk_loader  # noqa: E402
from tests import test_denorm as fakes  # noqa: E402


class FlakyAdapter(object):
    """Rows 1..n; the first `fail_times` reads fail after `fail_after` rows.
    Honours `lo`, so a retry that resumes is visible in `starts`."""

    def __init__(self, n, fail_after, fail_times, starts, exc=ConnectionError):
        self.n, self.fail_after = n, fail_after
        self.state = {'left': fail_times}
        self.starts, self.exc = starts, exc

    def connect(self):
        pass

    def fetch_iter(self, table, pk, lo, hi, batch=None):
        self.starts.append(lo)
        for i, pk_value in enumerate(range(lo, min(hi, self.n) + 1)):
            if self.state['left'] and i == self.fail_after:
                self.state['left'] -= 1
                raise self.exc('connection reset by peer')
            yield {'id': pk_value, 'v': str(pk_value)}


def run(adapter, n_hi):
    store = fakes.FakeStore()
    saved = (bulk_loader.ADAPTERS, bulk_loader.target_client, bulk_loader._sleep)
    shared = adapter
    bulk_loader.ADAPTERS = {'mysql': lambda cfg: shared}
    bulk_loader.target_client = lambda cfg: fakes.FakeValkey(store)
    bulk_loader._sleep = lambda s: None
    try:
        args = ('mysql', {}, {}, 't', 'id', 't:', 1, n_hi, None, 't', 0, '')
        return bulk_loader.load_range(args), store
    finally:
        bulk_loader.ADAPTERS, bulk_loader.target_client, bulk_loader._sleep = saved


class RetryTest(unittest.TestCase):
    def test_transient_failure_resumes_after_the_confirmed_key(self):
        starts = []
        # 2500 rows; the first read fails at row 1500 (row 1000 was confirmed by
        # the first 1000-row pipeline).
        written, store = run(FlakyAdapter(2500, 1500, 1, starts), 2500)
        self.assertEqual(starts, [1, 1001])
        self.assertEqual(len(store.hashes), 2500)
        self.assertEqual(written, 2500, 'rows must not be double-counted')

    def test_gives_up_after_the_retry_budget(self):
        starts = []
        with self.assertRaises(ConnectionError):
            run(FlakyAdapter(100, 10, 99, starts), 100)
        self.assertEqual(len(starts), bulk_loader.LOAD_RETRIES + 1)

    def test_failures_with_progress_between_them_do_not_use_up_the_budget(self):
        # Jira story F066: every attempt confirms 1000 rows before it fails, eight times
        # in all. Counting every failure gave up after five.
        starts = []
        written, store = run(FlakyAdapter(10000, 1500, 8, starts), 10000)
        self.assertEqual(starts, [1 + 1000 * i for i in range(9)])
        self.assertEqual(len(store.hashes), 10000)
        self.assertEqual(written, 10000)

    def test_failures_in_a_row_without_progress_still_give_up(self):
        # Progress first (1000 rows), then failures before any batch confirms.
        class ProgressThenStuck(FlakyAdapter):
            def fetch_iter(self, table, pk, lo, hi, batch=None):
                self.starts.append(lo)
                stop = 1500 if len(self.starts) == 1 else 10
                for i, pk_value in enumerate(range(lo, min(hi, self.n) + 1)):
                    if i == stop:
                        raise ConnectionError('connection reset by peer')
                    yield {'id': pk_value, 'v': str(pk_value)}
        starts = []
        with self.assertRaises(ConnectionError):
            run(ProgressThenStuck(5000, 0, 0, starts), 5000)
        # One attempt with progress, then LOAD_RETRIES in a row without.
        self.assertEqual(len(starts), 1 + bulk_loader.LOAD_RETRIES)
        self.assertEqual(starts[1:], [1001] * bulk_loader.LOAD_RETRIES)

    def test_the_total_cap_stops_a_chunk_that_keeps_failing(self):
        starts = []
        saved = bulk_loader.LOAD_RETRIES_TOTAL
        bulk_loader.LOAD_RETRIES_TOTAL = 3
        try:
            with self.assertRaises(ConnectionError):
                run(FlakyAdapter(10000, 1500, 8, starts), 10000)
        finally:
            bulk_loader.LOAD_RETRIES_TOTAL = saved
        self.assertEqual(len(starts), 4, 'three retries, then the fourth failure stops it')

    def test_the_backoff_starts_again_after_progress(self):
        delays = []
        saved = bulk_loader._sleep
        try:
            store = fakes.FakeStore()
            adapter = FlakyAdapter(4000, 1500, 3, [])
            sv = (bulk_loader.ADAPTERS, bulk_loader.target_client)
            bulk_loader.ADAPTERS = {'mysql': lambda cfg: adapter}
            bulk_loader.target_client = lambda cfg: fakes.FakeValkey(store)
            bulk_loader._sleep = delays.append
            bulk_loader.load_range(('mysql', {}, {}, 't', 'id', 't:', 1, 4000, None, 't', 0, ''))
        finally:
            bulk_loader.ADAPTERS, bulk_loader.target_client = sv
            bulk_loader._sleep = saved
        self.assertEqual(delays, [bulk_loader.LOAD_BACKOFF_SEC] * 3)

    def test_permanent_errors_are_not_retried(self):
        starts = []

        class Missing(Exception):
            pass

        def boom(msg):
            return Missing("(1054, \"Unknown column 'id' in 'field list'\")")
        with self.assertRaises(Missing):
            run(FlakyAdapter(100, 0, 1, starts, exc=boom), 100)
        self.assertEqual(starts, [1])


class KeyOrderTest(unittest.TestCase):
    """Resuming from a confirmed key is only safe when rows arrive in key order."""

    def test_every_engine_orders_the_range_by_the_key(self):
        for engine, cls in bulk_loader.ADAPTERS.items():
            sql = cls({})._sql(cls.RANGE_SQL, 'orders', 'id')
            self.assertRegex(sql, r'ORDER BY \W?id\W?$', engine)

    def test_out_of_order_rows_stop_the_chunk_instead_of_skipping(self):
        # A scan that returns 2001..2500 before 1..2000, then fails. Resuming
        # after confirmed key 2500 would have skipped 1..2000; it must fail.
        class Unordered(FlakyAdapter):
            def fetch_iter(self, table, pk, lo, hi, batch=None):
                self.starts.append(lo)
                for pk_value in list(range(2001, 2501)) + list(range(1, 2001)):
                    yield {'id': pk_value, 'v': str(pk_value)}
        with self.assertRaises(ValueError) as caught:
            run(Unordered(2500, 0, 0, []), 2500)
        self.assertIn('out of key order', str(caught.exception))


class ResumePlanTest(unittest.TestCase):
    ID = bulk_loader.run_identity('orders', 'orders:', None, False, 'id', ['id'],
                                  scope='None..None', stack='arn:stack/1')
    CHUNKS = '[[1, 250], [251, 500], [501, 750], [751, 1000]]'

    def test_incomplete_run_resumes_per_chunk(self):
        prev = dict(self.ID, chunks=self.CHUNKS, w0_done='1', w1_last_pk='300',
                    w2_last_pk='')
        self.assertEqual(bulk_loader.stored_chunks(prev, self.ID),
                         [(1, 250), (251, 500), (501, 750), (751, 1000)])
        self.assertEqual(bulk_loader.resume_plan(prev, 4), {0: 'done', 1: '300'})

    def test_not_resumable(self):
        for prev in ({}, dict(self.ID, chunks=self.CHUNKS, completed_at_ms='1'),
                     dict(self.ID, chunks=self.CHUNKS, table='other'),
                     dict(self.ID),                      # no stored chunks
                     dict(self.ID, chunks='not json'),
                     dict(self.ID, chunks='[]')):
            self.assertIsNone(bulk_loader.stored_chunks(prev, self.ID), prev)

    def test_every_identity_field_must_match(self):
        changed = {'table': 'sales.orders', 'key_prefix': 'v2:orders:',
                   'spec_hash': '0123456789abcdef', 'replace': '1',
                   'chunk_column': 'seq', 'key_columns': 'id,line',
                   'scope': '5..None', 'stack': 'arn:stack/2'}
        self.assertEqual(set(changed), set(self.ID), 'a field is untested')
        for field, value in changed.items():
            prev = dict(self.ID, chunks=self.CHUNKS, w0_done='1', **{field: value})
            self.assertIsNone(bulk_loader.stored_chunks(prev, self.ID), field)

    def test_the_key_range_and_worker_count_are_not_part_of_it(self):
        # Jira story F067: any insert moved MAX(pk), so a failed load almost never resumed.
        self.assertNotIn('pk_hi', self.ID)
        self.assertNotIn('workers', self.ID)
        prev = dict(self.ID, chunks=self.CHUNKS, pk_hi='1000', workers='4')
        self.assertTrue(bulk_loader.stored_chunks(prev, self.ID))

    def test_a_header_without_an_identity_is_not_resumed(self):
        # Written by a release before the identity was recorded.
        old = {'table': 'orders', 'pk_lo': '1', 'pk_hi': '1000', 'workers': '4',
               'w0_done': '1'}
        self.assertIsNone(bulk_loader.stored_chunks(old, self.ID))


class ExtendChunksTest(unittest.TestCase):
    def test_keys_added_above_get_a_chunk_of_their_own(self):
        self.assertEqual(bulk_loader.extend_chunks([(1, 500), (501, 1000)], 1, 1300),
                         [(1, 500), (501, 1000), (1001, 1300)])

    def test_keys_added_below_too(self):
        self.assertEqual(bulk_loader.extend_chunks([(10, 500), (501, 1000)], -5, 1000),
                         [(10, 500), (501, 1000), (-5, 9)])

    def test_rows_removed_at_the_ends_change_nothing(self):
        self.assertEqual(bulk_loader.extend_chunks([(1, 500), (501, 1000)], 40, 900),
                         [(1, 500), (501, 1000)])


class SplitRangesTest(unittest.TestCase):
    class Probe(object):
        def __init__(self, keys):
            self.keys = sorted(keys)
            self.calls = []

        def key_at_offset(self, table, pk, lo, offset):
            self.calls.append((lo, offset))
            after = [k for k in self.keys if k >= lo]
            return after[offset] if offset < len(after) else None

    def test_dense_keys_split_by_width(self):
        p = self.Probe(range(1, 1001))
        self.assertEqual(bulk_loader.split_ranges(p, 't', 'id', 1, 1000, 4, 1000),
                         [(1, 250), (251, 500), (501, 750), (751, 1000)])
        self.assertEqual(p.calls, [])

    def split(self, *args):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return bulk_loader.split_ranges(*args)

    def test_sparse_keys_split_by_quantile(self):
        # Jira story F073: 1000 rows at 1..500 and 1e9..1e9+499. By width, one chunk
        # would get half the rows and two would get none.
        keys = list(range(1, 501)) + list(range(10 ** 9, 10 ** 9 + 500))
        p = self.Probe(keys)
        out = self.split(p, 't', 'id', 1, 10 ** 9 + 499, 4, 1000)
        self.assertEqual(out[0][0], 1)
        self.assertEqual(out[-1][1], 10 ** 9 + 499)
        for (a, b), (c, d) in zip(out, out[1:]):
            self.assertEqual(b + 1, c, 'contiguous')
        counts = [sum(1 for k in keys if a <= k <= b) for a, b in out]
        self.assertEqual(counts, [250, 250, 250, 250])

    def test_few_distinct_values_still_cover_the_range(self):
        keys = [1] * 500 + [10 ** 6] * 500
        p = self.Probe(keys)
        out = self.split(p, 't', 'id', 1, 10 ** 6, 4, 1000)
        self.assertEqual(out[0][0], 1)
        self.assertEqual(out[-1][1], 10 ** 6)
        self.assertEqual(sum(sum(1 for k in keys if a <= k <= b) for a, b in out), 1000)

    def test_no_estimate_or_no_support_splits_by_width(self):
        self.assertEqual(len(bulk_loader.split_ranges(object(), 't', 'id', 1, 10 ** 9, 4,
                                                      1000)), 4)
        self.assertEqual(len(bulk_loader.split_ranges(self.Probe([1]), 't', 'id', 1,
                                                      10 ** 9, 4, None)), 4)

    def test_a_failing_quantile_read_falls_back_to_width(self):
        class Broken(object):
            def key_at_offset(self, *a):
                raise RuntimeError('timeout')
        import io
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            out = bulk_loader.split_ranges(Broken(), 't', 'id', 1, 10 ** 9, 4, 1000)
        self.assertEqual(out, bulk_loader.chunk_ranges(1, 10 ** 9, 4))

    def test_each_engine_reads_a_key_by_offset(self):
        from tests.test_paged_read import _Conn
        for engine, frag in (('mysql', 'LIMIT 1 OFFSET %s'), ('postgresql', 'LIMIT 1 OFFSET %s'),
                             ('sqlserver', 'OFFSET %s ROWS FETCH NEXT 1 ROWS ONLY'),
                             ('oracle', 'OFFSET :2 ROWS FETCH NEXT 1 ROWS ONLY')):
            a = bulk_loader.ADAPTERS[engine]({})
            a.conn = _Conn()
            a._dict = None
            self.assertIsNone(a.key_at_offset('orders', 'id', 5, 100))
            sql, params = a.conn.log[-1]
            self.assertTrue(sql.endswith(frag), (engine, sql))
            self.assertEqual(list(params), [5, 100])


class RunIdentityTest(unittest.TestCase):
    def test_the_spec_hash_follows_the_spec_not_its_spelling(self):
        a = {'tables': {'t': {'rename': {'a': 'b'}, 'columns': ['a']}}}
        b = {'tables': {'t': {'columns': ['a'], 'rename': {'a': 'b'}}}}
        self.assertEqual(bulk_loader.spec_hash(a), bulk_loader.spec_hash(b))
        self.assertNotEqual(bulk_loader.spec_hash(a),
                            bulk_loader.spec_hash({'tables': {'t': {'columns': ['b']}}}))
        self.assertEqual(bulk_loader.spec_hash(None), 'none')
        self.assertEqual(bulk_loader.spec_hash({}), 'none')


class ProgressKeyTest(unittest.TestCase):
    def test_one_hash_per_link_and_table(self):
        self.assertEqual(bulk_loader.progress_key_for('ecdl:shop:bulkload', 'orders:'),
                         'ecdl:shop:bulkload:orders')
        self.assertEqual(bulk_loader.progress_key_for('ecdl:shop:bulkload', 'v2:orders:'),
                         'ecdl:shop:bulkload:v2:orders')

    def test_a_run_by_hand_still_gets_one_per_table(self):
        self.assertEqual(bulk_loader.progress_key_for('', 'orders:'), 'ecdl:bulkload:orders')
        self.assertEqual(bulk_loader.progress_key_for(None, 'items:'), 'ecdl:bulkload:items')


if __name__ == '__main__':
    unittest.main()


class RetryBudgetAgainstValkeyTest(unittest.TestCase):
    """Jira story F066 end to end: main() over a real Valkey, with a source that drops
    the connection eight times, each time after 1500 rows."""

    def setUp(self):
        from tests import fake_source
        self.fs = fake_source
        self.ctx = fake_source.real_valkey_modules()
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.target = fake_source.target_from_env(self)
        import valkey
        self.vk = valkey.Valkey(host=self.target['host'], port=self.target['port'])
        fake_source.clean(self.vk, 'f066:*', 'ecdl:bulkload:f066*')
        self.addCleanup(fake_source.clean, self.vk, 'f066:*', 'ecdl:bulkload:f066*')
        rows = [{'id': i, 'v': 'r%d' % i} for i in range(1, 9001)]
        fake_source.FakeSource.reset({'public.f066': {
            'pk': ['id'], 'columns': [('id', 'integer'), ('v', 'text')], 'rows': rows}})

    def test_a_chunk_that_keeps_progressing_finishes(self):
        self.fs.FakeSource.fail.update({'fetch': 1500, 'every': 1500, 'times': 8})
        out = self.fs.run_main(self.target, 'f066', workers=1)
        self.assertIn('Loaded 9,000 rows', out)
        self.assertEqual(out.count('failed (ConnectionError'), 8)
        self.assertEqual(len(list(self.vk.scan_iter(match='f066:*', count=1000))), 9000)
        self.assertEqual(self.fs.FakeSource.fetches[1:], [(1 + 1000 * i, 9000) for i in range(1, 9)])

    def test_failures_without_progress_still_fail_the_load(self):
        self.fs.FakeSource.fail.update({'fetch': 10, 'every': 10, 'times': 99})
        with self.assertRaises(ConnectionError):
            self.fs.run_main(self.target, 'f066', workers=1)
        self.assertEqual(len(self.fs.FakeSource.fetches), 1 + bulk_loader.LOAD_RETRIES)
