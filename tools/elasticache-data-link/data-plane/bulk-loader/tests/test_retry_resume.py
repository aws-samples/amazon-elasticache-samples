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

    def test_permanent_errors_are_not_retried(self):
        starts = []

        class Missing(Exception):
            pass

        def boom(msg):
            return Missing("(1054, \"Unknown column 'id' in 'field list'\")")
        with self.assertRaises(Missing):
            run(FlakyAdapter(100, 0, 1, starts, exc=boom), 100)
        self.assertEqual(starts, [1])


class ResumePlanTest(unittest.TestCase):
    HEADER = {'table': 'orders', 'pk_lo': '1', 'pk_hi': '1000', 'workers': '4'}

    def test_incomplete_run_resumes_per_chunk(self):
        prev = dict(self.HEADER, w0_done='1', w1_last_pk='300', w2_last_pk='')
        self.assertEqual(bulk_loader.resume_plan(prev, 'orders', 1, 1000, 4),
                         {0: 'done', 1: '300'})

    def test_not_resumable(self):
        for prev in ({}, dict(self.HEADER, completed_at_ms='1', w0_done='1'),
                     dict(self.HEADER, table='other', w0_done='1'),
                     dict(self.HEADER, pk_hi='2000', w0_done='1'),
                     dict(self.HEADER, workers='8', w0_done='1')):
            self.assertEqual(bulk_loader.resume_plan(prev, 'orders', 1, 1000, 4), {},
                             prev)


if __name__ == '__main__':
    unittest.main()
