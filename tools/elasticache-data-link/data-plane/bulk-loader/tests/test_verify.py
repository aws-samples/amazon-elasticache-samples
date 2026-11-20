"""D6: the loader reads back what it wrote. After a failed attempt it resumes
from the first batch the target lost, and after the load every chunk's last
batches and a sample of its keys must be on the target, or the chunk is loaded
again (and the load fails if the loss remains)."""

import contextlib
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bulk_loader  # noqa: E402
from tests import test_denorm as fakes  # noqa: E402

V = '1700000000000-0'


class Rows(object):
    """Rows lo..n in key order. `on_fail(store)` runs when the read fails,
    which is how a test makes the target lose writes (a failover)."""

    def __init__(self, n, fail_at=None, on_fail=None, store=None, starts=None):
        self.n, self.fail_at, self.on_fail, self.store = n, fail_at, on_fail, store
        self.starts = starts if starts is not None else []

    def connect(self):
        pass

    def fetch_iter(self, table, pk, lo, hi, batch=None):
        self.starts.append(lo)
        for pk_value in range(lo, min(hi, self.n) + 1):
            if self.fail_at is not None and pk_value == self.fail_at:
                self.fail_at = None
                if self.on_fail:
                    self.on_fail(self.store)
                raise ConnectionError('Connection reset by peer')
            yield {'id': pk_value, 'v': str(pk_value)}


@contextlib.contextmanager
def patched(adapter, store, version=V, batches=None, client=None):
    saved = (bulk_loader.ADAPTERS, bulk_loader.target_client, bulk_loader._sleep,
             os.environ.get('LOAD_VERSION'), bulk_loader.VERIFY_BATCHES)
    bulk_loader.ADAPTERS = {'mysql': lambda cfg: adapter}
    bulk_loader.target_client = client or (lambda cfg: fakes.FakeValkey(store))
    bulk_loader._sleep = lambda s: None
    if version:
        os.environ['LOAD_VERSION'] = version
    else:
        os.environ.pop('LOAD_VERSION', None)
    if batches is not None:
        bulk_loader.VERIFY_BATCHES = batches
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            yield out
    finally:
        (bulk_loader.ADAPTERS, bulk_loader.target_client, bulk_loader._sleep,
         env, bulk_loader.VERIFY_BATCHES) = saved
        if env is None:
            os.environ.pop('LOAD_VERSION', None)
        else:
            os.environ['LOAD_VERSION'] = env


def task(hi, lo=1, replace=False, widx=0):
    return ('mysql', {}, {}, 't', 'id', 't:', lo, hi, None, 't', widx, '', replace)


def lose(*pks):
    def drop(store):
        for pk in pks:
            store.hashes.pop('t:{}'.format(pk), None)
    return drop


class MissingWrites(unittest.TestCase):
    def setUp(self):
        self.store = fakes.FakeStore()
        self.vk = fakes.FakeValkey(self.store)

    def put(self, key, version):
        self.store.hashes[key] = {'v': '1'} if version is None else {'_ecdl_v': version}

    def test_nothing_to_check_sends_nothing(self):
        class NoPipe(object):
            def pipeline(self, transaction=False):
                raise AssertionError('no round trip for an empty batch')
        self.assertEqual(bulk_loader.missing_writes(NoPipe(), []), [])

    def test_version_must_be_this_load_or_newer(self):
        self.put('a', V)                      # this load
        self.put('b', '1700000000001-0')      # a newer CDC change
        self.put('c', '1699999999999-9')      # an earlier load's copy
        self.put('d', None)                   # no version at all
        lost = bulk_loader.missing_writes(self.vk, ['a', 'b', 'c', 'd', 'e'], version=V)
        self.assertEqual([x.split(' ')[0] for x in lost], ['c', 'd', 'e'])
        self.assertIn('version 1699999999999-9', lost[0])
        self.assertIn('version none', lost[2])

    def test_sequence_part_of_the_version_counts(self):
        self.put('a', '1700000000000-1')
        self.assertEqual(bulk_loader.missing_writes(self.vk, ['a'], version=V), [])
        self.put('a', '1700000000000-0')
        self.assertEqual(bulk_loader.missing_writes(self.vk, ['a'], version='1700000000000-1'),
                         ['a (version 1700000000000-0)'])

    def test_without_a_version_existence_is_checked(self):
        self.put('a', None)
        self.assertEqual(bulk_loader.missing_writes(self.vk, ['a', 'b']), ['b (absent)'])

    def test_a_dropped_key_must_stay_gone_and_index_entries_must_exist(self):
        self.put('gone', V)
        self.store.sets['idx'] = {'1'}
        lost = bulk_loader.missing_writes(self.vk, [], dropped=['gone', 'away'],
                                          members=[('idx', '1'), ('idx', '2')], version=V)
        self.assertEqual(lost, ['gone (deleted by this load, present again)', 'idx lacks 2'])


