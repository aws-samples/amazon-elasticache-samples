"""Tests for discover.py.

The fakes here mirror shapes captured from the live APIs, absences included. That
detail is the point: twice in this project a fake that filled in a field the real
API omits let a bug pass tests and fail in production -- `DBSubnetGroup` returned
as a bare string by describe_db_clusters but an object by describe_db_instances,
and `SecurityGroups`, which describe_replication_groups does not return at all.

Verified absent against the live API (us-east-2, September 2026), so absent here:
  Aurora cluster ......... DatabaseName, MasterUserSecret
  DB instance ............ DBName, MasterUserSecret
  Replication group ...... EngineVersion, UserGroupIds
  Secret ................. Description, RotationEnabled, DeletedDate
"""

import os
import sys
import unittest

import ecdl_link.discover as discover_mod

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.discover import (  # noqa: E402
    discover,
    list_secrets,
    list_sources,
    list_targets,
    region_of,
    secrets_for_source,
)


# ── fakes ──────────────────────────────────────────────────────────────────

# Captured from the live API. Note what is NOT here.
AURORA_CLUSTER = {
    'DBClusterArn': 'arn:aws:rds:us-east-2:111122223333:cluster:ecdl-aurora-mysql',
    'DBClusterIdentifier': 'ecdl-aurora-mysql',
    'Engine': 'aurora-mysql',
    'EngineMode': 'provisioned',
    'EngineVersion': '8.0.mysql_aurora.3.08.0',
    'Endpoint': 'ecdl-aurora-mysql.cluster-clusterxxxxxx.us-east-2.rds.amazonaws.com',
    'Port': 3306,
    'MasterUsername': 'admin',
    'Status': 'available',
    'MultiAZ': False,
    'ServerlessV2ScalingConfiguration': {'MinCapacity': 0.5, 'MaxCapacity': 64.0},
    # DatabaseName and MasterUserSecret are genuinely absent.
}

AURORA_MEMBER_INSTANCE = {
    'DBInstanceArn': 'arn:aws:rds:us-east-2:111122223333:db:ecdl-aurora-mysql-instance',
    'DBInstanceIdentifier': 'ecdl-aurora-mysql-instance',
    'Engine': 'aurora-mysql',
    'EngineVersion': '8.0.mysql_aurora.3.08.0',
    'Endpoint': {'Address': 'ecdl-aurora-mysql-instance.clusterxxxxxx.us-east-2.rds.amazonaws.com',
                 'Port': 3306},
    'MasterUsername': 'admin',
    'DBInstanceStatus': 'available',
    'DBClusterIdentifier': 'ecdl-aurora-mysql',
    'MultiAZ': False,
}

STANDALONE_MYSQL = {
    'DBInstanceArn': 'arn:aws:rds:us-east-2:111122223333:db:plain-mysql',
    'DBInstanceIdentifier': 'plain-mysql',
    'Engine': 'mysql',
    'EngineVersion': '8.0.35',
    'Endpoint': {'Address': 'plain-mysql.clusterxxxxxx.us-east-2.rds.amazonaws.com',
                 'Port': 3306},
    'MasterUsername': 'root',
    'DBName': 'shop',
    'DBInstanceStatus': 'available',
    'MasterUserSecret': {
        'SecretArn': 'arn:aws:secretsmanager:us-east-2:111122223333:secret:rds!db-abc-xyz',
        'SecretStatus': 'active',
    },
}

CLUSTER_MODE_TARGET = {
    'ARN': 'arn:aws:elasticache:us-east-2:111122223333:replicationgroup:ecdl-bench-3shard',
    'ReplicationGroupId': 'ecdl-bench-3shard',
    'Description': 'ECDL 3-way benchmark 3-shard target',
    'Status': 'available',
    'ClusterEnabled': True,
    'ConfigurationEndpoint': {
        'Address': 'clustercfg.ecdl-bench-3shard.xxxxxx.use2.cache.amazonaws.com',
        'Port': 6379},
    'NodeGroups': [{'NodeGroupId': '0001'}, {'NodeGroupId': '0002'},
                   {'NodeGroupId': '0003'}],
    'CacheNodeType': 'cache.m7g.large',
    'Engine': 'valkey',
    'TransitEncryptionEnabled': True,
    'AuthTokenEnabled': False,
    # EngineVersion and UserGroupIds absent, as the live API returns them.
}

