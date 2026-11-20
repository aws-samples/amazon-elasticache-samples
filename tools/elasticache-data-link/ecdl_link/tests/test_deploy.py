"""Tests for the deployment executor, with hand-written AWS fakes.

The cases worth covering are all failure shapes -- a stack stuck in
ROLLBACK_COMPLETE, an unchanged stack, a load task that exits non-zero, a task
that never ran at all -- and none of them should need an account to reproduce.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.deploy import (  # noqa: E402
    Clients, DeployError, Deployer, add_load_step, compact_template,
)
from ecdl_link.provision import plan  # noqa: E402
from ecdl_link.tests.test_provision import resolution  # noqa: E402


class FakeCloudFormation(object):
    """Enough CloudFormation to walk a plan.

    Stacks move through a scripted sequence of states so waiting can be tested
    without real time passing.
    """

    def __init__(self, existing=None, outputs=None, states=None, fail=None):
        self.existing = dict(existing or {})     # name -> current status
        self.outputs = dict(outputs or {})       # name -> {key: value}
        self.states = dict(states or {})         # name -> [statuses to serve]
        self.fail = set(fail or [])              # names whose update raises
        self.created = []
        self.updated = []
        self.deleted = []
        self.events = {}
        # name -> {key: value}: what each stack holds, as create/update left it.
        self.params = {}
        self.tags = {}                           # name -> [{'Key', 'Value'}]

    def describe_stacks(self, StackName=None, **_):
        # A stack that has neither been created nor declared pre-existing MUST
        # raise "does not exist". An earlier version of this fake served a state
        # for an uncreated stack, so the deployer took the update path and the
        # create path was never exercised -- the same way a fake that returned too much hid
        # a real bug in resolve.py's tests.
        if StackName not in self.existing:
            raise Exception('Stack with id {} does not exist'.format(StackName))
        pending = self.states.get(StackName)
        if pending:
            self.existing[StackName] = pending.pop(0)
        stack = {
            'StackStatus': self.existing[StackName],
            'Outputs': [{'OutputKey': k, 'OutputValue': v}
                        for k, v in (self.outputs.get(StackName) or {}).items()],
            'Tags': list(self.tags.get(StackName) or []),
        }
        if StackName in self.params:
            stack['Parameters'] = [{'ParameterKey': k, 'ParameterValue': v}
                                   for k, v in sorted(self.params[StackName].items())]
        return {'Stacks': [stack]}

    def create_stack(self, StackName=None, Parameters=None, **kw):
        self.created.append({'name': StackName, 'parameters': Parameters, **kw})
        self.existing[StackName] = 'CREATE_IN_PROGRESS'
        self.params[StackName] = {p['ParameterKey']: p.get('ParameterValue')
                                  for p in Parameters or []}
        self.tags[StackName] = list(kw.get('Tags') or [])
        return {'StackId': 'id-' + StackName}

    def update_stack(self, StackName=None, Parameters=None, **kw):
        if StackName in self.fail:
            raise Exception('An error occurred: No updates are to be performed.')
        self.updated.append({'name': StackName, 'parameters': Parameters, **kw})
        held = self.params.setdefault(StackName, {})
        for p in Parameters or []:
            if not p.get('UsePreviousValue'):
                held[p['ParameterKey']] = p.get('ParameterValue')
        if 'Tags' in kw:
            self.tags[StackName] = list(kw['Tags'] or [])
        return {'StackId': 'id-' + StackName}

    def describe_stack_events(self, StackName=None, **_):
        return {'StackEvents': self.events.get(StackName, [])}

    def delete_stack(self, StackName=None, **_):
        # Deleting removes it from `existing`, so the next describe_stacks raises
        # "does not exist" -- which is exactly what the deployer waits for, and
        # what then sends it down the CREATE path rather than the UPDATE path.
        self.deleted.append(StackName)
        self.existing.pop(StackName, None)
        # `states` is deliberately LEFT ALONE. It is the script for what happens
        # next, and the interesting case is what the stack does after being
        # recreated -- dropping it here left the recreated stack stuck in
        # CREATE_IN_PROGRESS forever, which hung the suite instead of failing it.


class FakeECS(object):
    def __init__(self, clusters=None, running=None, tasks=None, desired=None,
                 loader_tasks=None):
        self.clusters = list(clusters or [])
        self.running = dict(running or {})     # service -> [running counts]
        self.tasks = list(tasks or [])         # scripted describe_tasks replies
        self.desired = dict(desired or {})     # service -> desiredCount
        self.loader_tasks = dict(loader_tasks or {})   # family -> [task arns]
        self.service_tasks = {}                         # service -> [task arns]
        self.listed = []
        self.stopped_tasks = []
        self.created_clusters = []
        self.updated_services = []
        self.run_tasks = []

    def describe_clusters(self, clusters=None, **_):
        return {'clusters': [{'clusterName': c, 'status': 'ACTIVE'}
                             for c in clusters if c in self.clusters]}

    def create_cluster(self, clusterName=None, **_):
        self.created_clusters.append(clusterName)
        self.clusters.append(clusterName)
        return {}

    def update_service(self, cluster=None, service=None, desiredCount=None, **_):
        self.updated_services.append((cluster, service, desiredCount))
        return {}

    def describe_services(self, cluster=None, services=None, **_):
        service = services[0]
        seq = self.running.get(service)
        count = seq.pop(0) if seq else 0
        return {'services': [{'serviceName': service, 'runningCount': count,
                              'desiredCount': self.desired.get(service, 1),
                              'status': 'ACTIVE'}]}

    def list_tasks(self, cluster=None, family=None, desiredStatus=None, serviceName=None, **_):
        if serviceName:
            return {'taskArns': list(self.service_tasks.get(serviceName) or [])}
        self.listed.append((cluster, family, desiredStatus))
        return {'taskArns': list(self.loader_tasks.get(family) or [])}

    def stop_task(self, cluster=None, task=None, reason=None):
        self.stopped_tasks.append(task)
        return {}

    def run_task(self, cluster=None, taskDefinition=None, overrides=None, **kw):
        self.run_tasks.append({'cluster': cluster, 'taskDefinition': taskDefinition,
                               'overrides': overrides, **kw})
        return {'tasks': [{'taskArn': 'arn:task/{}'.format(len(self.run_tasks))}],
                'failures': []}

    def describe_tasks(self, cluster=None, tasks=None, **_):
        return self.tasks.pop(0) if self.tasks else {
            'tasks': [{'lastStatus': 'STOPPED', 'containers': [{'exitCode': 0}]}]}


def stopped(exit_code=0, reason=None):
    container = {'exitCode': exit_code} if exit_code is not None else {}
    task = {'lastStatus': 'STOPPED', 'containers': [container]}
    if reason:
        task['stoppedReason'] = reason
    return {'tasks': [task]}


BROKER_OUTPUTS = {
    'PrimaryEndpoint': 'broker.abc.cache.amazonaws.com',
    'Port': '6379',
    'SecurityGroupId': 'sg-broker',
    'AuthSecretArn': 'arn:aws:secretsmanager:::secret:broker-tok',
    'TransitEncryption': 'true',
    'ReplicationGroupId': 'ecdl-checkout-broker',
}

LOADER_OUTPUTS = {
    'TaskDefinitionArn': 'arn:aws:ecs:::task-definition/ecdl-checkout-loader:1',
    'TaskSecurityGroupId': 'sg-loader',
    'Subnets': 'subnet-a,subnet-b',
    'AssignPublicIp': 'DISABLED',
    'ClusterName': 'ecdl-checkout',
    'Tables': 'orders,customers',
}


def confirmed_offset(plan_doc, outputs):
    """An offset probe that has seen Debezium's committed position."""
    return True