class Sample(unittest.TestCase):
    def test_bounded_and_spread_over_the_whole_chunk(self):
        s = bulk_loader._Sample(200)
        for i in range(100000):
            s.add(i)
        self.assertLessEqual(len(s.keys), 200)
        self.assertGreaterEqual(len(s.keys), 100)
        self.assertEqual(s.keys[0], 0)
        self.assertGreater(s.keys[-1], 99000)
        gaps = {b - a for a, b in zip(s.keys, s.keys[1:])}
        self.assertEqual(len(gaps), 1, 'evenly spaced')

    def test_small_chunks_are_checked_in_full(self):
        s = bulk_loader._Sample(200)
        for i in range(150):
            s.add(i)
        self.assertEqual(s.keys, list(range(150)))


class RewindAfterFailure(unittest.TestCase):
    """A failover loses the writes just before the error. The retry must
    resume before them, not after the last key the old primary confirmed."""

    def run_chunk(self, n, fail_at, on_fail, batches=None):
        store, starts = fakes.FakeStore(), []
        rows = Rows(n, fail_at, on_fail, store, starts)
        with patched(rows, store, batches=batches) as out:
            result = bulk_loader.load_chunk(task(n))
        return result, store, starts, out.getvalue()

    def test_nothing_lost_resumes_after_the_confirmed_key(self):
        result, store, starts, _ = self.run_chunk(2500, 2200, None)
        self.assertEqual(starts, [1, 2001])
        self.assertEqual((result['written'], len(store.hashes)), (2500, 2500))

    def test_a_lost_last_batch_is_written_again(self):
        result, store, starts, out = self.run_chunk(2500, 2200, lose(1500, 1999))
        self.assertEqual(starts, [1, 1001])
        self.assertEqual((result['written'], len(store.hashes)), (2500, 2500))
        self.assertIn('lost confirmed writes', out)

    def test_a_lost_first_batch_restarts_the_chunk(self):
        result, store, starts, _ = self.run_chunk(2500, 2200, lose(3))
        self.assertEqual(starts, [1, 1])
        self.assertEqual((result['written'], len(store.hashes)), (2500, 2500))

    def test_a_loss_older_than_the_remembered_batches_restarts_the_chunk(self):
        # Five batches remembered of nine; the loss is in the first batch, which
        # is no longer remembered, so its neighbour's loss marks it.
        result, store, starts, out = self.run_chunk(9500, 9200, lose(2500, 4001), batches=5)
        self.assertEqual(starts, [1, 1])
        self.assertEqual((result['written'], len(store.hashes)), (9500, 9500))
        self.assertIn('loading the chunk again', out)

    def test_an_unreadable_target_rewrites_the_remembered_batches(self):
        store, starts = fakes.FakeStore(), []
        calls = {'n': 0}

        def client(cfg):
            calls['n'] += 1
            if calls['n'] == 2:   # the read-back after the failure
                raise ConnectionError('target still failing over')
            return fakes.FakeValkey(store)
        rows = Rows(2500, 2200, None, store, starts)
        with patched(rows, store, client=client, batches=5) as out:
            result = bulk_loader.load_chunk(task(2500))
        self.assertEqual(starts, [1, 1])
        self.assertEqual(result['written'], 2500)
        self.assertIn('could not read back', out.getvalue())