SINGLE_NODE_TARGET = {
    'ARN': 'arn:aws:elasticache:us-east-2:111122223333:replicationgroup:ecdl-demo-broker-cache',
    'ReplicationGroupId': 'ecdl-demo-broker-cache',
    'Status': 'available',
    'ClusterEnabled': False,
    'NodeGroups': [{
        'NodeGroupId': '0001',
        'PrimaryEndpoint': {'Address': 'master.broker.use2.cache.amazonaws.com',
                            'Port': 6379},
    }],
    'CacheNodeType': 'cache.t4g.small',
    'Engine': 'valkey',
    'TransitEncryptionEnabled': False,
    'AuthTokenEnabled': False,
}

PLAIN_SECRET = {
    'ARN': 'arn:aws:secretsmanager:us-east-2:111122223333:secret:ecdl/aurora-db-password',
    'Name': 'ecdl/aurora-db-password',
    'CreatedDate': 'x',
    # Description, RotationEnabled, DeletedDate genuinely absent.
}

MANAGED_SECRET = {
    'ARN': 'arn:aws:secretsmanager:us-east-2:111122223333:secret:rds!db-abc-xyz',
    'Name': 'rds!db-abc-xyz',
}


class FakeRds(object):
    def __init__(self, clusters=None, instances=None, pages=None):
        self._clusters = clusters if clusters is not None else []
        self._instances = instances if instances is not None else []
        self._pages = pages or {}
        self.cluster_calls = []

    def describe_db_clusters(self, **kwargs):
        self.cluster_calls.append(kwargs)
        if 'clusters' in self._pages:
            return self._pages['clusters'](kwargs)
        return {'DBClusters': self._clusters}

    def describe_db_instances(self, **kwargs):
        return {'DBInstances': self._instances}


class FakeElastiCache(object):
    def __init__(self, groups=None):
        self._groups = groups if groups is not None else []

    def describe_replication_groups(self, **kwargs):
        return {'ReplicationGroups': self._groups}


class FakeSecrets(object):
    """Lists secrets, and raises an error if anything asks for a value."""

    def __init__(self, secrets=None, pages=None):
        self._secrets = secrets if secrets is not None else []
        self._pages = pages
        self.value_reads = 0

    def list_secrets(self, **kwargs):
        if self._pages:
            return self._pages(kwargs)
        return {'SecretList': self._secrets}

    def get_secret_value(self, **kwargs):
        self.value_reads += 1
        raise AssertionError(
            'get_secret_value must never be called from discovery')


class Denied(Exception):
    """Shaped like a botocore AccessDenied so _list_problem recognises it."""

    def __init__(self):
        super(Denied, self).__init__(
            'User is not authorized to perform this operation (AccessDenied)')


class FailingRds(object):
    def describe_db_clusters(self, **kwargs):
        raise Denied()

    def describe_db_instances(self, **kwargs):
        raise Denied()


class Clients(object):
    def __init__(self, rds=None, elasticache=None, secretsmanager=None, ec2=None):
        self.rds = rds or FakeRds()
        self.elasticache = elasticache or FakeElastiCache()
        self.secretsmanager = secretsmanager or FakeSecrets()
        self.ec2 = ec2


# ── sources ────────────────────────────────────────────────────────────────

