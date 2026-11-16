"""Tests for ARN-driven resolution.

Run with:  python3 ecdl_link/test_resolve.py

Uses hand-written fake AWS clients rather than a mocking library, so the tests
run on a Python interpreter with no extra packages and the fake responses read as documentation of the
shapes the real APIs return.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.resolve import (  # noqa: E402
    ENGINE_FACTS,
    Clients,
    ResolutionError,
    cache_key_prefix,
    default_port,
    describe_resolution,
    engine_family,
    mysql_server_id,
    parse_arn,
    resolve_link,
    resolve_tables,
    stream_key,
)

ACC = '111122223333'
SRC_ARN = 'arn:aws:rds:us-east-2:{}:cluster:my-source'.format(ACC)
SRC_INST_ARN = 'arn:aws:rds:us-east-2:{}:db:my-instance'.format(ACC)
TGT_ARN = ('arn:aws:elasticache:us-east-2:{}:replicationgroup:my-cache'
           .format(ACC))
SEC_ARN = 'arn:aws:secretsmanager:us-east-2:{}:secret:db-pw-AbCdEf'.format(ACC)
TGT_SEC_ARN = ('arn:aws:secretsmanager:us-east-2:{}:secret:cache-pw-AbCdEf'
               .format(ACC))


# ── fakes ──────────────────────────────────────────────────────────────────

class Denied(Exception):
    """Stands in for botocore's AccessDeniedException."""


class NotFound(Exception):
    """Stands in for botocore's ResourceNotFoundException."""


class FakeRds(object):
    """Mirrors the real API's shapes, including one asymmetry that matters.

    `describe_db_clusters` returns `DBSubnetGroup` as a bare **string** -- the
    group's name -- while `describe_db_instances` returns it as an **object**
    carrying VpcId and Subnets. An earlier version of this fake returned the
    object form for both, so the tests passed against a resolver that crashed on
    real AWS with `'str' object has no attribute 'get'`.
    """

    def __init__(self, engine='aurora-mysql', **over):
        self.cluster = {
            'DBClusterIdentifier': 'my-source',
            'Endpoint': 'my-source.cluster-abc.us-east-2.rds.amazonaws.com',
            'Port': None,
            'Engine': engine,
            'EngineVersion': '8.0.mysql_aurora.3.05.2',
            'MasterUsername': 'admin',
            'DatabaseName': None,
            'VpcSecurityGroups': [{'VpcSecurityGroupId': 'sg-source'}],
            'BackupRetentionPeriod': 7,
            'Status': 'available',
            'DBSubnetGroup': 'db-subnets',          # a NAME, per the real API
        }
        self.cluster.update(over)
        self.raise_with = None
        self.subnet_group_calls = 0

    def describe_db_clusters(self, **kw):
        if self.raise_with:
            raise self.raise_with
        return {'DBClusters': [self.cluster]}

    def describe_db_subnet_groups(self, **kw):
        self.subnet_group_calls += 1
        return {'DBSubnetGroups': [{
            'DBSubnetGroupName': 'db-subnets',
            'VpcId': 'vpc-1',
            'Subnets': [{'SubnetIdentifier': 'subnet-db-a'},
                        {'SubnetIdentifier': 'subnet-db-b'}],
        }]}

    def describe_db_instances(self, **kw):
        if self.raise_with:
            raise self.raise_with
        c = self.cluster
        return {'DBInstances': [{
            'Endpoint': {'Address': 'inst.abc.us-east-2.rds.amazonaws.com',
                         'Port': 1433},
            'Engine': c['Engine'],
            'EngineVersion': c['EngineVersion'],
            'MasterUsername': c['MasterUsername'],
            'DBName': 'Inventory',
            'VpcSecurityGroups': c['VpcSecurityGroups'],
            'BackupRetentionPeriod': c['BackupRetentionPeriod'],
            'DBInstanceStatus': 'available',
            # The object form, which is what instances actually return.
            'DBSubnetGroup': {
                'DBSubnetGroupName': 'db-subnets',
                'VpcId': 'vpc-1',
                'Subnets': [{'SubnetIdentifier': 'subnet-db-a'},
                            {'SubnetIdentifier': 'subnet-db-b'}],
            },
        }]}


