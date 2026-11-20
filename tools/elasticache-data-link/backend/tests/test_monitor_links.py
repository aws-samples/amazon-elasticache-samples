"""Tests for watching a chosen link, rather than one hardcoded pipeline.

Why this exists: the monitor's configuration described a single pipeline. That was
fine while there was one, and became actively misleading the moment the control
plane could deploy more — someone deployed a link, opened the page, and read a
healthy dashboard about a completely different pipeline. A healthy status for the
wrong pipeline is worse than no status at all.

Run:  DEMO_HISTORY_ENABLED=false python3 test_monitor_links.py
"""

import os
import sys
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, _BACKEND)                   # backend/  (import monitor)
sys.path.insert(0, os.path.dirname(_BACKEND))  # repo root (backend modules import ecdl_link)
# Forced, not setdefault: the sampler thread would run the collectors against
# whatever a test has patched at that moment (see test_monitor.py).
os.environ['DEMO_HISTORY_ENABLED'] = 'false'

import monitor  # noqa: E402


# Shapes taken from the real stacks a deploy produces.
CDC_PARAMS = {
    'SourceDBHost': 'shop.cluster-x.us-east-2.rds.amazonaws.com',
    'SourceDBPort': '3306',
    'SourceDBUser': 'admin',
    'SourceDBName': 'Inventory',
    'BrokerHost': 'master.ecdl-shop-broker.use2.cache.amazonaws.com',
    'BrokerPort': '6379',
    'BrokerSSL': 'true',
    'TargetHost': 'clustercfg.shop-cache.use2.cache.amazonaws.com',
    'TargetPort': '6379',
    'TargetSSL': 'true',
    'TargetCluster': 'true',
    'Streams': 'data:shop.Inventory.products',
    'Tables': 'Inventory.products',
    'ClusterName': 'ecdl-shop',
    'SchemaHistoryKey': 'ecdl:shop:schema_history',
}

BROKER_OUTPUTS = {
    'PrimaryEndpoint': 'master.ecdl-shop-broker.use2.cache.amazonaws.com',
    'Port': '6379',
    'TransitEncryption': 'true',
    'AuthSecretArn': 'arn:aws:secretsmanager:us-east-2:1:secret:BrokerAuthSecret-abc',
}


class FakeCfn(object):
    def __init__(self, stacks=None, summaries=None):
        self.stacks = stacks or {}
        self.summaries = summaries or []

    def describe_stacks(self, StackName=None):
        if StackName not in self.stacks:
            raise Exception(
                'Stack with id {} does not exist'.format(StackName))
        return {'Stacks': [self.stacks[StackName]]}

    def list_stacks(self, **kwargs):
        return {'StackSummaries': self.summaries}


def install(cfn):
    monitor._LINK_CACHE.clear()
    monitor._cfn = lambda: cfn


class OverlayTest(unittest.TestCase):
    """The mechanism: a per-request view of the configuration."""

    def test_a_watched_value_replaces_the_default(self):
        default = monitor.CFG['stream_key']
        with monitor.watching({'stream_key': 'data:other.T'}):
            self.assertEqual(monitor.CFG['stream_key'], 'data:other.T')
        self.assertEqual(monitor.CFG['stream_key'], default)

    def test_unlisted_keys_fall_through_to_the_default(self):
        with monitor.watching({'stream_key': 'x'}):
            self.assertEqual(monitor.CFG['region'],
                             monitor._DEFAULTS['region'])

    def test_items_reflects_the_overlay(self):
        # /health publishes CFG for diagnosis. Publishing the defaults while
        # serving a different link would describe a pipeline other than the one being viewed.
        with monitor.watching({'stream_key': 'data:other.T'}):
            self.assertEqual(dict(monitor.CFG.items())['stream_key'],
                             'data:other.T')

    def test_one_thread_cannot_see_another_threads_link(self):
        # The dev server is threaded. A swap on the shared dict would let a request
        # for link A be answered with link B's endpoints halfway through.
        seen = {}

        def worker():
            with monitor.watching({'stream_key': 'thread-B'}):
                barrier.wait()
                barrier.wait()
                seen['b'] = monitor.CFG['stream_key']

        barrier = threading.Barrier(2)
        thread = threading.Thread(target=worker)
        thread.start()
        with monitor.watching({'stream_key': 'thread-A'}):
            barrier.wait()
            seen['a'] = monitor.CFG['stream_key']
            barrier.wait()
        thread.join()
        self.assertEqual(seen['a'], 'thread-A')
        self.assertEqual(seen['b'], 'thread-B')

    def test_nesting_restores_the_outer_view(self):
        with monitor.watching({'stream_key': 'outer'}):
            with monitor.watching({'stream_key': 'inner'}):
                self.assertEqual(monitor.CFG['stream_key'], 'inner')
            self.assertEqual(monitor.CFG['stream_key'], 'outer')

    def test_watched_link_names_itself(self):
        with monitor.watching({'link_name': 'shop'}):
            self.assertEqual(monitor.CFG.watched_link(), 'shop')
        self.assertIsNone(monitor.CFG.watched_link())