class SourceTest(unittest.TestCase):

    def test_aurora_cluster_reads_from_real_shape(self):
        c = Clients(rds=FakeRds(clusters=[AURORA_CLUSTER]))
        out = list_sources(c)
        self.assertEqual(out['count'], 1)
        s = out['sources'][0]
        self.assertEqual(s['identifier'], 'ecdl-aurora-mysql')
        self.assertEqual(s['kind'], 'cluster')
        self.assertEqual(s['engine'], 'mysql')
        self.assertEqual(s['engine_label'], 'MySQL')
        self.assertEqual(s['port'], 3306)
        self.assertEqual(s['username'], 'admin')
        self.assertTrue(s['usable'])
        self.assertEqual(s['blockers'], [])

    def test_absent_database_name_becomes_none_not_a_crash(self):
        # Aurora omits DatabaseName, which is exactly why source.database is a
        # required field in the link schema rather than something we discover.
        c = Clients(rds=FakeRds(clusters=[AURORA_CLUSTER]))
        s = list_sources(c)['sources'][0]
        self.assertIsNone(s['database'])

    def test_absent_master_user_secret_becomes_none(self):
        c = Clients(rds=FakeRds(clusters=[AURORA_CLUSTER]))
        self.assertIsNone(list_sources(c)['sources'][0]['master_user_secret_arn'])

    def test_managed_master_password_is_surfaced(self):
        # When RDS manages the password it names the secret, so the credential
        # step can be pre-answered instead of asked.
        c = Clients(rds=FakeRds(instances=[STANDALONE_MYSQL]))
        s = list_sources(c)['sources'][0]
        self.assertEqual(s['master_user_secret_arn'],
                         'arn:aws:secretsmanager:us-east-2:111122223333:secret:rds!db-abc-xyz')

    def test_aurora_member_instance_is_flagged_and_names_the_cluster(self):
        c = Clients(rds=FakeRds(instances=[AURORA_MEMBER_INSTANCE]))
        s = list_sources(c)['sources'][0]
        self.assertTrue(s['aurora_member'])
        self.assertEqual(s['cluster_identifier'], 'ecdl-aurora-mysql')
        self.assertTrue(any('ecdl-aurora-mysql' in n for n in s['blockers']))
        # Review finding M7: resolving refuses it, so it is not offered as usable.
        self.assertFalse(s['usable'])

    def test_a_read_replica_is_not_usable(self):
        replica = dict(STANDALONE_MYSQL, ReadReplicaSourceDBInstanceIdentifier='primary-db')
        s = list_sources(Clients(rds=FakeRds(instances=[replica])))['sources'][0]
        self.assertFalse(s['usable'])
        self.assertTrue(any('read replica of primary-db' in b for b in s['blockers']))

    def test_standalone_instance_is_not_flagged_as_a_member(self):
        c = Clients(rds=FakeRds(instances=[STANDALONE_MYSQL]))
        s = list_sources(c)['sources'][0]
        self.assertFalse(s['aurora_member'])
        self.assertEqual(s.get('notes'), [])

    def test_unsupported_engine_is_listed_not_hidden(self):
        mongo = dict(AURORA_CLUSTER, Engine='docdb',
                     DBClusterIdentifier='docs',
                     DBClusterArn='arn:aws:rds:us-east-2:111122223333:cluster:docs')
        c = Clients(rds=FakeRds(clusters=[mongo]))
        out = list_sources(c)
        self.assertEqual(out['count'], 1)
        s = out['sources'][0]
        self.assertFalse(s['usable'])
        self.assertTrue(any('docdb' in b for b in s['blockers']))
        self.assertIsNone(s['engine'])

    def test_serverless_v1_is_blocked_with_the_reason(self):
        v1 = dict(AURORA_CLUSTER, EngineMode='serverless')
        c = Clients(rds=FakeRds(clusters=[v1]))
        s = list_sources(c)['sources'][0]
        self.assertFalse(s['usable'])
        self.assertTrue(any('replication log' in b for b in s['blockers']))
        self.assertTrue(any('Serverless v2' in b for b in s['blockers']))

    def test_serverless_v2_is_usable(self):
        # v2 carries ServerlessV2ScalingConfiguration with EngineMode provisioned.
        c = Clients(rds=FakeRds(clusters=[AURORA_CLUSTER]))
        s = list_sources(c)['sources'][0]
        self.assertTrue(s['serverless_v2'])
        self.assertTrue(s['usable'])

    def test_creating_cluster_is_blocked(self):
        creating = dict(AURORA_CLUSTER, Status='creating')
        c = Clients(rds=FakeRds(clusters=[creating]))
        s = list_sources(c)['sources'][0]
        self.assertFalse(s['usable'])
        self.assertTrue(any('creating' in b for b in s['blockers']))

    def test_usable_entries_sort_above_unusable(self):
        bad = dict(AURORA_CLUSTER, Status='creating',
                   DBClusterIdentifier='aaa-creating',
                   DBClusterArn='arn:aws:rds:us-east-2:111122223333:cluster:aaa')
        c = Clients(rds=FakeRds(clusters=[bad, AURORA_CLUSTER]))
        out = list_sources(c)
        self.assertTrue(out['sources'][0]['usable'])
        self.assertFalse(out['sources'][1]['usable'])

    def test_clusters_sort_above_instances(self):
        c = Clients(rds=FakeRds(clusters=[AURORA_CLUSTER],
                                instances=[AURORA_MEMBER_INSTANCE]))
        out = list_sources(c)
        self.assertEqual(out['sources'][0]['kind'], 'cluster')

    def test_contains_filters_by_identifier(self):
        c = Clients(rds=FakeRds(clusters=[AURORA_CLUSTER],
                                instances=[STANDALONE_MYSQL]))
        out = list_sources(c, contains='plain')
        self.assertEqual([s['identifier'] for s in out['sources']],
                         ['plain-mysql'])

    def test_contains_matches_engine_label(self):
        c = Clients(rds=FakeRds(clusters=[AURORA_CLUSTER]))
        self.assertEqual(list_sources(c, contains='mysql')['count'], 1)

    def test_limit_reports_truncation(self):
        many = []
        for i in range(5):
            many.append(dict(AURORA_CLUSTER, DBClusterIdentifier='c{}'.format(i),
                             DBClusterArn='arn:aws:rds:us-east-2:1:cluster:c{}'.format(i)))
        c = Clients(rds=FakeRds(clusters=many))
        out = list_sources(c, limit=2)
        self.assertEqual(out['count'], 2)
        self.assertEqual(out['total'], 5)
        self.assertTrue(out['truncated'])

    def test_denied_listing_is_reported_not_raised(self):
        c = Clients(rds=FailingRds())
        out = list_sources(c)
        self.assertEqual(out['sources'], [])
        self.assertTrue(out['problems'])
        self.assertIn('not permitted', out['problems'][0]['error'])

    def test_marker_pagination_is_followed(self):
        # RDS pages with Marker in and Marker out; confirmed from the service
        # model, where input_token and output_token are both 'Marker'.
        page_two = dict(AURORA_CLUSTER, DBClusterIdentifier='second',
                        DBClusterArn='arn:aws:rds:us-east-2:1:cluster:second')

        def pages(kwargs):
            if not kwargs.get('Marker'):
                return {'DBClusters': [AURORA_CLUSTER], 'Marker': 'more'}
            return {'DBClusters': [page_two]}

        rds = FakeRds(pages={'clusters': pages})
        out = list_sources(Clients(rds=rds))
        self.assertEqual(out['total'], 2)
        self.assertIn('second', [s['identifier'] for s in out['sources']])
        self.assertEqual(rds.cluster_calls[1]['Marker'], 'more')


