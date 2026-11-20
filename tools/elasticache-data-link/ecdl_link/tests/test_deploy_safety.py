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

    def test_interrupt_stops_the_load_task_before_resuming(self):
        # Lifecycle/load H1: a Ctrl-C resumed capture while the loader kept
        # writing for hours.
        order = []

        class Interrupting(FakeECS):
            calls = 0

            def describe_tasks(self, **_):
                Interrupting.calls += 1
                if Interrupting.calls == 1:
                    raise KeyboardInterrupt()
                order.append('stopped-seen')
                return stopped(exit_code=None, reason='stopped by ecdl')

            def stop_task(self, **kw):
                order.append('stop')
                return super().stop_task(**kw)

            def update_service(self, **kw):
                order.append('desired-{}'.format(kw.get('desiredCount')))
                return super().update_service(**kw)
        ecs = Interrupting(clusters=['ecdl-checkout'],
                           running={'ecdl-checkout-cdc-debezium': [0, 1]})
        d = deployer(_handoff_cfn(), ecs)
        with self.assertRaises(KeyboardInterrupt):
            d.run(_handoff_plan())
        self.assertEqual(order, ['desired-0', 'stop', 'stopped-seen', 'desired-1'])
        self.assertEqual(d.capture_state, 'resumed')

    def test_capture_stays_paused_when_the_stop_cannot_be_confirmed(self):
        # An expired session or throttling: describe_tasks keeps failing.
        class Blind(FakeECS):
            def describe_tasks(self, **_):
                raise Exception('ExpiredTokenException')
        ecs = Blind(clusters=['ecdl-checkout'], running={'ecdl-checkout-cdc-debezium': [0, 1]})
        events = []
        d = deployer(_handoff_cfn(), ecs, events)
        with self.assertRaises(DeployError) as caught:
            d.run(_handoff_plan())
        self.assertEqual([x for _c, _s, x in ecs.updated_services], [0])
        self.assertIn('CAPTURE IS STILL PAUSED, on purpose', caught.exception.hint)
        self.assertIn('--family ecdl-checkout-loader', caught.exception.hint)
        self.assertFalse(caught.exception.capture_resumed)
        self.assertEqual(d.capture_state, 'paused')
        self.assertIn('capture_left_paused', [e['event'] for e in events])

    def test_a_failing_stop_task_leaves_capture_paused(self):
        class Unstoppable(FakeECS):
            def describe_tasks(self, **_):
                return {'tasks': [{'lastStatus': 'RUNNING'}]}

            def stop_task(self, **_):
                raise Exception('AccessDenied')
        ecs = Unstoppable(clusters=['ecdl-checkout'], running={'ecdl-checkout-cdc-debezium': [0, 1]})
        d = deployer(_handoff_cfn(), ecs)
        d.load_timeout_seconds = 1
        with self.assertRaises(DeployError) as caught:
            d.run(_handoff_plan())
        self.assertIn('could not be stopped', caught.exception.hint)
        self.assertEqual([x for _c, _s, x in ecs.updated_services], [0])

    def test_a_task_that_never_stops_leaves_capture_paused(self):
        class Stuck(FakeECS):
            def describe_tasks(self, **_):
                return {'tasks': [{'lastStatus': 'DEACTIVATING'}]}
        ecs = Stuck(clusters=['ecdl-checkout'], running={'ecdl-checkout-cdc-debezium': [0, 1]})
        d = deployer(_handoff_cfn(), ecs)
        d.load_timeout_seconds = 1
        d.stop_wait_seconds = 3
        with self.assertRaises(DeployError) as caught:
            d.run(_handoff_plan())
        self.assertIn('still DEACTIVATING', caught.exception.hint)
        self.assertEqual(ecs.stopped_tasks, ['arn:task/1'])
        self.assertEqual([x for _c, _s, x in ecs.updated_services], [0])

    def test_a_timed_out_load_is_stopped_then_capture_resumed(self):
        class SlowThenStopped(FakeECS):
            def describe_tasks(self, **_):
                if self.stopped_tasks:
                    return stopped(exit_code=None)
                return {'tasks': [{'lastStatus': 'RUNNING'}]}
        ecs = SlowThenStopped(clusters=['ecdl-checkout'],
                              running={'ecdl-checkout-cdc-debezium': [0, 1]})
        d = deployer(_handoff_cfn(), ecs)
        d.load_timeout_seconds = 1
        with self.assertRaises(DeployError) as caught:
            d.run(_handoff_plan())
        self.assertIn('did not finish', str(caught.exception))
        self.assertEqual(ecs.stopped_tasks, ['arn:task/1'])
        self.assertEqual([x for _c, _s, x in ecs.updated_services], [0, 1])
        self.assertTrue(caught.exception.capture_resumed)

    def test_the_second_table_is_not_loaded_if_capture_came_back(self):
        # Something resumed capture between tables.
        class Resumed(FakeECS):
            def describe_tasks(self, **_):
                self.service_tasks['ecdl-checkout-cdc-debezium'] = ['arn:dbz/1']
                return stopped(exit_code=0)
        ecs = Resumed(clusters=['ecdl-checkout'], running={'ecdl-checkout-cdc-debezium': [0, 1]})
        with self.assertRaises(DeployError) as caught:
            deployer(_handoff_cfn(), ecs).run(_handoff_plan())
        self.assertIn('capture is running again', str(caught.exception))
        self.assertEqual(len(ecs.run_tasks), 1)

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

    def test_unreachable_broker_refuses_to_pause(self):
        # It used to pause after a fixed grace period: a guess that Debezium had
        # committed a position. If it had not, every change during the load was lost.
        ecs = FakeECS(clusters=['ecdl-checkout'])
        events, sleeps = [], []
        with self.assertRaises(DeployError) as caught:
            self._deployer(ecs, lambda p, o: None, events, sleeps).run(_handoff_plan())
        self.assertIn('NOT paused', str(caught.exception))
        self.assertIn('--offset-checked', caught.exception.hint)
        self.assertEqual(ecs.updated_services, [])
        self.assertEqual(ecs.run_tasks, [])
        self.assertNotIn(99, sleeps)

    def test_no_probe_at_all_refuses_to_pause(self):
        ecs = FakeECS(clusters=['ecdl-checkout'])
        d = Deployer(Clients(cloudformation=_handoff_cfn(), ecs=ecs),
                     template_body=lambda name: 'T', poll_seconds=0, sleep=lambda _s: None)
        with self.assertRaises(DeployError):
            d.run(_handoff_plan())
        self.assertEqual(ecs.updated_services, [])

    def test_probe_that_raises_is_treated_as_unreachable(self):
        def broken(plan_doc, outputs):
            raise RuntimeError('boom')
        ecs = FakeECS(clusters=['ecdl-checkout'])
        events, sleeps = [], []
        with self.assertRaises(DeployError):
            self._deployer(ecs, broken, events, sleeps).run(_handoff_plan())
        self.assertIn('offset_probe_error', [e['event'] for e in events])
        self.assertEqual(ecs.updated_services, [])

    def test_operator_confirmation_allows_the_grace_period(self):
        ecs = FakeECS(clusters=['ecdl-checkout'],
                      running={'ecdl-checkout-cdc-debezium': [0, 1]})
        events, sleeps = [], []
        d = Deployer(Clients(cloudformation=_handoff_cfn(), ecs=ecs),
                     template_body=lambda name: 'T', on_progress=events.append,
                     poll_seconds=1, sleep=sleeps.append, offset_probe=lambda p, o: None,
                     offset_wait_seconds=3, offset_grace_seconds=99,
                     allow_unverified_offset=True)
        d.run(_handoff_plan())
        self.assertIn('offset_unverified', [e['event'] for e in events])
        self.assertIn(99, sleeps)
        self.assertEqual([x for _c, _s, x in ecs.updated_services], [0, 1])

    def test_confirmation_does_not_override_a_broker_that_says_no(self):
        # The broker answered and there is no offset: no flag makes that safe.
        ecs = FakeECS(clusters=['ecdl-checkout'])
        d = Deployer(Clients(cloudformation=_handoff_cfn(), ecs=ecs),
                     template_body=lambda name: 'T', poll_seconds=1, sleep=lambda _s: None,
                     offset_probe=lambda p, o: False, offset_wait_seconds=2,
                     allow_unverified_offset=True)
        with self.assertRaises(DeployError):
            d.run(_handoff_plan())
        self.assertEqual(ecs.updated_services, [])


