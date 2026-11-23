"""Tests for the deployment planner. Stdlib only, no AWS.

The planner is a pure function of a resolution, which is the point: the plan a
customer reviews is the plan that executes, and it can be checked without an
account.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.provision import (  # noqa: E402
    DEFAULT_BROKER, DEFAULT_SINK, PlanError, cluster_name, describe_plan, offset_key, plan,
    schema_history_key, stack_names,
)


def resolution(mode='cdc', **over):
    """A resolution shaped exactly like resolve_link()'s return value."""
    doc = {
        'schema_version': 1,
        'name': 'checkout',
        'mode': mode,
        'source': {
            'endpoint': 'src.cluster-abc.us-east-2.rds.amazonaws.com',
            'port': 3306,
            'engine': 'mysql',
            'engine_label': 'MySQL',
            'username': 'admin',
            'database': 'Inventory',
            'schema': None,
            'security_group_ids': ['sg-source'],
            'secret': {'arn': 'arn:aws:secretsmanager:us-east-2:1:secret:db-x'},
            'vpc_id': 'vpc-1',
            'subnet_ids': ['subnet-a', 'subnet-b'],
        },
        'target': {
            'endpoint': 'clustercfg.tgt.use2.cache.amazonaws.com',
            'port': 6379,
            'tls': True,
            'cluster_mode': True,
            'shards': 3,
            'auth_mode': 'none',
            'security_group_ids': ['sg-target'],
            'vpc_id': 'vpc-1',
        },
        'tables': [
            {'input': 'orders', 'table': 'orders', 'schema': None,
             'database': 'Inventory', 'qualified': 'Inventory.orders',
             'topic_suffix': 'Inventory.orders',
             'stream': 'data:checkout.Inventory.orders', 'key_prefix': 'orders:'},
            {'input': 'customers', 'table': 'customers', 'schema': None,
             'database': 'Inventory', 'qualified': 'Inventory.customers',
             'topic_suffix': 'Inventory.customers',
             'stream': 'data:checkout.Inventory.customers', 'key_prefix': 'customers:'},
        ],
        'debezium': {
            'topic_prefix': 'checkout',
            # Per link, because Debezium requires the signalling collection to be
            # unique to a connector instance.
            'signal_collection': 'Inventory.ecdl_signal_checkout',
            'signal_table': 'ecdl_signal_checkout',
            'signal_topic_pattern':
                '(.*\\.ecdl_signal_checkout)',
            'connector_class': 'io.debezium.connector.mysql.MySqlConnector',
            'database_property': 'database.include.list',
            'table_include_list': 'Inventory.orders,Inventory.customers',
            'schema_include_list': None,
            'snapshot_mode': 'schema_only',
            'server_id': 3828119,
            'needs_schema_history': True,
            'enabled': mode != 'initial_load',
        },
        'network': {'vpc_id': 'vpc-1', 'subnet_ids': ['subnet-a', 'subnet-b']},
        'components': {
            'loader': mode in ('initial_load', 'initial_load_and_cdc'),
            'broker': mode != 'initial_load',
            'debezium': mode != 'initial_load',
            'sink': mode != 'initial_load',
        },
        'notes': [],
        'warnings': [],
        'link': {'name': 'checkout', 'mode': mode},
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(doc.get(key), dict):
            doc[key].update(value)
        else:
            doc[key] = value
    return doc


def steps_by_id(plan_doc):
    return {s['id']: s for s in plan_doc['steps']}


def step_ids(plan_doc):
    return [s['id'] for s in plan_doc['steps']]


class NamingTest(unittest.TestCase):
    def test_stack_names_derive_from_the_link(self):
        # Derived, not configured, so two links cannot collide and a stack can be
        # traced back to its link without a lookup table.
        names = stack_names('checkout')
        self.assertEqual(names['broker'], 'ecdl-checkout-broker')
        self.assertEqual(names['cdc'], 'ecdl-checkout-cdc')
        self.assertEqual(names['loader'], 'ecdl-checkout-loader')

    def test_two_links_do_not_collide(self):
        a = stack_names('checkout')
        b = stack_names('catalog')
        self.assertEqual(set(a) & set(b), {'broker', 'cdc', 'loader'})
        self.assertFalse(set(a.values()) & set(b.values()))

    def test_cluster_name(self):
        self.assertEqual(cluster_name('checkout'), 'ecdl-checkout')

    def test_state_keys_are_per_link(self):
        # These were constants, so two links on one broker overwrote each other's
        # committed position.
        self.assertEqual(offset_key('checkout'), 'ecdl:checkout:offsets')
        self.assertNotEqual(offset_key('checkout'), offset_key('catalog'))
        self.assertNotEqual(schema_history_key('checkout'), offset_key('checkout'))


class ModeTest(unittest.TestCase):
    def test_cdc_creates_broker_then_cdc(self):
        p = plan(resolution('cdc'))
        self.assertEqual(step_ids(p), ['broker', 'cdc'])

    def test_cdc_has_no_loader(self):
        self.assertNotIn('loader', step_ids(plan(resolution('cdc'))))

    def test_initial_load_creates_only_the_loader(self):
        # The loader writes straight to the target, so a one-shot load needs no
        # broker, no Debezium and no sink. Creating them would bill for three
        # idle components.
        p = plan(resolution('initial_load'))
        self.assertEqual(step_ids(p), ['loader'])

    def test_initial_load_and_cdc_orders_the_handoff(self):
        # The ordering invariant: capture must be running for the first deploy
        # (or the first start would skip the load window), paused across the
        # load, then resumed. The loader STACK is created before the pause so
        # its minutes of creation are not spent with capture paused (Jira story F086).
        p = plan(resolution('initial_load_and_cdc'))
        self.assertEqual(
            step_ids(p),
            ['broker', 'cdc', 'loader', 'pause_capture', 'resume_capture'])

    def test_handoff_pauses_and_resumes_capture(self):
        s = steps_by_id(plan(resolution('initial_load_and_cdc')))
        self.assertEqual(s['pause_capture']['desired'], 0)
        self.assertEqual(s['resume_capture']['desired'], 1)
        self.assertEqual(s['loader']['depends_on'], ['cdc'])
        self.assertEqual(s['pause_capture']['depends_on'], ['cdc', 'loader'])
        self.assertEqual(s['resume_capture']['depends_on'], ['loader'])

    def test_cdc_starts_with_capture_running(self):
        # Deploying with DebeziumDesiredCount=0 on a FIRST deploy would leave no
        # stored offset, and the eventual first start would begin at the current
        # log position and silently skip everything written meanwhile.
        for mode in ('cdc', 'initial_load_and_cdc'):
            s = steps_by_id(plan(resolution(mode)))
            self.assertEqual(s['cdc']['parameters']['DebeziumDesiredCount'], '1',
                             'capture must start on the first deploy of %s' % mode)

    def test_plain_initial_load_has_no_capture_actions(self):
        ids = step_ids(plan(resolution('initial_load')))
        self.assertNotIn('pause_capture', ids)
        self.assertNotIn('resume_capture', ids)


class BrokerWiringTest(unittest.TestCase):
    def test_broker_values_come_from_outputs_not_parameters(self):
        # They cannot be known before the broker stack exists, so hardcoding a
        # placeholder would deploy a CDC stack pointed at the wrong broker.
        s = steps_by_id(plan(resolution('cdc')))
        cdc = s['cdc']
        for name in ('BrokerHost', 'BrokerPort', 'BrokerSecurityGroupId',
                     'BrokerSecretArn', 'BrokerSSL'):
            self.assertNotIn(name, cdc['parameters'],
                             '%s must not be a literal parameter' % name)
            self.assertIn(name, cdc['from_outputs'])

    def test_wiring_names_real_broker_outputs(self):
        # These must match infra/ecdl-broker-quickstart.yaml's Outputs exactly.
        cdc = steps_by_id(plan(resolution('cdc')))['cdc']
        self.assertEqual(cdc['from_outputs']['BrokerHost'], ['broker', 'PrimaryEndpoint'])
        self.assertEqual(cdc['from_outputs']['BrokerSecretArn'], ['broker', 'AuthSecretArn'])
        self.assertEqual(cdc['from_outputs']['BrokerSSL'], ['broker', 'TransitEncryption'])

    def test_cdc_depends_on_broker(self):
        self.assertEqual(steps_by_id(plan(resolution('cdc')))['cdc']['depends_on'],
                         ['broker'])


class CdcParameterTest(unittest.TestCase):
    def setUp(self):
        self.params = steps_by_id(plan(resolution('cdc')))['cdc']['parameters']

    def test_engine_is_passed_so_the_image_can_pick_its_profile(self):
        self.assertEqual(self.params['SourceEngine'], 'mysql')

    def test_target_topology_comes_from_the_resolution(self):
        # Read from the ARN, never chosen: a non-cluster client on a sharded
        # target gets a MOVED reply for most writes.
        self.assertEqual(self.params['TargetCluster'], 'true')
        self.assertEqual(self.params['TargetSSL'], 'true')

    def test_non_cluster_target_is_passed_as_false(self):
        r = resolution('cdc')
        r['target']['cluster_mode'] = False
        r['target']['tls'] = False
        params = steps_by_id(plan(r))['cdc']['parameters']
        self.assertEqual(params['TargetCluster'], 'false')
        self.assertEqual(params['TargetSSL'], 'false')

    def test_booleans_are_strings_not_python_bools(self):
        # CloudFormation string parameters reject True/False.
        for key in ('TargetCluster', 'TargetSSL'):
            self.assertIsInstance(self.params[key], str)

    def test_streams_lists_every_table(self):
        # This is Task 7's contract: one sink service consumes all of them.
        self.assertEqual(
            self.params['Streams'],
            'data:checkout.Inventory.orders,data:checkout.Inventory.customers')

    def test_tables_uses_the_qualified_names(self):
        self.assertEqual(self.params['Tables'],
                         'Inventory.orders,Inventory.customers')

    def test_subnets_are_comma_joined(self):
        # A CloudFormation List<> parameter is supplied as one comma-joined string.
        self.assertEqual(self.params['SubnetIds'], 'subnet-a,subnet-b')

    def test_per_link_state_keys(self):
        self.assertEqual(self.params['OffsetKey'], 'ecdl:checkout:offsets')
        self.assertEqual(self.params['SchemaHistoryKey'],
                         'ecdl:checkout:schema_history')

    def test_server_id_present_for_mysql(self):
        self.assertEqual(self.params['ServerId'], '3828119')

    def test_server_id_omitted_for_other_engines(self):
        # Sending an empty string would override the template default with
        # nothing; omitting the key lets the default stand.
        r = resolution('cdc')
        r['source']['engine'] = 'postgresql'
        r['debezium']['server_id'] = None
        params = steps_by_id(plan(r))['cdc']['parameters']
        self.assertNotIn('ServerId', params)

    def test_all_values_are_strings(self):
        # boto3 rejects a non-string ParameterValue.
        for key, value in self.params.items():
            self.assertIsInstance(value, str, '%s is %r' % (key, type(value)))

    def test_debezium_task_size_defaults_to_2_vcpu(self):
        # It was hardcoded at 8 vCPU / 16 GB and was not a parameter at all, so
        # every link paid for benchmark sizing. Debezium is one log reader; extra
        # vCPU do not parallelise capture.
        self.assertEqual(self.params['DebeziumCpu'], '2048')
        self.assertEqual(self.params['DebeziumMemory'], '4096')

    def test_debezium_task_size_can_be_raised(self):
        r = resolution('cdc')
        r['link']['advanced'] = {'debezium': {'cpu': '8192', 'memory': '16384'}}
        params = steps_by_id(plan(r))['cdc']['parameters']
        self.assertEqual(params['DebeziumCpu'], '8192')
        self.assertEqual(params['DebeziumMemory'], '16384')

    def test_connector_settings_are_not_task_sizing(self):
        # advanced.debezium also carries connector configuration (snapshot_mode,
        # server_id, ...). Only cpu and memory are task sizing; the rest belongs to
        # the resolver and must not appear in the stack parameters.
        r = resolution('cdc')
        r['link']['advanced'] = {'debezium': {'snapshot_mode': 'never',
                                              'server_id': 4242,
                                              'cpu': '4096'}}
        params = steps_by_id(plan(r))['cdc']['parameters']
        self.assertEqual(params['DebeziumCpu'], '4096')
        self.assertNotIn('snapshot_mode', params)
        self.assertNotIn('SnapshotModeOverride', params)

    def test_sink_defaults_applied(self):
        self.assertEqual(self.params['SinkWorkers'], str(DEFAULT_SINK['workers']))
        self.assertEqual(self.params['SinkBatchSize'],
                         str(DEFAULT_SINK['batch_size']))

    def test_advanced_sink_overrides(self):
        r = resolution('cdc')
        r['link']['advanced'] = {'sink': {'workers': 32, 'desired_count': 5}}
        params = steps_by_id(plan(r))['cdc']['parameters']
        self.assertEqual(params['SinkWorkers'], '32')
        self.assertEqual(params['SinkDesiredCount'], '5')
        # An override of one field must not discard the other defaults.
        self.assertEqual(params['SinkBatchSize'], str(DEFAULT_SINK['batch_size']))

    def test_images_omitted_so_template_defaults_apply(self):
        # Naming a version here would be a second place to bump on release, and
        # the two would drift.
        self.assertNotIn('DebeziumImage', self.params)
        self.assertNotIn('SinkImage', self.params)

    def test_image_overrides_are_passed(self):
        r = resolution('cdc')
        r['link']['advanced'] = {'images': {'sink': 'my.registry/sink:9'}}
        params = steps_by_id(plan(r))['cdc']['parameters']
        self.assertEqual(params['SinkImage'], 'my.registry/sink:9')
        self.assertNotIn('DebeziumImage', params)

    def test_target_secret_empty_when_absent(self):
        # The template's Conditions treat empty as "no credential", which omits
        # both the grant and the injection.
        self.assertEqual(self.params['TargetSecretArn'], '')

    def test_target_secret_passed_when_present(self):
        r = resolution('cdc')
        r['target']['secret'] = {'arn': 'arn:aws:secretsmanager:::secret:tgt'}
        params = steps_by_id(plan(r))['cdc']['parameters']
        self.assertEqual(params['TargetSecretArn'],
                         'arn:aws:secretsmanager:::secret:tgt')


class CpuArchitectureTest(unittest.TestCase):
    """The task architecture has to match the images.

    The templates used to set no RuntimePlatform, so Fargate always ran X86_64
    and an arm64-only build failed with "exec format error".
    """

    def _params(self, mode, step, arch=None):
        r = resolution(mode)
        if arch:
            r['link']['advanced'] = {'cpu_architecture': arch}
        return steps_by_id(plan(r))[step]['parameters']

    def test_not_sent_by_default_so_the_template_default_applies(self):
        self.assertNotIn('CpuArchitecture', self._params('cdc', 'cdc'))
        self.assertNotIn('CpuArchitecture',
                         self._params('initial_load', 'loader'))

    def test_arm64_reaches_the_cdc_stack(self):
        self.assertEqual(self._params('cdc', 'cdc', 'arm64')['CpuArchitecture'],
                         'ARM64')

    def test_arm64_reaches_the_loader_stack(self):
        self.assertEqual(
            self._params('initial_load', 'loader', 'arm64')['CpuArchitecture'],
            'ARM64')

    def test_x86_64_is_spelled_the_way_cloudformation_wants(self):
        self.assertEqual(self._params('cdc', 'cdc', 'x86_64')['CpuArchitecture'],
                         'X86_64')

    def test_every_task_definition_sets_a_runtime_platform(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        for name, count in (('ecdl-cdc-ecs.yaml', 2), ('ecdl-loader-ecs.yaml', 2)):
            with open(os.path.join(root, 'infra', name)) as handle:
                src = handle.read()
            self.assertEqual(src.count('Type: AWS::ECS::TaskDefinition'), count, name)
            self.assertEqual(
                src.count('CpuArchitecture: !Ref CpuArchitecture'), count, name)

    def test_the_schema_accepts_only_the_two_architectures(self):
        from ecdl_link import problems_for
        doc = {
            'name': 'a', 'mode': 'cdc',
            'source': {'arn': 'arn:aws:rds:us-east-2:111122223333:cluster:s',
                       'database': 'd',
                       'secret_arn': 'arn:aws:secretsmanager:us-east-2:'  # nosec B105 - fake test credential
                                     '111122223333:secret:x'},
            'target': {'arn': 'arn:aws:elasticache:us-east-2:111122223333:'
                              'replicationgroup:t'},
            'tables': ['orders'],
        }
        for good in ('arm64', 'x86_64'):
            doc['advanced'] = {'cpu_architecture': good}
            self.assertEqual(problems_for(doc), [], good)
        doc['advanced'] = {'cpu_architecture': 'arm'}
        self.assertTrue(problems_for(doc))


class ImageDefaultsTest(unittest.TestCase):
    """The templates' image defaults must work in any account.

    They used to be digests in one development account, so in every other
    account the tasks could not pull their images and a reviewer had to edit
    the template before the first deploy. Now an empty default resolves to the
    deploying account's own ECR at ImageTag, where release.sh pushes.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    TEMPLATES = {
        'ecdl-cdc-ecs.yaml': ('DebeziumImage', 'SinkImage'),
        'ecdl-loader-ecs.yaml': ('LoaderImage',),
    }

    def _src(self, name):
        with open(os.path.join(self.ROOT, 'infra', name)) as handle:
            return handle.read()

    def _default(self, src, param):
        import re
        m = re.search(r'^  ' + param + r':\n(?:    .*\n)*?    Default: (.*)$',
                      src, re.M)
        self.assertIsNotNone(m, param)
        return m.group(1).strip()

    def test_image_defaults_name_no_account(self):
        import re
        for name, params in self.TEMPLATES.items():
            src = self._src(name)
            for param in params:
                self.assertEqual(self._default(src, param), "''",
                                 '%s %s' % (name, param))
            # No 12-digit account id anywhere an image could come from.
            self.assertIsNone(re.search(r'\b\d{12}\.dkr\.ecr', src), name)

    def test_empty_image_resolves_to_the_deploying_accounts_ecr(self):
        for name, params in self.TEMPLATES.items():
            src = self._src(name)
            for param in params:
                self.assertIn('Has%s: !Not [!Equals [!Ref %s, \'\']]'
                              % (param, param), src, name)
            self.assertIn('${AWS::AccountId}.dkr.ecr.${AWS::Region}', src, name)
            self.assertIn(':${ImageTag}', src, name)

    def test_image_tag_matches_the_release_version(self):
        # release.sh pushes :<VERSION> and `release.sh pin` writes it here. A
        # template pointing at another tag would pull an image nobody pushed.
        with open(os.path.join(self.ROOT, 'VERSION')) as handle:
            version = handle.read().strip()
        for name in self.TEMPLATES:
            self.assertEqual(self._default(self._src(name), 'ImageTag'), version,
                             name)


class SignallingTest(unittest.TestCase):
    def setUp(self):
        self.params = steps_by_id(plan(resolution('cdc')))['cdc']['parameters']

    def test_signal_collection_passed(self):
        # Without it the connector never reads the signalling table, so a table
        # added later can only yield future changes and never backfills.
        self.assertEqual(self.params['SignalCollection'],
                         'Inventory.ecdl_signal_checkout')

    def test_control_traffic_pattern_passed(self):
        # The signalling table's own inserts are dropped before the broker. The
        # heartbeat is NOT: it is what commits an offset on an idle source.
        pattern = self.params['SignalTopicPattern']
        self.assertNotIn('__debezium-heartbeat', pattern)
        self.assertIn('ecdl_signal_checkout', pattern)


class TransformsTest(unittest.TestCase):
    def test_absent_transforms_omit_the_parameter(self):
        self.assertNotIn('TransformsJson',
                         steps_by_id(plan(resolution('cdc')))['cdc']['parameters'])

    def test_transforms_passed_in_the_sink_shape(self):
        r = resolution('cdc')
        r['link']['transforms'] = {'tables': {
            'orders': {'filter': {'field': 'status', 'op': '=', 'value': 'active'}}}}
        params = steps_by_id(plan(r))['cdc']['parameters']
        spec = json.loads(params['TransformsJson'])
        self.assertEqual(list(spec), ['tables'])
        self.assertIn('orders', spec['tables'])

    def test_loader_gets_the_same_transforms_as_the_sink(self):
        # A load that ignored the transforms would fill the cache with rows the
        # CDC path would never produce, so the two would disagree forever.
        r = resolution('initial_load_and_cdc')
        r['link']['transforms'] = {'tables': {'orders': {'columns': ['id', 'total']}}}
        s = steps_by_id(plan(r))
        self.assertEqual(s['loader']['parameters']['TransformsJson'],
                         s['cdc']['parameters']['TransformsJson'])

    def _denorm_spec(self, parent_key_prefix=None):
        rule = {'parent_table': 'customers', 'join_field': 'customer_id',
                'fields': ['name', 'tier'], 'prefix': 'cust_'}
        if parent_key_prefix is not None:
            rule['parent_key_prefix'] = parent_key_prefix
        return {'tables': {'orders': {'denormalize': [rule]}}}

    def test_denorm_default_parent_key_prefix_is_filled(self):
        # The schema documents the default '<parent_table>:' but never wrote it,
        # and both engines read the field verbatim -- so an omitted prefix
        # embedded nothing. It must be filled where the executable spec is emitted.
        r = resolution('cdc')
        r['link']['transforms'] = self._denorm_spec()  # no parent_key_prefix
        spec = json.loads(
            steps_by_id(plan(r))['cdc']['parameters']['TransformsJson'])
        self.assertEqual(
            spec['tables']['orders']['denormalize'][0]['parent_key_prefix'],
            'customers:')

    def test_denorm_explicit_parent_key_prefix_is_preserved(self):
        r = resolution('cdc')
        r['link']['transforms'] = self._denorm_spec(parent_key_prefix='cust:')
        spec = json.loads(
            steps_by_id(plan(r))['cdc']['parameters']['TransformsJson'])
        self.assertEqual(
            spec['tables']['orders']['denormalize'][0]['parent_key_prefix'],
            'cust:')

    def test_denorm_default_uses_bare_parent_name(self):
        # A schema-qualified parent still keys as '<bare>:', so the default drops
        # the qualifier.
        r = resolution('cdc')
        r['link']['transforms'] = {'tables': {'orders': {'denormalize': [
            {'parent_table': 'Inventory.customers', 'join_field': 'customer_id',
             'fields': ['name']}]}}}
        spec = json.loads(
            steps_by_id(plan(r))['cdc']['parameters']['TransformsJson'])
        self.assertEqual(
            spec['tables']['orders']['denormalize'][0]['parent_key_prefix'],
            'customers:')

    def _emitted_prefix(self, r):
        spec = json.loads(
            steps_by_id(plan(r))['cdc']['parameters']['TransformsJson'])
        return spec['tables']['orders']['denormalize'][0]['parent_key_prefix']

    def test_denorm_default_carries_link_key_prefix(self):
        r = resolution('cdc')
        r['link']['advanced'] = {'key_prefix': 'v2'}
        r['link']['transforms'] = self._denorm_spec()
        self.assertEqual(self._emitted_prefix(r), 'v2:customers:')

    def test_wizard_bare_default_gets_link_prefix_for_in_link_parent(self):
        # The wizard writes 'customers:' explicitly; with a key prefix the sink
        # writes the parent as 'v2:customers:<pk>', so the bare value found nothing.
        r = resolution('cdc')
        r['link']['advanced'] = {'key_prefix': 'v2:'}
        r['link']['transforms'] = self._denorm_spec(parent_key_prefix='customers:')
        self.assertEqual(self._emitted_prefix(r), 'v2:customers:')

    def test_explicit_other_prefix_kept_with_link_prefix(self):
        r = resolution('cdc')
        r['link']['advanced'] = {'key_prefix': 'v2'}
        r['link']['transforms'] = self._denorm_spec(parent_key_prefix='shared:customers:')
        self.assertEqual(self._emitted_prefix(r), 'shared:customers:')

    def test_bare_prefix_kept_for_parent_outside_the_link(self):
        # A parent this link does not capture may well live under the bare key.
        r = resolution('cdc')
        r['link']['advanced'] = {'key_prefix': 'v2'}
        r['link']['transforms'] = {'tables': {'orders': {'denormalize': [
            {'parent_table': 'suppliers', 'join_field': 'sid', 'fields': ['name'],
             'parent_key_prefix': 'suppliers:'}]}}}
        self.assertEqual(self._emitted_prefix(r), 'suppliers:')

    def test_loader_and_sink_get_the_same_spec_with_prefix(self):
        r = resolution('initial_load_and_cdc')
        r['link']['advanced'] = {'key_prefix': 'v2'}
        r['link']['transforms'] = self._denorm_spec(parent_key_prefix='customers:')
        s = steps_by_id(plan(r))
        self.assertEqual(s['loader']['parameters']['TransformsJson'],
                         s['cdc']['parameters']['TransformsJson'])

    def test_qualified_keys_and_parent_emitted_bare(self):
        # Both engines look rules up by bare name; a block keyed 'Inventory.orders'
        # used to be emitted as is and never applied.
        r = resolution('initial_load_and_cdc')
        r['link']['transforms'] = {'tables': {
            'Inventory.orders': {'columns': ['id'], 'denormalize': [
                {'parent_table': 'Inventory.customers', 'join_field': 'cid',
                 'fields': ['name']}]},
            'Inventory.customers': {'columns': ['id', 'name']}}}
        s = steps_by_id(plan(r))
        spec = json.loads(s['cdc']['parameters']['TransformsJson'])
        self.assertEqual(sorted(spec['tables']), ['customers', 'orders'])
        rule = spec['tables']['orders']['denormalize'][0]
        self.assertEqual(rule['parent_table'], 'customers')
        self.assertEqual(rule['parent_key_prefix'], 'customers:')
        self.assertEqual(s['loader']['parameters']['TransformsJson'],
                         s['cdc']['parameters']['TransformsJson'])

    def test_two_blocks_for_one_table_refused(self):
        r = resolution('cdc')
        r['link']['transforms'] = {'tables': {
            'orders': {'columns': ['id']}, 'Inventory.orders': {'columns': ['id']}}}
        with self.assertRaises(PlanError):
            plan(r)

    def test_loader_orders_parent_before_child(self):
        # The fixture lists orders before customers; a denorm rule makes orders a
        # child of customers, so the parent must load first or the child embeds
        # empty values with nothing to correct them (the loader does no parent
        # fan-out on the initial load).
        r = resolution('initial_load_and_cdc')
        r['link']['transforms'] = self._denorm_spec()
        loader = steps_by_id(plan(r))['loader']['parameters']
        self.assertEqual(loader['Tables'], 'customers,orders')
        # PrimaryKeys stays positionally aligned with the reordered Tables.
        names = loader['Tables'].split(',')
        keys = loader['PrimaryKeys'].split(',')
        self.assertEqual(len(names), len(keys))

    def test_loader_order_unchanged_without_denorm(self):
        r = resolution('initial_load_and_cdc')
        loader = steps_by_id(plan(r))['loader']['parameters']
        self.assertEqual(loader['Tables'], 'orders,customers')


class SecretShapeTest(unittest.TestCase):
    """An RDS-managed secret is JSON; injected whole, the DB refused the login."""

    def test_plaintext_secret_is_injected_whole(self):
        for mode, step in (('cdc', 'cdc'), ('initial_load', 'loader')):
            params = steps_by_id(plan(resolution(mode)))[step]['parameters']
            self.assertEqual(params['DBSecretJsonKey'], '', step)

    def test_rds_managed_secret_selects_the_password_key(self):
        for mode, step in (('cdc', 'cdc'), ('initial_load', 'loader')):
            r = resolution(mode)
            r['source']['secret'] = {'arn': 'arn:aws:secretsmanager:us-east-2:1:secret:rds!cluster-x',
                                     'managed_by_rds': True}
            params = steps_by_id(plan(r))[step]['parameters']
            self.assertEqual(params['DBSecretJsonKey'], 'password', step)

    def test_a_json_key_named_in_the_link_is_used(self):  # Jira story F015
        for mode, step in (('cdc', 'cdc'), ('initial_load', 'loader'),
                           ('initial_load_and_cdc', 'cdc'),
                           ('initial_load_and_cdc', 'loader')):
            for managed in (False, True):
                r = resolution(mode)
                r['source']['secret'] = dict(r['source'].get('secret') or {},
                                             managed_by_rds=managed)
                r['source']['secret_json_key'] = 'db_pass'
                params = steps_by_id(plan(r))[step]['parameters']
                self.assertEqual(params['DBSecretJsonKey'], 'db_pass', (mode, step, managed))

    def test_an_empty_key_falls_back_to_the_secret_shape(self):
        r = resolution('cdc')
        r['source']['secret_json_key'] = None
        self.assertEqual(steps_by_id(plan(r))['cdc']['parameters']['DBSecretJsonKey'], '')


class KeyPrefixTest(unittest.TestCase):
    """advanced.key_prefix was validated but reached neither the sink nor loader."""

    def test_empty_by_default(self):
        for mode, step in (('cdc', 'cdc'), ('initial_load', 'loader')):
            params = steps_by_id(plan(resolution(mode)))[step]['parameters']
            self.assertEqual(params['KeyPrefix'], '', step)

    def test_passed_to_sink_and_loader_without_the_colon(self):
        for mode, step in (('cdc', 'cdc'), ('initial_load', 'loader')):
            r = resolution(mode)
            r['link']['advanced'] = {'key_prefix': 'v2:'}
            params = steps_by_id(plan(r))[step]['parameters']
            self.assertEqual(params['KeyPrefix'], 'v2', step)


class LoaderParameterTest(unittest.TestCase):
    def setUp(self):
        self.params = steps_by_id(plan(resolution('initial_load')))['loader']['parameters']

    def test_lists_every_table_and_key(self):
        self.assertEqual(self.params['Tables'], 'orders,customers')
        # Empty, not 'id,id'. An unnamed key means "the loader asks the source":
        # the control plane frequently cannot introspect the database, and a
        # guessed 'id' surfaced as the task failing on "Unknown column 'id'"
        # minutes into a deployment, naming neither the table nor its real key.
        self.assertEqual(self.params['PrimaryKeys'], ',')

    def test_primary_keys_can_be_named_per_table(self):
        r = resolution('initial_load')
        r['link']['advanced'] = {'loader': {'primary_keys': {'customers': 'customer_id'}}}
        params = steps_by_id(plan(r))['loader']['parameters']
        self.assertEqual(params['PrimaryKeys'], ',customer_id')

    def test_unnamed_keys_hold_their_position(self):
        # The list is matched to Tables by POSITION, so a named key must not
        # shift onto an earlier table. entrypoint.sh keeps empty entries when it
        # splits this for the same reason.
        r = resolution('initial_load')
        r['link']['advanced'] = {'loader': {'primary_keys': {'orders': 'order_id'}}}
        params = steps_by_id(plan(r))['loader']['parameters']
        self.assertEqual(params['Tables'], 'orders,customers')
        self.assertEqual(params['PrimaryKeys'], 'order_id,')

    def test_source_tables_are_qualified_and_aligned(self):
        # MySQL: database.table. Tables stays bare (the RunTask override and
        # the cache keys use it).
        self.assertEqual(self.params['SourceTables'],
                         'Inventory.orders,Inventory.customers')
        self.assertEqual(self.params['Tables'], 'orders,customers')
    def test_postgresql_non_public_schema_reaches_the_loader(self):
        r = resolution('initial_load')
        r['source']['engine'] = 'postgresql'
        for t in r['tables']:
            t.update(schema='sales', database=None,
                     qualified='sales.' + t['table'])
        params = steps_by_id(plan(r))['loader']['parameters']
        self.assertEqual(params['SourceTables'], 'sales.orders,sales.customers')
        self.assertEqual(params['Tables'], 'orders,customers')
    def test_source_tables_follow_the_parent_first_order(self):
        r = resolution('initial_load_and_cdc')
        r['link']['transforms'] = TransformsTest._denorm_spec(None)
        params = steps_by_id(plan(r))['loader']['parameters']
        self.assertEqual(params['Tables'], 'customers,orders')
        self.assertEqual(params['SourceTables'], 'Inventory.customers,Inventory.orders')
    def test_progress_hashes_are_per_link(self):
        self.assertEqual(self.params['ProgressKeyBase'], 'ecdl:checkout:bulkload')
    def test_progress_key_is_the_one_the_loader_writes(self):
        # The monitor reads progress_key(); the loader writes progress_key_for()
        # of the key prefix entrypoint.sh gives it. They must agree.
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), 'data-plane', 'bulk-loader'))
        import bulk_loader
        from ecdl_link.provision import progress_key, progress_key_base
        for table, prefix in (('orders', ''), ('orders', 'v2'), ('Inventory.orders', 'v2:'),
                              ('ORDERS', 'b')):
            bare = table.split('.')[-1]
            loader_prefix = ('{}:{}:'.format(prefix.rstrip(':'), bare) if prefix
                             else '{}:'.format(bare))
            self.assertEqual(
                progress_key('shop', table, prefix),
                bulk_loader.progress_key_for(progress_key_base('shop'), loader_prefix),
                (table, prefix))
        self.assertEqual(progress_key('shop', 'Inventory.orders', 'v2'),
                         'ecdl:shop:bulkload:v2:orders')
    def test_target_topology_reaches_the_loader(self):
        # The loader had no way to be told this, so an initial_load against a
        # non-sharded cache never worked.
        self.assertEqual(self.params['TargetCluster'], 'true')

    def test_loader_needs_no_broker_values(self):
        for key in self.params:
            self.assertNotIn('Broker', key)


def with_egress(kind, base='cdc', **extra):
    """A resolution whose subnets report a known egress arrangement."""
    r = resolution(base)
    r['network']['egress'] = dict({'kind': kind, 'detail': kind + ' detail',
                                   'endpoints': [], 'missing_endpoints': []},
                                  **extra)
    return r


class NetworkTest(unittest.TestCase):
    def test_public_ip_off_when_a_nat_gateway_exists(self):
        # The task carries a database credential; a Fargate task with a public IP
        # is internet-reachable unless a security group says otherwise. With a NAT
        # gateway the private arrangement is both safer AND known to work.
        p = plan(with_egress('nat'))
        self.assertEqual(steps_by_id(p)['cdc']['parameters']['AssignPublicIp'],
                         'DISABLED')

    def test_public_ip_off_when_interface_endpoints_cover_startup(self):
        p = plan(with_egress('endpoints'))
        self.assertEqual(steps_by_id(p)['cdc']['parameters']['AssignPublicIp'],
                         'DISABLED')

    def test_public_ip_on_when_the_only_egress_is_an_internet_gateway(self):
        # REGRESSION. This is the deployment that failed: subnets routing
        # 0.0.0.0/0 to an internet gateway, tasks planned with no public IP, and
        # therefore no path to Secrets Manager or ECR. An internet gateway route
        # does nothing for a task without a public address, so every task failed
        # to start and CloudFormation reported only "Exceeded attempts to wait".
        p = plan(with_egress('igw'))
        self.assertEqual(steps_by_id(p)['cdc']['parameters']['AssignPublicIp'],
                         'ENABLED')

    def test_public_ip_on_is_warned_about(self):
        # Turning it on is a compromise forced by the network, so it must not be
        # silent -- the operator should learn that endpoints or a NAT would avoid it.
        p = plan(with_egress('igw'))
        self.assertTrue(any('public IP' in w for w in p['warnings']),
                        p['warnings'])

    def test_no_egress_at_all_is_refused_at_plan_time(self):
        # Better to fail in a second than to create stacks that cannot start a
        # task and roll back twenty minutes later.
        with self.assertRaises(PlanError) as caught:
            plan(with_egress('none'))
        self.assertIn('no route to the AWS APIs', str(caught.exception))

    def test_unknown_egress_keeps_the_safer_value_but_says_so(self):
        p = plan(with_egress('unknown'))
        self.assertEqual(steps_by_id(p)['cdc']['parameters']['AssignPublicIp'],
                         'DISABLED')
        self.assertTrue(any('could not verify' in w for w in p['warnings']),
                        p['warnings'])

    def test_an_explicit_override_beats_the_routing(self):
        # The operator may know something the API does not.
        p = plan(with_egress('igw'), assign_public_ip=False)
        self.assertEqual(steps_by_id(p)['cdc']['parameters']['AssignPublicIp'],
                         'DISABLED')

    def test_override_also_bypasses_the_no_egress_refusal(self):
        p = plan(with_egress('none'), assign_public_ip=True)
        self.assertEqual(steps_by_id(p)['cdc']['parameters']['AssignPublicIp'],
                         'ENABLED')

    def test_public_ip_can_be_enabled(self):
        p = plan(resolution('cdc'), assign_public_ip=True)
        self.assertEqual(steps_by_id(p)['cdc']['parameters']['AssignPublicIp'],
                         'ENABLED')

    def test_the_loader_gets_the_same_decision_as_the_cdc_tasks(self):
        # The loader pulls an image and reads secrets exactly like the others, so
        # a divergence here would leave the load unable to start.
        p = plan(with_egress('igw', base='initial_load_and_cdc'))
        s = steps_by_id(p)
        self.assertEqual(s['loader']['parameters']['AssignPublicIp'],
                         s['cdc']['parameters']['AssignPublicIp'])


class UpdatePlanTest(unittest.TestCase):
    """`for_update` plans a change to a link that is already running."""

    def test_a_first_deployment_loads_and_pauses_capture(self):
        ids = [s['id'] for s in plan(resolution('initial_load_and_cdc'))['steps']]
        self.assertIn('pause_capture', ids)
        self.assertIn('resume_capture', ids)
        self.assertIn('loader', ids)

    def test_an_update_does_neither(self):
        # Re-running the bulk load would re-read tables that did not change, and
        # it can only run safely with capture paused -- so it interrupts a healthy
        # pipeline to do work that does not fix what a change actually breaks.
        ids = [s['id'] for s
               in plan(resolution('initial_load_and_cdc'), for_update=True)['steps']]
        self.assertNotIn('pause_capture', ids)
        self.assertNotIn('resume_capture', ids)
        self.assertNotIn('run_load', ids)

    def test_an_update_still_updates_every_stack(self):
        # The stacks are how the new table list and transforms reach the tasks, so
        # all of them still have to be applied.
        ids = [s['id'] for s
               in plan(resolution('initial_load_and_cdc'), for_update=True)['steps']]
        self.assertEqual(ids, ['broker', 'cdc', 'loader', 'ensure_capture'])

    def test_an_update_never_moves_capture(self):
        # Lifecycle H3/H4: DebeziumDesiredCount=1 on an update resumed capture
        # under a running load; ensure_capture decides instead.
        p = plan(resolution('initial_load_and_cdc'), for_update=True)
        s = steps_by_id(p)
        self.assertIn('DebeziumDesiredCount', s['cdc']['keep_previous'])
        self.assertIn('PgSlotName', s['cdc']['keep_previous'])
        self.assertEqual(s['ensure_capture']['action'], 'ensure_capture')
        self.assertEqual(p['steps'][-1]['id'], 'ensure_capture')
        first = steps_by_id(plan(resolution('initial_load_and_cdc')))
        self.assertNotIn('DebeziumDesiredCount', first['cdc']['keep_previous'])
        self.assertNotIn('ensure_capture', first)

    def test_no_capture_check_for_a_load_only_link(self):
        ids = [s['id'] for s in plan(resolution('initial_load'), for_update=True)['steps']]
        self.assertNotIn('ensure_capture', ids)

    def test_control_plane_values_are_kept_when_unknown(self):
        # Lifecycle M1: the CLI on a laptop sends neither value.
        s = steps_by_id(plan(resolution('initial_load_and_cdc'), for_update=True))
        self.assertIn('MonitorSecurityGroupId', s['broker']['keep_if_empty'])
        self.assertIn('PermissionsBoundaryArn', s['cdc']['keep_if_empty'])
        self.assertIn('PermissionsBoundaryArn', s['loader']['keep_if_empty'])

    def test_an_update_keeps_the_loader_stack_in_step(self):
        # Not run, but kept correct, so a later deliberate load uses the new table
        # list rather than a stale one.
        s = steps_by_id(plan(resolution('initial_load_and_cdc'), for_update=True))
        self.assertEqual(s['loader']['kind'], 'stack')

    def test_the_update_says_capture_is_left_running(self):
        p = plan(resolution('initial_load_and_cdc'), for_update=True)
        self.assertTrue(any('capture is left running' in n for n in p['notes']),
                        p['notes'])

    def test_for_update_is_recorded_on_the_document(self):
        # add_load_step reads this. Two places deciding whether a load runs is
        # how they end up disagreeing.
        self.assertTrue(plan(resolution('cdc'), for_update=True)['for_update'])
        self.assertFalse(plan(resolution('cdc'))['for_update'])

    def test_add_load_step_will_not_put_the_load_back(self):
        from ecdl_link.deploy import add_load_step
        p = add_load_step(plan(resolution('initial_load_and_cdc'),
                              for_update=True))
        self.assertNotIn('run_load', [s['id'] for s in p['steps']])

    def test_add_load_step_still_adds_it_to_a_first_deployment(self):
        from ecdl_link.deploy import add_load_step
        p = add_load_step(plan(resolution('initial_load_and_cdc')))
        self.assertIn('run_load', [s['id'] for s in p['steps']])

    def test_an_initial_load_only_link_updated_has_no_stale_note(self):
        # "the cache goes stale from the moment the load finishes" is about a
        # one-shot load actually running; on a stack update it would be untrue.
        p = plan(resolution('initial_load'), for_update=True)
        self.assertFalse(any('goes stale' in n for n in p['notes']), p['notes'])

    def test_cluster_name_shared_by_both_stacks(self):
        p = plan(resolution('initial_load_and_cdc'))
        s = steps_by_id(p)
        self.assertEqual(s['cdc']['parameters']['ClusterName'],
                         s['loader']['parameters']['ClusterName'])
        self.assertEqual(p['cluster_name'], s['cdc']['parameters']['ClusterName'])

    def test_missing_network_refused(self):
        r = resolution('cdc')
        r['network'] = {'vpc_id': None, 'subnet_ids': []}
        with self.assertRaises(PlanError):
            plan(r)


class SecurityGroupTest(unittest.TestCase):
    def test_single_group_used(self):
        params = steps_by_id(plan(resolution('cdc')))['cdc']['parameters']
        self.assertEqual(params['AuroraSecurityGroupId'], 'sg-source')
        self.assertEqual(params['TargetSecurityGroupId'], 'sg-target')

    def test_no_group_refused(self):
        r = resolution('cdc')
        r['source']['security_group_ids'] = []
        with self.assertRaises(PlanError) as caught:
            plan(r)
        self.assertIn('security group', str(caught.exception))

    def test_external_source_sends_an_empty_group_instead_of_refusing(self):
        # A self-managed / on-prem source has no AWS security group; the operator
        # opens their own firewall. The plan must send '' (so the template skips
        # the ingress rule) rather than refusing the way an AWS source with no
        # group does. BOTH stacks that connect to the source -- the CDC stack and
        # the loader -- have to be covered: the loader was missed at first and
        # blocked "Show what it will create" for an initial-load link.
        r = resolution('initial_load_and_cdc')
        r['source']['external'] = True
        r['source']['security_group_ids'] = []
        steps = steps_by_id(plan(r))
        self.assertEqual(steps['cdc']['parameters']['AuroraSecurityGroupId'], '')
        self.assertEqual(
            steps['loader']['parameters']['SourceSecurityGroupId'], '')

    def test_ambiguous_group_refused_rather_than_guessed(self):
        # Which group carries the ingress rule decides whether the pipeline can
        # connect at all, so picking one silently could produce a deployment that
        # comes up and then cannot reach the database.
        r = resolution('cdc')
        r['source']['security_group_ids'] = ['sg-a', 'sg-b']
        with self.assertRaises(PlanError) as caught:
            plan(r)
        self.assertIn('sg-a', str(caught.exception))
        self.assertIsNotNone(caught.exception.hint)


class CapabilitiesTest(unittest.TestCase):
    def test_iam_capability_where_roles_are_created(self):
        s = steps_by_id(plan(resolution('initial_load_and_cdc')))
        self.assertEqual(s['cdc']['capabilities'], ['CAPABILITY_IAM'])
        self.assertEqual(s['loader']['capabilities'], ['CAPABILITY_IAM'])

    def test_not_named_iam(self):
        # Neither template sets RoleName, so CAPABILITY_IAM is what
        # validate-template asks for. Requesting the wider one would let a
        # fixed-name role be added later without CloudFormation objecting.
        s = steps_by_id(plan(resolution('initial_load_and_cdc')))
        for step in ('cdc', 'loader'):
            self.assertNotIn('CAPABILITY_NAMED_IAM', s[step]['capabilities'])

    def test_broker_needs_none(self):
        # It creates a cache, a subnet group, a parameter group, a security group
        # and a secret. No IAM, so no capability.
        self.assertEqual(steps_by_id(plan(resolution('cdc')))['broker']['capabilities'],
                         [])


class DescribeTest(unittest.TestCase):
    def test_describes_every_step_in_order(self):
        text = describe_plan(plan(resolution('initial_load_and_cdc')))
        self.assertIn('checkout (initial_load_and_cdc)', text)
        for expected in ('ecdl-checkout-broker', 'ecdl-checkout-cdc',
                         'ecdl-checkout-loader', 'set_debezium_desired'):
            self.assertIn(expected, text)

    def test_describes_the_output_wiring(self):
        text = describe_plan(plan(resolution('cdc')))
        self.assertIn('BrokerHost<-broker.PrimaryEndpoint', text)

    def test_carries_resolution_warnings_through(self):
        r = resolution('cdc', warnings=['the target has no encryption in transit'])
        p = plan(r)
        self.assertIn('the target has no encryption in transit', p['warnings'])
        self.assertIn('the target has no encryption in transit', describe_plan(p))


class TemplateAgreementTest(unittest.TestCase):
    """The planner must only send parameters the templates actually declare.

    A parameter name that does not exist is rejected by CloudFormation at
    create time, which is a slow and confusing way to find a typo.
    """

    def _declared(self, filename):
        import re
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'infra', filename)
        with open(path) as handle:
            src = handle.read()
        block = src.split('\nParameters:', 1)[1]
        for marker in ('\nConditions:', '\nResources:', '\nMappings:'):
            if marker in block:
                block = block.split(marker, 1)[0]
        return set(re.findall(r'^  (\w+):', block, re.M))

    def _check(self, mode, step_id, filename):
        declared = self._declared(filename)
        step = steps_by_id(plan(resolution(mode)))[step_id]
        sent = set(step['parameters']) | set(step.get('from_outputs') or {})
        unknown = sent - declared
        self.assertFalse(unknown,
                         '%s does not declare: %s' % (filename, sorted(unknown)))

    def test_cdc_parameters_exist(self):
        self._check('cdc', 'cdc', 'ecdl-cdc-ecs.yaml')

    def test_broker_parameters_exist(self):
        self._check('cdc', 'broker', 'ecdl-broker-quickstart.yaml')

    def test_loader_parameters_exist(self):
        self._check('initial_load', 'loader', 'ecdl-loader-ecs.yaml')

    def test_required_cdc_parameters_are_all_supplied(self):
        # A parameter with no Default must be supplied, either directly or by
        # wiring from an earlier stack's output.
        import re
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'infra', 'ecdl-cdc-ecs.yaml')
        with open(path) as handle:
            src = handle.read()
        block = src.split('\nParameters:', 1)[1].split('\nConditions:', 1)[0]
        required = []
        for match in re.finditer(r'^  (\w+):', block, re.M):
            rest = block[match.end():]
            following = re.search(r'^  \w+:', rest, re.M)
            body = rest[:following.start()] if following else rest
            if 'Default:' not in body:
                required.append(match.group(1))
        step = steps_by_id(plan(resolution('cdc')))['cdc']
        supplied = set(step['parameters']) | set(step['from_outputs'])
        missing = [name for name in required if name not in supplied]
        self.assertFalse(missing, 'plan omits required parameters: %s' % missing)


class DerivedNameLengthTest(unittest.TestCase):
    """Every name derived from the link name must fit its service's limit.

    This test exists because a name once did not fit. The schema allowed a 32-character name, the
    broker's ElastiCache replication group is called ecdl-<name>-broker, and
    ElastiCache rejects a replication group id over 40 characters -- so a name of
    29 or more built a stack, waited, and then failed on
    "Replication Group ID cannot be longer than 40 characters".

    The schema's limit and this arithmetic have to agree, and nothing else was
    keeping them consistent.
    """

    # The longest name the schema will accept.
    LONGEST = 'a' + 'b' * 26 + 'c'

    def test_the_schema_limit_matches_what_elasticache_accepts(self):
        from ecdl_link import problems_for

        def name_ok(name):
            doc = {
                'name': name, 'mode': 'cdc',
                'source': {
                    'arn': 'arn:aws:rds:us-east-2:111122223333:cluster:src',
                    'database': 'd',
                    'secret_arn': ('arn:aws:secretsmanager:us-east-2:'  # nosec B105 - fake test credential
                                   '111122223333:secret:db-x'),
                },
                'target': {
                    'arn': ('arn:aws:elasticache:us-east-2:111122223333:'
                            'replicationgroup:tgt'),
                },
                'tables': ['t'],
            }
            return not [p for p in problems_for(doc) if p.path == 'name']

        self.assertEqual(len(self.LONGEST), 28)
        self.assertTrue(name_ok(self.LONGEST))
        # One more character must be refused by validation rather than by
        # CloudFormation several minutes into a deploy.
        self.assertFalse(name_ok(self.LONGEST + 'd'))

    def test_the_replication_group_id_fits_in_forty_characters(self):
        # ElastiCache's hard limit, and the reason the name limit is 28.
        cluster_name = 'ecdl-{}-broker'.format(self.LONGEST)
        self.assertLessEqual(len(cluster_name), 40)

    def test_the_broker_step_passes_a_short_enough_cluster_name(self):
        # Asserted against the PLAN rather than recomputed, so a change to how the
        # parameter is built cannot pass this while breaking the deploy.
        plan_doc = plan(resolution('initial_load_and_cdc', name=self.LONGEST))
        broker = [s for s in plan_doc['steps'] if s['id'] == 'broker'][0]
        self.assertLessEqual(len(broker['parameters']['ClusterName']), 40)

    def test_stack_names_fit_cloudformation(self):
        for name in stack_names(self.LONGEST).values():
            self.assertLessEqual(len(name), 128)



class BrokerMonitorAccessTest(unittest.TestCase):
    """The broker must admit the control plane, or the link is unobservable.

    The CDC stack grants only the pipeline's own task security group. Stream
    depth, consumer-group lag, pending entries, DLQ length and Debezium's
    committed offset all live in the broker, so without this grant the pipeline
    runs correctly while every one of those reads fails -- and ECS still reports
    the tasks healthy, which makes it look fine.
    """

    def test_monitor_sg_is_passed_to_the_broker_stack(self):
        s = steps_by_id(plan(resolution('cdc'), monitor_security_group_id='sg-cp1'))
        self.assertEqual(s['broker']['parameters']['MonitorSecurityGroupId'],
                         'sg-cp1')

    def test_absent_monitor_sg_is_an_empty_string_not_missing(self):
        # CloudFormation needs the parameter present; the template's condition
        # turns '' into "no ingress rule".
        s = steps_by_id(plan(resolution('cdc')))
        self.assertEqual(s['broker']['parameters']['MonitorSecurityGroupId'], '')

    def test_a_missing_monitor_sg_warns_about_what_will_be_blank(self):
        warnings = ' '.join(plan(resolution('cdc'))['warnings'])
        self.assertIn('dashboard cannot read', warnings)
        self.assertIn('ECDL_MONITOR_SECURITY_GROUP_ID', warnings)

    def test_no_warning_when_the_grant_is_supplied(self):
        warnings = ' '.join(
            plan(resolution('cdc'), monitor_security_group_id='sg-cp1')['warnings'])
        self.assertNotIn('dashboard cannot read', warnings)

    def test_an_initial_load_only_link_is_not_warned(self):
        # It has no broker at all, so there is nothing to be unable to read.
        warnings = ' '.join(plan(resolution('initial_load'))['warnings'])
        self.assertNotIn('dashboard cannot read', warnings)


class SettingsReachTheStackTest(unittest.TestCase):
    """Jira story F129: a setting the schema accepts is sent to the stack that uses it."""

    def test_broker_node_type_is_kept_unless_set(self):
        # Merged with Jira stories F023/F024: unset, the new-link default is sent and an
        # update keeps the stack's value (keep_previous), so it never changes.
        r = resolution('cdc')
        step = steps_by_id(plan(r))['broker']
        self.assertEqual(step['parameters']['NodeType'], DEFAULT_BROKER['node_type'])
        self.assertIn('NodeType', step['keep_previous'])
        r['link']['advanced'] = {'broker': {'node_type': 'cache.r7g.large'}}
        for for_update in (False, True):
            s = steps_by_id(plan(r, for_update=for_update))
            self.assertEqual(s['broker']['parameters']['NodeType'], 'cache.r7g.large')

    def test_loader_image_reaches_the_loader_stack(self):
        r = resolution('initial_load')
        r['link']['advanced'] = {'images': {'loader': 'acct.dkr.ecr/l:1'}}
        self.assertEqual(steps_by_id(plan(r))['loader']['parameters']['LoaderImage'],
                         'acct.dkr.ecr/l:1')

    def test_cluster_name_is_always_the_links_own(self):
        # The schema never accepted advanced.network.cluster_name, so a value
        # that slipped past it (a hand-made resolution) must not move the link.
        r = resolution('initial_load_and_cdc')
        r['link']['advanced'] = {'network': {'cluster_name': 'someone-elses'}}
        p = plan(r)
        self.assertEqual(p['cluster_name'], cluster_name('checkout'))
        self.assertEqual(steps_by_id(p)['cdc']['parameters']['ClusterName'], 'ecdl-checkout')


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=0).result
    print('ok: {} provision tests passed'.format(result.testsRun)
          if result.wasSuccessful() else 'FAILED')
    sys.exit(0 if result.wasSuccessful() else 1)


class SourceTLSTest(unittest.TestCase):
    """Jira story F078: the loader verifies TLS to the source unless an external source's
    link turns it off; only that override is sent."""

    def loader_params(self, **source):
        r = resolution('initial_load_and_cdc')
        r['source'].update(source)
        if source.get('external'):
            r['source']['security_group_ids'] = []
        return steps_by_id(plan(r))['loader']['parameters']

    def test_an_aws_source_sends_nothing_and_keeps_the_default(self):
        self.assertNotIn('SourceTLS', self.loader_params())

    def test_an_external_source_with_tls_sends_nothing(self):
        self.assertNotIn('SourceTLS', self.loader_params(external=True, tls=True))

    def test_an_external_source_without_tls_turns_it_off(self):
        self.assertEqual(self.loader_params(external=True, tls=False)['SourceTLS'], 'false')

    def test_tls_false_on_an_aws_source_does_not_turn_it_off(self):
        self.assertNotIn('SourceTLS', self.loader_params(tls=False))

    def test_the_change_is_a_setting_not_an_unexplained_refusal(self):
        from ecdl_link.change import SECTION
        self.assertEqual(SECTION.get('SourceTLS'), 'source.tls')
    def cdc_params(self, **source):
        r = resolution('initial_load_and_cdc')
        r['source'].update(source)
        if source.get('external'):
            r['source']['security_group_ids'] = []
        return steps_by_id(plan(r))['cdc']['parameters']
    def test_debezium_follows_the_same_rule(self):
        # Review finding H1: Debezium verifies TLS too, and the CDC stack has the
        # same SourceTLS parameter, sent only to turn it off.
        self.assertNotIn('SourceTLS', self.cdc_params())
        self.assertNotIn('SourceTLS', self.cdc_params(external=True, tls=True))
        self.assertNotIn('SourceTLS', self.cdc_params(tls=False))
        self.assertEqual(self.cdc_params(external=True, tls=False)['SourceTLS'], 'false')


class FetchBatchTest(unittest.TestCase):
    """Jira story F070: advanced.loader.fetch_batch reaches the loader (it was in the
    schema and went nowhere)."""

    def loader_params(self, loader=None):
        r = resolution('initial_load_and_cdc')
        if loader is not None:
            r['link']['advanced'] = {'loader': loader}
        return steps_by_id(plan(r))['loader']['parameters']

    def test_set_is_sent(self):
        self.assertEqual(self.loader_params({'fetch_batch': 5000})['FetchBatch'], '5000')

    def test_unset_is_not_sent(self):
        self.assertNotIn('FetchBatch', self.loader_params())
        self.assertNotIn('FetchBatch', self.loader_params({'workers': 8}))

    def test_a_change_is_a_setting(self):
        from ecdl_link.change import SECTION
        self.assertEqual(SECTION.get('FetchBatch'), 'advanced.loader')


class LoaderAlarmTest(unittest.TestCase):
    """Jira story F104: the loader stack's load alarm follows advanced.alarms."""

    def loader_params(self, alarms=None):
        r = resolution('initial_load_and_cdc')
        if alarms is not None:
            r['link']['advanced'] = {'alarms': alarms}
        return steps_by_id(plan(r))['loader']['parameters']

    def test_on_by_default_and_nothing_sent(self):
        p = self.loader_params()
        self.assertNotIn('AlarmsEnabled', p)
        self.assertNotIn('AlarmEmail', p)

    def test_off_and_email_follow_the_link(self):
        p = self.loader_params({'enabled': False, 'email': 'ops@example.com'})
        self.assertEqual(p['AlarmsEnabled'], 'false')
        self.assertEqual(p['AlarmEmail'], 'ops@example.com')

    def test_the_template_alarms_on_a_failed_load_task(self):
        import yaml

        class Loader(yaml.SafeLoader):
            pass
        Loader.add_multi_constructor('!', lambda loader, suffix, node: None)
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, '..', '..', 'infra', 'ecdl-loader-ecs.yaml')) as h:
            doc = yaml.load(h, Loader=Loader)
        rule = doc['Resources']['LoadFailedRule']['Properties']
        detail = rule['EventPattern']['detail']
        self.assertEqual(rule['EventPattern']['detail-type'], ['ECS Task State Change'])
        self.assertEqual(detail['lastStatus'], ['STOPPED'])
        self.assertEqual(detail['$or'], [{'containers': {'exitCode': [{'anything-but': 0}]}},
                                         {'stopCode': ['TaskFailedToStart']}])
        self.assertEqual(doc['Resources']['LoadAlarmTopic']['Condition'], 'Alarms')


