"""Jira story F059: a load that did not finish is remembered on the loader stack.

A failed initial load used to be forgotten: the next plain deploy saw every
stack COMPLETE, took the update path, loaded nothing and reported success,
while the cache was missing rows CDC never sends. The marker is a tag on the
loader stack (ecdl:initial-load), so both the CLI and the control plane read
the same truth.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.deploy import (  # noqa: E402
    LOAD_MARKER_TAG, Clients, DeployError, Deployer, add_load_step, deployment_state,
    initial_load_refusal, load_marker,
)
from ecdl_link.provision import plan  # noqa: E402
from ecdl_link.tests.test_deploy import (  # noqa: E402
    BROKER_OUTPUTS, LOADER_OUTPUTS, FakeCloudFormation, FakeECS, deployer, stopped,
)
from ecdl_link.tests.test_provision import resolution  # noqa: E402

LOADER = 'ecdl-checkout-loader'
NAMES = ('ecdl-checkout-broker', 'ecdl-checkout-cdc', LOADER)


def _cfn():
    return FakeCloudFormation(
        states={n: ['CREATE_COMPLETE', 'CREATE_COMPLETE'] for n in NAMES},
        outputs={'ecdl-checkout-broker': BROKER_OUTPUTS, 'ecdl-checkout-cdc': {},
                 LOADER: LOADER_OUTPUTS})


def _ecs(**kw):
    return FakeECS(clusters=['ecdl-checkout'],
                   running={'ecdl-checkout-cdc-debezium': [0, 1]}, **kw)


def _full_plan():
    return add_load_step(plan(resolution('initial_load_and_cdc')))


def _update_plan():
    return add_load_step(plan(resolution('initial_load_and_cdc'), for_update=True))


def _marker(cfn, name=LOADER):
    return load_marker(cfn.tags.get(name))


def _deployed(cfn):
    """The same fake, now standing in for a link whose stacks all exist."""
    for name in NAMES:
        cfn.existing[name] = 'CREATE_COMPLETE'
        cfn.states[name] = []
    return cfn


class MarkerWrittenTest(unittest.TestCase):
    def test_a_successful_load_ends_complete(self):
        cfn, ecs, events = _cfn(), _ecs(), []
        d = deployer(cfn, ecs, events)
        result = d.run(_full_plan())
        self.assertEqual(_marker(cfn), 'complete')
        self.assertTrue(result['load_recorded'])
        created = next(c for c in cfn.created if c['name'] == LOADER)
        self.assertIn({'Key': LOAD_MARKER_TAG, 'Value': 'incomplete'}, created['Tags'])
        mark = [u for u in cfn.updated if u['name'] == LOADER][-1]
        # Every parameter kept, the stack's other tags kept.
        self.assertTrue(mark['parameters'])
        self.assertTrue(all(p == {'ParameterKey': p['ParameterKey'], 'UsePreviousValue': True}
                            for p in mark['parameters']))
        self.assertIn({'Key': 'ecdl:link', 'Value': 'checkout'}, mark['Tags'])
        self.assertEqual([t for t in mark['Tags'] if t['Key'] == LOAD_MARKER_TAG],
                         [{'Key': LOAD_MARKER_TAG, 'Value': 'complete'}])

    def test_incomplete_before_the_first_table_complete_after_the_last(self):
        order = []
        cfn = _cfn()

        class Ordered(FakeECS):
            def run_task(self, **kw):
                order.append(('load', _marker(cfn)))
                return super().run_task(**kw)

            def update_service(self, **kw):
                order.append(('desired', kw['desiredCount'], _marker(cfn)))
                return super().update_service(**kw)
        ecs = Ordered(clusters=['ecdl-checkout'],
                      running={'ecdl-checkout-cdc-debezium': [0, 1]})
        deployer(cfn, ecs).run(_full_plan())
        # The loader stack (and its marker) comes before the pause (Jira story F086).
        self.assertEqual(order, [('desired', 0, 'incomplete'), ('load', 'incomplete'),
                                 ('load', 'incomplete'), ('desired', 1, 'complete')])

    def test_a_failed_load_stays_incomplete(self):
        cfn = _cfn()
        ecs = _ecs(tasks=[stopped(exit_code=0), stopped(exit_code=1)])
        with self.assertRaises(DeployError):
            deployer(cfn, ecs).run(_full_plan())
        self.assertEqual(_marker(cfn), 'incomplete')
        self.assertFalse([u for u in cfn.updated if u['name'] == LOADER])

    def test_the_mark_uses_the_pinned_template_and_the_role(self):
        # The control plane may only UpdateStack with a pinned TemplateURL and
        # the deploy role; UsePreviousTemplate would be denied.
        cfn = _cfn()
        d = Deployer(Clients(cloudformation=cfn, ecs=_ecs()),
                     template_body=lambda n: 'T', poll_seconds=0, sleep=lambda _s: None,
                     offset_probe=lambda p, o: True,
                     template_url=lambda n: 'https://b.s3/templates/1/' + n,
                     role_arn='arn:aws:iam::1:role/ECDLDeploy')
        d.run(_full_plan())
        mark = [u for u in cfn.updated if u['name'] == LOADER][-1]
        self.assertEqual(mark['TemplateURL'], 'https://b.s3/templates/1/ecdl-loader-ecs.yaml')
        self.assertEqual(mark['RoleARN'], 'arn:aws:iam::1:role/ECDLDeploy')
        self.assertNotIn('UsePreviousTemplate', mark)

    def test_a_failed_mark_does_not_fail_the_loaded_deploy(self):
        cfn = _cfn()
        calls = []
        real = cfn.update_stack

        def update_stack(**kw):
            calls.append(kw['StackName'])
            if kw['StackName'] == LOADER:
                raise Exception('AccessDenied: iam:TagRole')
            return real(**kw)
        cfn.update_stack = update_stack
        ecs, events = _ecs(), []
        result = deployer(cfn, ecs, events).run(_full_plan())
        self.assertFalse(result['load_recorded'])
        self.assertEqual(_marker(cfn), 'incomplete')
        failed = [e for e in events if e['event'] == 'load_marker_failed']
        self.assertIn('AccessDenied', failed[0]['error'])
        self.assertEqual([x for _c, _s, x in ecs.updated_services], [0, 1])

    def test_a_rolled_back_mark_is_reported_not_raised(self):
        cfn = _cfn()
        d = deployer(cfn, _ecs())
        real = d._wait_for_stack

        def wait(name, step_id):
            if step_id == 'run_load':
                raise DeployError('stack {} ended in UPDATE_ROLLBACK_COMPLETE'.format(name))
            return real(name, step_id)
        d._wait_for_stack = wait
        self.assertFalse(d.run(_full_plan())['load_recorded'])

    def test_a_load_only_link_is_marked_too(self):
        cfn = FakeCloudFormation(states={LOADER: ['CREATE_COMPLETE', 'CREATE_COMPLETE']},
                                 outputs={LOADER: LOADER_OUTPUTS})
        deployer(cfn, FakeECS(clusters=['ecdl-checkout'])).run(
            add_load_step(plan(resolution('initial_load'))))
        self.assertEqual(_marker(cfn), 'complete')

    def test_a_cdc_only_plan_has_no_marker(self):
        cfn = FakeCloudFormation(
            states={'ecdl-checkout-broker': ['CREATE_COMPLETE'] * 2,
                    'ecdl-checkout-cdc': ['CREATE_COMPLETE'] * 2},
            outputs={'ecdl-checkout-broker': BROKER_OUTPUTS, 'ecdl-checkout-cdc': {}})
        result = deployer(cfn, FakeECS()).run(plan(resolution('cdc')))
        self.assertIsNone(result['load_recorded'])
        for name, tags in cfn.tags.items():
            self.assertIsNone(load_marker(tags), name)


class MarkerKeptByUpdatesTest(unittest.TestCase):
    """UpdateStack replaces the whole tag set: an update must carry it over."""

    def _update(self, existing_marker):
        cfn = _deployed(_cfn())
        if existing_marker:
            cfn.tags[LOADER] = [{'Key': 'ecdl:link', 'Value': 'checkout'},
                                {'Key': LOAD_MARKER_TAG, 'Value': existing_marker}]
        ecs = FakeECS(clusters=['ecdl-checkout'])
        deployer(cfn, ecs).run(_update_plan())
        sent = [u for u in cfn.updated if u['name'] == LOADER]
        self.assertEqual(len(sent), 1)
        return load_marker(sent[0]['Tags'])

    def test_an_update_keeps_an_incomplete_marker(self):
        self.assertEqual(self._update('incomplete'), 'incomplete')

    def test_an_update_keeps_a_complete_marker(self):
        self.assertEqual(self._update('complete'), 'complete')

    def test_an_update_adds_none_to_an_old_stack(self):
        self.assertIsNone(self._update(None))

    def test_other_stacks_never_get_one(self):
        cfn = _deployed(_cfn())
        deployer(cfn, FakeECS(clusters=['ecdl-checkout'])).run(_update_plan())
        for sent in cfn.updated:
            if sent['name'] != LOADER:
                self.assertIsNone(load_marker(sent['Tags']), sent['name'])


class NextDeployTest(unittest.TestCase):
    def _after(self, tasks):
        """Run a first deploy, then read the state the next deploy sees."""
        cfn = _cfn()
        try:
            deployer(cfn, _ecs(tasks=tasks)).run(_full_plan())
        except DeployError:
            pass
        return deployment_state(_full_plan(), _deployed(cfn))

    def test_after_a_failed_load_a_plain_deploy_is_refused(self):
        current = self._after([stopped(exit_code=1)])
        self.assertEqual((current['state'], current['initial_load']), ('deployed', 'incomplete'))
        refusal = initial_load_refusal(_full_plan(), current)
        self.assertIsInstance(refusal, DeployError)
        self.assertIn('did not complete', str(refusal))
        self.assertIn('--reload', refusal.hint)

    def test_after_a_failed_load_a_reload_is_allowed(self):
        current = self._after([stopped(exit_code=1)])
        self.assertIsNone(initial_load_refusal(_full_plan(), current, reload=True))

    def test_after_a_successful_load_a_plain_deploy_goes_ahead(self):
        current = self._after([])
        self.assertEqual(current['initial_load'], 'complete')
        self.assertIsNone(initial_load_refusal(_full_plan(), current))

    def test_a_reload_that_succeeds_clears_the_marker(self):
        cfn = _deployed(_cfn())
        cfn.tags[LOADER] = [{'Key': LOAD_MARKER_TAG, 'Value': 'incomplete'}]
        deployer(cfn, _ecs()).run(_full_plan())
        self.assertEqual(deployment_state(_full_plan(), cfn)['initial_load'], 'complete')

    def test_stacks_without_the_marker_count_as_complete(self):
        cfn = _deployed(_cfn())
        current = deployment_state(_full_plan(), cfn)
        self.assertIsNone(current['initial_load'])
        self.assertIsNone(initial_load_refusal(_full_plan(), current))

    def test_a_partial_deploy_loads_anyway_so_is_not_refused(self):
        current = {'state': 'partial', 'initial_load': 'incomplete'}
        self.assertIsNone(initial_load_refusal(_full_plan(), current))

    def test_unknown_marker_values_are_ignored(self):
        self.assertIsNone(load_marker([{'Key': LOAD_MARKER_TAG, 'Value': 'maybe'}]))
        self.assertIsNone(load_marker(None))


if __name__ == '__main__':
    unittest.main()