class FanOutTest(unittest.TestCase):
    """The overlay must survive the dashboard's thread pool.

    This is the bug that made the whole mechanism appear to work when it did not:
    link_config resolved correctly, `watching()` applied correctly, and every
    collector called directly honoured it -- but the dashboard collects in a
    ThreadPoolExecutor, and thread-local state is not inherited by pool threads.
    So the page served the DEFAULT pipeline's numbers under the chosen link's name,
    with no error anywhere. The page showed healthy status for the wrong pipeline.
    """

    def test_a_pool_thread_sees_the_overlay(self):
        from concurrent.futures import ThreadPoolExecutor

        def read():
            return monitor.CFG['stream_key']

        with monitor.watching({'stream_key': 'data:chosen.T'}):
            overlay = monitor.current_overlay()
            with ThreadPoolExecutor(max_workers=1) as pool:
                # Submitted bare, a worker thread sees the default: the bug.
                self.assertEqual(pool.submit(read).result(),
                                 monitor._DEFAULTS['stream_key'])
                # Wrapped, it sees the chosen link: the fix.
                wrapped = monitor.in_overlay(overlay, read)
                self.assertEqual(pool.submit(wrapped).result(), 'data:chosen.T')

    def test_in_overlay_is_a_no_op_without_one(self):
        def read():
            return monitor.CFG['stream_key']
        self.assertIs(monitor.in_overlay(None, read), read)

    def test_in_overlay_passes_arguments_through(self):
        # collect_dashboard submits `safe(fn)`, so the wrapper has to forward args.
        def add(a, b=0):
            return a + b
        wrapped = monitor.in_overlay({'x': 1}, add)
        self.assertEqual(wrapped(2, b=3), 5)

    def test_current_overlay_is_none_by_default(self):
        self.assertIsNone(monitor.current_overlay())


class LinkConfigTest(unittest.TestCase):
    """Building that view from the stacks a deploy left behind."""

    def setUp(self):
        install(FakeCfn(stacks={
            'ecdl-shop-cdc': {'Parameters': [
                {'ParameterKey': k, 'ParameterValue': v}
                for k, v in CDC_PARAMS.items()]},
            'ecdl-shop-broker': {'Outputs': [
                {'OutputKey': k, 'OutputValue': v}
                for k, v in BROKER_OUTPUTS.items()]},
        }))

    def test_endpoints_come_from_the_stack(self):
        overlay = monitor.link_config('shop')
        self.assertEqual(overlay['db_host'], CDC_PARAMS['SourceDBHost'])
        self.assertEqual(overlay['cache_host'], CDC_PARAMS['TargetHost'])
        self.assertEqual(overlay['stream_key'], 'data:shop.Inventory.products')
        self.assertEqual(overlay['ecs_cluster'], 'ecdl-shop')

    def test_ports_and_flags_are_typed(self):
        # They arrive as CloudFormation strings; a collector comparing '6379' to an
        # int, or treating 'false' as truthy, would be wrong without raising any error.
        overlay = monitor.link_config('shop')
        self.assertEqual(overlay['db_port'], 3306)
        self.assertIsInstance(overlay['cache_port'], int)
        self.assertIs(overlay['cache_cluster_mode'], True)
        self.assertIs(overlay['broker_tls'], True)

    def test_false_flags_are_not_read_as_true(self):
        params = dict(CDC_PARAMS, TargetCluster='false', TargetSSL='false')
        install(FakeCfn(stacks={
            'ecdl-shop-cdc': {'Parameters': [
                {'ParameterKey': k, 'ParameterValue': v}
                for k, v in params.items()]},
        }))
        overlay = monitor.link_config('shop')
        self.assertIs(overlay['cache_cluster_mode'], False)
        self.assertIs(overlay['cache_tls'], False)

    def test_the_key_prefix_follows_the_table(self):
        overlay = monitor.link_config('shop')
        self.assertEqual(overlay['table'], 'products')
        self.assertEqual(overlay['key_prefix'], 'products:')

    def test_service_names_follow_the_cluster(self):
        overlay = monitor.link_config('shop')
        self.assertEqual(overlay['debezium_service'], 'ecdl-shop-cdc-debezium')
        self.assertEqual(overlay['sink_service'], 'ecdl-shop-cdc-sink')

    def test_the_broker_credential_is_taken_from_the_broker_stack(self):
        # Without it the broker collector cannot authenticate against a hardened
        # broker, and a broker refusing an unauthenticated read looks like a broker
        # that is down.
        overlay = monitor.link_config('shop')
        self.assertEqual(overlay['broker_secret_id'],
                         BROKER_OUTPUTS['AuthSecretArn'])

    def test_a_link_with_no_broker_stack_still_resolves(self):
        # An initial_load-only link never creates a broker, which is not an error.
        install(FakeCfn(stacks={
            'ecdl-loadonly-cdc': {'Parameters': [
                {'ParameterKey': k, 'ParameterValue': v}
                for k, v in CDC_PARAMS.items()]},
        }))
        overlay = monitor.link_config('loadonly')
        self.assertEqual(overlay['cache_host'], CDC_PARAMS['TargetHost'])

    def test_table_schemas_are_kept(self):
        # Jira story F114: Tables names the schema; dropping it queried SourceDBName.
        overlay = monitor.link_config('shop')
        self.assertEqual(overlay['source_tables'], {'products': 'Inventory.products'})
        self.assertEqual(overlay['table'], 'products')

    def test_an_unknown_link_is_refused_by_name(self):
        # Not silently answered with the default pipeline, which is the
        # purpose of this mechanism.
        with self.assertRaises(ValueError) as caught:
            monitor.link_config('nope')
        self.assertIn('nope', str(caught.exception))
        self.assertIn('ecdl-nope-cdc', str(caught.exception))

    def test_the_heartbeat_probe_is_on_by_default_for_a_watched_link(self):
        # The active CDC-lag probe is enabled for a deployed link. It no longer
        # inserts a row into the customer's table: for a per-link config the probe
        # uses the reuse-and-restore path (stamp one column of an existing cached
        # row with a nonce, time its arrival, then restore the value), so the
        # original "don't write to a customer table" objection no longer applies.
        # It stays switchable off with DEMO_PROBE_ENABLED=false.
        overlay = monitor.link_config('shop')
        self.assertIs(overlay['probe_enabled'], True)


