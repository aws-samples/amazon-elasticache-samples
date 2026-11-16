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
os.environ.setdefault('DEMO_HISTORY_ENABLED', 'false')

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
        self.assertEqual(monitor.link_config('shop')['bulkload_key'],
                         'ecdl:shop:bulkload')

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


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=0).result
    print('ok: {} link-selection tests passed'.format(result.testsRun)
          if result.wasSuccessful() else 'FAILED')
    raise SystemExit(0 if result.wasSuccessful() else 1)
