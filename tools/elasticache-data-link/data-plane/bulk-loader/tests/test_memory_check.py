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
