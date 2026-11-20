"""The loader refuses, before writing anything, a load the target cannot hold."""
import os
import unittest

import bulk_loader


class Adapter(object):
    def __init__(self, rows, sample):
        self.rows, self._sample = rows, sample

    def estimate_rows(self, table):
        return self.rows

    def sample(self, table, pk, lo, n):
        return self._sample[:n]


class Cache(object):
    def __init__(self, used, limit, present=()):
        self.used, self.limit, self.present = used, limit, set(present)
        self.writes = 0

    def info(self, section):
        return {'used_memory': self.used, 'maxmemory': self.limit}

    def exists(self, key):
        return 1 if key in self.present else 0

    def hset(self, *a, **k):              # must never be called by the check
        self.writes += 1


def rows(n, status='active'):
    return [{'id': i, 'name': 'Item %d' % i, 'status': status, 'price': '9.99'}
            for i in range(1, n + 1)]


class CheckFitsTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop('LOAD_SKIP_MEMORY_CHECK', None)

    def check(self, adapter, cache, spec=None):
        bulk_loader.check_fits(adapter, cache, 'products', 'id', 1, 'products:',
                               spec, 'products')

    def test_six_million_rows_into_a_full_small_cache_is_refused(self):
        cache = Cache(used=1_350_000_000, limit=1_400_000_000)   # ~96% used
        with self.assertRaises(SystemExit) as ctx:
            self.check(Adapter(6_000_000, rows(200)), cache)
        self.assertIn('will not fit', str(ctx.exception))
        self.assertIn('Nothing was written', str(ctx.exception))
        self.assertEqual(cache.writes, 0)

    def test_a_load_that_fits_passes(self):
        self.check(Adapter(10_000, rows(200)), Cache(used=100_000_000, limit=1_400_000_000))

    def test_rows_already_cached_are_not_counted_twice(self):
        # A reload overwrites its own keys, so it needs no new memory for them.
        present = ['products:%d' % i for i in range(1, 201)]
        self.check(Adapter(6_000_000, rows(200)),
                   Cache(used=1_350_000_000, limit=1_400_000_000, present=present))

    def test_rows_the_filter_drops_are_not_counted(self):
        spec = {'tables': {'products': {'filter': {'field': 'status', 'op': '=',
                                                    'value': 'active'}}}}
        self.check(Adapter(6_000_000, rows(200, status='inactive')),
                   Cache(used=1_350_000_000, limit=1_400_000_000), spec=spec)

    def test_no_memory_limit_or_no_estimate_skips_the_check(self):
        self.check(Adapter(6_000_000, rows(200)), Cache(used=10, limit=0))
        self.check(Adapter(None, rows(200)), Cache(used=1_350_000_000, limit=1_400_000_000))

    def test_override(self):
        os.environ['LOAD_SKIP_MEMORY_CHECK'] = 'true'
        try:
            self.check(Adapter(6_000_000, rows(200)),
                       Cache(used=1_350_000_000, limit=1_400_000_000))
        finally:
            os.environ.pop('LOAD_SKIP_MEMORY_CHECK', None)

    def test_row_size_estimate_is_within_expected_range(self):
        per_row, kept = bulk_loader.estimate_row_bytes(rows(50), 'products:', 'id',
                                                       None, 'products')
        self.assertEqual(kept, 1.0)
        self.assertGreater(per_row, 100)
        self.assertLess(per_row, 400)


if __name__ == '__main__':
    unittest.main()


# ── Jira story F069 ────────────────────────────────────────────────────────────────────

class Counted(Adapter):
    """No catalogue estimate (never analysed), but COUNT(*) works."""
    def estimate_rows(self, table):
        return None

    def count_rows(self, table):
        return self.rows


class Spread(object):
    """Narrow rows first, wide rows later in the key range."""
    def __init__(self, n):
        self.n = n

    def estimate_rows(self, table):
        return self.n

    def sample(self, table, pk, lo, n):
        out = []
        for i in range(lo, min(lo + n, self.n + 1)):
            wide = i > self.n // 4
            out.append({'id': i, 'name': ('x' * 4000) if wide else 'a', 'status': 'active'})
        return out