class FakeElastiCache(object):
    def __init__(self, cluster_mode=True, tls=True, auth_token=False,
                 user_groups=None, shards=1, **over):
        node_groups = []
        for i in range(shards):
            node_groups.append({
                'NodeGroupId': '000{}'.format(i + 1),
                'PrimaryEndpoint': {
                    'Address': 'my-cache-000{}-001.abc.use2.cache.amazonaws.com'
                               .format(i + 1),
                    'Port': 6379},
                'ReaderEndpoint': {
                    'Address': 'my-cache-ro.abc.use2.cache.amazonaws.com',
                    'Port': 6379},
            })
        self.group = {
            'ReplicationGroupId': 'my-cache',
            'ClusterEnabled': cluster_mode,
            'ConfigurationEndpoint': (
                {'Address': 'clustercfg.my-cache.abc.use2.cache.amazonaws.com',
                 'Port': 6379} if cluster_mode else None),
            'NodeGroups': node_groups,
            'TransitEncryptionEnabled': tls,
            'AtRestEncryptionEnabled': True,
            'AuthTokenEnabled': auth_token,
            'UserGroupIds': user_groups or [],
            'CacheNodeType': 'cache.m7g.large',
            'Engine': 'valkey',
            'EngineVersion': '7.2.4',
            # NOTE there is deliberately NEITHER a CacheSubnetGroupName NOR a
            # SecurityGroups here. The real describe_replication_groups response
            # contains neither -- verified against a live cluster, whose response
            # keys are ARN, AtRestEncryptionEnabled, AuthTokenEnabled, ...,
            # NodeGroups, ... with no subnet group and no security-group field of
            # any kind. Both are only on a member cache cluster.
            #
            # This fake invented BOTH at different times, and each invention hid
            # a real bug. The subnet group made the resolver silently fall back to
            # the database's subnets and skip the VPC-match check. SecurityGroups
            # made every target appear to have one when in fact the resolver
            # always produced an empty list -- which nothing noticed until the
            # deployment planner needed the target's security group to add
            # pipeline ingress to it. A fake that returns fields the real API
            # omits makes the tests meaningless.
            'Status': 'available',
            'MemberClusters': ['my-cache-0001-001'],
        }
        self.group.update(over)
        self.subnet_vpc = 'vpc-1'
        self.raise_with = None
        self.cache_cluster_calls = 0

    def describe_replication_groups(self, **kw):
        if self.raise_with:
            raise self.raise_with
        return {'ReplicationGroups': [self.group]}

    def describe_cache_clusters(self, **kw):
        # Where the subnet group and the security groups actually live.
        self.cache_cluster_calls += 1
        return {'CacheClusters': [{
            'CacheClusterId': kw.get('CacheClusterId'),
            'CacheSubnetGroupName': 'cache-subnets',
            'SecurityGroups': [{'SecurityGroupId': 'sg-cache',
                                'Status': 'active'}],
        }]}

    def describe_cache_subnet_groups(self, **kw):
        return {'CacheSubnetGroups': [{
            'CacheSubnetGroupName': 'cache-subnets',
            'VpcId': self.subnet_vpc,
            'Subnets': [{'SubnetIdentifier': 'subnet-cache-a'},
                        {'SubnetIdentifier': 'subnet-cache-b'}],
        }]}


class FakeSecrets(object):
    def __init__(self, name='db-pw'):
        self.name = name
        self.raise_with = None
        self.value_reads = 0

    def describe_secret(self, SecretId=None):
        if self.raise_with:
            raise self.raise_with
        return {'ARN': SecretId, 'Name': self.name, 'RotationEnabled': False}

    def get_secret_value(self, **kw):
        # Recorded so a test can assert resolution never reads a credential.
        self.value_reads += 1
        raise AssertionError('resolution must not read secret values')


def fakes(engine='aurora-mysql', **ec_kw):
    rds = FakeRds(engine=engine)
    ec = FakeElastiCache(**ec_kw)
    sm = FakeSecrets()
    return Clients(rds=rds, elasticache=ec, secretsmanager=sm, ec2=object()), \
        rds, ec, sm


def link(**over):
    doc = {
        'name': 'checkout-cache',
        'mode': 'initial_load_and_cdc',
        'source': {'arn': SRC_ARN, 'database': 'Inventory',
                   'secret_arn': SEC_ARN},
        'target': {'arn': TGT_ARN},
        'tables': ['products', 'orders'],
    }
    doc.update(over)
    return doc


# ── unit-level ─────────────────────────────────────────────────────────────