class CheckAfterTheLoad(unittest.TestCase):
    def load(self, n, store, replace=False, version=V):
        with patched(Rows(n), store, version=version):
            return [bulk_loader.load_chunk(task(n, replace=replace))]

    def verify(self, n, store, results, version=V, refuse=()):
        class Refusing(fakes.FakeValkey):
            def pipeline(self, transaction=False):
                pipe = super(Refusing, self).pipeline(transaction)
                real = pipe.hset

                def hset(key, mapping=None):
                    if key in refuse:
                        return pipe
                    return real(key, mapping)
                pipe.hset = hset
                return pipe
        with patched(Rows(n), store, version=version,
                     client=lambda cfg: Refusing(store)) as out:
            bulk_loader.verify_and_repair({}, [task(n)], results, version)
        return out.getvalue()

    def test_an_intact_load_passes_and_says_so(self):
        store = fakes.FakeStore()
        results = self.load(3000, store)
        out = self.verify(3000, store, results)
        self.assertIn("all at this load's version or newer", out)

    def test_a_lost_tail_is_loaded_again_from_that_batch(self):
        store = fakes.FakeStore()
        results = self.load(3000, store)
        del store.hashes['t:2990']
        out = self.verify(3000, store, results)
        self.assertIn('t:2990', store.hashes)
        self.assertIn('Loading it again from pk 2001', out)

    def test_a_lost_sampled_key_reloads_the_whole_chunk(self):
        store = fakes.FakeStore()
        results = self.load(9000, store)
        sampled = results[0]['sample'][3]
        tail_keys = {k for b in results[0]['tail'] for k in b['kept']}
        self.assertNotIn(sampled, tail_keys)
        del store.hashes[sampled]
        out = self.verify(9000, store, results)
        self.assertIn(sampled, store.hashes)
        self.assertIn('Loading it again from pk 1', out)

    def test_a_loss_that_stays_fails_the_load(self):
        store = fakes.FakeStore()
        results = self.load(3000, store)
        del store.hashes['t:2999']
        with self.assertRaises(SystemExit) as caught:
            self.verify(3000, store, results, refuse={'t:2999'})
        self.assertIn('FATAL', str(caught.exception))
        self.assertIn('t:2999', str(caught.exception))

    def test_an_older_copy_does_not_count_as_written(self):
        store = fakes.FakeStore()
        results = self.load(3000, store)
        store.hashes['t:2995']['_ecdl_v'] = '1600000000000-0'
        out = self.verify(3000, store, results)
        self.assertEqual(store.hashes['t:2995']['_ecdl_v'], V)
        self.assertIn('chunk 0 is missing 1', out)

    def test_without_a_version_existence_is_checked(self):
        store = fakes.FakeStore()
        results = self.load(3000, store, version=None)
        del store.hashes['t:2500']
        out = self.verify(3000, store, results, version=None)
        self.assertIn('t:2500', store.hashes)
        self.assertIn('existence only', out)