def deployer(cfn, ecs, events=None, probe=confirmed_offset):
    return Deployer(Clients(cloudformation=cfn, ecs=ecs),
                    template_body=lambda name: 'Template: ' + name,
                    on_progress=(events.append if events is not None else None),
                    poll_seconds=0, sleep=lambda _s: None, offset_probe=probe)


class CdcDeployTest(unittest.TestCase):
    def setUp(self):
        self.cfn = FakeCloudFormation(
            states={'ecdl-checkout-broker': ['CREATE_COMPLETE', 'CREATE_COMPLETE'],
                    'ecdl-checkout-cdc': ['CREATE_COMPLETE', 'CREATE_COMPLETE']},
            outputs={'ecdl-checkout-broker': BROKER_OUTPUTS,
                     'ecdl-checkout-cdc': {'SinkService': 'ecdl-checkout-cdc-sink'}})
        self.ecs = FakeECS()
        self.events = []
        self.result = deployer(self.cfn, self.ecs, self.events).run(
            plan(resolution('cdc')))

    def test_both_stacks_created_in_order(self):
        self.assertEqual([c['name'] for c in self.cfn.created],
                         ['ecdl-checkout-broker', 'ecdl-checkout-cdc'])

    def test_cluster_created_before_the_stacks(self):
        # The templates take an EXISTING cluster, so nothing can deploy without it.
        self.assertEqual(self.ecs.created_clusters, ['ecdl-checkout'])
        self.assertEqual(self.events[1]['event'], 'cluster_created')

    def test_broker_outputs_wired_into_the_cdc_stack(self):
        cdc = next(c for c in self.cfn.created if c['name'] == 'ecdl-checkout-cdc')
        params = {p['ParameterKey']: p['ParameterValue'] for p in cdc['parameters']}
        self.assertEqual(params['BrokerHost'], 'broker.abc.cache.amazonaws.com')
        self.assertEqual(params['BrokerSecretArn'],
                         'arn:aws:secretsmanager:::secret:broker-tok')
        self.assertEqual(params['BrokerSSL'], 'true')

    def test_capabilities_passed_where_roles_are_created(self):
        cdc = next(c for c in self.cfn.created if c['name'] == 'ecdl-checkout-cdc')
        self.assertEqual(cdc['Capabilities'], ['CAPABILITY_IAM'])

    def test_reports_completion(self):
        self.assertEqual(self.result['completed'], ['broker', 'cdc'])

    def test_on_failure_do_nothing(self):
        # Not ROLLBACK: a rolled-back stack lands in ROLLBACK_COMPLETE, which
        # cannot be updated, so the next attempt has to delete it first. Leaving
        # the failed resources lets the reason be read from the stack.
        for created in self.cfn.created:
            self.assertEqual(created['OnFailure'], 'DO_NOTHING')