class ClientFactoryTest(unittest.TestCase):
    """_cfn must use this module's real client factory.

    It was written against a `_client` helper that does not exist here, so every
    link lookup failed with "name '_client' is not defined" -- reported as
    a listing problem, which made a plain typo look like an AWS permissions issue.
    The tests all passed because they replace _cfn entirely.
    """

    def test_cfn_is_built_from_the_module_factory(self):
        calls = []
        original = monitor.aws
        monitor.aws = lambda service: calls.append(service) or 'client'
        try:
            # Bypass the fakes the other tests install.
            self.assertEqual(monitor._cfn.__wrapped__('x')
                             if hasattr(monitor._cfn, '__wrapped__')
                             else monitor._cfn(), 'client')
        finally:
            monitor.aws = original
        self.assertEqual(calls, ['cloudformation'])

    def test_the_factory_name_exists(self):
        # The specific mistake: a helper that was never defined in this module.
        self.assertTrue(callable(getattr(monitor, 'aws', None)))
        self.assertIsNone(getattr(monitor, '_client', None))


class SharedClientPoolTest(unittest.TestCase):
    """Pooled connections must be keyed by endpoint, not by the word 'target'.

    The subtlest bug of the set. Every collector honoured the overlay, the endpoint
    strings were right, and the NUMBERS came from a connection pooled under the key
    'target' -- created once for whichever pipeline was configured first and handed
    to every later request. So the page named one cache and counted another's keys,
    with nothing anywhere reporting a problem.
    """

    def setUp(self):
        monitor._shared.clear()
        self.built = []

        def factory():
            # Record what the factory was asked to connect to.
            self.built.append(monitor.CFG['cache_host'])
            return 'client-for-' + monitor.CFG['cache_host']

        self.factory = factory

    def tearDown(self):
        monitor._shared.clear()

    def test_two_links_get_two_clients(self):
        first = monitor._get_shared(
            'target:' + monitor._endpoint_identity(
                'cache_host', 'cache_port', 'cache_tls', 'cache_cluster_mode'),
            self.factory)
        with monitor.watching({'cache_host': 'other-cache.example.com'}):
            second = monitor._get_shared(
                'target:' + monitor._endpoint_identity(
                    'cache_host', 'cache_port', 'cache_tls', 'cache_cluster_mode'),
                self.factory)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.built), 2)

    def test_the_same_link_reuses_its_client(self):
        # Pooling still has to work: reconnecting every two seconds was the reason
        # this cache exists.
        key = 'target:' + monitor._endpoint_identity(
            'cache_host', 'cache_port', 'cache_tls', 'cache_cluster_mode')
        monitor._get_shared(key, self.factory)
        monitor._get_shared(key, self.factory)
        self.assertEqual(len(self.built), 1)

    def test_the_key_changes_with_tls_and_topology(self):
        base = monitor._endpoint_identity('cache_host', 'cache_port', 'cache_tls',
                                          'cache_cluster_mode')
        with monitor.watching({'cache_tls': not monitor.CFG['cache_tls']}):
            self.assertNotEqual(base, monitor._endpoint_identity(
                'cache_host', 'cache_port', 'cache_tls', 'cache_cluster_mode'))
        with monitor.watching({'cache_cluster_mode':
                               not monitor.CFG['cache_cluster_mode']}):
            self.assertNotEqual(base, monitor._endpoint_identity(
                'cache_host', 'cache_port', 'cache_tls', 'cache_cluster_mode'))

    def test_eviction_uses_the_same_key_as_the_pool(self):
        # A mismatch here means a failed client is never dropped, so every later
        # poll reuses the broken connection.
        key = 'target:' + monitor._endpoint_identity(
            'cache_host', 'cache_port', 'cache_tls', 'cache_cluster_mode')
        monitor._get_shared(key, self.factory)
        self.assertIn(key, monitor._shared)
        monitor._drop_target()
        self.assertNotIn(key, monitor._shared)

    def test_two_polls_racing_create_one_client(self):
        # Jira story F113: both callers found no client, both built one, and the one stored
        # first was overwritten and never closed.
        started, release = threading.Event(), threading.Event()
        built = []
        def slow_factory():
            built.append(1)
            started.set()
            release.wait(5)
            return 'client-{}'.format(len(built))
        got = []
        first = threading.Thread(target=lambda: got.append(
            monitor._get_shared('race', slow_factory)))
        first.start()
        self.assertTrue(started.wait(5))
        # The second caller arrives while the first is still connecting.
        second = threading.Thread(target=lambda: got.append(
            monitor._get_shared('race', slow_factory)))
        second.start()
        second.join(0.2)
        self.assertTrue(second.is_alive(), 'the second caller must wait, not build')
        release.set()
        first.join(5)
        second.join(5)
        self.assertEqual(len(built), 1)
        self.assertEqual(got, ['client-1', 'client-1'])
        self.assertEqual(monitor._shared['race'], 'client-1')

    def test_a_failed_create_lets_the_next_caller_try_again(self):
        calls = []
        def failing():
            calls.append(1)
            raise ConnectionError('refused')
        with self.assertRaises(ConnectionError):
            monitor._get_shared('flaky', failing)
        self.assertNotIn('flaky', monitor._shared)
        self.assertEqual(monitor._get_shared('flaky', lambda: 'ok'), 'ok')

    def test_creating_one_endpoint_does_not_block_another(self):
        release = threading.Event()
        started = threading.Event()
        def slow():
            started.set()
            release.wait(5)
            return 'slow'
        worker = threading.Thread(target=lambda: monitor._get_shared('a', slow))
        worker.start()
        self.assertTrue(started.wait(5))
        got = []
        other = threading.Thread(target=lambda: got.append(
            monitor._get_shared('b', lambda: 'fast')))
        other.start()
        other.join(1)
        blocked = other.is_alive()
        release.set()
        worker.join(5)
        other.join(5)
        self.assertFalse(blocked, 'a slow connect to one endpoint held up another')
        self.assertEqual(got, ['fast'])