class CheckFitsMoreTest(unittest.TestCase):
    FULLISH = dict(used=1_000_000_000, limit=1_400_000_000)    # ~400 MB free

    def setUp(self):
        os.environ.pop('LOAD_SKIP_MEMORY_CHECK', None)

    def test_without_an_estimate_the_rows_are_counted(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            bulk_loader.check_fits(Counted(6_000_000, rows(200)), Cache(**self.FULLISH),
                                   'products', 'id', 1, 'products:', None, 'products')
        self.assertIn('counted 6,000,000 rows', out.getvalue())

    def test_the_sample_is_spread_over_the_key_range(self):
        # 400,000 rows: the first quarter is narrow, the rest ~4 KB. The first
        # 200 keys alone said ~40 MB; spread, it is over 1 GB.
        args = ('products', 'id', 1, 'products:', None, 'products')
        bulk_loader.check_fits(Spread(400_000), Cache(**self.FULLISH), *args)   # hi unknown
        with self.assertRaises(SystemExit):
            bulk_loader.check_fits(Spread(400_000), Cache(**self.FULLISH), *args,
                                   hi=400_000)

    def test_spread_sample_reaches_every_quarter(self):
        sample = bulk_loader.spread_sample(Spread(1000), 't', 'id', 1, 1000, 200)
        ids = sorted(r['id'] for r in sample)
        self.assertEqual(len(ids), 200)
        self.assertLess(ids[0], 250)
        self.assertGreater(ids[-1], 750)

    def test_the_version_field_is_counted(self):
        plain, _ = bulk_loader.estimate_row_bytes(rows(10), 'p:', 'id', None, 'p')
        versioned, _ = bulk_loader.estimate_row_bytes(rows(10), 'p:', 'id', None, 'p',
                                                      version='1794882952638-0')
        self.assertAlmostEqual(versioned - plain,
                               bulk_loader.FRAGMENTATION * (len('_ecdl_v') + 15 + 4), places=3)

    def test_embedded_parent_fields_are_counted(self):
        spec = {'tables': {'orders': {'denormalize': [{
            'parent_table': 'customers', 'join_field': 'customer_id',
            'fields': ['name', 'bio'], 'prefix': 'customer_'}]}}}

        class Parents(object):
            def hmget(self, key, fields):
                return [b'Ada', b'b' * 2000, b'5-0'] if key == 'customers:7' else [None] * 3
        order = [{'id': i, 'customer_id': '7', 'total': '9.99'} for i in range(1, 21)]
        own, _ = bulk_loader.estimate_row_bytes(order, 'orders:', 'id', None, 'orders')
        with_parent, _ = bulk_loader.estimate_row_bytes(order, 'orders:', 'id', spec,
                                                        'orders', vk=Parents())
        self.assertGreater(with_parent - own, 2000)
        without_cache, _ = bulk_loader.estimate_row_bytes(order, 'orders:', 'id', spec,
                                                          'orders')
        self.assertGreater(without_cache, own, 'fk, pv and the index entry count too')


# ── Jira story F070 ────────────────────────────────────────────────────────────────────

class FetchBatchTest(unittest.TestCase):
    def setUp(self):
        self.saved = (bulk_loader.FETCH_BATCH, bulk_loader.FETCH_BATCH_SET,
                      os.environ.get('FETCH_BATCH'))
        bulk_loader.FETCH_BATCH, bulk_loader.FETCH_BATCH_SET = 50000, False

    def tearDown(self):
        bulk_loader.FETCH_BATCH, bulk_loader.FETCH_BATCH_SET, env = self.saved
        if env is None:
            os.environ.pop('FETCH_BATCH', None)
        else:
            os.environ['FETCH_BATCH'] = env

    def size(self, sample, workers=16, memory=4096 * 1048576):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            batch = bulk_loader.size_fetch_batch(sample, None, workers, memory)
        return batch, out.getvalue()

    def test_wide_rows_lower_the_batch(self):
        wide = [{'id': i, 'doc': 'x' * 20000} for i in range(50)]
        batch, out = self.size(wide)
        # 16 workers x batch x ~40 KB (raw + encoded) must fit in half of 4 GiB.
        self.assertLess(batch * 16 * 40000, 0.5 * 4096 * 1048576)
        self.assertGreater(batch, 1000)
        self.assertEqual(os.environ['FETCH_BATCH'], str(batch))
        self.assertIn('fetch batch: %s rows' % format(batch, ','), out)

    def test_narrow_rows_keep_the_default(self):
        self.assertEqual(self.size(rows(50))[0], 50000)

    def test_never_below_the_minimum(self):
        huge = [{'id': 1, 'doc': 'x' * 50_000_000}]
        self.assertEqual(self.size(huge)[0], bulk_loader.MIN_FETCH_BATCH)

    def test_a_set_batch_is_used_as_given(self):
        bulk_loader.FETCH_BATCH, bulk_loader.FETCH_BATCH_SET = 200000, True
        wide = [{'id': i, 'doc': 'x' * 20000} for i in range(50)]
        batch, out = self.size(wide)
        self.assertEqual(batch, 200000)
        self.assertIn('(FETCH_BATCH)', out)

    def test_task_memory_comes_from_the_task(self):
        saved = os.environ.get('TASK_MEMORY_MB')
        os.environ['TASK_MEMORY_MB'] = '8192'
        try:
            self.assertEqual(bulk_loader.task_memory_bytes(), 8192 * 1048576)
        finally:
            if saved is None:
                os.environ.pop('TASK_MEMORY_MB', None)
            else:
                os.environ['TASK_MEMORY_MB'] = saved

    def test_the_task_definition_passes_memory_and_batch(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, '..', '..', '..', 'infra', 'ecdl-loader-ecs.yaml')) as h:
            text = h.read()
        self.assertIn('- { Name: TASK_MEMORY_MB, Value: !Ref TaskMemory }', text)
        self.assertIn('- { Name: FETCH_BATCH, Value: !Ref FetchBatch }', text)