class IdempotenceTest(unittest.TestCase):
    def test_existing_stack_is_updated_not_recreated(self):
        cfn = FakeCloudFormation(
            existing={'ecdl-checkout-broker': 'CREATE_COMPLETE',
                      'ecdl-checkout-cdc': 'CREATE_COMPLETE'},
            outputs={'ecdl-checkout-broker': BROKER_OUTPUTS,
                     'ecdl-checkout-cdc': {}},
            states={'ecdl-checkout-broker': ['CREATE_COMPLETE', 'UPDATE_COMPLETE',
                                             'UPDATE_COMPLETE'],
                    'ecdl-checkout-cdc': ['CREATE_COMPLETE', 'UPDATE_COMPLETE',
                                          'UPDATE_COMPLETE']})
        deployer(cfn, FakeECS(clusters=['ecdl-checkout'])).run(plan(resolution('cdc')))
        self.assertEqual(cfn.created, [])
        self.assertEqual([u['name'] for u in cfn.updated],
                         ['ecdl-checkout-broker', 'ecdl-checkout-cdc'])

    def test_no_changes_is_success_not_failure(self):
        # "No updates are to be performed" means the stack already matches.
        # Treating it as an error would make redeploying an unchanged link
        # impossible, which is exactly what a repeatable deploy needs to allow.
        cfn = FakeCloudFormation(
            existing={'ecdl-checkout-broker': 'CREATE_COMPLETE',
                      'ecdl-checkout-cdc': 'CREATE_COMPLETE'},
            outputs={'ecdl-checkout-broker': BROKER_OUTPUTS, 'ecdl-checkout-cdc': {}},
            fail=['ecdl-checkout-broker', 'ecdl-checkout-cdc'])
        events = []
        result = deployer(cfn, FakeECS(clusters=['ecdl-checkout']), events).run(
            plan(resolution('cdc')))
        self.assertEqual(result['completed'], ['broker', 'cdc'])
        self.assertEqual([e['event'] for e in events].count('stack_unchanged'), 2)

    def test_unchanged_stack_still_yields_its_outputs(self):
        # The dependent stack needs the broker's endpoint whether or not the
        # broker stack changed this time.
        cfn = FakeCloudFormation(
            existing={'ecdl-checkout-broker': 'CREATE_COMPLETE',
                      'ecdl-checkout-cdc': 'CREATE_COMPLETE'},
            outputs={'ecdl-checkout-broker': BROKER_OUTPUTS, 'ecdl-checkout-cdc': {}},
            fail=['ecdl-checkout-broker'],
            states={'ecdl-checkout-cdc': ['CREATE_COMPLETE', 'UPDATE_COMPLETE',
                                          'UPDATE_COMPLETE']})
        deployer(cfn, FakeECS(clusters=['ecdl-checkout'])).run(plan(resolution('cdc')))
        cdc = next(u for u in cfn.updated if u['name'] == 'ecdl-checkout-cdc')
        params = {p['ParameterKey']: p['ParameterValue'] for p in cdc['parameters']}
        self.assertEqual(params['BrokerHost'], 'broker.abc.cache.amazonaws.com')


class FailureTest(unittest.TestCase):
    def test_rollback_complete_is_recycled_rather_than_refused(self):
        # BEHAVIOUR CHANGED DELIBERATELY. This used to raise, naming the remedy
        # ("delete it and deploy again") because CloudFormation's own error is an
        # opaque ValidationError. Stating the remedy was better than the raw error,
        # but it still made a person perform the only possible next action manually
        # between attempts -- and a first deploy often takes several
        # attempts. A stack in ROLLBACK_COMPLETE never finished creating, so there
        # is no need to decide what might be lost.
        #
        # The remedy is still named, but only when deleting genuinely fails; see
        # RecycleFailedStackTest.
        cfn = FakeCloudFormation(
            existing={'ecdl-checkout-broker': 'ROLLBACK_COMPLETE'},
            outputs={'ecdl-checkout-broker': BROKER_OUTPUTS,
                     'ecdl-checkout-cdc': {'SinkService': 'ecdl-checkout-cdc-sink'}},
            states={'ecdl-checkout-broker': ['ROLLBACK_COMPLETE',
                                             'CREATE_COMPLETE'],
                    'ecdl-checkout-cdc': ['CREATE_COMPLETE']})
        events = []
        deployer(cfn, FakeECS(clusters=['ecdl-checkout']), events).run(
            plan(resolution('cdc')))
        self.assertIn('ecdl-checkout-broker', cfn.deleted)
        self.assertIn('ecdl-checkout-broker', [c['name'] for c in cfn.created])
        self.assertIn('stack_recycled', [e['event'] for e in events])

    def test_failed_stack_reports_the_first_real_reason(self):
        cfn = FakeCloudFormation(
            states={'ecdl-checkout-broker': ['CREATE_FAILED']})
        cfn.events['ecdl-checkout-broker'] = [
            {'LogicalResourceId': 'BrokerCluster',
             'ResourceStatus': 'CREATE_FAILED',
             'ResourceStatusReason': 'Resource creation cancelled'},
            {'LogicalResourceId': 'BrokerAuthSecret',
             'ResourceStatus': 'CREATE_FAILED',
             'ResourceStatusReason': 'already scheduled for deletion'},
        ]
        with self.assertRaises(DeployError) as caught:
            deployer(cfn, FakeECS(clusters=['ecdl-checkout'])).run(plan(resolution('cdc')))
        # The earliest FAILED with a real reason, not the "cancelled" noise.
        self.assertIn('BrokerAuthSecret', caught.exception.hint)
        self.assertNotIn('cancelled', caught.exception.hint)

    def test_missing_output_is_named(self):
        cfn = FakeCloudFormation(
            states={'ecdl-checkout-broker': ['CREATE_COMPLETE', 'CREATE_COMPLETE']},
            outputs={'ecdl-checkout-broker': {'PrimaryEndpoint': 'x', 'Port': '6379'}})
        with self.assertRaises(DeployError) as caught:
            deployer(cfn, FakeECS(clusters=['ecdl-checkout'])).run(plan(resolution('cdc')))
        self.assertIn('SecurityGroupId', str(caught.exception))

    def test_timeout_says_it_may_still_be_running(self):
        cfn = FakeCloudFormation(existing={'ecdl-checkout-broker': 'CREATE_IN_PROGRESS'})
        d = Deployer(Clients(cloudformation=cfn, ecs=FakeECS(clusters=['ecdl-checkout'])),
                     template_body=lambda n: 'T', poll_seconds=1,
                     timeout_seconds=3, sleep=lambda _s: None)
        with self.assertRaises(DeployError) as caught:
            d.run(plan(resolution('cdc')))
        self.assertIn('did not finish', str(caught.exception))


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.cfn = FakeCloudFormation(
            states={n: ['CREATE_COMPLETE', 'CREATE_COMPLETE']
                    for n in ('ecdl-checkout-broker', 'ecdl-checkout-cdc',
                              'ecdl-checkout-loader')},
            outputs={'ecdl-checkout-broker': BROKER_OUTPUTS,
                     'ecdl-checkout-cdc': {},
                     'ecdl-checkout-loader': LOADER_OUTPUTS})
        self.ecs = FakeECS(
            clusters=['ecdl-checkout'],
            running={'ecdl-checkout-cdc-debezium': [0, 1]})
        self.plan = add_load_step(plan(resolution('initial_load_and_cdc')))
        self.events = []
        self.result = deployer(self.cfn, self.ecs, self.events).run(self.plan)

    def test_capture_paused_then_resumed_around_the_load(self):
        # The ordering invariant. Without the pause, CDC can write a fresh value
        # and the loader can then overwrite it with the older one it read earlier.
        desired = [(s, d) for _c, s, d in self.ecs.updated_services]
        self.assertEqual(desired, [('ecdl-checkout-cdc-debezium', 0),
                                   ('ecdl-checkout-cdc-debezium', 1)])

    def test_load_runs_between_the_pause_and_the_resume(self):
        order = [e for e in (ev.get('step') for ev in self.events) if e]
        self.assertLess(order.index('pause_capture'), order.index('run_load'))
        self.assertLess(order.index('run_load'), order.index('resume_capture'))

    def test_one_run_per_table_with_the_table_overridden(self):
        # One run loads one table, so the table has to be a per-run override.
        self.assertEqual(len(self.ecs.run_tasks), 2)
        overrides = [t['overrides']['containerOverrides'][0]['environment'][0]['value']
                     for t in self.ecs.run_tasks]
        self.assertEqual(overrides, ['orders', 'customers'])

    def test_load_uses_the_loader_stacks_network(self):
        net = self.ecs.run_tasks[0]['networkConfiguration']['awsvpcConfiguration']
        self.assertEqual(net['subnets'], ['subnet-a', 'subnet-b'])
        self.assertEqual(net['securityGroups'], ['sg-loader'])
        self.assertEqual(net['assignPublicIp'], 'DISABLED')

    def test_every_step_completed(self):
        self.assertEqual(self.result['completed'],
                         ['broker', 'cdc', 'loader', 'pause_capture', 'run_load',
                          'resume_capture'])


