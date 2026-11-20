"""The deployer's budgets, failure hints and update parameters.

Jira story F068  the load timeout comes from the link or the caller, not a constant
Jira story F085  a resume that fails after it was sent is not sent again, and the hint
      says whether the load finished
Jira story F138  stacks the deployer cannot fix name what to run
Jira story F139  an update keeps the sink's autoscaled task count
Hand-written fakes from test_deploy; no AWS.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.change import parameter_drift  # noqa: E402
from ecdl_link.deploy import (  # noqa: E402
    DEFAULT_LOAD_TIMEOUT_SECONDS, Clients, DeployError, Deployer, add_load_step)
from ecdl_link.link import problems_for  # noqa: E402
from ecdl_link.provision import plan  # noqa: E402
from ecdl_link.tests.test_deploy import (  # noqa: E402
    BROKER_OUTPUTS, LOADER_OUTPUTS, FakeCloudFormation, FakeECS, confirmed_offset)
from ecdl_link.tests.test_link import minimal  # noqa: E402
from ecdl_link.tests.test_provision import resolution, steps_by_id  # noqa: E402

DBZ = 'ecdl-checkout-cdc-debezium'
SINK = 'ecdl-checkout-cdc-sink'


def make(cfn, ecs, events=None, **kw):
    kw.setdefault('poll_seconds', 0)
    return Deployer(Clients(cloudformation=cfn, ecs=ecs),
                    template_body=lambda name: 'Template: ' + name,
                    on_progress=(events.append if events is not None else None),
                    sleep=lambda _s: None, offset_probe=confirmed_offset, **kw)


def full_cfn():
    """A first deploy of initial_load_and_cdc where every stack completes."""
    done = ['CREATE_COMPLETE'] * 6
    return FakeCloudFormation(
        states={'ecdl-checkout-broker': list(done), 'ecdl-checkout-cdc': list(done),
                'ecdl-checkout-loader': list(done)},
        outputs={'ecdl-checkout-broker': BROKER_OUTPUTS,
                 'ecdl-checkout-cdc': {'SinkService': SINK},
                 'ecdl-checkout-loader': LOADER_OUTPUTS})


def running_task():
    return {'tasks': [{'lastStatus': 'RUNNING', 'containers': [{}]}]}


# ── Jira story F068 ────────────────────────────────────────────────────────────────────
class LoadTimeoutTest(unittest.TestCase):
    def load_only(self, timeout_minutes=None):
        r = resolution('initial_load')
        if timeout_minutes is not None:
            r['link']['advanced'] = {'loader': {'timeout_minutes': timeout_minutes}}
        return add_load_step(plan(r))

    def run_hung_load(self, plan_doc, **kw):
        """A load task that never stops: the deploy fails at its budget."""
        cfn = FakeCloudFormation(
            states={'ecdl-checkout-loader': ['CREATE_COMPLETE'] * 4},
            outputs={'ecdl-checkout-loader': LOADER_OUTPUTS})
        ecs = FakeECS(clusters=['ecdl-checkout'], tasks=[running_task()] * 10000)
        d = make(cfn, ecs, poll_seconds=60, **kw)
        with self.assertRaises(DeployError) as caught:
            d.run(plan_doc)
        return caught.exception, d

    def test_the_plan_carries_the_link_setting(self):
        self.assertEqual(self.load_only(15)['load_timeout_seconds'], 900)
        self.assertNotIn('load_timeout_seconds', self.load_only())
        # A cdc link has no load, so nothing to carry.
        r = resolution('cdc')
        r['link']['advanced'] = {'loader': {'timeout_minutes': 15}}
        self.assertNotIn('load_timeout_seconds', plan(r))

    def test_the_link_setting_is_the_budget(self):
        exc, d = self.run_hung_load(self.load_only(15))
        self.assertEqual(d.load_timeout_seconds, 900)
        self.assertIn('within 900s', str(exc))
        self.assertIn('advanced.loader.timeout_minutes', exc.hint)
        self.assertIn('--load-timeout', exc.hint)

    def test_the_caller_overrides_the_link(self):
        exc, d = self.run_hung_load(self.load_only(15), load_timeout_seconds=120)
        self.assertEqual(d.load_timeout_seconds, 120)
        self.assertIn('within 120s', str(exc))

    def test_six_hours_when_nothing_says_otherwise(self):
        exc, d = self.run_hung_load(self.load_only())
        self.assertEqual(d.load_timeout_seconds, DEFAULT_LOAD_TIMEOUT_SECONDS)
        self.assertIn('within 21600s', str(exc))

    def test_the_schema_bounds(self):
        for good in (10, 360, 4320):
            self.assertEqual(problems_for(minimal(advanced={
                'loader': {'timeout_minutes': good}})), [], good)
        for bad in (9, 4321, '60', 1.5):
            self.assertTrue(problems_for(minimal(advanced={
                'loader': {'timeout_minutes': bad}})), bad)


# ── Jira story F085 ────────────────────────────────────────────────────────────────────
class ResumeFailureTest(unittest.TestCase):
    def full_plan(self):
        return add_load_step(plan(resolution('initial_load_and_cdc')))

    def test_a_resume_that_never_starts_a_task_is_not_sent_twice(self):
        # Debezium's service: running before the pause, 0 after it, and then
        # never 1 again -- ECS cannot start the task.
        ecs = FakeECS(clusters=['ecdl-checkout'], running={DBZ: [1, 0] + [0] * 1000})
        cfn = full_cfn()
        d = make(cfn, ecs, timeout_seconds=30)
        with self.assertRaises(DeployError) as caught:
            d.run(self.full_plan())
        exc = caught.exception
        self.assertEqual(exc.step, 'resume_capture')
        self.assertEqual([c for _cl, s, c in ecs.updated_services if s == DBZ], [0, 1])
        self.assertTrue(exc.capture_resumed)
        self.assertEqual(d.capture_state, 'resumed')
        self.assertIn('desired count is 1', exc.hint)
        self.assertIn('nothing needs reloading', exc.hint)
        self.assertNotIn('run the load again', exc.hint)
        self.assertNotIn('STILL PAUSED', exc.hint)
        self.assertTrue(cfn.created)

    def test_a_resume_call_that_fails_is_retried_and_says_the_load_finished(self):
        ecs = FakeECS(clusters=['ecdl-checkout'], running={DBZ: [1, 0, 1, 1, 1]})
        real = ecs.update_service
        calls = []

        def flaky(cluster=None, service=None, desiredCount=None, **kw):
            calls.append(desiredCount)
            if desiredCount == 1 and calls.count(1) == 1:
                raise Exception('ThrottlingException: Rate exceeded')
            return real(cluster=cluster, service=service, desiredCount=desiredCount)
        ecs.update_service = flaky
        d = make(full_cfn(), ecs)
        with self.assertRaises(DeployError) as caught:
            d.run(self.full_plan())
        exc = caught.exception
        self.assertEqual(calls, [0, 1, 1])
        self.assertTrue(exc.capture_resumed)
        self.assertIn('Capture was resumed', exc.hint)
        self.assertIn('nothing needs reloading', exc.hint)
        self.assertNotIn('run the load again', exc.hint)

    def test_a_retry_that_also_fails_is_still_paused_and_needs_no_reload(self):
        ecs = FakeECS(clusters=['ecdl-checkout'], running={DBZ: [1, 0]})
        real = ecs.update_service

        def broken(cluster=None, service=None, desiredCount=None, **kw):
            if desiredCount == 1:
                raise Exception('AccessDenied')
            return real(cluster=cluster, service=service, desiredCount=desiredCount)
        ecs.update_service = broken
        d = make(full_cfn(), ecs)
        with self.assertRaises(DeployError) as caught:
            d.run(self.full_plan())
        exc = caught.exception
        self.assertFalse(exc.capture_resumed)
        self.assertEqual(d.capture_state, 'paused')
        self.assertIn('STILL PAUSED', exc.hint)
        self.assertIn('nothing needs reloading', exc.hint)

    def test_a_failed_load_still_asks_for_a_reload(self):
        from ecdl_link.tests.test_deploy import stopped
        ecs = FakeECS(clusters=['ecdl-checkout'], running={DBZ: [1, 0, 1, 1]},
                      tasks=[stopped(exit_code=0), stopped(exit_code=1)])
        d = make(full_cfn(), ecs)
        with self.assertRaises(DeployError) as caught:
            d.run(self.full_plan())
        exc = caught.exception
        self.assertEqual(exc.step, 'run_load')
        self.assertTrue(exc.capture_resumed)
        self.assertIn('run the load again', exc.hint)
        self.assertNotIn('nothing needs reloading', exc.hint)


# ── Jira story F138 ────────────────────────────────────────────────────────────────────
class StuckStackHintTest(unittest.TestCase):
    def broker_only(self):
        p = plan(resolution('cdc'))
        p['steps'] = [s for s in p['steps'] if s['id'] == 'broker']
        return p

    def run_broker(self, cfn):
        d = make(cfn, FakeECS(clusters=['ecdl-checkout']))
        with self.assertRaises(DeployError) as caught:
            d.run(self.broker_only())
        return caught.exception

    def test_update_rollback_failed_is_refused_with_the_command(self):
        cfn = FakeCloudFormation(existing={'ecdl-checkout-broker': 'UPDATE_ROLLBACK_FAILED'})
        exc = self.run_broker(cfn)
        self.assertEqual(cfn.updated, [])
        self.assertIn('UPDATE_ROLLBACK_FAILED and cannot be updated', str(exc))
        self.assertIn('continue-update-rollback --stack-name ecdl-checkout-broker', exc.hint)
        self.assertIn('--resources-to-skip', exc.hint)

    def test_delete_failed_is_refused_with_the_way_out(self):
        cfn = FakeCloudFormation(existing={'ecdl-checkout-broker': 'DELETE_FAILED'})
        exc = self.run_broker(cfn)
        self.assertEqual((cfn.updated, cfn.created, cfn.deleted), ([], [], []))
        self.assertIn('ecdl destroy --link checkout', exc.hint)
        self.assertIn('--retain-resources', exc.hint)

    def test_a_failed_create_says_it_keeps_billing(self):
        cfn = FakeCloudFormation(states={'ecdl-checkout-broker': ['CREATE_FAILED']})
        exc = self.run_broker(cfn)
        self.assertIn('ended in CREATE_FAILED', str(exc))
        self.assertIn('billed', exc.hint)
        self.assertIn('ecdl destroy --link checkout', exc.hint)

    def test_a_rollback_that_fails_during_the_update_names_the_command(self):
        cfn = FakeCloudFormation(existing={'ecdl-checkout-broker': 'UPDATE_COMPLETE'},
                                 states={'ecdl-checkout-broker': [
                                     'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_FAILED']})
        exc = self.run_broker(cfn)
        self.assertIn('ended in UPDATE_ROLLBACK_FAILED', str(exc))
        self.assertIn('continue-update-rollback --stack-name ecdl-checkout-broker', exc.hint)

    def test_an_ordinary_rollback_keeps_its_own_hint(self):
        cfn = FakeCloudFormation(existing={'ecdl-checkout-broker': 'UPDATE_COMPLETE'},
                                 states={'ecdl-checkout-broker': [
                                     'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE']})
        exc = self.run_broker(cfn)
        self.assertIn('PREVIOUS', exc.hint)
        self.assertNotIn('continue-update-rollback', exc.hint)


# ── Jira story F139 ────────────────────────────────────────────────────────────────────
class SinkCountKeptTest(unittest.TestCase):
    def update_plan(self, sink=None):
        r = resolution('cdc')
        if sink is not None:
            r['link']['advanced'] = {'sink': sink}
        p = plan(r, for_update=True)
        p['steps'] = [s for s in p['steps'] if s['id'] == 'cdc']
        return p

    def run_update(self, plan_doc, sink_desired=None, describe_error=False, has_param=True):
        cfn = FakeCloudFormation(existing={'ecdl-checkout-cdc': 'UPDATE_COMPLETE'},
                                 states={'ecdl-checkout-cdc': ['UPDATE_COMPLETE'] * 3})
        cfn.params['ecdl-checkout-cdc'] = ({'SinkDesiredCount': '2'} if has_param else {})
        ecs = FakeECS(clusters=['ecdl-checkout'],
                      desired={SINK: sink_desired} if sink_desired is not None else {})
        if describe_error:
            def fail(**_):
                raise Exception('AccessDenied: ecs:DescribeServices')
            ecs.describe_services = fail
        events = []
        d = make(cfn, ecs, events)
        d._stack_step(plan_doc['steps'][0], {'broker': {
            'PrimaryEndpoint': 'b', 'Port': '6379', 'SecurityGroupId': 'sg',
            'AuthSecretArn': 'arn', 'TransitEncryption': 'true',
            'ReplicationGroupId': 'g'}}, 'checkout', plan_doc=plan_doc)
        sent = {p['ParameterKey']: p for p in cfn.updated[0]['parameters']}
        return sent['SinkDesiredCount'], events

    def test_the_live_count_is_sent(self):
        sent, events = self.run_update(self.update_plan(), sink_desired=6)
        self.assertEqual(sent, {'ParameterKey': 'SinkDesiredCount', 'ParameterValue': '6'})
        self.assertTrue(any(e['event'] == 'live_count_kept' and e['value'] == 6
                            for e in events))

    def test_the_live_count_stays_within_the_planned_bounds(self):
        self.assertEqual(self.run_update(self.update_plan(), sink_desired=50)[0]
                         ['ParameterValue'], '10')
        self.assertEqual(self.run_update(self.update_plan(), sink_desired=0)[0]
                         ['ParameterValue'], '2')
        bounded = self.update_plan({'min_count': 3, 'max_count': 4})
        self.assertEqual(self.run_update(bounded, sink_desired=9)[0]['ParameterValue'], '4')

    def test_an_unreadable_service_keeps_the_previous_value(self):
        sent, events = self.run_update(self.update_plan(), describe_error=True)
        self.assertEqual(sent, {'ParameterKey': 'SinkDesiredCount', 'UsePreviousValue': True})
        self.assertTrue(any(e['event'] == 'live_count_unknown' for e in events))
        # A stack without the parameter cannot use its previous value.
        sent, _ = self.run_update(self.update_plan(), describe_error=True, has_param=False)
        self.assertEqual(sent['ParameterValue'], '2')

    def test_an_explicit_desired_count_is_sent_as_written(self):
        p = self.update_plan({'desired_count': 3})
        self.assertNotIn('keep_live_count', p['steps'][0])
        sent, _ = self.run_update(p, sink_desired=6)
        self.assertEqual(sent['ParameterValue'], '3')

    def test_a_first_deploy_sends_the_planned_count(self):
        self.assertNotIn('keep_live_count', steps_by_id(plan(resolution('cdc')))['cdc'])

    def test_the_review_does_not_report_the_count_it_will_not_send(self):
        step = copy.deepcopy(self.update_plan()['steps'][0])
        deployed = dict(step['parameters'], SinkDesiredCount='6')
        self.assertEqual(parameter_drift(step, deployed, {}, {}), [])
        step.pop('keep_live_count')
        self.assertEqual(parameter_drift(step, deployed, {}, {}),
                         [('SinkDesiredCount', '6', '2')])


if __name__ == '__main__':
    unittest.main()
