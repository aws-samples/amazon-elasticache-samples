"""Jira story F022: the broker survives a delete or a replacement as a snapshot.

The broker is the only copy of Debezium's committed offset, the schema history,
the unapplied changes and the dead letters. These tests keep the snapshot
policy, the daily snapshots and the restore path in the template, the deploy
role able to take the final snapshot and restore only ECDL snapshots, and the
planner and change review handling RestoreSnapshotName as create-only.
"""
import copy
import fnmatch
import os
import re
import sys
import unittest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ecdl_link import problems_for  # noqa: E402
from ecdl_link.change import INVERSE, apply_settings, review_update  # noqa: E402
from ecdl_link.provision import plan  # noqa: E402
from ecdl_link.tests.policy_render import WORST_CASE, render  # noqa: E402
from ecdl_link.safe_yaml import load_cfn  # noqa: E402
from ecdl_link.tests.test_control_plane_iam import _template  # noqa: E402
from ecdl_link.tests.test_deploy import FakeCloudFormation, FakeECS, deployer  # noqa: E402
from ecdl_link.tests.test_link import minimal  # noqa: E402
from ecdl_link.tests.test_provision import resolution  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BROKER = os.path.join(ROOT, 'infra', 'ecdl-broker-quickstart.yaml')


def _broker_template():
    with open(BROKER) as handle:
        return load_cfn(handle)


def _minutes(hhmm):
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


class BrokerTemplateTest(unittest.TestCase):
    def setUp(self):
        self.t = _broker_template()
        self.broker = self.t['Resources']['Broker']
        self.params = self.t['Parameters']

    def test_delete_and_replace_take_a_snapshot(self):
        self.assertEqual(self.broker.get('DeletionPolicy'), 'Snapshot')
        self.assertEqual(self.broker.get('UpdateReplacePolicy'), 'Snapshot')

    def test_daily_snapshots_cannot_be_turned_off(self):
        limit = self.params['SnapshotRetentionLimit']
        self.assertGreaterEqual(limit['MinValue'], 1)
        self.assertTrue(1 <= limit['Default'] <= 7)
        props = self.broker['Properties']
        self.assertEqual(props['SnapshotRetentionLimit'], {'Ref': 'SnapshotRetentionLimit'})
        self.assertEqual(props['SnapshotWindow'], {'Ref': 'SnapshotWindow'})
        self.assertEqual(props['PreferredMaintenanceWindow'],
                         {'Ref': 'PreferredMaintenanceWindow'})

    def test_default_windows_do_not_overlap_and_snapshot_is_an_hour(self):
        snap = self.params['SnapshotWindow']['Default']
        maint = self.params['PreferredMaintenanceWindow']['Default']
        self.assertRegex(snap, self.params['SnapshotWindow']['AllowedPattern'])
        self.assertRegex(maint, self.params['PreferredMaintenanceWindow']['AllowedPattern'])
        s0, s1 = (_minutes(x) for x in snap.split('-'))
        self.assertGreaterEqual(s1 - s0, 60)
        m0, m1 = (_minutes(x.split(':', 1)[1]) for x in maint.split('-'))
        self.assertTrue(s1 <= m0 or m1 <= s0, (snap, maint))

    def test_restore_is_a_create_time_snapshot_name(self):
        self.assertEqual(self.params['RestoreSnapshotName']['Default'], '')
        self.assertEqual(self.broker['Properties']['SnapshotName'],
                         {'If': ['RestoreFromSnapshot', {'Ref': 'RestoreSnapshotName'},
                                 {'Ref': 'AWS::NoValue'}]})
        self.assertEqual(self.t['Conditions']['RestoreFromSnapshot'],
                         {'Not': [{'Equals': [{'Ref': 'RestoreSnapshotName'}, '']}]})

    def test_restore_pattern_admits_only_ecdl_snapshots(self):
        pattern = self.params['RestoreSnapshotName']['AllowedPattern']
        for ok in ('', 'ecdl-shop-broker-final', 'automatic.ecdl-shop-broker-2025-01-02-03-00'):
            self.assertRegex(ok, pattern)
        for bad in ('payments-prod-final', 'automatic.payments-2025', 'ecdl-', 'x ecdl-a'):
            self.assertNotRegex(bad, pattern)

    def test_link_schema_uses_the_same_rule(self):
        for ok in ('ecdl-shop-broker-final', 'automatic.ecdl-shop-2025-01-02'):
            self.assertEqual(problems_for(minimal(
                mode='cdc', advanced={'broker': {'restore_snapshot': ok}})), [])
        for bad in ('payments-prod-final', 'automatic.payments'):
            probs = problems_for(minimal(mode='cdc', advanced={'broker': {'restore_snapshot': bad}}))
            self.assertEqual([p.path for p in probs], ['advanced.broker.restore_snapshot'], bad)