class LoadFailureTest(unittest.TestCase):
    def _run(self, task_replies):
        cfn = FakeCloudFormation(
            states={'ecdl-checkout-loader': ['CREATE_COMPLETE', 'CREATE_COMPLETE']},
            outputs={'ecdl-checkout-loader': LOADER_OUTPUTS})
        ecs = FakeECS(clusters=['ecdl-checkout'], tasks=task_replies)
        p = add_load_step(plan(resolution('initial_load')))
        return deployer(cfn, ecs).run(p)

    def test_nonzero_exit_fails_the_deploy(self):
        # A failed load is not a partial success: CDC only carries CHANGES, so the
        # rows the load missed are never sent and the cache stays wrong.
        with self.assertRaises(DeployError) as caught:
            self._run([stopped(exit_code=1)])
        self.assertIn('exited 1', str(caught.exception))
        self.assertIsNotNone(caught.exception.hint)

    def test_container_that_never_ran_is_not_success(self):
        # No exit code means an image pull failure or a scheduler stop. Reading a
        # missing code as zero would report a load that never happened.
        with self.assertRaises(DeployError) as caught:
            self._run([stopped(exit_code=None, reason='CannotPullContainerError')])
        self.assertIn('without running', str(caught.exception))
        self.assertIn('CannotPullContainerError', str(caught.exception))

    def test_second_table_failing_fails_the_deploy(self):
        with self.assertRaises(DeployError):
            self._run([stopped(0), stopped(2)])

    def test_all_tables_succeeding_succeeds(self):
        result = self._run([stopped(0), stopped(0)])
        self.assertIn('run_load', result['completed'])