class LoadVersionTest(unittest.TestCase):
    """Loaded rows are versioned with the broker's clock at the pause (D4')."""

    def _run(self, broker_ms, tables=None):
        ecs = FakeECS(clusters=['ecdl-checkout'],
                      running={'ecdl-checkout-cdc-debezium': [0, 1]})
        seen = {}

        def probe(plan_doc, outputs):
            return True

        def broker_time_ms(plan_doc, outputs):
            # Must be read after Debezium is at zero tasks. The first read is
            # the load version; later ones time the checks after the resume.
            seen.setdefault('desired_at_read', [x for _c, _s, x in ecs.updated_services])
            if isinstance(broker_ms, Exception):
                raise broker_ms
            return broker_ms
        probe.broker_time_ms = broker_time_ms
        events = []
        cfn = _handoff_cfn()
        if tables:
            cfn.outputs['ecdl-checkout-loader'] = dict(LOADER_OUTPUTS, Tables=tables)
        deployer(cfn, ecs, events, probe=probe).run(_handoff_plan())
        envs = [{e['name']: e['value'] for e in t['overrides']['containerOverrides'][0]['environment']}
                for t in ecs.run_tasks]
        return envs, events, seen

    def test_every_load_task_gets_the_pause_time_as_a_stream_id(self):
        envs, events, seen = self._run(1794882952638, tables='orders,customers')
        self.assertEqual([e['LOAD_VERSION'] for e in envs], ['1794882952638-0'] * 2)
        self.assertEqual([e['TABLE'] for e in envs], ['orders', 'customers'])
        self.assertEqual(seen['desired_at_read'], [0])
        stamped = [e for e in events if e['event'] == 'load_version']
        self.assertEqual(stamped[0]['version'], '1794882952638-0')

    def test_every_load_task_replaces_and_sweeps(self):
        # Jira story F072: a merging reload left removed fields and deleted rows behind.
        envs, _events, _seen = self._run(1794882952638, tables='orders,customers')
        self.assertEqual([e.get('REPLACE_EXISTING') for e in envs], ['true', 'true'])

    def test_unreadable_broker_clock_loads_without_a_version(self):
        for answer in (None, RuntimeError('timeout')):
            envs, events, _ = self._run(answer)
            self.assertTrue(envs)
            self.assertTrue(all('LOAD_VERSION' not in e for e in envs), envs)
            note = [e for e in events if e['event'] == 'load_version'][0]
            self.assertIsNone(note['version'])
            self.assertIn('without a version', note['note'])

    def test_a_probe_without_a_clock_loads_without_a_version(self):
        ecs = FakeECS(clusters=['ecdl-checkout'],
                      running={'ecdl-checkout-cdc-debezium': [0, 1]})
        deployer(_handoff_cfn(), ecs).run(_handoff_plan())
        env = {e['name']: e['value'] for e in
               ecs.run_tasks[0]['overrides']['containerOverrides'][0]['environment']}
        # No LOAD_VERSION; every load replaces (Jira story F072).
        self.assertEqual(set(env), {'TABLE', 'REPLACE_EXISTING'})

    def test_a_load_only_link_has_no_pause_and_no_version(self):
        cfn = FakeCloudFormation(
            states={'ecdl-checkout-loader': ['CREATE_COMPLETE', 'CREATE_COMPLETE']},
            outputs={'ecdl-checkout-loader': LOADER_OUTPUTS})
        ecs = FakeECS(clusters=['ecdl-checkout'])
        deployer(cfn, ecs).run(add_load_step(plan(resolution('initial_load'))))
        env = {e['name'] for e in ecs.run_tasks[0]['overrides']['containerOverrides'][0]['environment']}
        self.assertEqual(env, {'TABLE', 'REPLACE_EXISTING'})


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

    def test_broker_time_is_read_in_milliseconds(self):
        class Secrets(object):
            def get_secret_value(self, SecretId):
                return {'SecretString': 'tok'}

        class Conn(object):
            def __init__(self, fail):
                self.fail = fail

            def time(self):
                if self.fail:
                    raise OSError('timed out')
                return (1794882952, 638123)

        for fail, want in ((False, 1794882952638), (True, None)):
            probe = broker_offset_probe(Secrets(), connect=lambda *a, **k: Conn(fail))
            self.assertEqual(probe.broker_time_ms(_handoff_plan(), {'broker': BROKER_OUTPUTS}), want)
        probe = broker_offset_probe(Secrets(), connect=lambda *a, **k: Conn(False))
        self.assertIsNone(probe.broker_time_ms(_handoff_plan(), {}))


