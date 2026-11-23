"""Jira story F062: rows are keyed exactly as the sink keys them; the split column is separate.

The loader used to key every row by the column it split the load by. A
composite or non-key --pk gave keys the sink never writes, and the FATAL for a
composite key advised naming one of its columns, after which every row sharing
that column's value overwrote the same key. Now the key is always the table's
whole primary key in the sink's format (sink_pk, checked against the cases in
tools/go-sink/testdata/sink_pk_cases.json that the Go tests also read), and the
chunk column only splits the read.
"""
import contextlib
import decimal
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bulk_loader  # noqa: E402
import cdc_values  # noqa: E402
from tests import test_denorm as fakes  # noqa: E402
from tests.fake_source import (FakeSource, clean, real_valkey_modules,  # noqa: E402
                               run_main, target_from_env)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
CASES = os.path.join(REPO, 'tools', 'go-sink', 'testdata', 'sink_pk_cases.json')


def _source_value(v):
    if isinstance(v, dict) and '$decimal' in v:
        return decimal.Decimal(v['$decimal'])
    return v


class SharedCasesTest(unittest.TestCase):
    """The same cases the Go sink's pkFromMsg is tested against."""

    def test_the_loader_builds_the_sinks_pk(self):
        with open(CASES) as handle:
            cases = json.load(handle)['cases']
        self.assertGreaterEqual(len(cases), 5)
        for case in cases:
            row = {k: _source_value(v) for k, v in case['row'].items()}
            got = bulk_loader.sink_pk(cdc_values.generic_encoder(row), case['key_columns'])
            self.assertEqual(got, case['pk'], case['name'])

    def test_the_cases_cover_composite_keys(self):
        with open(CASES) as handle:
            sizes = {len(c['key_columns']) for c in json.load(handle)['cases']}
        self.assertTrue({1, 2, 3} <= sizes, sizes)

    def test_key_json_names_the_same_columns(self):
        # A case whose key_json and key_columns disagree would test nothing.
        with open(CASES) as handle:
            for case in json.load(handle)['cases']:
                self.assertEqual(list(json.loads(case['key_json'])), case['key_columns'],
                                 case['name'])


class _Probe(object):
    def __init__(self, pk=None, error=None):
        self.pk, self.error = pk, error

    def primary_key(self, table):
        if self.error:
            raise self.error
        return self.pk

    def fold_identifier(self, name):
        return name


def _resolve(probe, requested=''):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = bulk_loader.resolve_keys(probe, 'orders', requested)
    return result, out.getvalue()


class ResolveKeysTest(unittest.TestCase):
    def test_single_primary_key_is_both(self):
        self.assertEqual(_resolve(_Probe(['id']))[0], ('id', ['id']))

    def test_composite_key_is_split_by_its_first_column_and_keyed_by_all(self):
        (chunk, keys), out = _resolve(_Probe(['order_id', 'line_no']))
        self.assertEqual((chunk, keys), ('order_id', ['order_id', 'line_no']))
        self.assertIn('keyed by all of them', out)

    def test_a_named_column_only_chooses_the_split(self):
        (chunk, keys), out = _resolve(_Probe(['sku']), 'seq')
        self.assertEqual((chunk, keys), ('seq', ['sku']))
        self.assertIn('keyed by the primary key (sku)', out)

    def test_naming_a_key_column_keeps_the_whole_key(self):
        # The old FATAL's advice: name one column of the composite key.
        self.assertEqual(_resolve(_Probe(['order_id', 'line_no']), 'line_no')[0],
                         ('line_no', ['order_id', 'line_no']))

    def test_unreadable_catalogue_uses_the_named_column(self):
        (chunk, keys), out = _resolve(_Probe(error=RuntimeError('denied')), 'id')
        self.assertEqual((chunk, keys), ('id', ['id']))
        self.assertIn('only right if id alone is the primary key', out)

    def test_unreadable_catalogue_without_a_name_is_fatal(self):
        with self.assertRaises(SystemExit) as caught:
            _resolve(_Probe(error=RuntimeError('denied')))
        self.assertIn('could not be read', str(caught.exception))

    def test_no_primary_key(self):
        with self.assertRaises(SystemExit) as caught:
            _resolve(_Probe([]))
        self.assertIn('no primary key', str(caught.exception))
        (chunk, keys), out = _resolve(_Probe([]), 'id')
        self.assertEqual((chunk, keys), ('id', ['id']))
        self.assertIn('`id` column', out)

    def test_no_fatal_advises_picking_one_column_of_a_composite_key(self):
        # That advice is what lost rows. A composite key is no longer fatal.
        _resolve(_Probe(['a', 'b', 'c']))