class LoadFailureMessageTest(unittest.TestCase):
    """Jira story F084: a failed load names its log stream, its last error and the
    command that runs it again (there is no `ecdl load`)."""

    class FakeLogs(object):
        def __init__(self, pages):
            self.pages, self.calls = list(pages), []

        def filter_log_events(self, **kw):
            self.calls.append(kw)
            return self.pages.pop(0) if self.pages else {'events': []}

    def _run(self, task_replies, logs=None, outputs=None):
        cfn = FakeCloudFormation(
            states={'ecdl-checkout-loader': ['CREATE_COMPLETE', 'CREATE_COMPLETE']},
            outputs={'ecdl-checkout-loader': dict(LOADER_OUTPUTS, **(outputs or {}))})
        ecs = FakeECS(clusters=['ecdl-checkout'], tasks=task_replies)
        d = Deployer(Clients(cloudformation=cfn, ecs=ecs, logs=logs),
                     template_body=lambda name: 'Template: ' + name,
                     poll_seconds=0, sleep=lambda _s: None, offset_probe=confirmed_offset)
        with self.assertRaises(DeployError) as caught:
            d.run(add_load_step(plan(resolution('initial_load'))))
        return caught.exception

    def test_one_logs_client_for_load_errors_and_teardown(self):
        # deploy (Jira story F084) and teardown (Jira story F140) both read Clients.logs(): the
        # injected client, built once
        fake = self.FakeLogs([])
        c = Clients(cloudformation=FakeCloudFormation(), ecs=FakeECS(), logs=fake)
        self.assertIs(c.logs(), fake)
        self.assertIs(c.logs(), fake)

    def test_no_log_group_reads_no_log(self):
        logs = self.FakeLogs([])
        exc = self._run([stopped(exit_code=1)], logs=logs)
        self.assertIn('exited 1', str(exc))
        self.assertEqual(logs.calls, [])

    def test_the_last_error_and_the_stream_are_named(self):
        logs = self.FakeLogs([
            {'events': [{'message': 'ERROR chunk 3 [1..9] failed after 6 attempt(s): x'}],
             'nextToken': 't1'},
            {'events': [{'message': 'FATAL: orders will not fit in the target cache: '
                                    'it needs about 9.00 GB'}]}])
        exc = self._run([stopped(exit_code=1)], logs=logs,
                        outputs={'LogGroup': '/ecs/ecdl-checkout-loader'})
        text = str(exc)
        self.assertIn('exited 1 (the loader stopped on an error)', text)
        self.assertIn('FATAL: orders will not fit', text)
        self.assertIn('loader/loader/1', exc.hint)
        self.assertIn('/ecs/ecdl-checkout-loader', exc.hint)
        self.assertIn('ecdl apply --reload', exc.hint)
        self.assertNotIn('ecdl load', exc.hint)
        self.assertEqual(logs.calls[0]['logStreamNames'], ['loader/loader/1'])
        self.assertEqual(logs.calls[1]['nextToken'], 't1')

    def test_an_out_of_memory_kill_says_so(self):
        reply = stopped(exit_code=137,
                        reason='OutOfMemoryError: Container killed due to memory usage')
        exc = self._run([reply])
        self.assertIn('exited 137 (the container was killed: out of memory', str(exc))
        self.assertIn('OutOfMemoryError', str(exc))

    def test_without_logs_the_message_still_names_the_stream(self):
        exc = self._run([stopped(exit_code=78)])
        self.assertIn('configuration is wrong', str(exc))
        self.assertIn('loader/loader/1', exc.hint)

    def test_unreadable_logs_do_not_hide_the_failure(self):
        class Broken(object):
            def filter_log_events(self, **kw):
                raise RuntimeError('AccessDenied')
        exc = self._run([stopped(exit_code=1)], logs=Broken(),
                        outputs={'LogGroup': '/ecs/x'})
        self.assertIn('exited 1', str(exc))

    def test_the_resume_hint_after_a_failure_names_the_command(self):
        import ecdl_link.deploy as deploy
        with open(deploy.__file__) as handle:
            self.assertNotIn('`ecdl load`', handle.read())


class InitialLoadOnlyTest(unittest.TestCase):
    def test_no_broker_and_no_capture_actions(self):
        cfn = FakeCloudFormation(
            states={'ecdl-checkout-loader': ['CREATE_COMPLETE', 'CREATE_COMPLETE']},
            outputs={'ecdl-checkout-loader': LOADER_OUTPUTS})
        ecs = FakeECS(clusters=['ecdl-checkout'])
        deployer(cfn, ecs).run(add_load_step(plan(resolution('initial_load'))))
        self.assertEqual([c['name'] for c in cfn.created], ['ecdl-checkout-loader'])
        self.assertEqual(ecs.updated_services, [])


class AddLoadStepTest(unittest.TestCase):
    def test_inserted_directly_after_the_loader_stack(self):
        p = add_load_step(plan(resolution('initial_load')))
        ids = [s['id'] for s in p['steps']]
        self.assertEqual(ids.index('run_load'), ids.index('loader') + 1)

    def test_after_the_pause_when_the_loader_stack_comes_first(self):
        # Jira story F086: the loader stack is created while capture still runs; only
        # the load itself sits inside the pause.
        p = add_load_step(plan(resolution('initial_load_and_cdc')))
        ids = [s['id'] for s in p['steps']]
        self.assertEqual(ids, ['broker', 'cdc', 'loader', 'pause_capture', 'run_load',
                               'resume_capture'])
        load = next(s for s in p['steps'] if s['id'] == 'run_load')
        self.assertEqual(load['depends_on'], ['loader', 'pause_capture'])

    def test_not_added_twice(self):
        p = add_load_step(add_load_step(plan(resolution('initial_load'))))
        self.assertEqual([s['id'] for s in p['steps']].count('run_load'), 1)

    def test_absent_when_there_is_no_loader(self):
        p = add_load_step(plan(resolution('cdc')))
        self.assertNotIn('run_load', [s['id'] for s in p['steps']])


