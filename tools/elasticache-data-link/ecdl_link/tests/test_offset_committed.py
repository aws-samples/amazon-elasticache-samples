"""Jira story F005: the CDC stack records that the link has committed an offset.

Debezium cannot tell a first start from a start after the broker lost its
offset. The deployer can: it sets the CDC stack's OffsetCommitted parameter to
true once it has seen an offset (or a fresh heartbeat), every update keeps it,
and the template passes it to Debezium as EXPECT_OFFSET. With it Debezium
refuses to start without its offset (EcdlOffsetBackingStore) instead of
starting at the current log position.
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import yaml  # noqa: E402

from ecdl_link.change import KEPT, parameter_drift  # noqa: E402
from ecdl_link.deploy import Clients, Deployer, add_load_step  # noqa: E402
from ecdl_link.provision import plan  # noqa: E402
from ecdl_link.tests.test_capture_config import properties  # noqa: E402
from ecdl_link.tests.test_control_plane_iam import _CfnLoader  # noqa: E402
from ecdl_link.tests.test_deploy import (  # noqa: E402
    BROKER_OUTPUTS, LOADER_OUTPUTS, FakeCloudFormation, FakeECS,
)
from ecdl_link.tests.test_provision import resolution  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
START = 1_800_000_000_000


def cdc_step(plan_doc):
    return next(s for s in plan_doc['steps'] if s.get('id') == 'cdc')


def probe(offset=True, heartbeat=START + 1):
    """offset: the answer to "is there an offset"; heartbeat: newest heartbeat ms."""
    def p(plan_doc, outputs):
        return offset
    p.broker_time_ms = lambda plan_doc, outputs: START
    p.heartbeat_ms = lambda plan_doc, outputs: heartbeat
    return p


def first_cdc_cfn():
    return FakeCloudFormation(
        states={'ecdl-checkout-broker': ['CREATE_COMPLETE', 'CREATE_COMPLETE'],
                'ecdl-checkout-cdc': ['CREATE_COMPLETE', 'CREATE_COMPLETE']},
        outputs={'ecdl-checkout-broker': BROKER_OUTPUTS, 'ecdl-checkout-cdc': {}})


def handoff_cfn():
    return FakeCloudFormation(
        states={n: ['CREATE_COMPLETE', 'CREATE_COMPLETE']
                for n in ('ecdl-checkout-broker', 'ecdl-checkout-cdc', 'ecdl-checkout-loader')},
        outputs={'ecdl-checkout-broker': BROKER_OUTPUTS, 'ecdl-checkout-cdc': {},
                 'ecdl-checkout-loader': LOADER_OUTPUTS})


def run(cfn, plan_doc, offset_probe, ecs=None):
    ecs = ecs or FakeECS(clusters=['ecdl-checkout'],
                         running={'ecdl-checkout-cdc-debezium': [0, 1]})
    events = []
    order = []
    real_update_service = ecs.update_service
    real_update_stack = cfn.update_stack

    def update_service(**kw):
        order.append(('desired', kw.get('desiredCount')))
        return real_update_service(**kw)

    def update_stack(**kw):
        order.append(('stack', kw.get('StackName')))
        return real_update_stack(**kw)
    ecs.update_service = update_service
    cfn.update_stack = update_stack
    d = Deployer(Clients(cloudformation=cfn, ecs=ecs), template_body=lambda name: 'T',
                 on_progress=events.append, poll_seconds=1, sleep=lambda _s: None,
                 offset_probe=offset_probe, capture_wait_seconds=3)
    result = d.run(plan_doc)
    return d, result, [e['event'] for e in events], order, events


def marking_updates(cfn):
    return [u for u in cfn.updated if u['name'] == 'ecdl-checkout-cdc'
            and any(p['ParameterKey'] == 'OffsetCommitted' and p.get('ParameterValue') == 'true'
                    for p in u['parameters'])]


class PlannerTest(unittest.TestCase):

    def test_a_first_deploy_sends_false(self):
        for mode in ('cdc', 'initial_load_and_cdc'):
            step = cdc_step(plan(resolution(mode)))
            self.assertEqual(step['parameters']['OffsetCommitted'], 'false', mode)
            # Not kept: a first deploy, a retried one, or a reload resets it.
            self.assertNotIn('OffsetCommitted', step['keep_previous'], mode)

    def test_an_update_keeps_it(self):
        step = cdc_step(plan(resolution('cdc'), for_update=True))
        self.assertIn('OffsetCommitted', step['keep_previous'])

    def test_it_is_not_reported_as_drift(self):
        self.assertIn('OffsetCommitted', KEPT)
        step = cdc_step(plan(resolution('cdc'), for_update=True))
        deployed = dict(step['parameters'], OffsetCommitted='true')
        drift = parameter_drift(step, deployed, {}, {})
        self.assertNotIn('OffsetCommitted', [p for p, _d, _n in drift])


class TemplateTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, 'infra', 'ecdl-cdc-ecs.yaml')) as f:
            cls.doc = yaml.load(f, Loader=_CfnLoader)  # nosec B506 - SafeLoader subclass

    def test_parameter_defaults_to_false_and_allows_empty(self):
        p = self.doc['Parameters']['OffsetCommitted']
        self.assertEqual(p['Default'], 'false')
        self.assertEqual(set(p['AllowedValues']), {'true', 'false', ''})

    def test_debezium_gets_it_as_expect_offset(self):
        task = self.doc['Resources']['DebeziumTaskDef']['Properties']
        container = next(c for c in task['ContainerDefinitions'] if c['Name'] == 'debezium')
        env = {e['Name']: e['Value'] for e in container['Environment'] if isinstance(e, dict) and 'Name' in e}
        self.assertEqual(env['EXPECT_OFFSET'], {'Ref': 'OffsetCommitted'})

    def test_the_image_reads_expect_offset(self):
        self.assertEqual(properties()['debezium.source.offset.storage.ecdl.expect.offset'],
                         '${EXPECT_OFFSET:false}')


class DeployerRecordsTest(unittest.TestCase):

    def test_a_cdc_first_deploy_records_it_after_the_heartbeat(self):
        cfn = first_cdc_cfn()
        d, result, names, order, _ = run(cfn, plan(resolution('cdc')), probe())
        self.assertTrue(result['offset_recorded'])
        self.assertEqual(len(marking_updates(cfn)), 1)
        self.assertEqual(cfn.params['ecdl-checkout-cdc']['OffsetCommitted'], 'true')
        update = marking_updates(cfn)[0]
        # Everything else stays as the stack holds it.
        others = [p for p in update['parameters'] if p['ParameterKey'] != 'OffsetCommitted']
        self.assertTrue(others and all(p == {'ParameterKey': p['ParameterKey'], 'UsePreviousValue': True}
                                       for p in others), others)
        self.assertEqual(update['Tags'], [{'Key': 'ecdl:link', 'Value': 'checkout'},
                                          {'Key': 'ecdl:component', 'Value': 'cdc'}])
        # The update replaced the Debezium task, so its streaming is checked again.
        self.assertEqual(names[-4:], ['offset_recording', 'offset_recorded',
                                      'capture_streaming', 'plan_complete'])

    def test_a_load_and_cdc_deploy_records_it_only_after_the_resume(self):
        cfn = handoff_cfn()
        _d, result, _names, order, _ = run(cfn, add_load_step(plan(resolution('initial_load_and_cdc'))),
                                           probe())
        self.assertTrue(result['offset_recorded'])
        # Never while paused: the stack update would apply the stack's desired
        # count and resume capture under the load.
        # The loader stack is tagged complete (Jira story F059) before the resume.
        self.assertEqual(order, [('desired', 0), ('stack', 'ecdl-checkout-loader'),
                                 ('desired', 1), ('stack', 'ecdl-checkout-cdc')])

    def test_the_offset_alone_is_enough_when_no_heartbeat_can_be_read(self):
        cfn = handoff_cfn()
        p = probe(heartbeat=None)
        _d, result, names, _order, _ = run(cfn, add_load_step(plan(resolution('initial_load_and_cdc'))), p)
        self.assertFalse(result['capture_verified'])
        self.assertTrue(result['offset_recorded'])

    def test_nothing_seen_records_nothing(self):
        # The CLI on a laptop: the broker cannot be read at all.
        cfn = first_cdc_cfn()
        _d, result, names, _order, _ = run(cfn, plan(resolution('cdc')), probe(offset=None, heartbeat=None))
        self.assertIsNone(result['offset_recorded'])
        self.assertEqual(marking_updates(cfn), [])
        self.assertEqual(cfn.params['ecdl-checkout-cdc']['OffsetCommitted'], 'false')

    def test_already_recorded_is_not_sent_again(self):
        cfn = first_cdc_cfn()
        plan_doc = plan(resolution('cdc'))
        cdc_step(plan_doc)['parameters']['OffsetCommitted'] = 'true'
        _d, result, names, _order, _ = run(cfn, plan_doc, probe())
        self.assertTrue(result['offset_recorded'])
        self.assertEqual(marking_updates(cfn), [])
        self.assertNotIn('offset_recording', names)

    def test_a_failure_to_record_does_not_fail_the_deploy(self):
        cfn = first_cdc_cfn()
        real = cfn.update_stack

        def refuse(**kw):
            if kw.get('StackName') == 'ecdl-checkout-cdc':
                raise Exception('AccessDenied')
            return real(**kw)
        cfn.update_stack = refuse
        _d, result, names, _order, events = run(cfn, plan(resolution('cdc')), probe())
        self.assertFalse(result['offset_recorded'])
        failed = [e for e in events if e['event'] == 'offset_record_failed']
        self.assertEqual(len(failed), 1)
        self.assertIn('AccessDenied', failed[0]['reason'])
        self.assertIn('not refused', failed[0]['hint'])
        self.assertEqual(names[-1], 'plan_complete')

    def test_a_stack_holding_debezium_at_zero_is_not_updated(self):
        cfn = first_cdc_cfn()
        plan_doc = plan(resolution('cdc'))
        cdc_step(plan_doc)['parameters']['DebeziumDesiredCount'] = '0'
        _d, result, _names, _order, _ = run(cfn, plan_doc, probe())
        self.assertFalse(result['offset_recorded'])
        self.assertEqual(marking_updates(cfn), [])


class UpdateTest(unittest.TestCase):
    """An update of a deployed link."""

    def run_update(self, stack_value, offset):
        cfn = FakeCloudFormation(existing={'ecdl-checkout-broker': 'UPDATE_COMPLETE',
                                           'ecdl-checkout-cdc': 'UPDATE_COMPLETE'},
                                 outputs={'ecdl-checkout-broker': BROKER_OUTPUTS})
        held = {'DebeziumDesiredCount': '1', 'PgSlotName': ''}
        if stack_value is not None:
            held['OffsetCommitted'] = stack_value
        cfn.params['ecdl-checkout-cdc'] = held
        cfn.params['ecdl-checkout-broker'] = {}
        plan_doc = add_load_step(plan(resolution('cdc'), for_update=True))
        ecs = FakeECS(clusters=['ecdl-checkout'], desired={'ecdl-checkout-cdc-debezium': 1},
                      running={'ecdl-checkout-cdc-debezium': [1, 1, 1]})
        _d, result, names, _order, _ = run(cfn, copy.deepcopy(plan_doc), probe(offset=offset), ecs=ecs)
        first = next(u for u in cfn.updated if u['name'] == 'ecdl-checkout-cdc')
        sent = {p['ParameterKey']: p for p in first['parameters']}['OffsetCommitted']
        return cfn, result, sent

    def test_true_is_kept_even_when_the_broker_lost_the_offset(self):
        # Exactly the case Jira story F005 is about: never downgraded by an update.
        cfn, _result, sent = self.run_update('true', offset=False)
        self.assertEqual(sent, {'ParameterKey': 'OffsetCommitted', 'UsePreviousValue': True})
        self.assertEqual(cfn.params['ecdl-checkout-cdc']['OffsetCommitted'], 'true')

    def test_an_offset_in_the_broker_is_recorded_in_the_update_itself(self):
        # Stacks deployed before this parameter: one update, one restart.
        cfn, result, sent = self.run_update(None, offset=True)
        self.assertEqual(sent, {'ParameterKey': 'OffsetCommitted', 'ParameterValue': 'true'})
        self.assertTrue(result['offset_recorded'])
        self.assertEqual(len([u for u in cfn.updated if u['name'] == 'ecdl-checkout-cdc']), 1)

    def test_false_with_no_offset_yet_stays_false_in_the_update(self):
        cfn, _result, sent = self.run_update('false', offset=False)
        self.assertEqual(sent, {'ParameterKey': 'OffsetCommitted', 'UsePreviousValue': True})

    def test_a_reload_resets_it(self):
        # `ecdl apply --reload` plans a first deploy over the existing stacks:
        # the position reset the Debezium refusal tells the operator to use.
        cfn = FakeCloudFormation(
            existing={'ecdl-checkout-broker': 'UPDATE_COMPLETE', 'ecdl-checkout-cdc': 'UPDATE_COMPLETE',
                      'ecdl-checkout-loader': 'UPDATE_COMPLETE'},
            outputs={'ecdl-checkout-broker': BROKER_OUTPUTS, 'ecdl-checkout-loader': LOADER_OUTPUTS})
        cfn.params['ecdl-checkout-cdc'] = {'OffsetCommitted': 'true', 'DebeziumDesiredCount': '1'}
        plan_doc = add_load_step(plan(resolution('initial_load_and_cdc')))
        _d, result, _names, order, _ = run(cfn, plan_doc, probe(offset=True, heartbeat=None))
        first = next(u for u in cfn.updated if u['name'] == 'ecdl-checkout-cdc')
        sent = {p['ParameterKey']: p for p in first['parameters']}['OffsetCommitted']
        self.assertEqual(sent, {'ParameterKey': 'OffsetCommitted', 'ParameterValue': 'false'})
        # ... and recorded again once the restarted capture commits a new one.
        self.assertEqual(cfn.params['ecdl-checkout-cdc']['OffsetCommitted'], 'true')


if __name__ == '__main__':
    unittest.main()