class ChunkBoundsTest(unittest.TestCase):
    def test_whole_numbers_pass(self):
        for lo, hi in ((1, 10), (decimal.Decimal('1'), decimal.Decimal('10')), (-5, 5.0)):
            bulk_loader.check_chunk_bounds('t', 'id', ['id'], lo, hi)

    def test_ordered_keys_pass(self):
        # Review finding M6: text, uuid and date keys are split at key values.
        import datetime
        import uuid
        for lo, hi in (('a', 'z'), (datetime.date(2024, 1, 1), datetime.date(2024, 2, 1)),
                       (uuid.UUID(int=1), uuid.UUID(int=2))):
            bulk_loader.check_chunk_bounds('t', 'sku', ['sku'], lo, hi)

    def test_mixed_fractional_or_boolean_bounds_are_refused_with_the_remedy(self):
        for lo, hi in ((decimal.Decimal('0.5'), 9), (True, False), (0.5, 9.5)):
            with self.assertRaises(SystemExit) as caught:
                bulk_loader.check_chunk_bounds('t', 'sku', ['sku'], lo, hi)
            msg = str(caught.exception)
            self.assertIn('advanced.loader.primary_keys', msg)
            self.assertIn('still keyed by the primary key (sku)', msg)


class OrderedBoundsTest(unittest.TestCase):
    """Review finding M6, found live: PostgreSQL has no MIN/MAX for uuid
    (SQL Server none for uniqueidentifier), so the bounds are the first and
    last key in index order instead."""
    def test_no_min_for_the_type_reads_the_ends_of_the_index(self):
        import pickle

        class Cursor(object):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql):
                raise RuntimeError('function min(uuid) does not exist')

        class Conn(object):
            def cursor(self, **kw):
                return Cursor()

        a = bulk_loader.PostgreSQLAdapter({})
        a.conn = Conn()
        seen = []

        def query_rows(sql, params):
            seen.append(sql)
            return [{'k': 'aaaa-0001'}] if 'DESC' not in sql else [{'k': 'ffff-0009'}]
        a.query_rows = query_rows
        self.assertEqual(a.bounds('public.t', 'id'), ('aaaa-0001', 'ffff-0009'))
        self.assertTrue(all('ORDER BY' in q for q in seen), seen)
        a.query_rows = lambda sql, params: []
        self.assertEqual(a.bounds('public.t', 'id'), (None, None))
        # A range start crosses to the worker processes.
        self.assertEqual(pickle.loads(pickle.dumps(bulk_loader.After('x'))), bulk_loader.After('x'))


class ResumePositionTest(unittest.TestCase):
    def test_a_unique_column_restarts_after_the_confirmed_value(self):
        self.assertEqual(bulk_loader._after(500, 1, unique=True), 501)

    def test_a_repeating_column_restarts_at_the_confirmed_value(self):
        self.assertEqual(bulk_loader._after(500, 1, unique=False), 500)

    def test_a_non_integer_restarts_at_the_chunk_start(self):
        self.assertEqual(bulk_loader._after('x', 7, unique=False), 7)


# ── load_chunk with in-memory fakes ─────────────────────────────────────────