class RecycleFailedStackTest(unittest.TestCase):
    """A stack that never finished creating is deleted and retried, not refused.

    Every failed first attempt used to leave a CREATE_FAILED stack holding the
    name, so the next attempt failed on the leftover rather than on anything real
    and someone had to delete it manually in between. A first deploy often
    takes several attempts, so each failed attempt added a manual delete step.
    """

    STEP = {'id': 'broker', 'name': 'ecdl-x-broker', 'kind': 'stack',
            'template': 'ecdl-broker-quickstart.yaml', 'parameters': {},
            'capabilities': [], 'purpose': 'the broker'}

    def test_a_create_failed_stack_is_deleted_then_created(self):
        cfn = FakeCloudFormation(
            existing={'ecdl-x-broker': 'CREATE_FAILED'},
            states={'ecdl-x-broker': ['CREATE_FAILED', 'CREATE_COMPLETE']},
            outputs={'ecdl-x-broker': {'Endpoint': 'b:6379'}})
        events = []
        d = deployer(cfn, FakeECS(), events)
        out = d._stack_step(dict(self.STEP), {})

        self.assertEqual(cfn.deleted, ['ecdl-x-broker'])
        # CREATED, not updated: the point of deleting it is that the name is free.
        self.assertEqual([c['name'] for c in cfn.created], ['ecdl-x-broker'])
        self.assertEqual(cfn.updated, [])
        self.assertEqual(out, {'Endpoint': 'b:6379'})

        kinds = [e['event'] for e in events]
        self.assertIn('stack_recycling', kinds)
        self.assertIn('stack_recycled', kinds)
        # The event order matters: recycled before creating.
        self.assertLess(kinds.index('stack_recycled'), kinds.index('stack_creating'))

    def test_rollback_complete_is_recycled_too(self):
        cfn = FakeCloudFormation(
            existing={'ecdl-x-broker': 'ROLLBACK_COMPLETE'},
            states={'ecdl-x-broker': ['ROLLBACK_COMPLETE', 'CREATE_COMPLETE']})
        events = []
        deployer(cfn, FakeECS(), events)._stack_step(dict(self.STEP), {})
        self.assertEqual(cfn.deleted, ['ecdl-x-broker'])

    def test_the_reason_is_reported_not_just_the_action(self):
        cfn = FakeCloudFormation(
            existing={'ecdl-x-broker': 'CREATE_FAILED'},
            states={'ecdl-x-broker': ['CREATE_FAILED', 'CREATE_COMPLETE']})
        events = []
        deployer(cfn, FakeECS(), events)._stack_step(dict(self.STEP), {})
        recycling = [e for e in events if e['event'] == 'stack_recycling'][0]
        self.assertEqual(recycling['state'], 'CREATE_FAILED')
        self.assertIn('nothing to preserve', recycling['reason'])

    def test_a_live_stack_is_updated_never_deleted(self):
        # Limiting deletion to these states keeps it safe: an UPDATE_ROLLBACK_COMPLETE stack
        # is still serving its previous version, so it must be updated in place.
        cfn = FakeCloudFormation(
            existing={'ecdl-x-broker': 'UPDATE_ROLLBACK_COMPLETE'},
            states={'ecdl-x-broker': ['UPDATE_ROLLBACK_COMPLETE',
                                      'UPDATE_COMPLETE']})
        deployer(cfn, FakeECS())._stack_step(dict(self.STEP), {})
        self.assertEqual(cfn.deleted, [])
        self.assertEqual([u['name'] for u in cfn.updated], ['ecdl-x-broker'])
        self.assertEqual(cfn.created, [])

    def test_a_completed_stack_is_updated_never_deleted(self):
        cfn = FakeCloudFormation(
            existing={'ecdl-x-broker': 'CREATE_COMPLETE'},
            states={'ecdl-x-broker': ['CREATE_COMPLETE', 'UPDATE_COMPLETE']})
        deployer(cfn, FakeECS())._stack_step(dict(self.STEP), {})
        self.assertEqual(cfn.deleted, [])
        self.assertEqual(len(cfn.updated), 1)


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=0).result
    print('ok: {} deploy tests passed'.format(result.testsRun)
          if result.wasSuccessful() else 'FAILED')
    sys.exit(0 if result.wasSuccessful() else 1)


class CompactTemplateTest(unittest.TestCase):
    """Comments are stripped before a template is sent, because CloudFormation
    caps inline TemplateBody at 51,200 bytes and the CDC template's comments
    alone pushed it past that. Stripping must never change the document."""

    INFRA = os.path.join(os.path.dirname(__file__), '..', '..', 'infra')

    @staticmethod
    def _load(text):
        """The document as plain data, keeping every CloudFormation !Tag.

        Built from yaml.compose (the parsed node tree), which constructs no
        Python objects at all, so it needs no custom loader and accepts the
        !Ref/!Sub/... tags that yaml.safe_load would reject.
        """
        import yaml

        def walk(node):
            if isinstance(node, yaml.MappingNode):
                value = {walk(k): walk(v) for k, v in node.value}
            elif isinstance(node, yaml.SequenceNode):
                value = [walk(v) for v in node.value]
            else:
                value = node.value
            return {node.tag: value} if node.tag.startswith('!') else value
        return walk(yaml.compose(text))

    def test_every_template_parses_to_the_same_document_and_fits(self):
        from ecdl_link.deploy import TEMPLATE_BODY_LIMIT, compact_template
        names = sorted(n for n in os.listdir(self.INFRA) if n.endswith('.yaml'))
        self.assertIn('ecdl-cdc-ecs.yaml', names)
        for name in names:
            with open(os.path.join(self.INFRA, name)) as fh:
                original = fh.read()
            compact = compact_template(original)
            self.assertEqual(self._load(original), self._load(compact), name)
            if name == 'ecdl-control-plane.yaml':
                # Created from S3 (--template-url), where the limit is 1 MB;
                # never sent inline.
                self.assertLess(len(original.encode('utf-8')), 1_000_000, name)
                continue
            # Link templates: `ecdl apply` sends them inline, compacted.
            self.assertLessEqual(len(compact.encode('utf-8')), TEMPLATE_BODY_LIMIT, name)

    def test_the_control_plane_is_documented_and_released_through_s3(self):
        root = os.path.dirname(self.INFRA)
        for doc in ('README.md', os.path.join('docs', 'getting-started.md')):
            with open(os.path.join(root, doc)) as fh:
                text = fh.read()
            self.assertNotIn('--template-body file://infra/ecdl-control-plane.yaml', text, doc)
            self.assertIn('/ecdl-control-plane.yaml', text, doc)
        with open(os.path.join(root, 'release.sh')) as fh:
            release = fh.read()
        self.assertIn('UPLOADED_TEMPLATES="$LINK_TEMPLATES ecdl-control-plane.yaml"', release)

    def test_comment_lines_inside_a_block_scalar_are_kept(self):
        from ecdl_link.deploy import compact_template
        text = ('UserData:\n'
                '  Fn::Base64: !Sub |\n'
                '    #!/bin/bash\n'
                '    # a comment that is part of the script\n'
                '\n'
                '    echo hi\n'
                '# a template comment\n'
                'Other: 1  # trailing, left alone\n')
        out = compact_template(text)
        self.assertIn('#!/bin/bash', out)
        self.assertIn('# a comment that is part of the script', out)
        self.assertNotIn('a template comment', out)
        self.assertIn('Other: 1  # trailing, left alone', out)
        self.assertEqual(self._load(text), self._load(out))

    def test_the_deployer_sends_the_compacted_body(self):
        cfn = FakeCloudFormation(states={'ecdl-x-broker': ['CREATE_COMPLETE']})
        d = deployer(cfn, FakeECS(clusters=['c']))
        d.template_body = lambda name: '# comment\nResources: {}\n'
        d._stack_step({'id': 'broker', 'name': 'ecdl-x-broker', 'template': 't.yaml',
                       'parameters': {}, 'capabilities': [], 'purpose': 'p'}, {}, 'x')
        sent = cfn.created[0]
        self.assertEqual(sent['TemplateBody'], compact_template('# comment\nResources: {}\n'))
        self.assertNotIn('TemplateURL', sent)
        self.assertNotIn('RoleARN', sent)