class EngineTest(unittest.TestCase):

    def test_aurora_variants_map_to_their_family(self):
        self.assertEqual(engine_family('aurora-mysql'), 'mysql')
        self.assertEqual(engine_family('aurora-postgresql'), 'postgresql')

    def test_longest_prefix_wins(self):
        """'aurora-postgresql' must not match the 'postgres' prefix."""
        self.assertEqual(engine_family('aurora-postgresql'), 'postgresql')

    def test_editions_and_custom_engines(self):
        for raw, family in (('sqlserver-ee', 'sqlserver'),
                            ('sqlserver-se', 'sqlserver'),
                            ('oracle-ee', 'oracle'),
                            ('oracle-se2', 'oracle'),
                            ('custom-oracle-ee', 'oracle'),
                            ('mariadb', 'mysql'),
                            ('postgres', 'postgresql'),
                            ('mysql', 'mysql')):
            self.assertEqual(engine_family(raw), family, raw)

    def test_unsupported_engine_named_in_the_error(self):
        with self.assertRaises(ResolutionError) as ctx:
            engine_family('db2')
        self.assertIn('db2', str(ctx.exception))
        self.assertIn('MySQL, PostgreSQL, SQL Server and Oracle',
                      str(ctx.exception))

    def test_default_port_is_per_engine(self):
        """The bug being fixed: everything used to default to 5432."""
        self.assertEqual(default_port('aurora-mysql'), 3306)
        self.assertEqual(default_port('mysql'), 3306)
        self.assertEqual(default_port('aurora-postgresql'), 5432)
        self.assertEqual(default_port('sqlserver-ee'), 1433)
        self.assertEqual(default_port('oracle-ee'), 1521)
        self.assertIsNone(default_port('db2'))

    def test_every_engine_has_complete_facts(self):
        keys = {'label', 'port', 'connector_class', 'database_property',
                'qualify_with', 'topic_parts', 'default_schema',
                'uppercase_identifiers', 'needs_server_id',
                'needs_schema_history', 'no_data_snapshot_mode', 'log_name'}
        for family, facts in ENGINE_FACTS.items():
            self.assertEqual(set(facts), keys, family)


class ArnTest(unittest.TestCase):

    def test_colon_separated_resource(self):
        p = parse_arn(SRC_ARN)
        self.assertEqual(p['service'], 'rds')
        self.assertEqual(p['region'], 'us-east-2')
        self.assertEqual(p['account'], ACC)
        self.assertEqual(p['resource_type'], 'cluster')
        self.assertEqual(p['resource_id'], 'my-source')

    def test_slash_separated_resource(self):
        p = parse_arn('arn:aws:ecs:us-east-2:{}:service/my-cluster/my-svc'
                      .format(ACC))
        self.assertEqual(p['resource_type'], 'service')
        self.assertEqual(p['resource_id'], 'my-cluster/my-svc')

    def test_partition_preserved(self):
        self.assertEqual(
            parse_arn('arn:aws-us-gov:rds:us-gov-west-1:{}:cluster:c'
                      .format(ACC))['partition'], 'aws-us-gov')

    def test_malformed_value_rejected(self):
        for bad in ('', 'my-cluster', 'arn:aws:rds:us-east-2:cluster:c'):
            with self.assertRaises(ResolutionError):
                parse_arn(bad)


class TableNamingTest(unittest.TestCase):
    """Per-engine naming. Getting Oracle wrong once put 2,000,000 events in the
    broker and none in the target, so each engine is tested explicitly."""

    def test_mysql_qualifies_with_database(self):
        rows = resolve_tables(['orders'], 'mysql', 'Inventory', None)
        self.assertEqual(rows[0]['qualified'], 'Inventory.orders')
        self.assertEqual(rows[0]['topic_suffix'], 'Inventory.orders')

    def test_postgres_qualifies_with_schema(self):
        rows = resolve_tables(['orders'], 'postgresql', 'Inventory', 'public')
        self.assertEqual(rows[0]['qualified'], 'public.orders')
        self.assertEqual(rows[0]['topic_suffix'], 'public.orders')

    def test_sqlserver_topic_includes_the_database(self):
        """SQL Server is the only four-part topic name."""
        rows = resolve_tables(['orders'], 'sqlserver', 'Inventory', 'dbo')
        self.assertEqual(rows[0]['qualified'], 'dbo.orders')
        self.assertEqual(rows[0]['topic_suffix'], 'Inventory.dbo.orders')

    def test_oracle_uppercases_and_omits_the_database(self):
        rows = resolve_tables(['orders3w'], 'oracle', 'ORCL', 'ADMIN')
        self.assertEqual(rows[0]['table'], 'ORDERS3W')
        self.assertEqual(rows[0]['qualified'], 'ADMIN.ORDERS3W')
        self.assertEqual(rows[0]['topic_suffix'], 'ADMIN.ORDERS3W')
        self.assertNotIn('ORCL', rows[0]['topic_suffix'])

    def test_customer_qualification_wins(self):
        rows = resolve_tables(['sales.orders'], 'postgresql', 'db', 'public')
        self.assertEqual(rows[0]['schema'], 'sales')
        self.assertEqual(rows[0]['qualified'], 'sales.orders')

    def test_two_part_name_is_database_qualified_for_mysql(self):
        rows = resolve_tables(['Other.orders'], 'mysql', 'Inventory', None)
        self.assertEqual(rows[0]['database'], 'Other')
        self.assertEqual(rows[0]['qualified'], 'Other.orders')

    def test_three_part_name_allowed_only_for_sqlserver(self):
        rows = resolve_tables(['Inventory.dbo.orders'], 'sqlserver', 'X', 'dbo')
        self.assertEqual(rows[0]['topic_suffix'], 'Inventory.dbo.orders')
        with self.assertRaises(ResolutionError) as ctx:
            resolve_tables(['a.b.c'], 'mysql', 'Inventory', None)
        self.assertIn('three parts', str(ctx.exception))

    def test_schemaless_engine_without_schema_is_an_error(self):
        with self.assertRaises(ResolutionError) as ctx:
            resolve_tables(['orders'], 'postgresql', 'db', None)
        self.assertIn('no schema is known', str(ctx.exception))

    def test_stream_key_carries_the_data_prefix(self):
        self.assertEqual(stream_key('checkout', 'Inventory.orders'),
                         'data:checkout.Inventory.orders')

    def test_cache_key_prefix_matches_what_the_sink_writes(self):
        self.assertEqual(cache_key_prefix('orders'), 'orders:')
        self.assertEqual(cache_key_prefix('ORDERS3W'), 'ORDERS3W:')
        self.assertEqual(cache_key_prefix('orders', 'live'), 'live:orders:')