class CloudWatchIdentityTest(unittest.TestCase):
    """The CloudWatch panels must follow the link too.

    They were the last thing still describing the default pipeline, and the hardest
    to notice: every live panel followed the chosen link while Aurora's CPU and
    capacity, and Valkey's engine CPU and memory, kept reporting the DEFAULT
    clusters. Two links sharing a source hid it completely, because the numbers
    looked plausible.
    """

    def setUp(self):
        install(FakeCfn(stacks={
            'ecdl-shop-cdc': {'Parameters': [
                {'ParameterKey': k, 'ParameterValue': v}
                for k, v in CDC_PARAMS.items()]},
        }))

    def test_the_rds_cluster_id_comes_from_the_endpoint(self):
        overlay = monitor.link_config('shop')
        self.assertEqual(overlay['rds_cluster_id'], 'shop')

    def test_the_cache_node_id_comes_from_a_cluster_endpoint(self):
        # clustercfg.<group>.<...> -> the group, then its first node.
        overlay = monitor.link_config('shop')
        self.assertEqual(overlay['cache_replication_group'], 'shop-cache')
        self.assertEqual(overlay['cache_cluster_id'], 'shop-cache-0001-001')

    def test_a_single_node_target_endpoint_also_resolves(self):
        params = dict(CDC_PARAMS,
                      TargetHost='master.plain-cache.abc.ng.0001.use2.cache.amazonaws.com')
        install(FakeCfn(stacks={'ecdl-shop-cdc': {'Parameters': [
            {'ParameterKey': k, 'ParameterValue': v}
            for k, v in params.items()]}}))
        overlay = monitor.link_config('shop')
        self.assertEqual(overlay['cache_replication_group'], 'plain-cache')

    def test_an_endpoint_with_no_prefix_resolves(self):
        params = dict(CDC_PARAMS, TargetHost='bare-cache.abc.use2.cache.amazonaws.com')
        install(FakeCfn(stacks={'ecdl-shop-cdc': {'Parameters': [
            {'ParameterKey': k, 'ParameterValue': v}
            for k, v in params.items()]}}))
        self.assertEqual(monitor.link_config('shop')['cache_replication_group'],
                         'bare-cache')

    def test_credentials_are_carried(self):
        # Otherwise a live panel connects to the link's endpoint holding the
        # default pipeline's password.
        params = dict(CDC_PARAMS,
                      DBSecretArn='arn:aws:secretsmanager:us-east-2:1:secret:shop-db',
                      TargetSecretArn='arn:aws:secretsmanager:us-east-2:1:secret:shop-cache')
        install(FakeCfn(stacks={'ecdl-shop-cdc': {'Parameters': [
            {'ParameterKey': k, 'ParameterValue': v}
            for k, v in params.items()]}}))
        overlay = monitor.link_config('shop')
        self.assertIn('shop-db', overlay['db_secret_id'])
        self.assertIn('shop-cache', overlay['cache_secret_id'])

    def test_the_bulkload_key_is_per_link(self):
        # The hash the loader itself writes: ecdl:<link>:bulkload:<table>.
        self.assertEqual(monitor.link_config('shop')['bulkload_key'],
                         'ecdl:shop:bulkload:products')
    def test_one_bulkload_key_per_table_with_the_key_prefix(self):
        params = dict(CDC_PARAMS, Tables='Inventory.products,Inventory.orders',
                      KeyPrefix='v2')
        install(FakeCfn(stacks={'ecdl-shop-cdc': {'Parameters': [
            {'ParameterKey': k, 'ParameterValue': v} for k, v in params.items()]}}))
        overlay = monitor.link_config('shop')
        self.assertEqual(overlay['bulkload_keys'],
                         ['ecdl:shop:bulkload:v2:products', 'ecdl:shop:bulkload:v2:orders'])
        self.assertEqual(overlay['bulkload_key'], 'ecdl:shop:bulkload:v2:products')

    def test_host_scoped_panels_are_turned_off(self):
        # The app-latency scrape is a demo application on this host, unrelated to
        # any link. Leaving it on would put one pipeline's read latencies inside
        # another's dashboard.
        overlay = monitor.link_config('shop')
        self.assertEqual(overlay['demo_app_url'], '')
        self.assertEqual(overlay['sink_metrics_url'], '')

    def test_nothing_identifying_the_default_pipeline_leaks_through(self):
        # The guard that would have caught the original omission: every config key
        # naming a specific pipeline must be answered by the overlay.
        overlay = monitor.link_config('shop')
        must_follow = ['db_host', 'db_name', 'rds_cluster_id', 'cache_host',
                       'cache_cluster_id', 'stream_key', 'offset_key',
                       'schema_history_key', 'ecs_cluster', 'debezium_service',
                       'sink_service', 'debezium_log_group', 'sink_log_group',
                       'table', 'key_prefix', 'bulkload_key']
        missing = [k for k in must_follow if k not in overlay]
        self.assertEqual(missing, [])