class ServiceRoleTest(unittest.TestCase):
    """D1: every stack call goes through the deploy role and a pinned template."""

    STEP = {'id': 'broker', 'name': 'ecdl-x-broker', 'template': 'ecdl-broker-quickstart.yaml',
            'parameters': {}, 'capabilities': [], 'purpose': 'p'}
    URL = 'https://bucket.s3.us-east-2.amazonaws.com/templates/0.2.20/'
    ROLE = 'arn:aws:iam::111122223333:role/ECDLDeploy-cp-us-east-2'

    def make(self, cfn, url=True, role=True):
        d = deployer(cfn, FakeECS(clusters=['c']))
        d.template_url = (lambda name: self.URL + name) if url else None
        d.role_arn = self.ROLE if role else None
        d.template_body = lambda name: self.fail('the template must not be read inline')
        return d

    def test_create_sends_the_url_and_the_role_and_no_body(self):
        cfn = FakeCloudFormation(states={'ecdl-x-broker': ['CREATE_COMPLETE']})
        self.make(cfn)._stack_step(dict(self.STEP), {}, 'x')
        sent = cfn.created[0]
        self.assertEqual(sent['TemplateURL'], self.URL + 'ecdl-broker-quickstart.yaml')
        self.assertEqual(sent['RoleARN'], self.ROLE)
        self.assertNotIn('TemplateBody', sent)

    def test_update_sends_the_url_and_the_role(self):
        cfn = FakeCloudFormation(existing={'ecdl-x-broker': 'CREATE_COMPLETE'},
                                 states={'ecdl-x-broker': ['UPDATE_COMPLETE']})
        self.make(cfn)._stack_step(dict(self.STEP), {}, 'x')
        sent = cfn.updated[0]
        self.assertEqual((sent['TemplateURL'], sent['RoleARN']),
                         (self.URL + 'ecdl-broker-quickstart.yaml', self.ROLE))
        self.assertNotIn('TemplateBody', sent)

    def test_a_failed_stack_is_deleted_with_the_role(self):
        cfn = FakeCloudFormation(existing={'ecdl-x-broker': 'ROLLBACK_COMPLETE'},
                                 states={'ecdl-x-broker': ['ROLLBACK_COMPLETE']})
        deleted = []
        real = cfn.delete_stack
        cfn.delete_stack = lambda StackName=None, **kw: (deleted.append(kw), real(StackName=StackName))
        try:
            self.make(cfn)._stack_step(dict(self.STEP), {}, 'x')
        except Exception:  # noqa: BLE001 - the recreate path is covered elsewhere
            pass
        self.assertEqual(deleted[0], {'RoleARN': self.ROLE})

    def test_half_a_configuration_is_refused(self):
        for url, role in ((True, False), (False, True)):
            cfn = FakeCloudFormation()
            with self.assertRaises(DeployError):
                self.make(cfn, url=url, role=role)._stack_step(dict(self.STEP), {}, 'x')
            self.assertEqual(cfn.created, [])