# ── targets ────────────────────────────────────────────────────────────────

class TargetTest(unittest.TestCase):

    def test_cluster_mode_uses_the_configuration_endpoint(self):
        c = Clients(elasticache=FakeElastiCache([CLUSTER_MODE_TARGET]))
        t = list_targets(c)['targets'][0]
        self.assertTrue(t['cluster_mode'])
        self.assertTrue(t['endpoint'].startswith('clustercfg.'))
        self.assertEqual(t['shards'], 3)
        self.assertTrue(t['usable'])

    def test_single_node_uses_the_primary_endpoint(self):
        c = Clients(elasticache=FakeElastiCache([SINGLE_NODE_TARGET]))
        t = list_targets(c)['targets'][0]
        self.assertFalse(t['cluster_mode'])
        self.assertEqual(t['endpoint'], 'master.broker.use2.cache.amazonaws.com')
        self.assertEqual(t['shards'], 1)

    def test_absent_engine_version_and_user_groups_are_tolerated(self):
        # The live API omits both on a Valkey group.
        c = Clients(elasticache=FakeElastiCache([CLUSTER_MODE_TARGET]))
        t = list_targets(c)['targets'][0]
        self.assertIsNone(t['engine_version'])
        self.assertEqual(t['auth_mode'], 'none')
        self.assertFalse(t['needs_secret'])

    def test_auth_token_requires_a_secret(self):
        g = dict(CLUSTER_MODE_TARGET, AuthTokenEnabled=True)
        t = list_targets(Clients(elasticache=FakeElastiCache([g])))['targets'][0]
        self.assertEqual(t['auth_mode'], 'token')
        self.assertTrue(t['needs_secret'])
        self.assertTrue(any('target.secret_arn' in n for n in t['notes']))

    def test_rbac_wins_over_token(self):
        # RBAC supersedes AUTH on Redis/Valkey 6 and later.
        g = dict(CLUSTER_MODE_TARGET, AuthTokenEnabled=True,
                 UserGroupIds=['app-users'])
        t = list_targets(Clients(elasticache=FakeElastiCache([g])))['targets'][0]
        self.assertEqual(t['auth_mode'], 'rbac')
        self.assertTrue(t['needs_secret'])

    def test_plaintext_target_is_noted(self):
        t = list_targets(
            Clients(elasticache=FakeElastiCache([SINGLE_NODE_TARGET])))['targets'][0]
        self.assertFalse(t['tls'])
        self.assertTrue(any('unencrypted' in n for n in t['notes']))

    def test_creating_target_is_blocked(self):
        g = dict(CLUSTER_MODE_TARGET, Status='creating')
        t = list_targets(Clients(elasticache=FakeElastiCache([g])))['targets'][0]
        self.assertFalse(t['usable'])

    def test_target_with_no_endpoint_yet_is_blocked(self):
        g = dict(CLUSTER_MODE_TARGET)
        g.pop('ConfigurationEndpoint')
        t = list_targets(Clients(elasticache=FakeElastiCache([g])))['targets'][0]
        self.assertFalse(t['usable'])
        self.assertTrue(any('configuration endpoint' in b for b in t['blockers']))

    # ── ECDL's own brokers are not targets ──────────────────────────────────
    # These were being offered as selectable targets in the wizard. Picking one
    # would write cached rows into the broker keyspace that holds another
    # pipeline's change stream, Debezium offsets and schema history -- corrupting
    # a working link rather than failing cleanly.

    def test_broker_named_like_one_is_not_offered_as_a_target(self):
        g = dict(CLUSTER_MODE_TARGET, ReplicationGroupId='ecdl-uitest-broker',
                 Description='ECDL CDC broker (single shard)')
        t = list_targets(Clients(elasticache=FakeElastiCache([g])))['targets'][0]
        self.assertFalse(t['usable'])
        self.assertTrue(any('ECDL broker' in b for b in t['blockers']),
                        t['blockers'])

    def test_broker_is_detected_by_description_when_the_name_does_not_say_so(self):
        # The deployed demo broker is exactly this shape: it ends '-cache', so a
        # name-suffix test alone would have let it through.
        g = dict(CLUSTER_MODE_TARGET,
                 ReplicationGroupId='ecdl-demo-broker-cache',
                 Description='ECDL CDC broker (single shard, cluster-mode disabled)')
        t = list_targets(Clients(elasticache=FakeElastiCache([g])))['targets'][0]
        self.assertFalse(t['usable'])

    def test_broker_is_detected_by_name_when_it_has_no_description(self):
        g = dict(CLUSTER_MODE_TARGET,
                 ReplicationGroupId='ecdl-shop-broker', Description='')
        t = list_targets(Clients(elasticache=FakeElastiCache([g])))['targets'][0]
        self.assertFalse(t['usable'])

    def test_a_broker_is_listed_not_hidden(self):
        # This module's contract is that nothing usable is hidden: an unusable
        # entry is returned carrying its reason so the form can disable it and
        # say why, rather than the operator wondering where a cache went.
        g = dict(CLUSTER_MODE_TARGET, ReplicationGroupId='ecdl-uitest-broker',
                 Description='ECDL CDC broker')
        out = list_targets(Clients(elasticache=FakeElastiCache([g])))
        self.assertEqual(out['total'], 1)
        self.assertEqual(len(out['targets']), 1)

    def test_a_real_target_is_still_usable(self):
        g = dict(CLUSTER_MODE_TARGET, ReplicationGroupId='ecdl-uitest-target',
                 Description='ECDL test target')
        t = list_targets(Clients(elasticache=FakeElastiCache([g])))['targets'][0]
        self.assertTrue(t['usable'], t['blockers'])

    def test_a_customer_cache_mentioning_broker_is_not_mistaken_for_ours(self):
        # 'broker' in the middle of a name, and no ECDL description: someone
        # else's message broker replacement is a valid target.
        g = dict(CLUSTER_MODE_TARGET,
                 ReplicationGroupId='my-message-broker-cache',
                 Description='kafka replacement')
        t = list_targets(Clients(elasticache=FakeElastiCache([g])))['targets'][0]
        self.assertTrue(t['usable'], t['blockers'])

    def test_brokers_sort_below_usable_targets(self):
        broker = dict(CLUSTER_MODE_TARGET, ReplicationGroupId='aaa-ecdl-x',
                      Description='ECDL CDC broker')
        # Named to sort first alphabetically, so only the usable-first ordering
        # can put the real target above it.
        real = dict(CLUSTER_MODE_TARGET, ReplicationGroupId='zzz-real-target',
                    Description='a real cache')
        out = list_targets(Clients(elasticache=FakeElastiCache([broker, real])))
        self.assertEqual(out['targets'][0]['identifier'], 'zzz-real-target')