class Lines(object):
    """order_lines: (order_id, line_no) primary key, 3 lines per order.
    Rows come in order_id order; optionally fails once after `fail_after` rows."""

    def __init__(self, orders, fail_after=None):
        self.rows = [{'order_id': o, 'line_no': n, 'qty': o * 10 + n}
                     for o in range(1, orders + 1) for n in (1, 2, 3)]
        self.fail_after, self.starts = fail_after, []

    def connect(self):
        pass

    def fetch_iter(self, table, pk, lo, hi, batch=None):
        self.starts.append(lo)
        for i, row in enumerate(r for r in self.rows if lo <= r['order_id'] <= hi):
            if self.fail_after is not None and i == self.fail_after:
                self.fail_after = None
                raise ConnectionError('connection reset by peer')
            yield dict(row)


@contextlib.contextmanager
def _patched(adapter, store):
    saved = (bulk_loader.ADAPTERS, bulk_loader.target_client, bulk_loader._sleep)
    bulk_loader.ADAPTERS = {'mysql': lambda cfg: adapter}
    bulk_loader.target_client = lambda cfg: fakes.FakeValkey(store)
    bulk_loader._sleep = lambda s: None
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            yield out
    finally:
        bulk_loader.ADAPTERS, bulk_loader.target_client, bulk_loader._sleep = saved


def _task(hi, spec=None, keys=('order_id', 'line_no'), replace=False):
    return ('mysql', {}, {}, 'order_lines', 'order_id', 'order_lines:', 1, hi, spec,
            'order_lines', 0, '', replace, [], keys)


class CompositeLoadTest(unittest.TestCase):
    def test_every_row_gets_the_sinks_key(self):
        store = fakes.FakeStore()
        with _patched(Lines(5), store):
            written = bulk_loader.load_range(_task(5))
        self.assertEqual(written, 15)
        self.assertEqual(len(store.hashes), 15, 'no row overwrote another')
        # line_no sorts before order_id: <line_no>:<order_id>.
        self.assertEqual(store.hashes['order_lines:3:5']['qty'], '53')

    def test_a_retry_inside_a_repeated_value_loses_no_row(self):
        # 400 orders x 3 lines = 1200 rows. The first pipeline confirms rows
        # 1..1000, ending inside order 334 (rows 1000-1002). The read fails at
        # row 1100; restarting AFTER order 334 would skip its last two lines.
        adapter = Lines(400, fail_after=1100)
        store = fakes.FakeStore()
        with _patched(adapter, store):
            written = bulk_loader.load_range(_task(400))
        self.assertEqual(adapter.starts, [1, 334])
        self.assertEqual(len(store.hashes), 1200)
        self.assertIn('order_lines:3:334', store.hashes)
        self.assertEqual(written, 1200, 're-read rows are not counted twice')

    def test_a_denorm_child_with_a_composite_key(self):
        spec = {'tables': {'order_lines': {'denormalize': [{
            'parent_table': 'orders', 'join_field': 'order_id', 'fields': ['status'],
            'parent_key_prefix': 'orders:'}]}}}
        store = fakes.FakeStore()
        store.hashes['orders:2'] = {'status': 'paid'}
        with _patched(Lines(2), store):
            written = bulk_loader.load_range(_task(2, spec=spec))
        self.assertEqual(written, 6)
        self.assertEqual(store.hashes['order_lines:3:2']['status'], 'paid')
        # The reverse index member is the sink's pk too (SAdd(idxKey, pk)).
        self.assertEqual(store.sets['idx:order_lines:by:orders:2'],
                         {'1:2', '2:2', '3:2'})

    def test_a_denorm_retry_inside_a_repeated_value_loses_no_row(self):
        spec = {'tables': {'order_lines': {'denormalize': [{
            'parent_table': 'orders', 'join_field': 'order_id', 'fields': ['status'],
            'parent_key_prefix': 'orders:'}]}}}
        adapter = Lines(400, fail_after=1100)
        store = fakes.FakeStore()
        with _patched(adapter, store):
            written = bulk_loader.load_range(_task(400, spec=spec))
        self.assertEqual(adapter.starts, [1, 334])
        self.assertEqual(len([k for k in store.hashes if k.startswith('order_lines:')]), 1200)
        self.assertEqual(written, 1200)

    def test_rows_out_of_order_are_still_refused_for_a_repeating_column(self):
        class Backwards(Lines):
            def fetch_iter(self, table, pk, lo, hi, batch=None):
                for row in reversed(self.rows):
                    yield dict(row)
        with self.assertRaises(ValueError) as caught:
            with _patched(Backwards(3), fakes.FakeStore()):
                bulk_loader.load_range(_task(3))
        self.assertIn('out of key order', str(caught.exception))

    def test_a_fractional_chunk_value_stops_the_chunk(self):
        class Fractional(Lines):
            def fetch_iter(self, table, pk, lo, hi, batch=None):
                yield {'order_id': decimal.Decimal('1.5'), 'line_no': 1, 'qty': 1}
        with self.assertRaises(ValueError) as caught:
            with _patched(Fractional(1), fakes.FakeStore()):
                bulk_loader.load_range(_task(3))
        self.assertIn('not a whole number', str(caught.exception))


