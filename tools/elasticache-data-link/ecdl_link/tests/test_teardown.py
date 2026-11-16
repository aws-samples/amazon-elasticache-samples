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

    def delete_stack(self, StackName=None, **_):
        self.deleted.append(StackName)
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


class _Clients(object):
    def __init__(self, cfn, ecs):
        self.cloudformation = cfn
        self.ecs = ecs


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
