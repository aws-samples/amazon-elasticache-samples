"""Review finding H7: one deploy of a link at a time, across processes, held
in the link's broker. The lock against a real Valkey (ECDL_TEST_VALKEY), and
the deployer's use of it against fakes."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ecdl_link import deploy  # noqa: E402
from ecdl_link.deploy import Clients, DeployError, Deployer, broker_offset_probe  # noqa: E402
from ecdl_link.provision import plan  # noqa: E402
from ecdl_link.tests.test_deploy import BROKER_OUTPUTS, FakeCloudFormation, FakeECS  # noqa: E402
from ecdl_link.tests.test_provision import resolution  # noqa: E402

PLAN = {'link': 'locktest', 'steps': []}
OUTPUTS = {'broker': {'PrimaryEndpoint': 'local', 'Port': '6379', 'TransitEncryption': 'false'}}


@unittest.skipUnless(os.environ.get('ECDL_TEST_VALKEY'), 'ECDL_TEST_VALKEY not set')
class LockAgainstValkey(unittest.TestCase):
    def setUp(self):
        import valkey
        host, port = os.environ['ECDL_TEST_VALKEY'].rsplit(':', 1)
        self.v = valkey.Valkey(host=host, port=int(port))
        self.key = deploy.deploy_lock_key('locktest')
        self.v.delete(self.key)
        self.addCleanup(self.v.delete, self.key)
        self.probe = broker_offset_probe(None, connect=lambda *a: self.v)

    def test_one_owner_at_a_time(self):
        state, token = self.probe.lock(PLAN, OUTPUTS, 'cli on laptop pid 1')
        self.assertEqual(state, 'taken')
        state, holder = self.probe.lock(PLAN, OUTPUTS, 'control plane pid 2')
        self.assertEqual(state, 'held')
        self.assertIn('cli on laptop pid 1', holder)
        self.assertFalse(self.probe.unlock(PLAN, OUTPUTS, 'not-the-token'))
        self.assertTrue(self.probe.unlock(PLAN, OUTPUTS, token))
        self.assertEqual(self.probe.lock(PLAN, OUTPUTS, 'control plane pid 2')[0], 'taken')

    def test_a_killed_deployers_lock_expires_and_refresh_keeps_a_live_one(self):
        state, token = self.probe.lock(PLAN, OUTPUTS, 'killed', ttl_ms=300)
        self.assertTrue(self.probe.refresh_lock(PLAN, OUTPUTS, token, ttl_ms=300))
        self.assertGreater(self.v.pttl(self.key), 0)
        time.sleep(0.4)
        self.assertEqual(self.probe.lock(PLAN, OUTPUTS, 'next')[0], 'taken')


class FakeProbe(object):
    def __init__(self, state, detail):
        self.result, self.calls = (state, detail), []

    def __call__(self, plan_doc, outputs):
        return True

    def lock(self, plan_doc, outputs, owner):
        self.calls.append(('lock', owner))
        return self.result

    def refresh_lock(self, *a):
        return True

    def unlock(self, plan_doc, outputs, token):
        self.calls.append(('unlock', token))
        return True


class DeployerUsesTheLock(unittest.TestCase):
    def deployer(self, probe, broker_exists=True):
        cfn = FakeCloudFormation(
            existing={'ecdl-checkout-broker': 'CREATE_COMPLETE'} if broker_exists else None,
            states={'ecdl-checkout-broker': ['UPDATE_COMPLETE'] * 3,
                    'ecdl-checkout-cdc': ['CREATE_COMPLETE'] * 3},
            outputs={'ecdl-checkout-broker': BROKER_OUTPUTS,
                     'ecdl-checkout-cdc': {'SinkService': 'ecdl-checkout-cdc-sink'}})
        events = []
        d = Deployer(Clients(cloudformation=cfn, ecs=FakeECS(clusters=['ecdl-checkout'])),
                     template_body=lambda name: 'Template: ' + name, on_progress=events.append,
                     poll_seconds=0, sleep=lambda _s: None, offset_probe=probe)
        return d, cfn, events

    def test_a_held_lock_refuses_before_anything_changes(self):
        probe = FakeProbe('held', 'ip-10-0-0-1 pid 77 (since 2026-01-01 00:00:00 UTC)')
        d, cfn, _ = self.deployer(probe)
        with self.assertRaises(DeployError) as caught:
            d.run(plan(resolution('cdc')))
        self.assertIn('another deploy of', str(caught.exception))
        self.assertIn('pid 77', str(caught.exception))
        self.assertIn('expires', caught.exception.hint)
        self.assertEqual((cfn.created, cfn.updated, cfn.deleted), ([], [], []))

    def test_a_taken_lock_is_released_after_the_deploy(self):
        probe = FakeProbe('taken', 'tok-1')
        d, _, events = self.deployer(probe)
        try:
            d.run(plan(resolution('cdc')))
        except DeployError:
            pass
        self.assertEqual(probe.calls[0][0], 'lock')
        self.assertEqual(probe.calls[-1], ('unlock', 'tok-1'))
        self.assertIn('deploy_lock_taken', [e.get('event') for e in events])

    def test_an_unreachable_broker_deploys_without_it_and_says_so(self):
        probe = FakeProbe(None, 'ConnectionError: timed out')
        d, _, events = self.deployer(probe)
        try:
            d.run(plan(resolution('cdc')))
        except DeployError:
            pass
        self.assertIn('deploy_lock_skipped', [e.get('event') for e in events])
        self.assertNotIn('unlock', [c[0] for c in probe.calls])

    def test_a_first_deploy_has_no_broker_to_lock(self):
        probe = FakeProbe('taken', 'tok')
        d, _, _ = self.deployer(probe, broker_exists=False)
        try:
            d.run(plan(resolution('cdc')))
        except DeployError:
            pass
        self.assertEqual(probe.calls, [])


class DowngradeTest(unittest.TestCase):
    """Review finding H8: an update with an older release than the stack runs
    is refused unless asked for."""
    TEMPLATE = "Parameters:\n  ImageTag:\n    Type: String\n    Default: 0.2.30\n"

    def run_update(self, running, **kwargs):
        cfn = FakeCloudFormation(
            existing={'ecdl-checkout-broker': 'CREATE_COMPLETE', 'ecdl-checkout-cdc': 'CREATE_COMPLETE'},
            states={'ecdl-checkout-broker': ['UPDATE_COMPLETE'] * 3,
                    'ecdl-checkout-cdc': ['UPDATE_COMPLETE'] * 3},
            outputs={'ecdl-checkout-broker': BROKER_OUTPUTS,
                     'ecdl-checkout-cdc': {'SinkService': 'ecdl-checkout-cdc-sink'}})
        cfn.params['ecdl-checkout-cdc'] = {'ImageTag': running}
        d = Deployer(Clients(cloudformation=cfn, ecs=FakeECS(clusters=['ecdl-checkout'])),
                     template_body=lambda name: self.TEMPLATE, poll_seconds=0,
                     sleep=lambda _s: None, offset_probe=lambda *a: True, **kwargs)
        doc = plan(resolution('cdc'))
        try:
            d.run(doc)
        except DeployError as exc:
            return exc, cfn
        return None, cfn

    def test_an_older_release_is_refused(self):
        exc, cfn = self.run_update('0.2.36')
        self.assertIsNotNone(exc)
        self.assertIn('runs release 0.2.36', str(exc))
        self.assertIn('--allow-downgrade', exc.hint)
        self.assertNotIn('ecdl-checkout-cdc', [u['name'] for u in cfn.updated])

    def test_same_or_newer_and_asked_for_are_not(self):
        for running, kwargs in (('0.2.30', {}), ('0.2.9', {}), ('0.2.36', {'allow_downgrade': True}),
                                ('sha256:abc', {})):
            exc, _ = self.run_update(running, **kwargs)
            self.assertFalse(exc is not None and 'older images' in str(exc), (running, exc))

    def test_release_parsing(self):
        self.assertEqual(deploy.template_release(self.TEMPLATE), '0.2.30')
        self.assertEqual(deploy.template_release("  ImageTag:\n    Type: String\n    Default: '1.0.0'\n"), '1.0.0')
        self.assertIsNone(deploy.template_release('Parameters: {}'))
        self.assertGreater(deploy._release_key('0.2.36'), deploy._release_key('0.2.9'))


if __name__ == '__main__':
    unittest.main()