class DiscoverLinksTest(unittest.TestCase):

    def test_links_are_found_from_their_cdc_stacks(self):
        install(FakeCfn(summaries=[
            {'StackName': 'ecdl-shop-cdc', 'CreationTime': '2026-01-02'},
            {'StackName': 'ecdl-shop-broker', 'CreationTime': '2026-01-01'},
            {'StackName': 'ecdl-users-cache-cdc', 'CreationTime': '2026-01-03'},
            {'StackName': 'unrelated-stack', 'CreationTime': '2026-01-04'},
        ]))
        found = monitor.discover_links()
        self.assertEqual([f['link'] for f in found],
                         ['users-cache', 'shop'])      # newest first

    def test_a_broker_stack_alone_is_not_a_link(self):
        install(FakeCfn(summaries=[
            {'StackName': 'ecdl-orphan-broker', 'CreationTime': '2026-01-01'},
        ]))
        self.assertEqual(monitor.discover_links(), [])

    def test_a_listing_failure_is_reported_not_raised(self):
        class Broken(object):
            def list_stacks(self, **kwargs):
                raise Exception('AccessDenied')

        install(Broken())
        found = monitor.discover_links()
        self.assertTrue(found[0]['error'])


class EffectiveDefaultLinkTest(unittest.TestCase):
    """Which pipeline the trend sampler binds to when no link is selected.

    The bug this guards: a freshly installed control plane has no configured
    default link, so the background history sampler read the static placeholder
    endpoints forever and the whole trend showed 'no data' even though a link
    was deployed and healthy.
    """

    def test_a_configured_default_is_used_verbatim(self):
        install(FakeCfn(summaries=[
            {'StackName': 'ecdl-other-cdc', 'CreationTime': '2026-02-01'},
        ]))
        with monitor.watching({'default_link': 'shop'}):
            self.assertEqual(monitor._effective_default_link(), 'shop')

    def test_with_no_configured_default_the_newest_deployed_link_is_chosen(self):
        install(FakeCfn(summaries=[
            {'StackName': 'ecdl-shop-cdc', 'CreationTime': '2026-01-02'},
            {'StackName': 'ecdl-fresh-cdc', 'CreationTime': '2026-03-09'},
        ]))
        with monitor.watching({'default_link': ''}):
            self.assertEqual(monitor._effective_default_link(), 'fresh')

    def test_none_when_nothing_configured_and_nothing_deployed(self):
        install(FakeCfn(summaries=[
            {'StackName': 'unrelated-stack', 'CreationTime': '2026-01-01'},
        ]))
        with monitor.watching({'default_link': ''}):
            self.assertIsNone(monitor._effective_default_link())

    def test_a_listing_failure_falls_back_to_no_link_not_a_crash(self):
        class Broken(object):
            def list_stacks(self, **kwargs):
                raise Exception('AccessDenied')

        install(Broken())
        with monitor.watching({'default_link': ''}):
            self.assertIsNone(monitor._effective_default_link())