class ServerIdTest(unittest.TestCase):

    def test_stable_across_calls(self):
        self.assertEqual(mysql_server_id('checkout-cache'),
                         mysql_server_id('checkout-cache'))

    def test_differs_between_links(self):
        self.assertNotEqual(mysql_server_id('link-a'), mysql_server_id('link-b'))

    def test_within_mysql_range_and_not_the_hardcoded_default(self):
        for name in ('a', 'checkout-cache', 'z' * 32):
            value = mysql_server_id(name)
            self.assertGreater(value, 0)
            self.assertLess(value, 2 ** 32)
            self.assertNotEqual(value, 88888)


# ── end to end ─────────────────────────────────────────────────────────────

class ResolveTest(unittest.TestCase):

    def test_mysql_cluster_resolves(self):
        clients, rds, ec, sm = fakes()
        r = resolve_link(link(), clients=clients)
        self.assertEqual(r['source']['engine'], 'mysql')
        self.assertEqual(r['source']['port'], 3306)
        self.assertEqual(r['source']['username'], 'admin')
        self.assertEqual(r['source']['connector_class'],
                         'io.debezium.connector.mysql.MySqlConnector')
        self.assertEqual(r['debezium']['table_include_list'],
                         'Inventory.products,Inventory.orders')
        self.assertEqual([t['stream'] for t in r['tables']],
                         ['data:checkout-cache.Inventory.products',
                          'data:checkout-cache.Inventory.orders'])
        self.assertEqual(r['debezium']['snapshot_mode'], 'schema_only')
        self.assertIsNotNone(r['debezium']['server_id'])

    def test_port_falls_back_to_the_engine_default_not_5432(self):
        clients, rds, ec, sm = fakes()
        rds.cluster['Port'] = None
        r = resolve_link(link(), clients=clients)
        self.assertEqual(r['source']['port'], 3306)

    def test_reported_port_is_preferred(self):
        clients, rds, ec, sm = fakes()
        rds.cluster['Port'] = 3307
        self.assertEqual(resolve_link(link(), clients=clients)['source']['port'],
                         3307)

    def test_postgres_resolves_with_public_schema(self):
        clients, rds, ec, sm = fakes(engine='aurora-postgresql')
        r = resolve_link(link(), clients=clients)
        self.assertEqual(r['source']['schema'], 'public')
        self.assertEqual(r['source']['port'], 5432)
        self.assertEqual(r['debezium']['table_include_list'],
                         'public.products,public.orders')
        self.assertEqual(r['debezium']['snapshot_mode'], 'no_data')
        self.assertIsNone(r['debezium']['server_id'],
                          'only MySQL needs a server id')
        self.assertEqual(r['debezium']['database_property'], 'database.dbname')

    def test_sqlserver_resolves_with_dbo_and_plural_database_property(self):
        clients, rds, ec, sm = fakes(engine='sqlserver-ee')
        r = resolve_link(link(), clients=clients)
        self.assertEqual(r['source']['schema'], 'dbo')
        self.assertEqual(r['debezium']['database_property'], 'database.names')
        self.assertEqual(r['tables'][0]['stream'],
                         'data:checkout-cache.Inventory.dbo.products')

    def test_oracle_schema_defaults_to_the_username_uppercased(self):
        clients, rds, ec, sm = fakes(engine='oracle-ee')
        rds.cluster['MasterUsername'] = 'admin'
        r = resolve_link(link(), clients=clients)
        self.assertEqual(r['source']['schema'], 'ADMIN')
        self.assertEqual(r['debezium']['table_include_list'],
                         'ADMIN.PRODUCTS,ADMIN.ORDERS')
        self.assertEqual(r['debezium']['schema_include_list'], 'ADMIN')
        self.assertEqual(r['tables'][0]['key_prefix'], 'PRODUCTS:')
        self.assertEqual(r['tables'][0]['stream'],
                         'data:checkout-cache.ADMIN.PRODUCTS')

    def test_instance_arn_resolves(self):
        clients, rds, ec, sm = fakes(engine='sqlserver-ee')
        r = resolve_link(link(source={'arn': SRC_INST_ARN,
                                      'database': 'Inventory',
                                      'secret_arn': SEC_ARN}),
                         clients=clients)
        self.assertEqual(r['source']['kind'], 'db')
        self.assertEqual(r['source']['port'], 1433)

    def test_explicit_username_and_schema_win(self):
        clients, rds, ec, sm = fakes(engine='aurora-postgresql')
        r = resolve_link(link(source={'arn': SRC_ARN, 'database': 'Inventory',
                                      'secret_arn': SEC_ARN,
                                      'username': 'svc', 'schema': 'sales'}),
                         clients=clients)
        self.assertEqual(r['source']['username'], 'svc')
        self.assertEqual(r['source']['schema'], 'sales')

    def test_secret_values_are_never_read(self):
        clients, rds, ec, sm = fakes()
        resolve_link(link(), clients=clients)
        self.assertEqual(sm.value_reads, 0)