class BrokerSizingTest(unittest.TestCase):  # Jira stories F023, F024
    def broker(self, broker_cfg=None, for_update=False):
        link = {'name': 'checkout', 'mode': 'cdc'}
        if broker_cfg is not None:
            link['advanced'] = {'broker': broker_cfg}
        p = plan(resolution('cdc', link=link), for_update=for_update)
        return steps_by_id(p)['broker'], p

    def test_a_new_link_gets_a_non_burstable_three_node_broker(self):
        b, p = self.broker()
        self.assertEqual(b['parameters']['NodeType'], 'cache.m7g.large')
        self.assertEqual(b['parameters']['NodeCount'], '3')
        # Kept on an update, so a running broker is never resized by default.
        self.assertIn('NodeType', b['keep_previous'])
        self.assertIn('NodeCount', b['keep_previous'])
        self.assertIn('RestoreSnapshotName', b['keep_previous'])
        note = [n for n in p['notes'] if n.startswith('broker ')]
        self.assertEqual(len(note), 1, p['notes'])
        self.assertIn('cache.m7g.large, 3 nodes: about 4.8 GB', note[0])
        self.assertIn('about 72 minutes of backlog at 1,000 changes/s', note[0])

    def test_set_values_are_sent_and_not_kept(self):
        b, p = self.broker({'node_type': 'cache.r7g.xlarge', 'replicas': 1})
        self.assertEqual(b['parameters']['NodeType'], 'cache.r7g.xlarge')
        self.assertEqual(b['parameters']['NodeCount'], '2')
        self.assertNotIn('NodeType', b['keep_previous'])
        self.assertNotIn('NodeCount', b['keep_previous'])
        self.assertTrue(any('cache.r7g.xlarge, 2 nodes' in n for n in p['notes']))
        # Each setting on its own.
        b, _ = self.broker({'replicas': 4})
        self.assertEqual(b['parameters']['NodeCount'], '5')
        self.assertIn('NodeType', b['keep_previous'])
        self.assertNotIn('NodeCount', b['keep_previous'])

    def test_an_update_without_settings_says_the_broker_is_kept(self):
        b, p = self.broker(for_update=True)
        self.assertIn('NodeType', b['keep_previous'])
        self.assertTrue(any('keeps its deployed node type and node count' in n for n in p['notes']))
        self.assertFalse(any(n.startswith('broker cache.') for n in p['notes']))

    def test_a_burstable_node_warns_and_estimates(self):
        b, p = self.broker({'node_type': 'cache.t4g.small'})
        self.assertTrue(any('burstable' in w for w in p['warnings']), p['warnings'])
        self.assertTrue(any('about 16 minutes' in n for n in p['notes']), p['notes'])
        _, p = self.broker({'node_type': 'cache.x9.huge'})
        self.assertTrue(any('no buffer estimate' in n for n in p['notes']))
        self.assertFalse(any('burstable' in w for w in p['warnings']))

    def test_the_template_takes_the_count_and_defaults_to_two(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            'infra', 'ecdl-broker-quickstart.yaml')
        with open(path) as handle:
            src = handle.read()
        self.assertIn("NumCacheClusters: !If [HasNodeCount, !Ref NodeCount, 2]", src)
        self.assertIn("AllowedPattern: '^[2-6]?$'", src)
        # Stacks created before the planner sent NodeType keep their node.
        self.assertRegex(src, r'NodeType:\n    Type: String\n(    #.*\n)*    Default: cache\.t4g\.small')