class ProbeRestoreTest(unittest.TestCase):
    """The reuse-and-restore probe must never leave a customer row changed.

    It stamps one column of an existing row, waits for the stamp in the cache,
    then restores the column. Two defects left rows changed for good: two
    probes overlapping (the second saved the first one's stamp as "original"),
    and an unconditional restore that reverted an application write made while
    the probe was waiting.
    """

    def setUp(self):
        self.row = {'id': 7, 'name': 'widget'}
        # What information_schema says about the columns (Jira story F149 tests vary it).
        self.columns = [('id', 'int', None, 'PRI', ''),
                        ('name', 'varchar', 64, '', ''),
                        ('updated_at', 'timestamp', None, '',
                         'DEFAULT_GENERATED on update CURRENT_TIMESTAMP')]
        self.updates = []
        self.fail_restore = False
        self.during_poll = None          # callable run on the first cache poll
        test = self

        class Cur:
            rowcount = 0

            def execute(self, sql, args=None):
                self.sql, self.args = sql, args
                text = ' '.join(sql.split())
                if text.startswith('UPDATE'):
                    test.updates.append(text)
                if text.startswith('UPDATE') and 'AND `name` = %s' in text:
                    if test.fail_restore:
                        raise RuntimeError('connection lost')
                    value, _pk, expect = args
                    self.rowcount = 0
                    if test.row['name'] == expect:
                        test.row['name'] = value
                        self.rowcount = 1
                elif text.startswith('UPDATE'):
                    test.row['name'] = args[0]
                    self.rowcount = 1

            def fetchone(self):
                return (self.row_value(),)

            def row_value(self):
                return test.row['name']

            def fetchall(self):
                if 'information_schema' in self.sql:
                    return list(test.columns)
                return [(7,)]

        class Conn:
            def cursor(self):
                return Cur()

            def close(self):
                pass

        class Target:
            def __init__(self):
                self.polls = 0

            def hgetall(self, key):
                return {'id': '7', 'name': test.row['name']}

            def hget(self, key, field):
                self.polls += 1
                if self.polls == 1 and test.during_poll:
                    test.during_poll()
                return test.row['name']

        self.saved = (monitor.mysql_conn, monitor.get_target, monitor.table_shape)
        monitor.mysql_conn = lambda: Conn()
        target = Target()
        monitor.get_target = lambda: target
        monitor.table_shape = lambda cur, table: {'pk': 'id', 'label': 'name',
                                                  'compare': ['name']}
        monitor._probe_state.clear()
        monitor.reset_state()              # the column list is cached per table
        self.overlay = {'link_name': 'shop', 'probe_enabled': True, 'table': 'items',
                        'key_prefix': 'items:', 'probe_timeout_ms': 200,
                        'probe_min_interval_sec': 0, 'transforms': {}}

    def tearDown(self):
        monitor.mysql_conn, monitor.get_target, monitor.table_shape = self.saved

    def probe(self):
        with monitor.watching(self.overlay):
            return monitor.run_probe()

    def test_the_probe_writes_the_table_in_its_own_schema(self):
        # Jira story F114: Tables 'Shop2.items' with SourceDBName 'Inventory' must stamp
        # Shop2.items, not Inventory.items.
        self.overlay['source_tables'] = {'items': 'Shop2.items'}
        out = self.probe()
        self.assertTrue(out['ok'], out)
        self.assertTrue(self.updates)
        for statement in self.updates:
            self.assertIn('`Shop2`.`items`', statement)

    def test_an_unreachable_target_leaves_no_database_connection_open(self):
        # Jira story F113: the database was opened first, so a target error leaked it.
        opened = []
        class Conn:
            closed = False
            def cursor(self):
                raise AssertionError('no query should run')
            def close(self):
                self.closed = True
        def connect():
            conn = Conn()
            opened.append(conn)
            return conn
        def no_target():
            raise ConnectionError('target unreachable')
        monitor.mysql_conn = connect
        monitor.get_target = no_target
        with self.assertRaises(ConnectionError):
            self.probe()
        self.assertEqual([c for c in opened if not c.closed], [])

    def test_a_probe_restores_the_original_value(self):
        out = self.probe()
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['restore'], 'restored')
        self.assertEqual(self.row['name'], 'widget')

    def test_an_application_write_during_the_probe_is_kept(self):
        def app_writes():
            self.row['name'] = 'written by the app'
        self.during_poll = app_writes
        out = self.probe()
        self.assertEqual(out['restore'], 'kept')
        self.assertEqual(self.row['name'], 'written by the app')

    def test_a_second_probe_is_refused_while_one_runs(self):
        started, release = threading.Event(), threading.Event()
        results = {}

        def hold():
            started.set()
            release.wait(5)
        self.during_poll = hold
        first = threading.Thread(target=lambda: results.setdefault('a', self.probe()))
        first.start()
        self.assertTrue(started.wait(5))
        second = self.probe()
        release.set()
        first.join(5)
        self.assertTrue(second.get('throttled'), second)
        self.assertIn('already running', second['error'])
        self.assertEqual(results['a']['restore'], 'restored')
        self.assertEqual(self.row['name'], 'widget')

    def test_a_probe_says_its_write_fires_on_update_and_triggers(self):
        out = self.probe()
        self.assertTrue(out['ok'], out)
        self.assertIn('ON UPDATE', out['side_effects'])
        self.assertIn('triggers', out['side_effects'])
        self.assertEqual(out['also_changed'], ['updated_at'])

    def test_no_qualifying_column_refuses_the_probe_without_writing(self):
        # Jira story F149: `name` is too short to hold the nonce, and nothing else is text.
        self.columns = [('id', 'int', None, 'PRI', ''),
                        ('name', 'varchar', 20, '', ''),
                        ('price', 'decimal', None, '', '')]
        out = self.probe()
        self.assertFalse(out['ok'])
        self.assertTrue(out['refused'])
        self.assertIn('no column is safe to stamp', out['error'])
        self.assertIn('name (varchar(20) is shorter than 32 characters)', out['error'])
        self.assertEqual(self.updates, [])
        self.assertEqual(self.row['name'], 'widget')

    def test_a_filter_column_is_never_stamped(self):
        self.overlay['transforms'] = {'items': {'filter': {
            'field': 'name', 'op': '!=', 'value': 'x'}}}
        out = self.probe()
        self.assertTrue(out['refused'], out)
        self.assertEqual(out['refused_columns']['name'],
                         'used by a filter or transform of this link')
        self.assertEqual(self.updates, [])

    def test_a_failed_restore_is_reported_with_the_original_value(self):
        self.fail_restore = True
        out = self.probe()
        self.assertTrue(out['restore'].startswith('failed'), out['restore'])
        self.assertTrue(out['source_row_left_changed'])
        self.assertEqual(out['original_value'], 'widget')
        self.assertIn('RESTORE FAILED', out['restore_note'])


