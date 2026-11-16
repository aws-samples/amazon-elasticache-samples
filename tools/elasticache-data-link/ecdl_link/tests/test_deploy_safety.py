"""Failure-path safety of the deployer.

The happy path is covered in test_deploy.py. These are the cases from the code
review where a failure used to leave the pipeline in a bad state silently:

  * an update that rolled back was reported as deployed
  * a load failure (or Ctrl-C) left Debezium at zero tasks, so capture stayed
    paused until the source's log retention ran out
  * capture could be paused before Debezium had committed an offset, so resume
    started from the current position and skipped the load window
  * the load shared the 45-minute stack timeout and was left running on timeout
  * stacks were never tagged with their link
  * a redeploy could not tell "deployed" from "half-deployed" or "in progress"
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.deploy import (  # noqa: E402
    Clients, DeployError, Deployer, add_load_step, broker_offset_probe,
    deployment_state, wants_update_plan,
)
from ecdl_link.provision import plan  # noqa: E402
from ecdl_link.tests.test_deploy import (  # noqa: E402
    BROKER_OUTPUTS, LOADER_OUTPUTS, FakeCloudFormation, FakeECS, deployer, stopped,
)
from ecdl_link.tests.test_provision import resolution  # noqa: E402

BROKER_STEP = {'id': 'broker', 'name': 'ecdl-x-broker', 'kind': 'stack',
               'template': 'ecdl-broker-quickstart.yaml', 'parameters': {},
               'capabilities': [], 'purpose': 'the broker'}


def _handoff_cfn():
    return FakeCloudFormation(
        states={n: ['CREATE_COMPLETE', 'CREATE_COMPLETE']
                for n in ('ecdl-checkout-broker', 'ecdl-checkout-cdc',
                          'ecdl-checkout-loader')},
        outputs={'ecdl-checkout-broker': BROKER_OUTPUTS,
                 'ecdl-checkout-cdc': {},
                 'ecdl-checkout-loader': LOADER_OUTPUTS})


def _handoff_plan():
    return add_load_step(plan(resolution('initial_load_and_cdc')))


class _ResumeFailsECS(FakeECS):
    def update_service(self, cluster=None, service=None, desiredCount=None, **kw):
        if desiredCount == 1:
            raise Exception('AccessDenied')
        return super(_ResumeFailsECS, self).update_service(
            cluster=cluster, service=service, desiredCount=desiredCount, **kw)


class _StoppableECS(FakeECS):
    def __init__(self, *a, **kw):
        super(_StoppableECS, self).__init__(*a, **kw)
        self.stopped = []

    def stop_task(self, cluster=None, task=None, reason=None):
        self.stopped.append(task)


class RolledBackUpdateTest(unittest.TestCase):
    def test_update_rollback_complete_fails_the_step(self):
        cfn = FakeCloudFormation(
            existing={'ecdl-x-broker': 'CREATE_COMPLETE'},
            states={'ecdl-x-broker': ['CREATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE']})
        with self.assertRaises(DeployError) as caught:
            deployer(cfn, FakeECS())._stack_step(dict(BROKER_STEP), {})
        self.assertIn('UPDATE_ROLLBACK_COMPLETE', str(caught.exception))
        self.assertIn('PREVIOUS', caught.exception.hint)


class CaptureAlwaysResumedTest(unittest.TestCase):
    def test_failed_load_resumes_capture_and_still_fails(self):
        ecs = FakeECS(clusters=['ecdl-checkout'], tasks=[stopped(exit_code=1)],
                      running={'ecdl-checkout-cdc-debezium': [0, 1]})
        events = []
        with self.assertRaises(DeployError) as caught:
            deployer(_handoff_cfn(), ecs, events).run(_handoff_plan())
        self.assertEqual([d for _c, _s, d in ecs.updated_services], [0, 1])
        self.assertIn('exited 1', str(caught.exception))
        self.assertIn('Capture was resumed', caught.exception.hint)
        self.assertTrue(caught.exception.capture_resumed)
        self.assertIn('capture_resumed_after_failure', [e['event'] for e in events])

    def test_interrupt_during_load_resumes_capture(self):
        class Interrupting(FakeECS):
            def describe_tasks(self, **_):
                raise KeyboardInterrupt()
        ecs = Interrupting(clusters=['ecdl-checkout'],
                           running={'ecdl-checkout-cdc-debezium': [0, 1]})
        with self.assertRaises(KeyboardInterrupt):
            deployer(_handoff_cfn(), ecs).run(_handoff_plan())
        self.assertEqual([d for _c, _s, d in ecs.updated_services], [0, 1])

    def test_a_failed_resume_is_reported_loudly(self):
        ecs = _ResumeFailsECS(clusters=['ecdl-checkout'], tasks=[stopped(exit_code=1)],
                              running={'ecdl-checkout-cdc-debezium': [0]})
        events = []
        with self.assertRaises(DeployError) as caught:
            deployer(_handoff_cfn(), ecs, events).run(_handoff_plan())
        self.assertIn('CAPTURE IS STILL PAUSED', caught.exception.hint)
        self.assertFalse(caught.exception.capture_resumed)
        self.assertIn('capture_resume_failed', [e['event'] for e in events])

    def test_no_resume_when_capture_was_never_paused(self):
        cfn = _handoff_cfn()
        cfn.states['ecdl-checkout-cdc'] = ['CREATE_FAILED']
        ecs = FakeECS(clusters=['ecdl-checkout'])
        with self.assertRaises(DeployError):
            deployer(cfn, ecs).run(_handoff_plan())
        self.assertEqual(ecs.updated_services, [])


class OffsetBeforePauseTest(unittest.TestCase):
    def _deployer(self, ecs, probe, events, sleeps):
        return Deployer(Clients(cloudformation=_handoff_cfn(), ecs=ecs),
                        template_body=lambda name: 'T', on_progress=events.append,
                        poll_seconds=1, sleep=sleeps.append, offset_probe=probe,
                        offset_wait_seconds=3, offset_grace_seconds=99)

    def test_no_offset_refuses_to_pause(self):
        ecs = FakeECS(clusters=['ecdl-checkout'])
        events, sleeps = [], []
        with self.assertRaises(DeployError) as caught:
            self._deployer(ecs, lambda p, o: False, events, sleeps).run(_handoff_plan())
        self.assertIn('NOT paused', str(caught.exception))
        self.assertEqual(ecs.updated_services, [])
        self.assertEqual(ecs.run_tasks, [])

    def test_offset_appearing_lets_the_pause_go_ahead(self):
        answers = [False, True]
        ecs = FakeECS(clusters=['ecdl-checkout'],
                      running={'ecdl-checkout-cdc-debezium': [0, 1]})
        events, sleeps = [], []
        self._deployer(ecs, lambda p, o: answers.pop(0), events, sleeps).run(
            _handoff_plan())
        self.assertIn('offset_confirmed', [e['event'] for e in events])
        self.assertNotIn(99, sleeps)

    def test_unreachable_broker_falls_back_to_a_grace_period(self):
        ecs = FakeECS(clusters=['ecdl-checkout'],
                      running={'ecdl-checkout-cdc-debezium': [0, 1]})
        events, sleeps = [], []
        self._deployer(ecs, lambda p, o: None, events, sleeps).run(_handoff_plan())
        self.assertIn('offset_unverified', [e['event'] for e in events])
        self.assertIn(99, sleeps)


class LoadTimeoutTest(unittest.TestCase):
    def test_load_has_its_own_budget_and_is_stopped_on_timeout(self):
        cfn = FakeCloudFormation(
            states={'ecdl-checkout-loader': ['CREATE_COMPLETE', 'CREATE_COMPLETE']},
            outputs={'ecdl-checkout-loader': LOADER_OUTPUTS})
        running = {'tasks': [{'lastStatus': 'RUNNING', 'containers': [{}]}]}
        ecs = _StoppableECS(clusters=['ecdl-checkout'], tasks=[running] * 10)
        d = Deployer(Clients(cloudformation=cfn, ecs=ecs), template_body=lambda n: 'T',
                     poll_seconds=1, timeout_seconds=1000, load_timeout_seconds=3,
                     sleep=lambda _s: None)
        with self.assertRaises(DeployError) as caught:
            d.run(add_load_step(plan(resolution('initial_load'))))
        self.assertIn('within 3s', str(caught.exception))
        self.assertEqual(ecs.stopped, ['arn:task/1'])

    def test_zero_poll_interval_cannot_hang_the_waits(self):
        cfn = FakeCloudFormation(existing={'ecdl-x-broker': 'CREATE_IN_PROGRESS'})
        d = Deployer(Clients(cloudformation=cfn, ecs=FakeECS()),
                     template_body=lambda n: 'T', poll_seconds=0, timeout_seconds=3,
                     sleep=lambda _s: None)
        with self.assertRaises(DeployError):
            d._wait_for_stack('ecdl-x-broker', 'broker')


class TaggingTest(unittest.TestCase):
    def test_every_stack_is_tagged_with_its_link(self):
        cfn = _handoff_cfn()
        ecs = FakeECS(clusters=['ecdl-checkout'],
                      running={'ecdl-checkout-cdc-debezium': [0, 1]})
        deployer(cfn, ecs).run(_handoff_plan())
        self.assertEqual(len(cfn.created), 3)
        for created in cfn.created:
            tags = {t['Key']: t['Value'] for t in created['Tags']}
            self.assertEqual(tags['ecdl:link'], 'checkout')


class DeploymentStateTest(unittest.TestCase):
    NAMES = ('ecdl-checkout-broker', 'ecdl-checkout-cdc', 'ecdl-checkout-loader')

    def _state(self, existing):
        return deployment_state(_handoff_plan(),
                                FakeCloudFormation(existing=existing))['state']

    def test_classification(self):
        n = self.NAMES
        self.assertEqual(self._state({}), 'new')
        self.assertEqual(self._state({x: 'CREATE_COMPLETE' for x in n}), 'deployed')
        self.assertEqual(self._state({n[0]: 'CREATE_COMPLETE'}), 'partial')
        self.assertEqual(self._state({n[0]: 'CREATE_COMPLETE',
                                      n[1]: 'UPDATE_IN_PROGRESS'}), 'in_progress')

    def test_describe_errors_are_not_read_as_absent(self):
        class Broken(FakeCloudFormation):
            def describe_stacks(self, **_):
                raise Exception('Throttling')
        with self.assertRaises(Exception):
            deployment_state(_handoff_plan(), Broken())

    def test_update_plan_only_for_a_deployed_cdc_link(self):
        cdc = _handoff_plan()
        load_only = add_load_step(plan(resolution('initial_load')))
        self.assertTrue(wants_update_plan(cdc, 'deployed'))
        self.assertFalse(wants_update_plan(cdc, 'deployed', reload=True))
        self.assertFalse(wants_update_plan(cdc, 'partial'))
        self.assertFalse(wants_update_plan(load_only, 'deployed'))


class PgSlotTest(unittest.TestCase):
    """Per-link replication slot, which must never change on an existing stack."""

    def _cdc(self, engine):
        import copy
        r = copy.deepcopy(resolution('cdc'))
        r['source']['engine'] = engine
        return next(s for s in plan(r)['steps'] if s['id'] == 'cdc')

    def test_postgres_links_get_their_own_slot(self):
        from ecdl_link.provision import pg_slot_name
        self.assertEqual(self._cdc('postgresql')['parameters']['PgSlotName'],
                         'ecdl_checkout')
        self.assertEqual(pg_slot_name('Shop-EU'), 'ecdl_shop_eu')
        self.assertNotIn('PgSlotName', self._cdc('mysql')['parameters'])

    def _update(self, existing_params):
        class Cfn(FakeCloudFormation):
            def describe_stacks(self, StackName=None, **kw):
                out = super(Cfn, self).describe_stacks(StackName=StackName, **kw)
                out['Stacks'][0]['Parameters'] = [
                    {'ParameterKey': k, 'ParameterValue': v}
                    for k, v in existing_params.items()]
                return out
        step = dict(self._cdc('postgresql'), from_outputs={})
        cfn = Cfn(existing={step['name']: 'CREATE_COMPLETE'},
                  states={step['name']: ['CREATE_COMPLETE', 'UPDATE_COMPLETE']})
        deployer(cfn, FakeECS())._stack_step(step, {})
        sent = {p['ParameterKey']: p for p in cfn.updated[0]['parameters']}
        return sent['PgSlotName']

    def test_update_keeps_the_existing_slot(self):
        self.assertEqual(self._update({'PgSlotName': 'ecdl_checkout'}),
                         {'ParameterKey': 'PgSlotName', 'UsePreviousValue': True})

    def test_a_stack_from_before_per_link_slots_keeps_the_shared_one(self):
        # Empty -> the image default ecdl_slot, which that stack was using.
        self.assertEqual(self._update({}),
                         {'ParameterKey': 'PgSlotName', 'ParameterValue': ''})

    def test_teardown_says_how_to_drop_the_slot(self):
        from ecdl_link.teardown import destroy_plan
        fu = destroy_plan('checkout')['follow_up']
        self.assertEqual(fu['pg_drop_slot_sql'],
                         "SELECT pg_drop_replication_slot('ecdl_checkout');")


class BrokerOffsetProbeTest(unittest.TestCase):
    def _probe(self, hlen=None, error=None):
        class Secrets(object):
            def get_secret_value(self, SecretId):
                return {'SecretString': 'tok'}

        seen = {}

        class Conn(object):
            def hlen(self, key):
                seen['key'] = key
                if error:
                    raise error
                return hlen

        def connect(host, port, password, ssl, timeout):
            seen.update(host=host, password=password, ssl=ssl)
            return Conn()
        probe = broker_offset_probe(Secrets(), connect=connect)
        return probe(_handoff_plan(), {'broker': BROKER_OUTPUTS}), seen

    def test_reads_the_links_offset_key_with_the_broker_token(self):
        answer, seen = self._probe(hlen=1)
        self.assertTrue(answer)
        self.assertEqual(seen['key'], 'ecdl:checkout:offsets')
        self.assertEqual(seen['password'], 'tok')
        self.assertTrue(seen['ssl'])

    def test_empty_is_false_and_unreachable_is_none(self):
        self.assertFalse(self._probe(hlen=0)[0])
        self.assertIsNone(self._probe(error=OSError('timed out'))[0])


if __name__ == '__main__':
    unittest.main()