class TargetTopologyTest(unittest.TestCase):

    def test_cluster_mode_on_uses_the_configuration_endpoint(self):
        clients, rds, ec, sm = fakes(cluster_mode=True, shards=3)
        r = resolve_link(link(), clients=clients)
        self.assertTrue(r['target']['cluster_mode'])
        self.assertEqual(r['target']['endpoint_source'], 'ConfigurationEndpoint')
        self.assertTrue(r['target']['endpoint'].startswith('clustercfg.'))
        self.assertEqual(r['target']['shards'], 3)
        self.assertIsNone(r['target']['reader_endpoint'])

    def test_cluster_mode_off_uses_the_primary_endpoint(self):
        clients, rds, ec, sm = fakes(cluster_mode=False)
        r = resolve_link(link(), clients=clients)
        self.assertFalse(r['target']['cluster_mode'])
        self.assertEqual(r['target']['endpoint_source'], 'PrimaryEndpoint')
        self.assertNotIn('clustercfg', r['target']['endpoint'])
        self.assertIsNotNone(r['target']['reader_endpoint'])

    def test_cluster_mode_is_reported_as_detected(self):
        for mode in (True, False):
            clients, rds, ec, sm = fakes(cluster_mode=mode)
            r = resolve_link(link(), clients=clients)
            expected = 'enabled' if mode else 'disabled'
            self.assertIn('target cluster mode: {} (detected)'.format(expected),
                          r['notes'])

    def test_missing_endpoint_is_explained(self):
        clients, rds, ec, sm = fakes(cluster_mode=True)
        ec.group['ConfigurationEndpoint'] = None
        with self.assertRaises(ResolutionError) as ctx:
            resolve_link(link(), clients=clients)
        self.assertIn('still be creating', str(ctx.exception))


class TargetAuthTest(unittest.TestCase):

    def test_no_auth_needs_no_secret(self):
        clients, rds, ec, sm = fakes(auth_token=False, user_groups=[])
        self.assertEqual(resolve_link(link(), clients=clients)
                         ['target']['auth_mode'], 'none')

    def test_auth_token_detected_and_secret_required(self):
        clients, rds, ec, sm = fakes(auth_token=True)
        with self.assertRaises(ResolutionError) as ctx:
            resolve_link(link(), clients=clients)
        self.assertIn('requires token authentication', str(ctx.exception))
        self.assertIn('Secrets Manager', str(ctx.exception))

    def test_auth_token_with_secret_resolves(self):
        clients, rds, ec, sm = fakes(auth_token=True)
        r = resolve_link(link(target={'arn': TGT_ARN,
                                      'secret_arn': TGT_SEC_ARN}),
                         clients=clients)
        self.assertEqual(r['target']['auth_mode'], 'token')
        self.assertIn('secret', r['target'])

    def test_rbac_detected_from_user_groups(self):
        clients, rds, ec, sm = fakes(user_groups=['app-users'])
        r = resolve_link(link(target={'arn': TGT_ARN,
                                      'secret_arn': TGT_SEC_ARN}),
                         clients=clients)
        self.assertEqual(r['target']['auth_mode'], 'rbac')
        self.assertEqual(r['target']['user_group_ids'], ['app-users'])

    def test_rbac_wins_over_auth_token(self):
        clients, rds, ec, sm = fakes(auth_token=True, user_groups=['g'])
        r = resolve_link(link(target={'arn': TGT_ARN,
                                      'secret_arn': TGT_SEC_ARN}),
                         clients=clients)
        self.assertEqual(r['target']['auth_mode'], 'rbac')

    def test_unused_secret_warns_rather_than_failing(self):
        clients, rds, ec, sm = fakes(auth_token=False)
        r = resolve_link(link(target={'arn': TGT_ARN,
                                      'secret_arn': TGT_SEC_ARN}),
                         clients=clients)
        self.assertTrue(any('will not be used' in w for w in r['warnings']))

    def test_tls_off_on_the_target_warns(self):
        clients, rds, ec, sm = fakes(tls=False)
        r = resolve_link(link(), clients=clients)
        self.assertTrue(any('plaintext' in w for w in r['warnings']))