# ── Deploy role: just enough to take the final snapshot and restore ─────────
VALUES = dict(WORST_CASE, **{'AWS::Partition': 'aws', 'AWS::Region': 'us-east-2',
                             'AWS::AccountId': '111122223333'})


def _ec(kind, name):
    return 'arn:aws:elasticache:us-east-2:111122223333:{}:{}'.format(kind, name)


def _as_list(value):
    return [value] if isinstance(value, str) else list(value or [])


def deploy_role_allows(action, resource):
    """Whether the deploy role's policies allow action on resource. Conditions
    are ignored; this checks the action and resource scoping only."""
    resources = _template()['Resources']
    allowed = False
    for ref in resources['DeployRole']['Properties']['ManagedPolicyArns']:
        for st in render(resources, ref['Ref'], VALUES)['Statement']:
            actions = _as_list(st.get('Action'))
            if not any(fnmatch.fnmatchcase(action, a) for a in actions):
                continue
            if not any(fnmatch.fnmatchcase(resource, r) for r in _as_list(st.get('Resource'))):
                continue
            if st['Effect'] == 'Deny':
                return False
            allowed = True
    return allowed


class DeployRoleSnapshotTest(unittest.TestCase):
    def test_the_final_snapshot_on_delete_is_allowed(self):
        # DeleteReplicationGroup with FinalSnapshotIdentifier is authorised on
        # the group and the snapshot, and also needs CreateSnapshot on both.
        snap = _ec('snapshot', 'ecdl-shop-broker-broker-1a2b3c4d')
        for action in ('elasticache:DeleteReplicationGroup', 'elasticache:CreateSnapshot'):
            self.assertTrue(deploy_role_allows(action, _ec('replicationgroup', 'ecdl-shop-broker')))
            self.assertTrue(deploy_role_allows(action, snap))
        # CloudFormation picks the name, so any name is allowed on the snapshot.
        self.assertTrue(deploy_role_allows('elasticache:CreateSnapshot',
                                           _ec('snapshot', 'Final-Snapshot-XYZ')))

    def test_only_ecdl_brokers_can_be_snapshotted(self):
        self.assertFalse(deploy_role_allows('elasticache:CreateSnapshot',
                                            _ec('replicationgroup', 'payments-prod')))
        self.assertFalse(deploy_role_allows('elasticache:CreateSnapshot',
                                            _ec('cluster', 'payments-prod-001')))

    def test_restore_only_from_ecdl_snapshots(self):
        self.assertTrue(deploy_role_allows('elasticache:CreateReplicationGroup',
                                           _ec('snapshot', 'ecdl-shop-broker-final')))
        self.assertTrue(deploy_role_allows('elasticache:CreateReplicationGroup',
                                           _ec('snapshot', 'automatic.ecdl-shop-broker-2025')))
        self.assertFalse(deploy_role_allows('elasticache:CreateReplicationGroup',
                                            _ec('snapshot', 'payments-prod-final')))

    def test_no_snapshot_deletes_or_copies(self):
        for action in ('elasticache:DeleteSnapshot', 'elasticache:CopySnapshot',
                       'elasticache:ExportSnapshot'):
            self.assertFalse(deploy_role_allows(action, _ec('snapshot', 'ecdl-shop-final')), action)

    def test_snapshot_star_carries_no_other_action(self):
        # snapshot:* is safe only because both actions also need the group.
        resources = _template()['Resources']
        doc = resources['DeployLinksPolicy']['Properties']['PolicyDocument']
        st = next(s for s in doc['Statement'] if s.get('Sid') == 'BrokerFinalSnapshot')
        self.assertEqual(sorted(st['Action']), ['elasticache:CreateSnapshot',
                                                'elasticache:DeleteReplicationGroup'])


# ── Planner, deployer and change review ──────────────────────────────────────
def _broker_step(restore=None, for_update=False):
    r = copy.deepcopy(resolution('cdc'))
    if restore is not None:
        r['link']['advanced'] = {'broker': {'restore_snapshot': restore}}
    return next(s for s in plan(r, for_update=for_update)['steps'] if s['id'] == 'broker')


