"""Jira story F104: a running load shows its progress, and a stalled one says so.

The loader logged a line only when a whole chunk finished, which for a large
table is hours of silence."""
import contextlib
import io
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bulk_loader  # noqa: E402
from tests.fake_source import (FakeSource, clean, real_valkey_modules,  # noqa: E402
                               run_main, target_from_env)


class Clock(object):
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TickerTest(unittest.TestCase):
    def ticker(self, clock):
        return bulk_loader.ProgressTicker({}, 'ecdl:x:bulkload:t', 4, clock(), every=60,
                                          stall=600, clock=clock)

    def test_a_line_with_the_totals(self):
        clock = Clock()
        t = self.ticker(clock)
        clock.now += 120
        with contextlib.redirect_stdout(io.StringIO()) as out:
            line = t.tick({b'w0_rows': b'1000', b'w1_rows': b'500', b'w1_done': b'1',
                           b'updated_at_ms': b'1'})
        self.assertEqual(line, '  progress: 1,500 rows written, 1/4 chunks done, '
                               '12 rows/s over 2.0 min')
        self.assertNotIn('WARNING', out.getvalue())

    def test_a_stall_is_reported_once_and_cleared_by_progress(self):
        clock = Clock()
        t = self.ticker(clock)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            t.tick({'w0_rows': '10'})
            clock.now += 599
            t.tick({'w0_rows': '10'})
            self.assertNotIn('WARNING', out.getvalue())
            clock.now += 2
            t.tick({'w0_rows': '10'})
            clock.now += 300
            t.tick({'w0_rows': '10'})
            self.assertEqual(out.getvalue().count('WARNING'), 1)
            t.tick({'w0_rows': '11'})                 # moving again
            clock.now += 601
            t.tick({'w0_rows': '11'})
        self.assertEqual(out.getvalue().count('WARNING: the load has written no rows '
                                              'for 10 min'), 2)

    def test_no_progress_key_no_thread(self):
        t = bulk_loader.ProgressTicker({}, '', 1, 0)
        t.start()
        self.assertIsNone(t._thread)
        t.stop()


class TickerAgainstValkeyTest(unittest.TestCase):
    def setUp(self):
        self.ctx = real_valkey_modules()
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.target = target_from_env(self)
        import valkey
        self.vk = valkey.Valkey(host=self.target['host'], port=self.target['port'])
        clean(self.vk, 'f104*', 'ecdl:bulkload:f104*')
        self.addCleanup(clean, self.vk, 'f104*', 'ecdl:bulkload:f104*')
        saved = (bulk_loader.LOG_EVERY_SEC, bulk_loader.STALL_SEC)
        bulk_loader.LOG_EVERY_SEC, bulk_loader.STALL_SEC = 0.05, 3600
        self.addCleanup(setattr, bulk_loader, 'LOG_EVERY_SEC', saved[0])
        self.addCleanup(setattr, bulk_loader, 'STALL_SEC', saved[1])

    def test_a_slow_load_logs_its_progress_while_it_runs(self):
        class Slow(FakeSource):
            def fetch_iter(self, *a, **k):
                for i, row in enumerate(FakeSource.fetch_iter(self, *a, **k)):
                    if i % 1000 == 999:
                        time.sleep(0.15)
                    yield row
        rows = [{'id': i, 'v': 'x'} for i in range(1, 4001)]
        FakeSource.reset({'public.f104': {'pk': ['id'], 'columns': [('id', 'int'),
                                                                   ('v', 'text')],
                                          'rows': rows}})
        out = run_main(self.target, 'f104', workers=2, adapter=Slow)
        lines = [l for l in out.splitlines() if l.startswith('  progress: ')]
        self.assertGreaterEqual(len(lines), 2, out)
        self.assertRegex(lines[-1], r'progress: [\d,]+ rows written, \d/2 chunks done')
        self.assertIn('Loaded 4,000 rows', out)


if __name__ == '__main__':
    unittest.main()