class DenormAndReplace(unittest.TestCase):
    SPEC = {'tables': {'t': {'denormalize': [{
        'parent_table': 'customers', 'join_field': 'cid', 'fields': ['name'],
        'parent_key_prefix': 'customers:'}]}}}

    def test_a_lost_reverse_index_entry_is_written_again(self):
        class Children(Rows):
            def fetch_iter(self, table, pk, lo, hi, batch=None):
                for row in Rows.fetch_iter(self, table, pk, lo, hi, batch):
                    row['cid'] = str(row['id'] % 7)
                    yield row
        store = fakes.FakeStore()
        args = ('mysql', {}, {}, 't', 'id', 't:', 1, 2500, self.SPEC, 't', 0, '')
        with patched(Children(2500), store):
            results = [bulk_loader.load_chunk(args)]
        self.assertTrue(any(b['members'] for b in results[0]['tail']))
        store.sets['idx:t:by:customers:5'].discard('2497')
        with patched(Children(2500), store) as out:
            bulk_loader.verify_and_repair({}, [args], results, V)
        self.assertIn('2497', store.sets['idx:t:by:customers:5'])
        self.assertIn('lacks 2497', out.getvalue())

    def test_a_replacing_denorm_load_checks_dropped_rows_too(self):
        spec = {'tables': {'t': dict(self.SPEC['tables']['t'], filter={
            'field': 'v', 'op': '!=', 'value': '2400'})}}

        class Children(Rows):
            def fetch_iter(self, table, pk, lo, hi, batch=None):
                for row in Rows.fetch_iter(self, table, pk, lo, hi, batch):
                    row['cid'] = '1'
                    yield row
        store = fakes.FakeStore()
        args = ('mysql', {}, {}, 't', 'id', 't:', 1, 2500, spec, 't', 0, '', True)
        with patched(Children(2500), store):
            results = [bulk_loader.load_chunk(args)]
        self.assertIn('t:2400', [k for b in results[0]['tail'] for k in b['dropped']])
        store.hashes['t:2400'] = {'v': '2400'}
        with patched(Children(2500), store) as out:
            bulk_loader.verify_and_repair({}, [args], results, V)
        self.assertNotIn('t:2400', store.hashes)
        self.assertIn('present again', out.getvalue())

    def test_a_replacing_load_checks_that_dropped_rows_stay_deleted(self):
        spec = {'tables': {'t': {'filter': {'field': 'v', 'op': '!=', 'value': '2400'}}}}
        store = fakes.FakeStore()
        args = ('mysql', {}, {}, 't', 'id', 't:', 1, 2500, spec, 't', 0, '', True)
        with patched(Rows(2500), store):
            results = [bulk_loader.load_chunk(args)]
        self.assertNotIn('t:2400', store.hashes)
        self.assertIn('t:2400', [k for b in results[0]['tail'] for k in b['dropped']])
        store.hashes['t:2400'] = {'v': '2400'}     # the delete was lost
        with patched(Rows(2500), store) as out:
            bulk_loader.verify_and_repair({}, [args], results, V)
        self.assertNotIn('t:2400', store.hashes)
        self.assertIn('present again', out.getvalue())


class RealValkey(unittest.TestCase):
    """Against a real Valkey (ECDL_TEST_VALKEY, ECDL_TEST_VALKEY_CLUSTER), with
    the real client: the read-back commands work in both topologies."""

    def setUp(self):
        # test_target_client replaces the valkey modules with recording stubs;
        # this test needs the real library, and puts the stubs back afterwards.
        self.saved = {k: v for k, v in sys.modules.items()
                      if k == 'valkey' or k.startswith('valkey.')}
        if self.saved and not getattr(sys.modules.get('valkey'), '__file__', None):
            for k in self.saved:
                del sys.modules[k]

    def tearDown(self):
        for k in [k for k in sys.modules if k == 'valkey' or k.startswith('valkey.')]:
            del sys.modules[k]
        sys.modules.update(self.saved)

    def target(self, env, cluster):
        addr = os.environ.get(env)
        if not addr:
            self.skipTest(env + ' not set')
        try:
            import valkey  # noqa: F401
        except ImportError:
            self.skipTest('valkey-py not installed')
        host, port = addr.rsplit(':', 1)
        return {'host': host, 'port': int(port), 'ssl': False, 'cluster': cluster}

    def run_real(self, cfg):
        real_client = bulk_loader.target_client
        rows = Rows(3000)
        args = ('mysql', {}, cfg, 't', 'id', 'vtest:t:', 1, 3000, None, 't', 0, '')
        with patched(rows, None, client=real_client):
            results = [bulk_loader.load_chunk(args)]
        vk = real_client(cfg)
        try:
            vk.delete('vtest:t:2999')
            vk.hset('vtest:t:2998', '_ecdl_v', '1600000000000-0')
            with patched(rows, None, client=real_client) as out:
                bulk_loader.verify_and_repair(cfg, [args], results, V)
            self.assertEqual(vk.hget('vtest:t:2999', '_ecdl_v'), V.encode())
            self.assertEqual(vk.hget('vtest:t:2998', '_ecdl_v'), V.encode())
            self.assertIn('chunk 0 is missing 2', out.getvalue())
        finally:
            for pk in range(1, 3001):
                vk.delete('vtest:t:{}'.format(pk))
            vk.close()

    def test_standalone(self):
        self.run_real(self.target('ECDL_TEST_VALKEY', False))

    def test_cluster(self):
        self.run_real(self.target('ECDL_TEST_VALKEY_CLUSTER', True))


if __name__ == '__main__':
    unittest.main()