class VerifyCompositeTest(unittest.TestCase):
    """D6 read-back works with composite keys and a repeating chunk column."""

    def test_a_lost_composite_key_is_loaded_again(self):
        store = fakes.FakeStore()
        adapter = Lines(400)
        task = _task(400)
        with _patched(adapter, store):
            results = [bulk_loader.load_chunk(task)]
        tail = [k for b in results[0]['tail'] for k in b['kept']]
        self.assertIn('order_lines:3:400', tail)
        del store.hashes['order_lines:3:400']
        with _patched(adapter, store) as out:
            bulk_loader.verify_and_repair({}, [task], results, None)
        self.assertIn('order_lines:3:400', store.hashes)
        # The lost batch began after order 334's line 1 was confirmed, so
        # the repair restarts AT 334 (not 335).
        self.assertIn('Loading it again from pk 334', out.getvalue())
        self.assertEqual(adapter.starts[-1], 334)


class MemoryEstimateTest(unittest.TestCase):
    def test_the_estimate_uses_the_full_key(self):
        rows = [{'order_id': 1, 'line_no': 2, 'qty': 3}]
        short, _ = bulk_loader.estimate_row_bytes(rows, 'p:', 'order_id', None, 't')
        full, _ = bulk_loader.estimate_row_bytes(rows, 'p:', 'order_id', None, 't',
                                                 key_columns=['order_id', 'line_no'])
        self.assertAlmostEqual(full - short, 2 * bulk_loader.FRAGMENTATION)


# ── main() against a real Valkey ─────────────────────────────────────────────

def _table(pk, columns, rows):
    return {'pk': pk, 'columns': columns, 'rows': rows}