# ── credentials ────────────────────────────────────────────────────────────

class SecretTest(unittest.TestCase):

    def test_lists_name_and_arn(self):
        c = Clients(secretsmanager=FakeSecrets([PLAIN_SECRET]))
        out = list_secrets(c)
        self.assertEqual(out['count'], 1)
        self.assertEqual(out['secrets'][0]['name'], 'ecdl/aurora-db-password')

    def test_never_reads_a_secret_value(self):
        # The rule the design depends on: the control plane passes an ARN to
        # ECS, which reads the value at task start. Nothing here needs to see it.
        sm = FakeSecrets([PLAIN_SECRET, MANAGED_SECRET])
        c = Clients(secretsmanager=sm)
        list_secrets(c)
        discover(c)
        self.assertEqual(sm.value_reads, 0)

    def test_rds_managed_secrets_are_marked(self):
        c = Clients(secretsmanager=FakeSecrets([MANAGED_SECRET]))
        self.assertTrue(list_secrets(c)['secrets'][0]['rds_managed'])
        c2 = Clients(secretsmanager=FakeSecrets([PLAIN_SECRET]))
        self.assertFalse(list_secrets(c2)['secrets'][0]['rds_managed'])

    def test_absent_optional_fields_are_tolerated(self):
        c = Clients(secretsmanager=FakeSecrets([PLAIN_SECRET]))
        s = list_secrets(c)['secrets'][0]
        self.assertIsNone(s['description'])
        self.assertFalse(s['rotation_enabled'])
        self.assertFalse(s['deleted_pending'])

    def test_pending_deletion_sorts_last(self):
        doomed = dict(PLAIN_SECRET, Name='aaa-doomed', DeletedDate='soon',
                      ARN='arn:aws:secretsmanager:us-east-2:1:secret:aaa')
        c = Clients(secretsmanager=FakeSecrets([doomed, PLAIN_SECRET]))
        out = list_secrets(c)
        self.assertFalse(out['secrets'][0]['deleted_pending'])
        self.assertTrue(out['secrets'][1]['deleted_pending'])

    def test_nexttoken_pagination_is_followed(self):
        second = dict(PLAIN_SECRET, Name='second',
                      ARN='arn:aws:secretsmanager:us-east-2:1:secret:second')
        seen = []

        def pages(kwargs):
            seen.append(kwargs)
            if not kwargs.get('NextToken'):
                return {'SecretList': [PLAIN_SECRET], 'NextToken': 'more'}
            return {'SecretList': [second]}

        c = Clients(secretsmanager=FakeSecrets(pages=pages))
        out = list_secrets(c)
        self.assertEqual(out['total'], 2)
        self.assertEqual(seen[1]['NextToken'], 'more')