class CaptureStreamingTest(unittest.TestCase):
    """Jira story F058: a start or resume is only a success once Debezium writes a
    heartbeat after it. A RUNNING task can still be crash-looping."""

    START = 1_800_000_000_000

    def probe(self, heartbeats, now=None):
        """heartbeats: successive heartbeat_ms answers (last one repeats)."""
        answers = list(heartbeats)

        def probe(plan_doc, outputs):
            return True

        def heartbeat_ms(plan_doc, outputs):
            value = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(value, Exception):
                raise value
            return value
        probe.broker_time_ms = lambda p, o: self.START if now is None else now
        probe.heartbeat_ms = heartbeat_ms
        return probe

    def run_handoff(self, probe, wait=5):
        ecs = FakeECS(clusters=['ecdl-checkout'],
                      running={'ecdl-checkout-cdc-debezium': [0, 1]})
        events, sleeps = [], []
        d = Deployer(Clients(cloudformation=_handoff_cfn(), ecs=ecs),
                     template_body=lambda name: 'T', on_progress=events.append,
                     poll_seconds=1, sleep=sleeps.append, offset_probe=probe,
                     capture_wait_seconds=wait)
        try:
            result, err = d.run(_handoff_plan()), None
        except DeployError as exc:
            result, err = None, exc
        return d, ecs, [e['event'] for e in events], result, err

    def test_resume_waits_for_a_heartbeat_newer_than_the_resume(self):
        old, new = self.START - 60_000, self.START + 900
        d, _ecs, events, result, err = self.run_handoff(self.probe([old, old, new]))
        self.assertIsNone(err)
        self.assertTrue(result['capture_verified'])
        self.assertEqual(events.count('capture_waiting'), 2)
        self.assertEqual(events[-2:], ['capture_streaming', 'plan_complete'])

    def test_a_heartbeat_at_the_resume_instant_is_not_newer(self):
        _d, _ecs, events, _result, err = self.run_handoff(self.probe([self.START]), wait=2)
        self.assertIsNotNone(err)

    def test_no_heartbeat_fails_the_deploy_with_capture_left_running(self):
        d, ecs, events, result, err = self.run_handoff(self.probe([self.START - 60_000]), wait=3)
        self.assertIsNotNone(err)
        self.assertEqual(err.step, 'resume_capture')
        self.assertIn('no heartbeat', str(err))
        self.assertIn('Debezium log group', err.hint)
        # Capture was resumed once and not "resumed again" by the failure path.
        self.assertEqual([x for _c, _s, x in ecs.updated_services], [0, 1])
        self.assertNotIn('capture_resuming_after_failure', events)
        self.assertEqual(d.capture_state, 'running')
        self.assertFalse(d.capture_verified)

    def test_no_heartbeat_stream_at_all_also_fails(self):
        _d, _ecs, _events, _result, err = self.run_handoff(self.probe([0]), wait=2)
        self.assertIn('no heartbeat', str(err))

    def test_an_unreadable_broker_is_unverified_not_failed(self):
        d, _ecs, events, result, err = self.run_handoff(self.probe([None]), wait=3)
        self.assertIsNone(err)
        self.assertFalse(result['capture_verified'])
        self.assertIn('capture_unverified', events)

    def test_a_reader_that_raises_is_unreadable(self):
        _d, _ecs, events, result, err = self.run_handoff(
            self.probe([RuntimeError('boom')]), wait=2)
        self.assertIsNone(err)
        self.assertIn('capture_unverified', events)

    def test_a_broker_clock_that_cannot_be_read_is_unverified(self):
        probe = self.probe([self.START + 5])
        probe.broker_time_ms = lambda p, o: None
        _d, _ecs, events, result, err = self.run_handoff(probe)
        self.assertIsNone(err)
        self.assertFalse(result['capture_verified'])

    def test_an_old_probe_without_a_heartbeat_reader_is_unverified(self):
        _d, _ecs, events, result, _err = self.run_handoff(lambda p, o: True)
        self.assertFalse(result['capture_verified'])
        self.assertIn('capture_unverified', events)

    def test_a_cdc_only_first_deploy_is_checked_after_the_stack(self):
        cfn = FakeCloudFormation(
            states={'ecdl-checkout-broker': ['CREATE_COMPLETE', 'CREATE_COMPLETE'],
                    'ecdl-checkout-cdc': ['CREATE_COMPLETE', 'CREATE_COMPLETE']},
            outputs={'ecdl-checkout-broker': BROKER_OUTPUTS, 'ecdl-checkout-cdc': {}})
        events = []
        d = Deployer(Clients(cloudformation=cfn, ecs=FakeECS(clusters=['ecdl-checkout'])),
                     template_body=lambda name: 'T', on_progress=events.append,
                     poll_seconds=1, sleep=lambda _s: None,
                     offset_probe=self.probe([self.START + 1]), capture_wait_seconds=3)
        result = d.run(plan(resolution('cdc')))
        self.assertTrue(result['capture_verified'])
        names = [e['event'] for e in events]
        # One check for the plan; recording OffsetCommitted (Jira story F005) replaces
        # the task, and that is checked again after it.
        before = names[:names.index('offset_recording')]
        self.assertEqual(before.count('capture_streaming'), 1)
        streaming = next(e for e in events if e['event'] == 'capture_streaming')
        self.assertEqual(streaming['step'], 'cdc')
        self.assertEqual(names[names.index('offset_recording'):],
                         ['offset_recording', 'offset_recorded', 'capture_streaming',
                          'plan_complete'])

    def test_the_initial_load_first_deploy_checks_only_at_the_resume(self):
        events = []
        d = Deployer(Clients(cloudformation=_handoff_cfn(),
                             ecs=FakeECS(clusters=['ecdl-checkout'],
                                         running={'ecdl-checkout-cdc-debezium': [0, 1]})),
                     template_body=lambda name: 'T', on_progress=events.append,
                     poll_seconds=1, sleep=lambda _s: None,
                     offset_probe=self.probe([self.START + 1]), capture_wait_seconds=3)
        d.run(_handoff_plan())
        names = [e['event'] for e in events]
        steps = [e['step'] for e in events[:names.index('offset_recording')]
                 if e['event'] == 'capture_streaming']
        self.assertEqual(steps, ['resume_capture'])

    def test_an_update_checks_capture_whether_it_resumed_it_or_not(self):
        import copy
        from ecdl_link.tests.test_deploy import UpdateKeepsCaptureTest
        for desired, wants_resume in ((1, False), (0, True)):
            cfn = FakeCloudFormation(existing={'ecdl-checkout-cdc': 'UPDATE_COMPLETE'})
            ecs = FakeECS(clusters=['ecdl-checkout'],
                          desired={'ecdl-checkout-cdc-debezium': desired},
                          running={'ecdl-checkout-cdc-debezium': [0, 1, 1]})
            events = []
            d = Deployer(Clients(cloudformation=cfn, ecs=ecs),
                         template_body=lambda name: 'T', on_progress=events.append,
                         poll_seconds=1, sleep=lambda _s: None,
                         offset_probe=self.probe([self.START - 5, self.START + 5]),
                         capture_wait_seconds=3)
            result = d.run(copy.deepcopy(UpdateKeepsCaptureTest.PLAN))
            names = [e['event'] for e in events]
            self.assertTrue(result['capture_verified'], desired)
            self.assertEqual('capture_resumed' in names, wants_resume)
            self.assertIn('capture_streaming', names)