class CountingCfn(FakeCfn):
    """FakeCfn that counts DescribeStacks calls and can be told to throttle."""
    def __init__(self, stacks=None, throttle=()):
        FakeCfn.__init__(self, stacks=stacks)
        self.calls = []
        self.throttle = set(throttle)
    def describe_stacks(self, StackName=None):
        self.calls.append(StackName)
        if StackName in self.throttle:
            raise Exception('An error occurred (Throttling) when calling the '
                            'DescribeStacks operation: Rate exceeded')
        return FakeCfn.describe_stacks(self, StackName=StackName)


def _shop_stacks():
    return {
        'ecdl-shop-cdc': {'Parameters': [
            {'ParameterKey': k, 'ParameterValue': v} for k, v in CDC_PARAMS.items()]},
        'ecdl-shop-broker': {'Outputs': [
            {'OutputKey': k, 'OutputValue': v} for k, v in BROKER_OUTPUTS.items()]},
    }


_APP = []


def _client():
    if not _APP:
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(monitor.monitor_bp)
        _APP.append(app.test_client())
    return _APP[0]


class LinkConfigCacheTest(unittest.TestCase):
    """Jira story F109: three DescribeStacks calls per request, and a throttle answered as
    "not deployed" or with the env-configured pipeline."""
    def setUp(self):
        self.cfn = CountingCfn(stacks=_shop_stacks())
        install(self.cfn)
        self.saved_ttl = monitor._LINK_CACHE_TTL
    def tearDown(self):
        monitor._LINK_CACHE_TTL = self.saved_ttl
        monitor._LINK_CACHE.clear()

    def test_a_second_request_within_the_ttl_reads_no_stack(self):
        first = monitor.link_config('shop')
        reads = len(self.cfn.calls)
        second = monitor.link_config('shop')
        self.assertEqual(reads, 3)                 # cdc, broker, loader
        self.assertEqual(len(self.cfn.calls), 3)
        self.assertEqual(first, second)

    def test_the_overlay_is_read_again_after_the_ttl(self):
        monitor.link_config('shop')
        monitor._LINK_CACHE_TTL = -1
        monitor.link_config('shop')
        self.assertEqual(len(self.cfn.calls), 6)

    def test_callers_cannot_change_the_cached_overlay(self):
        first = monitor.link_config('shop')
        first['db_host'] = 'changed'
        first['transforms']['x'] = {}
        second = monitor.link_config('shop')
        self.assertEqual(second['db_host'], CDC_PARAMS['SourceDBHost'])
        self.assertNotIn('x', second['transforms'])

    def test_a_throttled_cdc_stack_is_temporary_not_missing(self):
        self.cfn.throttle.add('ecdl-shop-cdc')
        with self.assertRaises(monitor.LinkUnavailable) as caught:
            monitor.link_config('shop')
        self.assertIn('Throttling', str(caught.exception))
        # Not cached: the next call after recovery resolves.
        self.cfn.throttle.clear()
        self.assertEqual(monitor.link_config('shop')['link_name'], 'shop')

    def test_a_throttled_broker_stack_is_not_read_as_no_broker(self):
        # Without the broker's credential every broker read fails NOAUTH.
        self.cfn.throttle.add('ecdl-shop-broker')
        with self.assertRaises(monitor.LinkUnavailable):
            monitor.link_config('shop')
        self.assertNotIn(('overlay', 'shop'), monitor._LINK_CACHE)

    def test_a_throttled_loader_stack_is_temporary(self):
        self.cfn.throttle.add('ecdl-shop-loader')
        with self.assertRaises(monitor.LinkUnavailable):
            monitor.link_config('shop')

    def test_a_missing_link_is_still_a_plain_value_error(self):
        with self.assertRaises(ValueError) as caught:
            monitor.link_config('nope')
        self.assertNotIsInstance(caught.exception, monitor.LinkUnavailable)

    def test_a_throttle_answers_503_and_a_missing_link_404(self):
        self.cfn.throttle.add('ecdl-shop-cdc')
        throttled = _client().get('/api/monitor/bulkload?link=shop')
        missing = _client().get('/api/monitor/bulkload?link=nope')
        self.assertEqual(throttled.status_code, 503)
        self.assertTrue(throttled.get_json()['retry'])
        self.assertEqual(missing.status_code, 404)

    def test_the_default_link_failing_is_an_error_not_the_env_pipeline(self):
        seen = []
        saved = monitor.collect_bulkload
        monitor.collect_bulkload = lambda: seen.append(monitor.CFG.watched_link()) or {}
        try:
            saved_default = monitor.CFG['default_link']
            monitor.CFG['default_link'] = 'shop'
            try:
                self.cfn.throttle.add('ecdl-shop-cdc')
                throttled = _client().get('/api/monitor/bulkload')
                self.cfn.throttle.clear()
                monitor.CFG['default_link'] = 'gone'
                missing = _client().get('/api/monitor/bulkload')
                monitor.CFG['default_link'] = 'shop'
                fine = _client().get('/api/monitor/bulkload')
            finally:
                monitor.CFG['default_link'] = saved_default
        finally:
            monitor.collect_bulkload = saved
        self.assertEqual(throttled.status_code, 503)
        self.assertEqual(missing.status_code, 404)
        self.assertIn('DEMO_DEFAULT_LINK', missing.get_json()['error'])
        self.assertEqual(fine.status_code, 200)
        # The collector ran once, for the default link, and never for the env one.
        self.assertEqual(seen, ['shop'])


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=0).result
    print('ok: {} link-selection tests passed'.format(result.testsRun)
          if result.wasSuccessful() else 'FAILED')
    raise SystemExit(0 if result.wasSuccessful() else 1)