class NetworkTest(unittest.TestCase):

    def test_subnets_come_from_the_cache_subnet_group(self):
        clients, rds, ec, sm = fakes()
        r = resolve_link(link(), clients=clients)
        self.assertEqual(r['network']['subnet_ids'],
                         ['subnet-cache-a', 'subnet-cache-b'])
        self.assertEqual(r['network']['vpc_id'], 'vpc-1')

    def test_explicit_subnets_win(self):
        clients, rds, ec, sm = fakes()
        r = resolve_link(link(advanced={'network': {
            'subnet_ids': ['subnet-mine-a']}}), clients=clients)
        self.assertEqual(r['network']['subnet_ids'], ['subnet-mine-a'])

    def test_vpc_mismatch_fails_early_with_both_ids(self):
        clients, rds, ec, sm = fakes()
        ec.subnet_vpc = 'vpc-2'
        with self.assertRaises(ResolutionError) as ctx:
            resolve_link(link(), clients=clients)
        text = str(ctx.exception)
        self.assertIn('vpc-1', text)
        self.assertIn('vpc-2', text)
        self.assertIn('peering', text)

    def test_target_subnets_take_two_hops(self):
        """The subnet group is on a member cluster, not the replication group."""
        clients, rds, ec, sm = fakes()
        r = resolve_link(link(), clients=clients)
        self.assertEqual(ec.cache_cluster_calls, 1)
        self.assertEqual(r['target']['subnet_group_name'], 'cache-subnets')
        self.assertEqual(r['target']['vpc_id'], 'vpc-1')

    def test_falls_back_to_database_subnets_and_says_the_check_was_skipped(self):
        """A failed network lookup must not silently skip the VPC check."""
        clients, rds, ec, sm = fakes()
        ec.describe_cache_subnet_groups = lambda **kw: {'CacheSubnetGroups': []}
        r = resolve_link(link(), clients=clients)
        self.assertEqual(r['network']['subnet_ids'],
                         ['subnet-db-a', 'subnet-db-b'])
        self.assertTrue(
            any('could not confirm the source and target share a VPC' in w
                for w in r['warnings']),
            'a skipped VPC check has to be visible: {}'.format(r['warnings']))

    def test_no_member_clusters_is_reported(self):
        clients, rds, ec, sm = fakes()
        ec.group['MemberClusters'] = []
        r = resolve_link(link(), clients=clients)
        self.assertTrue(any('no member clusters' in w for w in r['warnings']))

    def test_security_groups_captured_for_both_stores(self):
        clients, rds, ec, sm = fakes()
        r = resolve_link(link(), clients=clients)
        self.assertEqual(r['source']['security_group_ids'], ['sg-source'])
        self.assertEqual(r['target']['security_group_ids'], ['sg-cache'])

    def test_target_security_groups_come_from_the_member_cluster(self):
        """Not from the replication group, which has no such field.

        Regression: describe_target read g['SecurityGroups'], which the live API
        never returns, so every target resolved with an empty list. It went
        unnoticed because nothing consumed it until the deployment planner needed
        the target's security group to add pipeline ingress to it -- at which
        point planning failed with "the target has no security group".
        """
        clients, rds, ec, sm = fakes()
        # Prove the value can ONLY have come from the member-cluster hop.
        self.assertNotIn('SecurityGroups', ec.group)
        r = resolve_link(link(), clients=clients)
        self.assertEqual(r['target']['security_group_ids'], ['sg-cache'])
        self.assertGreaterEqual(ec.cache_cluster_calls, 1)

    def test_target_security_groups_empty_when_the_hop_fails(self):
        """A failed hop yields no groups AND a warning, never a silent empty list."""
        clients, rds, ec, sm = fakes()
        ec.describe_cache_clusters = lambda **kw: {'CacheClusters': []}
        r = resolve_link(link(), clients=clients)
        self.assertEqual(r['target']['security_group_ids'], [])
        self.assertTrue(any('could not confirm' in w for w in r['warnings']),
                        'a skipped lookup must be reported: %s' % r['warnings'])

    def test_cluster_subnet_group_needs_a_second_lookup(self):
        """describe_db_clusters gives only the subnet group's NAME.

        Regression guard: the resolver previously treated it as an object and
        crashed against real AWS with `'str' object has no attribute 'get'`.
        """
        clients, rds, ec, sm = fakes()
        r = resolve_link(link(), clients=clients)
        self.assertEqual(rds.subnet_group_calls, 1)
        self.assertEqual(r['source']['vpc_id'], 'vpc-1')
        self.assertEqual(r['source']['subnet_ids'],
                         ['subnet-db-a', 'subnet-db-b'])

    def test_instance_subnet_group_is_already_expanded(self):
        """Instances return the object form, so no extra call is needed."""
        clients, rds, ec, sm = fakes(engine='sqlserver-ee')
        r = resolve_link(link(source={'arn': SRC_INST_ARN,
                                      'database': 'Inventory',
                                      'secret_arn': SEC_ARN}),
                         clients=clients)
        self.assertEqual(rds.subnet_group_calls, 0)
        self.assertEqual(r['source']['vpc_id'], 'vpc-1')
        self.assertEqual(r['source']['subnet_ids'],
                         ['subnet-db-a', 'subnet-db-b'])