class WorkerKilledTest(unittest.TestCase):
    def test_a_killed_worker_is_named_as_memory(self):
        from concurrent.futures.process import BrokenProcessPool
        from tests.fake_source import FakeSource, run_main

        def broken(*a, **k):
            raise BrokenProcessPool('A process in the process pool was terminated abruptly')
        FakeSource.reset({'public.t': {'pk': ['id'], 'columns': [('id', 'int')],
                                       'rows': [{'id': 1}]}})
        saved = bulk_loader.run_chunks
        bulk_loader.run_chunks = broken
        try:
            with self.assertRaises(SystemExit) as caught:
                run_main({'host': '127.0.0.1', 'port': 1, 'ssl': False, 'cluster': False},
                         't', extra=['--progress-key', ''], env={'TASK_MEMORY_MB': '4096'})
        finally:
            bulk_loader.run_chunks = saved
        text = str(caught.exception)
        self.assertIn('out of memory', text)
        self.assertIn('4,096 MiB', text)
        self.assertIn('advanced.loader.fetch_batch', text)


class TargetFullTest(unittest.TestCase):
    def test_a_full_target_names_the_cleanup(self):
        import contextlib
        import io
        from tests import test_denorm as fakes

        class FullPipe(fakes.FakePipe):
            def execute(self):
                raise Exception("OOM command not allowed when used memory > 'maxmemory'")

        class Full(fakes.FakeValkey):
            def pipeline(self, transaction=False):
                return FullPipe(self.store)
        rows_ = [{'id': i, 'v': 'x'} for i in range(1, 1500)]
        saved = (bulk_loader.ADAPTERS, bulk_loader.target_client)
        bulk_loader.ADAPTERS = {'mysql': lambda cfg: fakes.FakeAdapter(rows_)}
        bulk_loader.target_client = lambda cfg: Full(fakes.FakeStore())
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), self.assertRaises(Exception):
                bulk_loader.load_range(('mysql', {}, {}, 't', 'id', 't:', 1, 1500, None,
                                        't', 0, ''))
        finally:
            bulk_loader.ADAPTERS, bulk_loader.target_client = saved
        self.assertIn('the target cache is full', out.getvalue())
        self.assertIn('A load that filled the target', out.getvalue())