class RankingTest(unittest.TestCase):

    def test_managed_secret_ranks_first(self):
        source = {'identifier': 'plain-mysql', 'username': 'root',
                  'master_user_secret_arn': MANAGED_SECRET['ARN']}
        secrets = [{'arn': PLAIN_SECRET['ARN'], 'name': 'ecdl/aurora-db-password'},
                   {'arn': MANAGED_SECRET['ARN'], 'name': 'rds!db-abc-xyz'}]
        ranked = secrets_for_source(secrets, source)
        self.assertEqual(ranked[0]['arn'], MANAGED_SECRET['ARN'])
        self.assertTrue(ranked[0]['likely'])
        self.assertIn('RDS manages', ranked[0]['match_reason'])

    def test_declared_beats_a_better_looking_name(self):
        # A secret named exactly after the database still ranks below the one RDS
        # says holds the password.
        source = {'identifier': 'shopdb', 'username': 'admin',
                  'master_user_secret_arn': 'arn:managed'}  # nosec B105 - fake test credential
        ranked = secrets_for_source(
            [{'arn': 'arn:managed', 'name': 'rds!db-zzz'},
             {'arn': 'arn:looks-right', 'name': 'shopdb-password'}], source)
        self.assertEqual(ranked[0]['arn'], 'arn:managed')
        self.assertFalse(ranked[1]['likely'])

    def test_cloudformation_identifier_matches_on_shared_words(self):
        # The case that broke substring matching against real data: neither name
        # contains the other, but they share 'ecdl' and 'demo'.
        source = {'identifier': 'ecdl-demo-source-dbcluster-xxxxxxxxxxxx',
                  'username': 'admin'}
        secrets = [
            {'arn': 'a', 'name': 'ecdl-mssql-bench-password'},
            {'arn': 'b', 'name': 'ecdl-demo-db-password'},
            {'arn': 'c', 'name': 'ecdl/aurora-db-password'},
            {'arn': 'd', 'name': 'BrokerAuthSecret-0Yr21HJe3u51'},
        ]
        ranked = secrets_for_source(secrets, source)
        self.assertEqual(ranked[0]['arn'], 'b')
        self.assertTrue(ranked[0]['likely'])
        self.assertIn('demo', ranked[0]['match_reason'])
        # And only the strongest match is badged, or the badge means nothing.
        self.assertEqual([s['arn'] for s in ranked if s['likely']], ['b'])

    def test_more_shared_words_wins(self):
        source = {'identifier': 'shop-orders-eu'}
        ranked = secrets_for_source(
            [{'arn': 'one', 'name': 'shop-secret'},
             {'arn': 'two', 'name': 'shop-orders-eu-secret'}], source)
        self.assertEqual(ranked[0]['arn'], 'two')

    def test_weak_words_alone_do_not_count_as_a_match(self):
        # 'db' and 'password' appear in nearly every secret name, so matching on
        # them would badge everything.
        source = {'identifier': 'db-cluster-prod'}
        ranked = secrets_for_source(
            [{'arn': 'a', 'name': 'unrelated-db-password'}], source)
        self.assertFalse(ranked[0]['likely'])
        self.assertIsNone(ranked[0]['match_reason'])

    def test_username_match_is_a_weaker_signal_than_name_overlap(self):
        source = {'identifier': 'shopdb', 'username': 'ordersvc'}
        ranked = secrets_for_source(
            [{'arn': 'byuser', 'name': 'ordersvc-credentials'},
             {'arn': 'byname', 'name': 'shopdb-credentials'}], source)
        self.assertEqual(ranked[0]['arn'], 'byname')

    def test_ranking_tolerates_an_empty_source(self):
        ranked = secrets_for_source([{'arn': 'a', 'name': 'x'}], {})
        self.assertEqual(len(ranked), 1)
        self.assertFalse(ranked[0]['likely'])

    def test_ranking_does_not_mutate_the_input(self):
        # The API layer reuses the same list for other purposes; a ranking call
        # that wrote its result into it would leak between requests.
        secrets = [{'arn': 'a', 'name': 'x'}]
        secrets_for_source(secrets, {'identifier': 'x'})
        self.assertEqual(secrets, [{'arn': 'a', 'name': 'x'}])

    def test_ranking_an_empty_list_is_safe(self):
        self.assertEqual(secrets_for_source([], {'identifier': 'x'}), [])