class ModeTest(unittest.TestCase):

    def test_initial_load_creates_no_pipeline(self):
        clients, rds, ec, sm = fakes()
        r = resolve_link(link(mode='initial_load'), clients=clients)
        self.assertEqual(r['components'], {'loader': True, 'broker': False,
                                           'debezium': False, 'sink': False})
        self.assertFalse(r['debezium']['enabled'])

    def test_cdc_creates_no_loader(self):
        clients, rds, ec, sm = fakes()
        r = resolve_link(link(mode='cdc'), clients=clients)
        self.assertEqual(r['components'], {'loader': False, 'broker': True,
                                           'debezium': True, 'sink': True})

    def test_both_creates_everything(self):
        clients, rds, ec, sm = fakes()
        r = resolve_link(link(mode='initial_load_and_cdc'), clients=clients)
        self.assertTrue(all(r['components'].values()))

    def test_snapshot_mode_override_respected(self):
        clients, rds, ec, sm = fakes()
        r = resolve_link(link(advanced={'debezium': {
            'snapshot_mode': 'initial'}}), clients=clients)
        self.assertEqual(r['debezium']['snapshot_mode'], 'initial')

    def test_server_id_override_respected(self):
        clients, rds, ec, sm = fakes()
        r = resolve_link(link(advanced={'debezium': {'server_id': 777}}),
                         clients=clients)
        self.assertEqual(r['debezium']['server_id'], 777)


class ErrorTest(unittest.TestCase):

    def test_access_denied_says_what_permission_is_missing(self):
        clients, rds, ec, sm = fakes()
        rds.raise_with = Denied('User is not authorized to perform: '
                                'rds:DescribeDBClusters')
        with self.assertRaises(ResolutionError) as ctx:
            resolve_link(link(), clients=clients)
        self.assertIn('not permitted to describe', str(ctx.exception))
        self.assertIn('describe access', str(ctx.exception))

    def test_not_found_points_at_account_and_region(self):
        clients, rds, ec, sm = fakes()
        ec.raise_with = NotFound('ReplicationGroupNotFoundFault')
        with self.assertRaises(ResolutionError) as ctx:
            resolve_link(link(), clients=clients)
        self.assertIn('does not exist', str(ctx.exception))
        self.assertIn('region', str(ctx.exception))

    def test_unsafe_database_name_is_a_resolution_error(self):
        # It is written into the signalling-table SQL, so it must be a plain
        # identifier -- and the refusal reads like any other, not a traceback.
        clients, rds, ec, sm = fakes()
        with self.assertRaises(ResolutionError) as ctx:
            resolve_link(link(source={'arn': SRC_ARN, 'secret_arn': SEC_ARN,
                                      'database': 'Inventory; DROP DATABASE x'}),
                         clients=clients)
        self.assertIn('not a plain SQL identifier', str(ctx.exception))

    def test_unreadable_secret_is_reported_before_deploying(self):
        clients, rds, ec, sm = fakes()
        sm.raise_with = Denied('AccessDeniedException')
        with self.assertRaises(ResolutionError) as ctx:
            resolve_link(link(), clients=clients)
        self.assertIn('source secret', str(ctx.exception))

    def test_missing_credentials_reads_as_an_operational_state(self):
        """Not an internal error -- it means 'refresh your credentials'."""
        class NoCredentialsError(Exception):
            pass

        clients, rds, ec, sm = fakes()
        rds.raise_with = NoCredentialsError('Unable to locate credentials')
        with self.assertRaises(ResolutionError) as ctx:
            resolve_link(link(), clients=clients)
        text = str(ctx.exception)
        self.assertIn('no usable AWS credentials', text)
        self.assertNotIn('not permitted', text)

    def test_expired_token_recognised_as_a_credential_problem(self):
        class ClientError(Exception):
            pass

        clients, rds, ec, sm = fakes()
        rds.raise_with = ClientError(
            'An error occurred (ExpiredToken) when calling DescribeDBClusters')
        with self.assertRaises(ResolutionError) as ctx:
            resolve_link(link(), clients=clients)
        self.assertIn('no usable AWS credentials', str(ctx.exception))

    def test_missing_region_names_the_fix(self):
        class NoRegionError(Exception):
            pass

        clients, rds, ec, sm = fakes()
        rds.raise_with = NoRegionError('You must specify a region')
        with self.assertRaises(ResolutionError) as ctx:
            resolve_link(link(), clients=clients)
        self.assertIn('no AWS region configured', str(ctx.exception))

    def test_zero_backup_retention_warns_about_log_purging(self):
        clients, rds, ec, sm = fakes()
        rds.cluster['BackupRetentionPeriod'] = 0
        r = resolve_link(link(), clients=clients)
        self.assertTrue(any('binlog' in w for w in r['warnings']))

    def test_non_available_source_warns(self):
        clients, rds, ec, sm = fakes()
        rds.cluster['Status'] = 'modifying'
        r = resolve_link(link(), clients=clients)
        self.assertTrue(any('modifying' in w for w in r['warnings']))

    def test_missing_username_is_explained(self):
        clients, rds, ec, sm = fakes()
        rds.cluster['MasterUsername'] = None
        with self.assertRaises(ResolutionError) as ctx:
            resolve_link(link(), clients=clients)
        self.assertIn('source.username', str(ctx.exception))