class RestorePlanTest(unittest.TestCase):
    def test_sent_when_set(self):
        self.assertEqual(_broker_step('ecdl-checkout-broker-final')['parameters']
                         ['RestoreSnapshotName'], 'ecdl-checkout-broker-final')

    def test_empty_by_default_so_the_broker_starts_empty(self):
        self.assertEqual(_broker_step()['parameters']['RestoreSnapshotName'], '')
        self.assertEqual(_broker_step('  ')['parameters']['RestoreSnapshotName'], '')

    def test_an_update_keeps_the_deployed_value(self):
        for update in (False, True):
            self.assertIn('RestoreSnapshotName', _broker_step(for_update=update)['keep_previous'])

    def _sent_on_update(self, existing):
        class Cfn(FakeCloudFormation):
            def describe_stacks(self, StackName=None, **kw):
                out = super(Cfn, self).describe_stacks(StackName=StackName, **kw)
                out['Stacks'][0]['Parameters'] = [
                    {'ParameterKey': k, 'ParameterValue': v} for k, v in existing.items()]
                return out
        step = _broker_step('ecdl-checkout-other')
        cfn = Cfn(existing={step['name']: 'CREATE_COMPLETE'},
                  states={step['name']: ['CREATE_COMPLETE', 'UPDATE_COMPLETE']})
        deployer(cfn, FakeECS())._stack_step(step, {})
        return {p['ParameterKey']: p for p in cfn.updated[0]['parameters']}['RestoreSnapshotName']

    def test_the_deployer_never_sends_a_new_snapshot_to_a_running_broker(self):
        self.assertEqual(self._sent_on_update({'RestoreSnapshotName': 'ecdl-checkout-first'}),
                         {'ParameterKey': 'RestoreSnapshotName', 'UsePreviousValue': True})
        # A broker from before the parameter: empty, which is what it was.
        self.assertEqual(self._sent_on_update({'ClusterName': 'ecdl-checkout-broker'}),
                         {'ParameterKey': 'RestoreSnapshotName', 'ParameterValue': ''})

    def test_a_first_deploy_sends_it(self):
        step = _broker_step('ecdl-checkout-final')
        cfn = FakeCloudFormation(states={step['name']: ['CREATE_COMPLETE']})
        deployer(cfn, FakeECS())._stack_step(step, {})
        sent = {p['ParameterKey']: p['ParameterValue'] for p in cfn.created[0]['parameters']}
        self.assertEqual(sent['RestoreSnapshotName'], 'ecdl-checkout-final')


class RestoreReviewTest(unittest.TestCase):
    def _review(self, deployed_value, planned_value):
        step = {'kind': 'stack', 'id': 'broker',
                'parameters': {'ClusterName': 'ecdl-l-broker',
                               'RestoreSnapshotName': planned_value}}
        deployed = {'broker': {'ClusterName': 'ecdl-l-broker'}, 'cdc': {}}
        if deployed_value is not None:
            deployed['broker']['RestoreSnapshotName'] = deployed_value
        return review_update({'steps': [step]}, {'name': 'l', 'mode': 'cdc'}, deployed,
                             {'broker': {'RestoreSnapshotName': ''}},
                             {'broker': {'RestoreSnapshotName': ''}})

    def test_a_new_snapshot_on_a_deployed_broker_is_refused_with_the_way_to_do_it(self):
        for before in (None, '', 'ecdl-l-first'):
            r = self._review(before, 'ecdl-l-second')
            self.assertTrue(r['refuse'], before)
            change = r['diff']['changes'][0]
            self.assertEqual(change['kind'], 'create_only')
            self.assertEqual(change['field'], 'advanced.broker.restore_snapshot')
            self.assertIn('delete the link', change['action'])

    def test_keeping_or_removing_it_is_no_change(self):
        self.assertFalse(self._review('ecdl-l-first', 'ecdl-l-first')['diff']['changed'])
        self.assertFalse(self._review('ecdl-l-first', '')['diff']['changed'])
        self.assertFalse(self._review(None, '')['diff']['changed'])

    def test_round_trips_into_the_link_file(self):
        self.assertEqual(INVERSE['RestoreSnapshotName'][0], ('advanced', 'broker', 'restore_snapshot'))
        doc = {}
        apply_settings(doc, {'RestoreSnapshotName': 'ecdl-l-first'}, {},
                       only={'RestoreSnapshotName'})
        self.assertEqual(doc, {'advanced': {'broker': {'restore_snapshot': 'ecdl-l-first'}}})
        doc = {}
        apply_settings(doc, {'RestoreSnapshotName': ''}, {}, only={'RestoreSnapshotName'})
        self.assertEqual(doc, {})


class RunbookTest(unittest.TestCase):
    def test_operations_documents_the_restore_and_the_reboot_rule(self):
        with open(os.path.join(ROOT, 'docs', 'operations.md')) as handle:
            text = handle.read()
        heading = '## Restoring the broker from a snapshot'
        self.assertTrue(heading in text, heading)
        section = text[text.index(heading):]
        section = section[:section.index('\n## ', 4)]
        for needed in ('restore_snapshot', 'PostgreSQL needs a reload', 'reboot',
                       'final snapshot', 'dead-letters'):
            self.assertTrue(needed in section, needed)
        self.assertTrue(re.search(r'[Nn]ever reboot the primary', text))
        teardown = text[text.index('## Tearing a link down'):]
        self.assertTrue('final snapshot' in teardown)


if __name__ == '__main__':
    unittest.main()
