"""Jira story F071: the loader reads a chunk in bounded pages, each its own transaction.

One statement per chunk used to run for hours, holding a MySQL metadata lock
and read view (blocking any DDL, and every query queued behind it) or a
PostgreSQL snapshot (xmin) that stops VACUUM; the probe connection also kept a
transaction open for the whole load.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bulk_loader  # noqa: E402
from tests.fake_source import (FakeSource, clean, real_valkey_modules,  # noqa: E402
                               run_main, target_from_env)
from tests.test_source_tls import _FakeDrivers  # noqa: E402


class ListSource(bulk_loader.SourceAdapter):
    """page()/value_rows() over a list, recording every read."""

    def __init__(self, rows, pk='id'):
        self.rows = sorted(rows, key=lambda r: r[pk])
        self.reads = []

    def page(self, table, pk, lo, hi, n):
        self.reads.append(('page', lo, hi, n))
        if len(self.reads) > 1000:
            raise AssertionError('the read does not advance')
        return [dict(r) for r in self.rows if lo <= r[pk] <= hi][:n]

    def value_rows(self, table, pk, value):
        self.reads.append(('value', value))
        return [dict(r) for r in self.rows if r[pk] == value]


class PagedRowsTest(unittest.TestCase):
    def read(self, rows, lo, hi, batch, unique):
        src = ListSource(rows)
        got = list(src.paged_rows('t', 'id', lo, hi, batch, unique))
        return got, src.reads

    def test_a_unique_column_continues_after_each_full_page(self):
        rows = [{'id': i} for i in range(1, 1001)]
        got, reads = self.read(rows, 1, 1000, 300, True)
        self.assertEqual([r['id'] for r in got], list(range(1, 1001)))
        self.assertEqual([r[1] for r in reads], [1, 301, 601, 901])
        self.assertTrue(all(r[3] == 300 for r in reads))

    def test_sparse_keys_and_an_exact_last_page(self):
        rows = [{'id': i * 1000} for i in range(1, 7)]
        got, reads = self.read(rows, 1, 10 ** 9, 3, True)
        self.assertEqual([r['id'] for r in got], [1000, 2000, 3000, 4000, 5000, 6000])
        self.assertEqual([r[1] for r in reads], [1, 3001, 6001])

    def test_an_empty_range(self):
        self.assertEqual(self.read([{'id': 5}], 10, 20, 100, True), ([], [('page', 10, 20, 100)]))
        self.assertEqual(self.read([{'id': 5}], 20, 10, 100, True), ([], []))

    def test_a_repeating_column_does_not_split_a_value_across_pages(self):
        # (id, line): 3 lines per id, pages of 4 rows end inside an id.
        rows = [{'id': i, 'line': j} for i in range(1, 11) for j in range(3)]
        got, reads = self.read(rows, 1, 10, 4, False)
        self.assertEqual(sorted((r['id'], r['line']) for r in got),
                         sorted((r['id'], r['line']) for r in rows))
        self.assertEqual(len(got), len(rows), 'no row twice')
        ids = [r['id'] for r in got]
        self.assertEqual(ids, sorted(ids), 'still in key order')

    def test_a_value_with_more_rows_than_a_page_is_read_by_itself(self):
        rows = [{'id': 1, 'line': j} for j in range(10)] + [{'id': 2, 'line': 0}]
        got, reads = self.read(rows, 1, 2, 4, False)
        self.assertEqual(len(got), 11)
        self.assertIn(('value', 1), reads)

    def test_every_read_is_bounded(self):
        rows = [{'id': i} for i in range(1, 5001)]
        src = ListSource(rows)
        for _ in src.paged_rows('t', 'id', 1, 5000, 700, True):
            pass
        self.assertTrue(all(r[0] == 'page' and r[3] == 700 for r in src.reads))


class _Cursor(object):
    def __init__(self, log):
        self.log = log
        self.arraysize = self.prefetchrows = 0
        self.rowfactory = None
        self.description = [('ID',)]

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchall(self):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn(object):
    def __init__(self):
        self.log = []

    def cursor(self, *a, **k):
        return _Cursor(self.log)


class PageSqlTest(unittest.TestCase):
    def test_each_engine_bounds_the_page(self):
        want = {
            'mysql': ('SELECT * FROM `shop`.`orders` WHERE `id` >= %s AND `id` <= %s '
                      'ORDER BY `id` LIMIT %s', (5, 9, 100)),
            'postgresql': ('SELECT * FROM "shop"."orders" WHERE "id" >= %s AND "id" <= %s '
                           'ORDER BY "id" LIMIT %s', (5, 9, 100)),
            'sqlserver': ('SELECT TOP (%s) * FROM [shop].[orders] WHERE [id] >= %s AND '
                          '[id] <= %s ORDER BY [id]', (100, 5, 9)),
            'oracle': ('SELECT * FROM shop.orders WHERE id >= :1 AND id <= :2 ORDER BY id '
                       'FETCH FIRST :3 ROWS ONLY', [5, 9, 100]),
        }
        for engine, (sql, params) in want.items():
            a = bulk_loader.ADAPTERS[engine]({})
            a.conn = _Conn()
            a._dict = None
            a.page('shop.orders', 'id', 5, 9, 100)
            self.assertEqual(a.conn.log[-1], (sql, params), engine)

    def test_each_engine_reads_one_value(self):
        for engine, marker in (('mysql', '%s'), ('postgresql', '%s'), ('sqlserver', '%s'),
                               ('oracle', ':1')):
            a = bulk_loader.ADAPTERS[engine]({})
            a.conn = _Conn()
            a._dict = None
            a.value_rows('orders', 'id', 7)
            sql, params = a.conn.log[-1]
            self.assertTrue(sql.endswith('= ' + marker), (engine, sql))
            self.assertEqual(list(params), [7])


class ConnectionTest(_FakeDrivers, unittest.TestCase):
    def test_mysql_autocommits_and_waits_for_locks_briefly(self):
        kw = self.connect('mysql', tls=False)
        self.assertIs(kw['autocommit'], True)
        self.assertIn('lock_wait_timeout = 10', kw['init_command'])
        self.assertIn("time_zone = '+00:00'", kw['init_command'])

    def test_postgresql_autocommits_and_waits_for_locks_briefly(self):
        kw = self.connect('postgresql', tls=False)
        self.assertEqual(kw['options'], '-c lock_timeout=10s')
        self.assertIs(self.pg_conns[-1].autocommit, True)


class PagedFakeSource(FakeSource, bulk_loader.SourceAdapter):
    """The fake source with the real paged_rows() over it."""
    pages = []
    closed = []

    def page(self, table, pk, lo, hi, n):
        type(self).pages.append((lo, hi, n))
        fail = type(self).fail
        if fail.get('page') == len(type(self).pages):
            fail.clear()
            raise ConnectionError('connection reset by peer')
        rows = [r for r in self._table(table)['rows'] if lo <= r[pk] <= hi]
        return [dict(r) for r in sorted(rows, key=lambda r: r[pk])[:n]]

    def value_rows(self, table, pk, value):
        return [dict(r) for r in self._table(table)['rows'] if r[pk] == value]

    instances = []

    def connect(self):
        self.conn = self
        type(self).instances.append(self)

    def close(self):
        type(self).closed.append(self)


class PagedLoadAgainstValkeyTest(unittest.TestCase):
    def setUp(self):
        self.ctx = real_valkey_modules()
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.target = target_from_env(self)
        import valkey
        self.vk = valkey.Valkey(host=self.target['host'], port=self.target['port'])
        clean(self.vk, 'f071*', 'ecdl:bulkload:f071*')
        self.addCleanup(clean, self.vk, 'f071*', 'ecdl:bulkload:f071*')
        saved = bulk_loader.FETCH_BATCH
        bulk_loader.FETCH_BATCH = 300
        self.addCleanup(setattr, bulk_loader, 'FETCH_BATCH', saved)
        PagedFakeSource.pages, PagedFakeSource.closed = [], []
        PagedFakeSource.instances = []

    def test_a_load_reads_bounded_pages_and_survives_a_dropped_page(self):
        rows = [{'id': i, 'v': 'r%d' % i} for i in range(1, 3001)]
        FakeSource.reset({'public.f071': {'pk': ['id'], 'columns': [('id', 'int'), ('v', 'text')],
                                          'rows': rows}})
        FakeSource.fail = {'page': 5}
        out = run_main(self.target, 'f071', workers=2, adapter=PagedFakeSource)
        self.assertIn('Loaded 3,000 rows', out)
        self.assertEqual(len(list(self.vk.scan_iter(match='f071:*', count=1000))), 3000)
        self.assertTrue(all(n == 300 for (_lo, _hi, n) in PagedFakeSource.pages))
        self.assertIn('failed (ConnectionError', out)
        probe = PagedFakeSource.instances[0]
        self.assertIn(probe, PagedFakeSource.closed, 'the probe connection is closed')

    def test_a_composite_key_split_by_a_repeating_column(self):
        rows = [{'order_id': i, 'line': j, 'v': 'x'} for i in range(1, 401) for j in range(3)]
        FakeSource.reset({'public.f071l': {'pk': ['order_id', 'line'],
                                           'columns': [('order_id', 'int'), ('line', 'int'),
                                                       ('v', 'text')], 'rows': rows}})
        run_main(self.target, 'f071l', workers=2, adapter=PagedFakeSource)
        self.assertEqual(len(list(self.vk.scan_iter(match='f071l:*', count=1000))), 1200)

    def test_streaming_is_still_there_when_turned_off(self):
        rows = [{'id': i, 'v': 'r'} for i in range(1, 501)]
        FakeSource.reset({'public.f071': {'pk': ['id'], 'columns': [('id', 'int'), ('v', 'text')],
                                          'rows': rows}})
        saved = bulk_loader.LOAD_PAGED
        bulk_loader.LOAD_PAGED = False
        try:
            run_main(self.target, 'f071', workers=1, adapter=PagedFakeSource)
        finally:
            bulk_loader.LOAD_PAGED = saved
        self.assertEqual(PagedFakeSource.pages, [])
        self.assertEqual(FakeSource.fetches, [(1, 500)])


if __name__ == '__main__':
    unittest.main()


class StreamingPostgresTest(unittest.TestCase):
    def test_the_single_statement_read_runs_in_a_transaction(self):
        events = []

        class Cur(object):
            itersize = 0

            def execute(self, sql, params):
                events.append(('execute', conn.autocommit))

            def __iter__(self):
                return iter([{'id': 1}])

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Conn(object):
            autocommit = True

            def cursor(self, **kw):
                events.append(('cursor', kw.get('name')))
                return Cur()

            def commit(self):
                events.append(('commit', self.autocommit))
        conn = Conn()
        a = bulk_loader.ADAPTERS['postgresql']({})
        a.conn, a._dict = conn, None
        self.assertEqual(list(a.fetch_iter('t', 'id', 1, 9, batch=10)), [{'id': 1}])
        self.assertEqual(events, [('cursor', 'ecdl_bulk_1_9'), ('execute', False),
                                  ('commit', False)])
        self.assertIs(conn.autocommit, True)