class HeartbeatProbeTest(unittest.TestCase):
    def _probe(self, reply=None, error=None, plan_doc=None):
        class Secrets(object):
            def get_secret_value(self, SecretId):
                return {'SecretString': 'tok'}
        seen = {}

        class Conn(object):
            def xrevrange(self, name, max='+', min='-', count=None):
                seen.update(name=name, max=max, min=min, count=count)
                if error:
                    raise error
                return reply
        probe = broker_offset_probe(Secrets(), connect=lambda *a, **k: Conn())
        return probe.heartbeat_ms(plan_doc or _handoff_plan(), {'broker': BROKER_OUTPUTS}), seen

    def test_reads_the_newest_heartbeat_of_this_link(self):
        got, seen = self._probe([(b'1800000000123-4', {b'k': b'v'})])
        self.assertEqual(got, 1800000000123)
        self.assertEqual(seen, {'name': 'data:__debezium-heartbeat.checkout',
                                'max': '+', 'min': '-', 'count': 1})

    def test_no_heartbeat_is_zero_and_unreadable_is_none(self):
        self.assertEqual(self._probe([])[0], 0)
        self.assertIsNone(self._probe(error=OSError('timed out'))[0])
        self.assertIsNone(self._probe([(b'garbage', {})])[0])

    def test_the_stream_matches_the_cdc_template(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, '..', '..', 'infra', 'ecdl-cdc-ecs.yaml')) as handle:
            text = handle.read()
        from ecdl_link.deploy import heartbeat_stream
        self.assertEqual(heartbeat_stream('${TopicPrefix}'), 'data:__debezium-heartbeat.${TopicPrefix}')
        self.assertIn("CAPTURE_HEARTBEAT_STREAM, Value: !Sub '{}'".format(
            heartbeat_stream('${TopicPrefix}')), text)


if __name__ == '__main__':
    unittest.main()
