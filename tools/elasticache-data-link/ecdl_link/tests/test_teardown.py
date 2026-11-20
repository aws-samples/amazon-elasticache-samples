"""Tests for tearing a link down.

The behaviour that matters is all about what is and is NOT deleted: a link's own
three stacks go, in the right order; a stack that is already gone is skipped, not
an error; a stack name that does not belong to the link is refused; the per-link
ECS cluster is removed only when empty; and the shared source and target are
never named at all. None of this needs an AWS account.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.deploy import DeployError  # noqa: E402
from ecdl_link.teardown import (  # noqa: E402
    Destroyer, describe_destroy_plan, destroy_plan)


class FakeCloudFormation(object):
    """Enough CloudFormation to delete stacks and watch them disappear.

    A deleted stack vanishes after `delete_latency` describe calls, so the
    wait loop is exercised without real time passing. A stack listed in
    `stuck` reports DELETE_FAILED instead of going away.
    """

    def __init__(self, existing=None, delete_latency=0, stuck=None, params=None):
        self.existing = dict(existing or {})     # name -> status
        self.delete_latency = delete_latency
        self.stuck = set(stuck or [])
        self.params = dict(params or {})         # name -> {ParameterKey: value}
        self.deleted = []
        self._countdown = {}

    def describe_stacks(self, StackName=None, **_):
        if StackName in self.stuck and StackName in self.deleted:
            return {'Stacks': [{'StackStatus': 'DELETE_FAILED', 'Outputs': []}]}
        if StackName in self._countdown:
            if self._countdown[StackName] <= 0:
                self.existing.pop(StackName, None)
                self._countdown.pop(StackName, None)
            else:
                self._countdown[StackName] -= 1
        if StackName not in self.existing:
            raise Exception('Stack with id {} does not exist'.format(StackName))
        return {'Stacks': [{
            'StackStatus': self.existing[StackName],
            'Outputs': [],
            'Parameters': [{'ParameterKey': k, 'ParameterValue': v}
                           for k, v in (self.params.get(StackName) or {}).items()],
        }]}

    def delete_stack(self, StackName=None, **kw):
        self.deleted.append(StackName)
        self.delete_kwargs = getattr(self, 'delete_kwargs', []) + [kw]
        if StackName in self.stuck:
            self.existing[StackName] = 'DELETE_IN_PROGRESS'
            return {}
        self._countdown[StackName] = self.delete_latency
        if self.delete_latency == 0:
            self.existing.pop(StackName, None)
        return {}


class FakeECS(object):
    def __init__(self, clusters=None):
        # name -> {activeServicesCount, runningTasksCount, pendingTasksCount, status}
        self.clusters = dict(clusters or {})
        self.deleted_clusters = []

    def describe_clusters(self, clusters=None, **_):
        out = []
        for name in clusters or []:
            info = self.clusters.get(name)
            if info is not None:
                out.append(dict(info, clusterName=name,
                                status=info.get('status', 'ACTIVE')))
        return {'clusters': out}

    def delete_cluster(self, cluster=None, **_):
        self.deleted_clusters.append(cluster)
        self.clusters.pop(cluster, None)
        return {}
    def list_tasks(self, **_):
        # No load running (Jira story F136 checks before deleting anything).
        return {'taskArns': []}


class FakeElastiCache(object):
    """DescribeSnapshots over a fixed list; `error` makes it fail."""
    def __init__(self, snapshots=None, error=None, appear_after=0):
        self.snapshots = list(snapshots or [])
        self.error = error
        self.calls = []
        self.appear_after = appear_after   # empty answers before the snapshot is listed
    def describe_snapshots(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise Exception(self.error)
        # As the real API: a ReplicationGroupId filter only matches snapshots
        # recorded against the group, never a cluster-mode-disabled node's.
        want = kw.get('ReplicationGroupId')
        snaps = [s for s in self.snapshots if not want or s.get('ReplicationGroupId') == want]
        if self.appear_after:
            self.appear_after -= 1
            return {'Snapshots': []}
        return {'Snapshots': snaps}
def _node_snapshot(name, when, node='ecdl-shop-broker-001'):
    """The shape a cluster-mode-disabled broker's snapshot really has (seen
    live): recorded against the node, ReplicationGroupId empty."""
    snap = _snapshot(name, when)
    snap['ReplicationGroupId'] = None
    snap['CacheClusterId'] = node
    return snap


def _snapshot(name, when, group='ecdl-shop-broker'):
    import datetime
    node = {'CacheNodeId': '0001'}
    if when is not None:
        node['SnapshotCreateTime'] = datetime.datetime(2025, 1, when, tzinfo=datetime.timezone.utc)
    return {'SnapshotName': name, 'ReplicationGroupId': group, 'SnapshotSource': 'manual',
            'SnapshotStatus': 'available', 'NodeSnapshots': [node]}
class _Clients(object):
    def __init__(self, cfn, ecs, elasticache=None):
        self.cloudformation = cfn
        self.ecs = ecs
        self._elasticache = elasticache
    def elasticache(self):
        if self._elasticache is None:
            raise Exception('no elasticache client in this test')
        return self._elasticache


def quiet(_event):
    pass


class DestroyPlanTest(unittest.TestCase):
    def test_names_the_three_owned_stacks_in_delete_order(self):
        plan = destroy_plan('shop')
        self.assertEqual([s['name'] for s in plan['stacks']],
                         ['ecdl-shop-cdc', 'ecdl-shop-loader', 'ecdl-shop-broker'])

    def test_honours_a_custom_prefix(self):
        plan = destroy_plan('shop', prefix='team1')
        self.assertEqual([s['name'] for s in plan['stacks']],
                         ['team1-shop-cdc', 'team1-shop-loader', 'team1-shop-broker'])
        self.assertEqual(plan['cluster_name'], 'team1-shop')

    def test_source_and_target_are_declared_kept_and_never_named(self):
        plan = destroy_plan('shop')
        joined = ' '.join(s['name'] for s in plan['stacks'])
        self.assertNotIn('source', joined)
        self.assertNotIn('target', joined)
        self.assertEqual(len(plan['keeps']), 2)

    def test_follow_up_names_the_signal_table_and_broker_keys(self):
        plan = destroy_plan('shop')
        self.assertEqual(plan['follow_up']['signal_table'], 'ecdl_signal_shop')
        self.assertEqual(plan['follow_up']['broker_offset_key'], 'ecdl:shop:offsets')
        self.assertEqual(plan['follow_up']['broker_schema_history_key'],
                         'ecdl:shop:schema_history')

    def test_an_empty_name_is_rejected(self):
        with self.assertRaises(ValueError):
            destroy_plan('   ')

    def test_describe_mentions_order_and_the_kept_resources(self):
        text = describe_destroy_plan(destroy_plan('shop'))
        self.assertIn('ecdl-shop-cdc', text)
        self.assertIn('ecdl-shop-broker', text)
        self.assertIn('KEPT', text)


class DestroyRunTest(unittest.TestCase):
    def _run(self, cfn, ecs=None, **kw):
        ecs = ecs or FakeECS()
        d = Destroyer(_Clients(cfn, ecs), on_progress=quiet,
                      poll_seconds=0, **kw)
        return d.run(destroy_plan('shop')), cfn, ecs

    def test_deletes_every_present_stack_in_order(self):
        cfn = FakeCloudFormation(existing={
            'ecdl-shop-cdc': 'CREATE_COMPLETE',
            'ecdl-shop-loader': 'CREATE_COMPLETE',
            'ecdl-shop-broker': 'CREATE_COMPLETE',
        })
        result, cfn, _ = self._run(cfn)
        self.assertEqual(cfn.deleted,
                         ['ecdl-shop-cdc', 'ecdl-shop-loader', 'ecdl-shop-broker'])
        self.assertEqual(result['deleted'],
                         ['ecdl-shop-cdc', 'ecdl-shop-loader', 'ecdl-shop-broker'])
        self.assertEqual(result['skipped'], [])

    def test_every_delete_goes_through_the_deploy_role(self):
        # D1: a stack created with the service role is deleted with it.
        role = 'arn:aws:iam::111122223333:role/ECDLDeploy-cp-us-east-2'
        cfn = FakeCloudFormation(existing={'ecdl-shop-cdc': 'CREATE_COMPLETE',
                                           'ecdl-shop-broker': 'CREATE_COMPLETE'})
        self._run(cfn, role_arn=role)
        self.assertEqual(cfn.delete_kwargs, [{'RoleARN': role}, {'RoleARN': role}])

    def test_without_a_role_the_caller_deletes(self):
        cfn = FakeCloudFormation(existing={'ecdl-shop-cdc': 'CREATE_COMPLETE'})
        self._run(cfn)
        self.assertEqual(cfn.delete_kwargs, [{}])

    def test_a_missing_stack_is_skipped_not_an_error(self):
        # A cdc-only link has no loader stack; that absence is normal.
        cfn = FakeCloudFormation(existing={
            'ecdl-shop-cdc': 'CREATE_COMPLETE',
            'ecdl-shop-broker': 'CREATE_COMPLETE',
        })
        result, cfn, _ = self._run(cfn)
        self.assertEqual(cfn.deleted, ['ecdl-shop-cdc', 'ecdl-shop-broker'])
        self.assertEqual(result['skipped'], ['ecdl-shop-loader'])

    def test_nothing_deployed_is_a_clean_no_op(self):
        result, cfn, _ = self._run(FakeCloudFormation(existing={}))
        self.assertEqual(cfn.deleted, [])
        self.assertEqual(result['deleted'], [])
        self.assertEqual(len(result['skipped']), 3)

    def test_waits_for_a_slow_delete(self):
        cfn = FakeCloudFormation(existing={'ecdl-shop-broker': 'CREATE_COMPLETE'},
                                 delete_latency=3)
        result, _, _ = self._run(cfn)
        self.assertEqual(result['deleted'], ['ecdl-shop-broker'])

    def test_delete_failed_raises_with_a_hint(self):
        cfn = FakeCloudFormation(existing={'ecdl-shop-cdc': 'CREATE_COMPLETE'},
                                 stuck=['ecdl-shop-cdc'])
        with self.assertRaises(DeployError) as caught:
            self._run(cfn)
        self.assertIn('DELETE_FAILED', str(caught.exception))

    def test_refuses_a_stack_not_owned_by_the_link(self):
        cfn = FakeCloudFormation(existing={'ecdl-shop-cdc': 'CREATE_COMPLETE'})
        d = Destroyer(_Clients(cfn, FakeECS()), on_progress=quiet, poll_seconds=0)
        plan = destroy_plan('shop')
        # Include the source stack the way a hand-edited plan might.
        plan['stacks'].append({'id': 'source', 'name': 'ecdl-shop-source'})
        with self.assertRaises(DeployError) as caught:
            d.run(plan)
        self.assertIn('refusing to delete', str(caught.exception))
        self.assertNotIn('ecdl-shop-source', cfn.deleted)


class FinalSnapshotTest(unittest.TestCase):  # Jira story F022
    """The broker's DeletionPolicy is Snapshot: the teardown says so and names it."""
    def _run(self, elasticache, existing=None):
        events = []
        cfn = FakeCloudFormation(existing=existing if existing is not None else {
            'ecdl-shop-cdc': 'CREATE_COMPLETE', 'ecdl-shop-broker': 'CREATE_COMPLETE'})
        d = Destroyer(_Clients(cfn, FakeECS(), elasticache), on_progress=events.append,
                      poll_seconds=0, sleep=lambda _s: None)
        return d.run(destroy_plan('shop')), events
    def test_the_plan_says_a_final_snapshot_is_kept(self):
        plan = destroy_plan('shop')
        self.assertEqual(plan['follow_up']['broker_replication_group'], 'ecdl-shop-broker')
        self.assertIn('--replication-group-id ecdl-shop-broker',
                      plan['follow_up']['broker_snapshot_find_command'])
        text = describe_destroy_plan(plan)
        self.assertIn('FINAL SNAPSHOT', text)
        self.assertIn('charges for it', text)
        self.assertIn('restore_snapshot', text)
    def test_the_group_name_is_the_one_the_planner_gives_the_broker(self):
        from ecdl_link.provision import plan as provision_plan
        from ecdl_link.tests.test_provision import resolution
        r = resolution('cdc')
        broker = next(s for s in provision_plan(r)['steps'] if s['id'] == 'broker')
        self.assertEqual(destroy_plan(r['name'])['follow_up']['broker_replication_group'],
                         broker['parameters']['ClusterName'])
    def test_names_the_snapshot_left_by_the_delete(self):
        ec = FakeElastiCache([_snapshot('ecdl-shop-older-by-hand', 1),
                              _snapshot('ecdl-shop-broker-final-x1', 9),
                              _snapshot('ecdl-other-broker-final', 20, group='ecdl-other-broker')])
        result, events = self._run(ec)
        fu = result['follow_up']
        self.assertEqual(fu['broker_final_snapshot'], 'ecdl-shop-broker-final-x1')
        self.assertEqual(fu['broker_snapshot_delete_command'],
                         'aws elasticache delete-snapshot --snapshot-name ecdl-shop-broker-final-x1')
        self.assertEqual(fu['broker_other_snapshots'], ['ecdl-shop-older-by-hand'])
        # Manual snapshots only, matched by group or node, read after the
        # broker stack is gone.
        self.assertEqual(ec.calls, [{'SnapshotSource': 'user'}])
        kept = [e for e in events if e['event'] == 'broker_snapshot_kept']
        self.assertEqual(kept[0]['snapshot'], 'ecdl-shop-broker-final-x1')
        order = [e['event'] for e in events]
        self.assertLess(order.index('stack_deleted'), order.index('broker_snapshot_kept'))
    def test_a_node_recorded_snapshot_is_found(self):
        # Live: the final snapshot of a cluster-mode-disabled broker is recorded
        # against <group>-001, so a ReplicationGroupId filter returned nothing.
        ec = FakeElastiCache([_node_snapshot('ecdl-shop-broker-snapshot-broker-z6r4', 9),
                              _node_snapshot('ecdl-shop-broker-x-broker-snap', 10,
                                             node='ecdl-shop-broker-x-broker-001'),
                              _node_snapshot('ecdl-other-snap', 11, node='ecdl-other-broker-001')])
        result, _ = self._run(ec)
        fu = result['follow_up']
        self.assertEqual(fu['broker_final_snapshot'], 'ecdl-shop-broker-snapshot-broker-z6r4')
        self.assertEqual(fu['broker_other_snapshots'], [])

    def test_a_snapshot_listed_late_is_waited_for(self):
        ec = FakeElastiCache([_node_snapshot('ecdl-shop-broker-final', 9)], appear_after=3)
        result, _ = self._run(ec)
        self.assertEqual(result['follow_up']['broker_final_snapshot'], 'ecdl-shop-broker-final')
        self.assertEqual(len(ec.calls), 4)

    def test_a_snapshot_still_being_written_counts_as_newest(self):
        ec = FakeElastiCache([_snapshot('ecdl-shop-older', 3), _snapshot('ecdl-shop-final', None)])
        result, _ = self._run(ec)
        self.assertEqual(result['follow_up']['broker_final_snapshot'], 'ecdl-shop-final')
    def test_a_failed_read_is_reported_and_the_teardown_still_succeeds(self):
        result, events = self._run(FakeElastiCache(error='AccessDenied'))
        self.assertEqual(result['deleted'], ['ecdl-shop-cdc', 'ecdl-shop-broker'])
        self.assertIsNone(result['follow_up']['broker_final_snapshot'])
        unknown = [e for e in events if e['event'] == 'broker_snapshot_unknown']
        self.assertIn('AccessDenied', unknown[0]['error'])
        self.assertIn('describe-snapshots', unknown[0]['find'])
    def test_no_snapshot_found_is_reported(self):
        result, events = self._run(FakeElastiCache([]))
        self.assertIsNone(result['follow_up']['broker_final_snapshot'])
        self.assertIn('broker_snapshot_missing', [e['event'] for e in events])
    def test_no_lookup_when_the_broker_stack_was_already_gone(self):
        ec = FakeElastiCache([_snapshot('ecdl-shop-broker-final', 2)])
        result, _ = self._run(ec, existing={'ecdl-shop-cdc': 'CREATE_COMPLETE'})
        self.assertEqual(ec.calls, [])
        self.assertNotIn('broker_final_snapshot', result['follow_up'])