class UpdateKeepsCaptureTest(unittest.TestCase):
    """Lifecycle H3/H4 and M1: an update of a running link."""

    PLAN = {'link': 'checkout', 'mode': 'initial_load_and_cdc', 'prefix': 'ecdl',
            'cluster_name': 'ecdl-checkout',
            'steps': [
                {'kind': 'stack', 'id': 'cdc', 'name': 'ecdl-checkout-cdc',
                 'template': 'ecdl-cdc-ecs.yaml', 'capabilities': [], 'purpose': 'p',
                 'parameters': {'DebeziumDesiredCount': '1', 'MonitorSecurityGroupId': '',
                                'PermissionsBoundaryArn': 'arn:new', 'PgSlotName': 'x'},
                 'from_outputs': {}, 'keep_previous': ['PgSlotName', 'DebeziumDesiredCount'],
                 'keep_if_empty': ['MonitorSecurityGroupId', 'PermissionsBoundaryArn']},
                {'kind': 'action', 'id': 'ensure_capture', 'action': 'ensure_capture',
                 'stack': 'cdc', 'purpose': 'p'}]}

    def run_plan(self, desired, loaders=None, params=None):
        import copy
        cfn = FakeCloudFormation(existing={'ecdl-checkout-cdc': 'UPDATE_COMPLETE'})
        stacks = {'ecdl-checkout-cdc': params if params is not None else
                  ['DebeziumDesiredCount', 'MonitorSecurityGroupId',
                   'PermissionsBoundaryArn', 'PgSlotName']}
        real = cfn.describe_stacks

        def describe(StackName=None, **kw):
            out = real(StackName=StackName, **kw)
            out['Stacks'][0]['Parameters'] = [{'ParameterKey': k, 'ParameterValue': 'old'}
                                              for k in stacks.get(StackName, [])]
            return out
        cfn.describe_stacks = describe
        ecs = FakeECS(clusters=['ecdl-checkout'],
                      desired={'ecdl-checkout-cdc-debezium': desired},
                      running={'ecdl-checkout-cdc-debezium': [0, 1, 1]},
                      loader_tasks={'ecdl-checkout-loader': loaders or []})
        events = []
        d = deployer(cfn, ecs, events)
        try:
            d.run(copy.deepcopy(self.PLAN))
            err = None
        except DeployError as exc:
            err = exc
        return cfn, ecs, events, err

    def sent(self, cfn):
        return {p['ParameterKey']: p for p in cfn.updated[0]['parameters']}

    def test_the_desired_count_is_never_sent_on_update(self):
        cfn, _ecs, _ev, err = self.run_plan(desired=1)
        self.assertIsNone(err)
        self.assertEqual(self.sent(cfn)['DebeziumDesiredCount'], {
            'ParameterKey': 'DebeziumDesiredCount', 'UsePreviousValue': True})

    def test_empty_control_plane_values_keep_the_stack_value(self):
        cfn, _ecs, _ev, _err = self.run_plan(desired=1)
        sent = self.sent(cfn)
        self.assertTrue(sent['MonitorSecurityGroupId'].get('UsePreviousValue'))
        # A value the plan does know is sent.
        self.assertEqual(sent['PermissionsBoundaryArn']['ParameterValue'], 'arn:new')

    def test_an_empty_value_absent_from_the_stack_is_sent_empty(self):
        cfn, _ecs, _ev, _err = self.run_plan(desired=1, params=['DebeziumDesiredCount'])
        self.assertEqual(self.sent(cfn)['MonitorSecurityGroupId']['ParameterValue'], '')

    def test_running_capture_is_left_alone(self):
        _cfn, ecs, events, err = self.run_plan(desired=1)
        self.assertIsNone(err)
        self.assertEqual(ecs.updated_services, [])
        self.assertIn('capture_running', [e['event'] for e in events])

    def test_paused_capture_with_no_load_is_resumed(self):
        # A failed resume, a restarted control plane, or a pause by hand: the
        # redeploy used to report succeeded with capture still at 0.
        _cfn, ecs, events, err = self.run_plan(desired=0)
        self.assertIsNone(err)
        self.assertEqual(ecs.updated_services,
                         [('ecdl-checkout', 'ecdl-checkout-cdc-debezium', 1)])
        self.assertIn('capture_resumed', [e['event'] for e in events])
        self.assertEqual(ecs.listed, [('ecdl-checkout', 'ecdl-checkout-loader', 'RUNNING')])

    def test_paused_capture_under_a_running_load_is_not_resumed(self):
        _cfn, ecs, _events, err = self.run_plan(desired=0, loaders=['arn:task/1'])
        self.assertIsNotNone(err)
        self.assertIn('still running', str(err))
        self.assertEqual(ecs.updated_services, [])


class LoadInProgressStateTest(unittest.TestCase):
    PLAN = {'link': 'checkout', 'cluster_name': 'ecdl-checkout', 'prefix': 'ecdl',
            'steps': [{'kind': 'stack', 'id': 'cdc', 'name': 'ecdl-checkout-cdc'}]}

    def test_a_running_load_makes_a_deployed_link_in_progress(self):
        from ecdl_link.deploy import deployment_state
        cfn = FakeCloudFormation(existing={'ecdl-checkout-cdc': 'CREATE_COMPLETE'})
        ecs = FakeECS(loader_tasks={'ecdl-checkout-loader': ['arn:task/9']})
        st = deployment_state(self.PLAN, cfn, ecs=ecs)
        self.assertEqual(st['state'], 'in_progress')
        self.assertEqual(st['loader_tasks'], ['arn:task/9'])

    def test_no_load_keeps_deployed_and_no_ecs_means_stacks_only(self):
        from ecdl_link.deploy import deployment_state
        cfn = FakeCloudFormation(existing={'ecdl-checkout-cdc': 'CREATE_COMPLETE'})
        self.assertEqual(deployment_state(self.PLAN, cfn, ecs=FakeECS())['state'], 'deployed')
        self.assertEqual(deployment_state(self.PLAN, cfn)['state'], 'deployed')

    def test_a_missing_cluster_is_no_load(self):
        from ecdl_link.deploy import deployment_state

        class NoCluster(FakeECS):
            def list_tasks(self, **_):
                raise Exception('ClusterNotFoundException: Cluster not found.')
        cfn = FakeCloudFormation(existing={'ecdl-checkout-cdc': 'CREATE_COMPLETE'})
        self.assertEqual(deployment_state(self.PLAN, cfn, ecs=NoCluster())['state'], 'deployed')
