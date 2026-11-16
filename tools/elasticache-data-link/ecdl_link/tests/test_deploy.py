"""Tests for the deployment executor, with hand-written AWS fakes.

The cases worth covering are all failure shapes -- a stack stuck in
ROLLBACK_COMPLETE, an unchanged stack, a load task that exits non-zero, a task
that never ran at all -- and none of them should need an account to reproduce.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.deploy import Clients, DeployError, Deployer, add_load_step  # noqa: E402
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
        return {'Stacks': [{
            'StackStatus': self.existing[StackName],
            'Outputs': [{'OutputKey': k, 'OutputValue': v}
                        for k, v in (self.outputs.get(StackName) or {}).items()],
        }]}

    def create_stack(self, StackName=None, Parameters=None, **kw):
        self.created.append({'name': StackName, 'parameters': Parameters, **kw})
        self.existing[StackName] = 'CREATE_IN_PROGRESS'
        return {'StackId': 'id-' + StackName}

    def update_stack(self, StackName=None, Parameters=None, **kw):
        if StackName in self.fail:
            raise Exception('An error occurred: No updates are to be performed.')
        self.updated.append({'name': StackName, 'parameters': Parameters, **kw})
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
    def __init__(self, clusters=None, running=None, tasks=None):
        self.clusters = list(clusters or [])
        self.running = dict(running or {})     # service -> [running counts]
        self.tasks = list(tasks or [])         # scripted describe_tasks replies
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
        return {'services': [{'serviceName': service, 'runningCount': count}]}

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


def deployer(cfn, ecs, events=None):
    return Deployer(Clients(cloudformation=cfn, ecs=ecs),
                    template_body=lambda name: 'Template: ' + name,
                    on_progress=(events.append if events is not None else None),
                    poll_seconds=0, sleep=lambda _s: None)


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
                         ['broker', 'cdc', 'pause_capture', 'loader', 'run_load',
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
        p = add_load_step(plan(resolution('initial_load_and_cdc')))
        ids = [s['id'] for s in p['steps']]
        self.assertEqual(ids.index('run_load'), ids.index('loader') + 1)

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
            self.assertLessEqual(len(compact.encode('utf-8')), TEMPLATE_BODY_LIMIT, name)

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
        # Reads the call sites rather than running a deploy (which polls).
        import inspect
        import ecdl_link.deploy as deploy_mod
        src = inspect.getsource(deploy_mod.Deployer)
        self.assertIn("compact_template(self.template_body(step['template']))", src)
        self.assertEqual(src.count('TemplateBody=body'), 2)