# ── the combined call ──────────────────────────────────────────────────────

class DiscoverTest(unittest.TestCase):

    def test_returns_all_three_lists(self):
        c = Clients(rds=FakeRds(clusters=[AURORA_CLUSTER]),
                    elasticache=FakeElastiCache([CLUSTER_MODE_TARGET]),
                    secretsmanager=FakeSecrets([PLAIN_SECRET]))
        out = discover(c)
        self.assertEqual(len(out['sources']), 1)
        self.assertEqual(len(out['targets']), 1)
        self.assertEqual(len(out['secrets']), 1)
        self.assertEqual(out['counts'],
                         {'sources': 1, 'targets': 1, 'secrets': 1})
        self.assertEqual(out['problems'], [])

    def test_one_denied_list_does_not_empty_the_others(self):
        # Discovery reads four kinds of resource, and access to any of them can be denied.
        # Losing secrets should not stop someone choosing a source.
        c = Clients(rds=FailingRds(),
                    elasticache=FakeElastiCache([CLUSTER_MODE_TARGET]),
                    secretsmanager=FakeSecrets([PLAIN_SECRET]))
        out = discover(c)
        self.assertEqual(out['sources'], [])
        self.assertEqual(len(out['targets']), 1)
        self.assertEqual(len(out['secrets']), 1)
        self.assertEqual(len(out['problems']), 2)   # clusters and instances

    def test_contains_does_not_filter_credentials(self):
        # The filter exists to find a database by name; the secret that grants access to it
        # rarely shares that name, so filtering it out would hide the answer.
        c = Clients(rds=FakeRds(clusters=[AURORA_CLUSTER]),
                    secretsmanager=FakeSecrets([PLAIN_SECRET]))
        out = discover(c, contains='aurora-mysql')
        self.assertEqual(len(out['sources']), 1)
        self.assertEqual(len(out['secrets']), 1)