class ClusterCleanupTest(unittest.TestCase):
    def _teardown(self, ecs, delete_cluster=True):
        cfn = FakeCloudFormation(existing={'ecdl-shop-broker': 'CREATE_COMPLETE'})
        d = Destroyer(_Clients(cfn, ecs), on_progress=quiet, poll_seconds=0,
                      delete_cluster=delete_cluster)
        return d.run(destroy_plan('shop'))

    def test_empty_cluster_is_deleted(self):
        ecs = FakeECS(clusters={'ecdl-shop': {
            'activeServicesCount': 0, 'runningTasksCount': 0,
            'pendingTasksCount': 0}})
        result = self._teardown(ecs)
        self.assertTrue(result['cluster_deleted'])
        self.assertEqual(ecs.deleted_clusters, ['ecdl-shop'])

    def test_a_cluster_still_running_tasks_is_kept(self):
        ecs = FakeECS(clusters={'ecdl-shop': {
            'activeServicesCount': 1, 'runningTasksCount': 2,
            'pendingTasksCount': 0}})
        result = self._teardown(ecs)
        self.assertFalse(result['cluster_deleted'])
        self.assertEqual(ecs.deleted_clusters, [])

    def test_absent_cluster_is_a_no_op(self):
        result = self._teardown(FakeECS(clusters={}))
        self.assertFalse(result['cluster_deleted'])

    def test_keep_cluster_flag_leaves_an_empty_cluster_alone(self):
        ecs = FakeECS(clusters={'ecdl-shop': {
            'activeServicesCount': 0, 'runningTasksCount': 0,
            'pendingTasksCount': 0}})
        result = self._teardown(ecs, delete_cluster=False)
        self.assertFalse(result['cluster_deleted'])
        self.assertEqual(ecs.deleted_clusters, [])

    def test_cleans_the_cluster_the_cdc_stack_actually_used(self):
        # quickstart names the cluster ecdl-<prefix>-cluster, not the derived
        # ecdl-<link>. The real name lives in the cdc stack's ClusterName
        # parameter and must be read BEFORE that stack is deleted.
        cfn = FakeCloudFormation(
            existing={'ecdl-shop-cdc': 'CREATE_COMPLETE',
                      'ecdl-shop-broker': 'CREATE_COMPLETE'},
            params={'ecdl-shop-cdc': {'ClusterName': 'ecdl-shop-cluster'}})
        ecs = FakeECS(clusters={'ecdl-shop-cluster': {
            'activeServicesCount': 0, 'runningTasksCount': 0,
            'pendingTasksCount': 0}})
        d = Destroyer(_Clients(cfn, ecs), on_progress=quiet, poll_seconds=0)
        result = d.run(destroy_plan('shop'))
        self.assertTrue(result['cluster_deleted'])
        self.assertEqual(ecs.deleted_clusters, ['ecdl-shop-cluster'])

    def test_falls_back_to_the_derived_cluster_name(self):
        # No ClusterName parameter (e.g. cdc stack already gone) -> derived name.
        cfn = FakeCloudFormation(
            existing={'ecdl-shop-broker': 'CREATE_COMPLETE'})
        ecs = FakeECS(clusters={'ecdl-shop': {
            'activeServicesCount': 0, 'runningTasksCount': 0,
            'pendingTasksCount': 0}})
        d = Destroyer(_Clients(cfn, ecs), on_progress=quiet, poll_seconds=0)
        result = d.run(destroy_plan('shop'))
        self.assertTrue(result['cluster_deleted'])
        self.assertEqual(ecs.deleted_clusters, ['ecdl-shop'])


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=0).result
    print('ok: {} teardown tests passed'.format(result.testsRun)
          if result.wasSuccessful() else 'FAILED')
    raise SystemExit(0 if result.wasSuccessful() else 1)