class _EndToEnd(object):
    CLUSTER = False
    ENV = 'ECDL_TEST_VALKEY'

    def setUp(self):
        # Skip before touching the valkey modules: a skip in setUp runs no
        # cleanup registered after it.
        self.target = target_from_env(self, self.ENV, self.CLUSTER)
        modules = real_valkey_modules()
        modules.__enter__()
        self.addCleanup(modules.__exit__, None, None, None)
        self.vk = bulk_loader.target_client(self.target)
        self.addCleanup(self.vk.close)
        self.addCleanup(clean, self.vk, 'kt_*', 'ecdl:bulkload:kt_*')

    def keys(self, pattern):
        return sorted(k.decode() for k in self.vk.scan_iter(match=pattern, count=1000))

    def test_single_primary_key(self):
        FakeSource.reset({'public.kt_one': _table(
            ['id'], [('id', 'int'), ('v', 'varchar')],
            [{'id': i, 'v': 'r%d' % i} for i in range(1, 51)])})
        run_main(self.target, 'kt_one', workers=4)
        self.assertEqual(self.keys('kt_one:*'), sorted('kt_one:%d' % i for i in range(1, 51)))

    def test_two_column_key_loses_no_row(self):
        rows = [{'order_id': o, 'line_no': n, 'qty': o * 10 + n}
                for o in range(1, 101) for n in (1, 2, 3)]
        FakeSource.reset({'public.kt_lines': _table(
            ['order_id', 'line_no'],
            [('order_id', 'int'), ('line_no', 'int'), ('qty', 'int')], rows)})
        out = run_main(self.target, 'kt_lines', workers=3)
        self.assertEqual(len(self.keys('kt_lines:*')), 300)
        self.assertEqual(self.vk.hget('kt_lines:2:100', 'qty'), b'1002')
        self.assertIn('split by order_id', out)

    def test_following_the_old_advice_loses_no_row(self):
        # The old FATAL said: pick one column of the composite key with --pk.
        rows = [{'order_id': o, 'line_no': n, 'qty': 1}
                for o in range(1, 21) for n in (1, 2)]
        FakeSource.reset({'public.kt_lines': _table(
            ['order_id', 'line_no'],
            [('order_id', 'int'), ('line_no', 'int'), ('qty', 'int')], rows)})
        run_main(self.target, 'kt_lines', pk='order_id', workers=2)
        self.assertEqual(len(self.keys('kt_lines:*')), 40)

    def test_three_column_key(self):
        rows = [{'tenant': t, 'region': r, 'id': i, 'v': '%s-%s-%d' % (t, r, i)}
                for t in ('acme', 'beta') for r in ('eu', 'us') for i in (1, 2)]
        FakeSource.reset({'public.kt_three': _table(
            ['tenant', 'region', 'id'],
            [('tenant', 'varchar'), ('region', 'varchar'), ('id', 'int'), ('v', 'varchar')],
            rows)})
        # The leading key column is text, so the split column is named.
        run_main(self.target, 'kt_three', pk='id', workers=2)
        self.assertEqual(len(self.keys('kt_three:*')), 8)
        # Sorted names: id, region, tenant.
        self.assertEqual(self.vk.hget('kt_three:2:us:beta', 'v'), b'beta-us-2')

    def test_split_column_differs_from_the_key(self):
        # Keyed by id; split by batch_no, which repeats.
        rows = [{'id': 1000 + i, 'batch_no': i // 7, 'v': str(i)} for i in range(70)]
        FakeSource.reset({'public.kt_split': _table(
            ['id'], [('id', 'int'), ('batch_no', 'int'), ('v', 'varchar')], rows)})
        run_main(self.target, 'kt_split', pk='batch_no', workers=4)
        self.assertEqual(self.keys('kt_split:*'),
                         sorted('kt_split:%d' % (1000 + i) for i in range(70)))

    def test_text_key_with_an_integer_split_column(self):
        rows = [{'sku': 'SKU-%03d' % i, 'seq': i, 'v': str(i)} for i in range(1, 31)]
        FakeSource.reset({'public.kt_sku': _table(
            ['sku'], [('sku', 'varchar'), ('seq', 'int'), ('v', 'varchar')], rows)})
        run_main(self.target, 'kt_sku', pk='seq', workers=3)
        self.assertEqual(len(self.keys('kt_sku:SKU-*')), 30)
        self.assertEqual(self.vk.hget('kt_sku:SKU-007', 'v'), b'7')

    def test_a_text_key_alone_is_loaded_in_ranges_of_keys(self):
        # Review finding M6: it used to be FATAL ("not whole numbers").
        rows = [{'sku': 'SKU-%04d' % i, 'v': str(i)} for i in range(1, 2001)]
        FakeSource.reset({'public.kt_sku': _table(
            ['sku'], [('sku', 'varchar'), ('v', 'varchar')], rows)})
        out = run_main(self.target, 'kt_sku', workers=4, env={'FETCH_BATCH': '300'})
        self.assertIn('split into 4 ranges at keys', out)
        self.assertEqual(self.keys('kt_sku:*'), sorted('kt_sku:SKU-%04d' % i for i in range(1, 2001)))
        self.assertEqual(self.vk.hget('kt_sku:SKU-1234', 'v'), b'1234')

    def test_a_text_key_load_resumes_after_its_last_confirmed_key(self):
        rows = [{'sku': 'SKU-%04d' % i, 'v': str(i)} for i in range(1, 3001)]
        FakeSource.reset({'public.kt_sku': _table(
            ['sku'], [('sku', 'varchar'), ('v', 'varchar')], rows)})
        FakeSource.fail = {'fetch': 1500}
        out = run_main(self.target, 'kt_sku', workers=1, env={'FETCH_BATCH': '200'})
        self.assertIn('retry 1/', out)
        self.assertIn('confirmed up to SKU-', out)
        self.assertEqual(len(self.keys('kt_sku:*')), 3000)
        # The retry read only what followed the confirmed key.
        self.assertTrue(any(isinstance(lo, bulk_loader.After) for lo, _ in FakeSource.fetches[1:]),
                        FakeSource.fetches)

    def test_a_uuid_key_with_repeating_split_values(self):
        import uuid
        # Composite key (tenant uuid, n): the split column repeats, and no
        # tenant may be split between two chunks or skipped.
        tenants = [str(uuid.UUID(int=i * 7919)) for i in range(1, 41)]
        rows = [{'tenant': t, 'n': n, 'v': '%s-%d' % (t[:4], n)} for t in tenants for n in range(1, 26)]
        FakeSource.reset({'public.kt_uuid': _table(
            ['tenant', 'n'], [('tenant', 'uuid'), ('n', 'int'), ('v', 'varchar')], rows)})
        run_main(self.target, 'kt_uuid', workers=3, env={'FETCH_BATCH': '60'})
        self.assertEqual(len(self.keys('kt_uuid:*')), 40 * 25)

    def test_a_split_column_the_table_lacks_is_fatal(self):
        FakeSource.reset({'public.kt_one': _table(
            ['id'], [('id', 'int')], [{'id': 1}])})
        with self.assertRaises(SystemExit) as caught:
            run_main(self.target, 'kt_one', pk='nope')
        self.assertIn('no column nope', str(caught.exception))

    def test_a_retry_inside_a_repeated_split_value_loses_no_row(self):
        rows = [{'id': i, 'grp': i // 300, 'v': str(i)} for i in range(2500)]
        FakeSource.reset({'public.kt_split': _table(
            ['id'], [('id', 'int'), ('grp', 'int'), ('v', 'varchar')], rows)})
        FakeSource.fail = {'fetch': 1500}
        out = run_main(self.target, 'kt_split', pk='grp', workers=1)
        self.assertEqual(len(self.keys('kt_split:*')), 2500)
        # Rows 0..999 were confirmed; row 999 is in group 3, so the retry
        # restarts at group 3 and does not count its rows twice.
        self.assertEqual(FakeSource.fetches[1][0], 3)
        self.assertIn('Loaded 2,500 rows', out)

    def test_a_resumed_run_restarts_at_a_repeated_split_value(self):
        rows = [{'id': i, 'grp': i // 300, 'v': str(i)} for i in range(2500)]
        FakeSource.reset({'public.kt_split': _table(
            ['id'], [('id', 'int'), ('grp', 'int'), ('v', 'varchar')], rows)})
        # The first run fails for good after 1,500 rows; 1,000 were confirmed,
        # the last of them in group 3 (rows 900..1199).
        FakeSource.fail = {'fetch': 1500}
        saved = bulk_loader.LOAD_RETRIES
        bulk_loader.LOAD_RETRIES = 0
        try:
            with self.assertRaises(ConnectionError):
                run_main(self.target, 'kt_split', pk='grp', workers=1)
        finally:
            bulk_loader.LOAD_RETRIES = saved
        self.assertNotIn('kt_split:1000', self.keys('kt_split:*'))
        FakeSource.fetches = []
        out = run_main(self.target, 'kt_split', pk='grp', workers=1)
        self.assertIn('resuming an incomplete load', out)
        self.assertEqual(FakeSource.fetches[0][0], 3, 'at group 3, not after it')
        self.assertEqual(len(self.keys('kt_split:*')), 2500)


class EndToEndStandalone(_EndToEnd, unittest.TestCase):
    pass


class EndToEndCluster(_EndToEnd, unittest.TestCase):
    CLUSTER = True
    ENV = 'ECDL_TEST_VALKEY_CLUSTER'


if __name__ == '__main__':
    unittest.main()