class RegionTest(unittest.TestCase):

    def test_reads_the_region_from_an_arn(self):
        self.assertEqual(region_of(AURORA_CLUSTER['DBClusterArn']), 'us-east-2')

    def test_returns_none_for_invalid_input(self):
        self.assertIsNone(region_of('not-an-arn'))
        self.assertIsNone(region_of(None))



class ControlPlaneSecurityGroupTest(unittest.TestCase):
    """How the broker learns which security group to admit.

    Explicit configuration takes precedence because it always works and can be reviewed. IMDS
    is the fallback so the common case needs no configuration -- asking an
    operator to look up their own security group id is a step that gets skipped,
    and skipping it produces a pipeline that runs and cannot be monitored.
    """

    def setUp(self):
        discover_mod.reset_control_plane_security_group_cache()

    def tearDown(self):
        discover_mod.reset_control_plane_security_group_cache()

    def test_the_environment_variable_is_used_when_set(self):
        got = discover_mod.control_plane_security_group(
            env={'ECDL_MONITOR_SECURITY_GROUP_ID': 'sg-explicit'})
        self.assertEqual(got, 'sg-explicit')

    def test_whitespace_only_is_treated_as_unset(self):
        calls = []
        saved = discover_mod._imds_security_group_ids
        discover_mod._imds_security_group_ids = lambda *a, **k: calls.append(1) or []
        try:
            got = discover_mod.control_plane_security_group(
                env={'ECDL_MONITOR_SECURITY_GROUP_ID': '   '})
        finally:
            discover_mod._imds_security_group_ids = saved
        self.assertEqual(got, '')
        self.assertEqual(len(calls), 1, 'blank should fall through to IMDS')

    def test_falls_back_to_imds_and_is_deterministic(self):
        saved = discover_mod._imds_security_group_ids
        discover_mod._imds_security_group_ids = lambda *a, **k: ['sg-b', 'sg-a']
        try:
            got = discover_mod.control_plane_security_group(env={})
        finally:
            discover_mod._imds_security_group_ids = saved
        # Any of an interface's groups grants access; sorted so the choice is
        # reviewable rather than dependent on IMDS ordering.
        self.assertEqual(got, 'sg-a')

    def test_returns_empty_when_undeterminable_rather_than_raising(self):
        # A link that deploys with reduced observability is better than a link that will
        # not deploy; plan() attaches a warning saying what will be missing.
        saved = discover_mod._imds_security_group_ids
        discover_mod._imds_security_group_ids = lambda *a, **k: []
        try:
            got = discover_mod.control_plane_security_group(env={})
        finally:
            discover_mod._imds_security_group_ids = saved
        self.assertEqual(got, '')

    def test_the_lookup_is_cached(self):
        calls = []
        saved = discover_mod._imds_security_group_ids

        def counted(*a, **k):
            calls.append(1)
            return ['sg-once']

        discover_mod._imds_security_group_ids = counted
        try:
            first = discover_mod.control_plane_security_group(env={})
            second = discover_mod.control_plane_security_group(env={})
        finally:
            discover_mod._imds_security_group_ids = saved
        self.assertEqual((first, second), ('sg-once', 'sg-once'))
        self.assertEqual(len(calls), 1, 'IMDS should be consulted once per process')

    def test_imds_failure_is_silent(self):
        # Not being on EC2 is a normal outcome on the deploy path, not an error.
        # The stub used to be defined and never installed, so on an EC2 host
        # (a CI runner) the test reached the real IMDS and got its groups.
        import http.client

        def boom(*a, **k):
            raise OSError('no route to host')

        saved = http.client.HTTPConnection
        http.client.HTTPConnection = boom
        try:
            self.assertEqual(discover_mod._imds_security_group_ids(timeout=0.001), [])
        finally:
            http.client.HTTPConnection = saved


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=0).result
    print('ok: {} discovery tests passed'.format(result.testsRun)
          if result.wasSuccessful() else 'FAILED')
    raise SystemExit(0 if result.wasSuccessful() else 1)
