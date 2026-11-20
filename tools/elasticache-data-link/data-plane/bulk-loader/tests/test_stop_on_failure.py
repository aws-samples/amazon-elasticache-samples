"""Jira story F079: the first chunk that fails for good stops the whole load.

The pool used to run every other chunk to the end first, so a load that had
already failed kept writing for hours before it exited with the error.
"""
import concurrent.futures
import contextlib
import io
import multiprocessing
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bulk_loader  # noqa: E402
from tests.fake_source import (FakeSource, clean, real_valkey_modules,  # noqa: E402
                               run_main, target_from_env)


def _slow_or_fail(task):
    """Top level, so a process pool can run it: 'fail' raises at once, any
    other task sleeps for a minute."""
    if task == 'fail':
        raise ValueError('permanent failure')
    time.sleep(60)
    return task


class RunChunksTest(unittest.TestCase):
    def test_chunks_not_started_are_cancelled(self):
        ran = []

        def fn(task):
            ran.append(task)
            if task == 0:
                raise ValueError('permanent failure')
            return task

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(ValueError):
            bulk_loader.run_chunks(ex, fn, list(range(10)), lambda r: None)
        time.sleep(0.2)
        self.assertEqual(ran, [0], 'no chunk after the failed one started')
        self.assertIn('9 chunk(s) not started are cancelled', out.getvalue())

    def test_a_successful_load_runs_every_chunk(self):
        got = []
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        bulk_loader.run_chunks(ex, lambda t: t * 2, list(range(10)), got.append)
        self.assertEqual(sorted(got), [t * 2 for t in range(10)])

    def test_running_worker_processes_are_terminated(self):
        # Three workers sleep for a minute; one task fails at once. The error
        # must come back in seconds, with no worker process left.
        ex = concurrent.futures.ProcessPoolExecutor(max_workers=4)
        began = time.time()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            bulk_loader.run_chunks(ex, _slow_or_fail, ['a', 'b', 'c', 'fail'],
                                   lambda r: None)
        self.assertLess(time.time() - began, 20)
        self.assertEqual([c for c in multiprocessing.active_children() if c.is_alive()], [])


class StopOnFailureAgainstValkeyTest(unittest.TestCase):
    """main() over a real Valkey: a chunk with a bad value fails the load and
    chunks that had not started write nothing."""

    def setUp(self):
        self.ctx = real_valkey_modules()
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.target = target_from_env(self)
        import valkey
        self.vk = valkey.Valkey(host=self.target['host'], port=self.target['port'])
        clean(self.vk, 'f079:*', 'ecdl:bulkload:f079*')
        self.addCleanup(clean, self.vk, 'f079:*', 'ecdl:bulkload:f079*')

    def test_the_first_failure_stops_the_rest(self):
        rows = [{'id': i, 'v': 'r%d' % i} for i in range(1, 801)]
        # A fractional value in the first chunk fails it for good (Jira story F073).
        rows[5]['id'] = 5.5
        FakeSource.reset({'public.f079': {
            'pk': ['id'], 'columns': [('id', 'integer'), ('v', 'text')], 'rows': rows}})
        saved = bulk_loader.ProcessPoolExecutor
        # One worker, eight chunks: the failure in the first must stop the
        # seven others before they start.
        try:
            with self.assertRaises(ValueError):
                run_main(self.target, 'f079', workers=8,
                         env={'LOAD_SKIP_MEMORY_CHECK': 'true'},
                         pool=lambda max_workers: concurrent.futures.ThreadPoolExecutor(1))
        finally:
            bulk_loader.ProcessPoolExecutor = saved
        self.assertEqual(FakeSource.fetches, [(1, 100)])
        self.assertEqual(list(self.vk.scan_iter(match='f079:*')), [])


if __name__ == '__main__':
    unittest.main()