class SummaryTest(unittest.TestCase):

    def test_summary_shows_streams_and_what_gets_created(self):
        clients, rds, ec, sm = fakes()
        text = describe_resolution(resolve_link(link(), clients=clients))
        self.assertIn('checkout-cache (initial_load_and_cdc)', text)
        self.assertIn('data:checkout-cache.Inventory.products', text)
        self.assertIn('creates: broker, debezium, loader, sink', text)
        self.assertIn('cluster mode: enabled (detected)', text)

    def test_summary_flags_warnings(self):
        clients, rds, ec, sm = fakes(tls=False)
        text = describe_resolution(resolve_link(link(), clients=clients))
        self.assertIn('!', text)


class ExternalSourceResolution(unittest.TestCase):
    """A self-managed / on-prem source resolves from the link, not from RDS."""

    def ext_link(self, **src_over):
        src = {'kind': 'external', 'engine': 'postgresql',
               'host': 'db.corp.internal', 'database': 'Inventory',
               'username': 'repl', 'secret_arn': SEC_ARN}
        src.update(src_over)
        return link(source=src)

    def test_resolves_without_querying_rds(self):
        clients, rds, ec, sm = fakes()
        # Prove the RDS path is never taken: any describe would raise.
        rds.raise_with = AssertionError('RDS must not be queried for external')
        res = resolve_link(self.ext_link(), clients=clients)
        s = res['source']
        self.assertEqual(s['engine'], 'postgresql')
        self.assertEqual(s['endpoint'], 'db.corp.internal')
        self.assertTrue(s['external'])
        # No AWS security group, so the plan sends an empty one and the template
        # skips the source ingress rule.
        self.assertEqual(s['security_group_ids'], [])

    def test_port_defaults_to_the_engine_then_honours_an_override(self):
        clients, _, _, _ = fakes()
        self.assertEqual(
            resolve_link(self.ext_link(), clients=clients)['source']['port'], 5432)
        self.assertEqual(
            resolve_link(self.ext_link(port=5433),
                         clients=clients)['source']['port'], 5433)

    def test_notes_reachability_and_warns_on_unknowable_retention(self):
        clients, _, _, _ = fakes()
        res = resolve_link(self.ext_link(), clients=clients)
        self.assertTrue(any('reachability' in n for n in res['notes']))
        self.assertTrue(any('retention cannot be checked' in w
                            for w in res['warnings']))

    def test_uses_the_target_vpc_for_the_pipeline(self):
        # The source has no VPC, so the tasks run in the cache's VPC and the
        # source/target VPC-match check must not fire.
        clients, _, _, _ = fakes()
        res = resolve_link(self.ext_link(), clients=clients)
        self.assertEqual(res['network']['vpc_id'], 'vpc-1')

    def test_missing_username_raises_a_clear_error(self):
        # Resolution can run without prior validation, so it repeats the
        # semantic rule with a message that does not mention RDS.
        clients, _, _, _ = fakes()
        broken = self.ext_link()
        del broken['source']['username']
        with self.assertRaises(ResolutionError) as ctx:
            resolve_link(broken, clients=clients)
        self.assertIn('external source', str(ctx.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
