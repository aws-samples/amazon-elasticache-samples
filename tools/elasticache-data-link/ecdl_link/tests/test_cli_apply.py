"""`ecdl apply` runs the same review as the control plane (D7). The CLI script is
loaded as a module and every AWS-facing call is faked."""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, ROOT)

import ecdl_link.change as change  # noqa: E402
import ecdl_link.conflicts as conflicts  # noqa: E402
import ecdl_link.deploy as deploy  # noqa: E402
import ecdl_link.provision as provision  # noqa: E402


class _FreshSourceLoader(importlib.machinery.SourceFileLoader):
    """Compiles from the source every time. Never from or into __pycache__: a
    cached compile of an edited script with the same timestamp would test the
    old code."""

    def get_code(self, fullname):
        return self.source_to_code(self.get_data(self.path), self.path)


def load_cli():
    path = os.path.join(ROOT, 'ecdl-cli', 'ecdl')
    loader = _FreshSourceLoader('ecdl_cli_under_test', path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


CLI = load_cli()
PLAN = {'link': 'demo', 'mode': 'initial_load_and_cdc', 'cluster_name': 'ecdl-demo',
        'prefix': 'ecdl', 'steps': [], 'notes': [], 'warnings': []}
RESOLUTION = {'link': {'name': 'demo'}, 'source': {'engine': 'mysql'},
              'target': {'endpoint': 't'}, 'tables': [], 'debezium': {}}


def review(severity):
    return {'diff': {'changed': True, 'severity': severity, 'changes': [
        {'field': 'tables', 'severity': severity, 'consequence': 'c', 'action': 'a'}]},
        'drift': [], 'refuse': severity == 'recreate',
        'needs_ack': severity in ('backfill', 'cleanup')}


class ApplyTest(unittest.TestCase):
    def run_apply(self, state='deployed', the_review=None, loaders=(), initial_load=None,
                  steps=(), made=None, result=None, **flags):
        ran = []

        interrupt = flags.pop('interrupt', False)
        # The source preflight connects to the source; Jira story F143's tests drive it.
        source_check = flags.pop('preflight', {'checked': True, 'blocks': 0,
                                               'warnings': 0, 'findings': []})
        resolution = dict(RESOLUTION, target=flags.pop('target', RESOLUTION['target']))

        class FakeDeployer:
            capture_message = None

            def __init__(self, *a, **k):
                if made is not None:
                    made.append(k)

            def run(self, plan_doc):
                ran.append(plan_doc)
                if interrupt:
                    self.capture_message = 'Capture was resumed, so no changes are lost.'
                    raise KeyboardInterrupt()
                return dict({'link': 'demo', 'mode': 'cdc', 'completed': ['cdc']},
                            **(result or {}))

        first_plan = dict(PLAN, steps=list(steps))
        patches = [
            (CLI, '_plan_from_file',
             lambda args, with_resolution=False: (resolution, dict(first_plan))),
            (deploy, 'Clients', lambda **k: type('C', (), {'cloudformation': None, 'ecs': None,
                                                          'secrets': lambda self: None})()),
            (deploy, 'deployment_state', lambda plan_doc, cfn, ecs=None: {
                'state': state, 'stacks': {'ecdl-demo-cdc': 'UPDATE_COMPLETE'} if state != 'new' else {},
                'loader_tasks': list(loaders), 'initial_load': initial_load}),
            (deploy, 'Deployer', FakeDeployer),
            (deploy, 'broker_offset_probe', lambda *a: None),
            (provision, 'plan', lambda resolution, **k: dict(PLAN, for_update=k.get('for_update'))),
            (change, 'review_deployed', lambda *a: the_review),
            (change, 'follow_up', lambda r, res: ({}, [{'why': 'snapshot orders',
                                                        'statement': 'INSERT ...'}])),
            (conflicts, 'resolution_conflicts', lambda *a: []),
            (CLI, '_source_preflight', lambda args, res: source_check),
        ]
        saved = [(m, n, getattr(m, n)) for m, n, _ in patches]
        for m, n, v in patches:
            setattr(m, n, v)
        args = argparse.Namespace(file='x', region=None, profile=None, dry_run=False,
                                  yes=True, reload=False, offset_checked=False,
                                  acknowledge=False, ignore_preflight=False)
        for k, v in flags.items():
            setattr(args, k, v)
        out = io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(out):
                CLI.cmd_apply(args)
        except SystemExit as exc:
            code = exc.code
        finally:
            for m, n, v in saved:
                setattr(m, n, v)
        return code, out.getvalue(), ran

    def test_a_change_that_cannot_be_made_in_place_is_refused(self):
        code, out, ran = self.run_apply(the_review=review('recreate'))
        self.assertEqual(code, 1)
        self.assertEqual(ran, [])
        self.assertIn('cannot be made to the running link', out)

    def test_yes_alone_does_not_acknowledge_a_backfill(self):
        code, out, ran = self.run_apply(the_review=review('backfill'))
        self.assertEqual((code, ran), (1, []))
        self.assertIn('--acknowledge', out)
        self.assertIn('INSERT ...', out)

    def test_acknowledged_backfill_deploys_and_prints_the_steps_again(self):
        code, out, ran = self.run_apply(the_review=review('backfill'), acknowledge=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(ran), 1)
        self.assertIn('still to do', out)

    def test_a_running_load_stops_apply(self):
        code, out, ran = self.run_apply(state='in_progress', loaders=['arn:task/1'])
        self.assertEqual((code, ran), (1, []))
        self.assertIn('a load is running', out)

    def test_a_first_deploy_has_nothing_to_review(self):
        code, out, ran = self.run_apply(state='new', the_review=review('recreate'))
        self.assertEqual(code, 0)
        self.assertEqual(len(ran), 1)
        self.assertNotIn('change to the deployed link', out)

    def test_ctrl_c_says_what_happened_to_capture(self):
        code, out, _ran = self.run_apply(state='new', interrupt=True)
        self.assertEqual(code, 130)
        self.assertIn('interrupted', out)
        self.assertIn('Capture was resumed', out)

    def test_a_settings_change_deploys_without_acknowledgement(self):
        code, _out, ran = self.run_apply(the_review=review('deploy'))
        self.assertEqual((code, len(ran)), (0, 1))


    # D6: the target's eviction policy, the same rule the control plane applies.
    def test_an_evicting_target_is_refused(self):
        code, out, ran = self.run_apply(state='new', target={
            'endpoint': 't', 'maxmemory_policy': 'allkeys-lru', 'parameter_group': 'pg-1'})
        self.assertEqual((code, ran), (1, []))
        self.assertIn('allkeys-lru (parameter group pg-1)', out)
        self.assertIn('noeviction', out)
        self.assertIn('--ignore-preflight', out)

    def test_ignore_preflight_deploys_to_an_evicting_target(self):
        code, out, ran = self.run_apply(state='new', ignore_preflight=True, target={
            'endpoint': 't', 'maxmemory_policy': 'allkeys-random', 'parameter_group': 'pg-1'})
        self.assertEqual((code, len(ran)), (0, 1))
        self.assertIn('  ! the target', out)

    def test_a_volatile_policy_warns_and_deploys(self):
        code, out, ran = self.run_apply(state='new', target={
            'endpoint': 't', 'maxmemory_policy': 'volatile-lru', 'parameter_group': 'default.valkey8'})
        self.assertEqual((code, len(ran)), (0, 1))
        self.assertIn('  ! the target', out)
        self.assertIn('delete markers', out)

    def test_noeviction_says_nothing(self):
        code, out, ran = self.run_apply(state='new', target={
            'endpoint': 't', 'maxmemory_policy': 'noeviction', 'parameter_group': 'pg-1'})
        self.assertEqual((code, len(ran)), (0, 1))
        self.assertNotIn('maxmemory-policy', out)

    def test_an_unread_policy_warns_and_deploys(self):
        code, out, ran = self.run_apply(state='new', target={
            'endpoint': 't', 'maxmemory_policy': None,
            'maxmemory_policy_problem': 'AccessDenied on DescribeCacheParameters'})
        self.assertEqual((code, len(ran)), (0, 1))
        self.assertIn('could not check the target eviction policy (AccessDenied', out)


    # Jira story F059: a load that did not finish is not hidden by a plain update.
    CDC_STEPS = ({'kind': 'stack', 'id': 'cdc', 'name': 'ecdl-demo-cdc',
                  'template': 'ecdl-cdc-ecs.yaml', 'purpose': 'capture'},)

    def test_an_unfinished_load_refuses_a_plain_apply(self):
        code, out, ran = self.run_apply(initial_load='incomplete', steps=self.CDC_STEPS,
                                        the_review=review('deploy'))
        self.assertEqual((code, ran), (1, []))
        self.assertIn('did not complete', out)
        self.assertIn('--reload', out)

    def test_an_unfinished_load_allows_a_reload(self):
        code, _out, ran = self.run_apply(initial_load='incomplete', steps=self.CDC_STEPS,
                                         the_review=review('deploy'), reload=True)
        self.assertEqual((code, len(ran)), (0, 1))
        self.assertFalse(ran[0].get('for_update'))

    def test_a_complete_or_unmarked_load_updates_as_before(self):
        for marker in ('complete', None):
            code, _out, ran = self.run_apply(initial_load=marker, steps=self.CDC_STEPS,
                                             the_review=review('deploy'))
            self.assertEqual((code, len(ran)), (0, 1), marker)
            self.assertTrue(ran[0].get('for_update'), marker)

    def test_an_unrecorded_completion_is_reported(self):
        code, out, _ran = self.run_apply(state='new', result={'load_recorded': False})
        self.assertEqual(code, 0)
        self.assertIn('could not be marked complete', out)


    # Jira story F057: --reset-position.
    PAUSE_STEPS = ({'kind': 'stack', 'id': 'cdc', 'name': 'ecdl-demo-cdc',
                    'template': 'ecdl-cdc-ecs.yaml', 'purpose': 'capture',
                    'parameters': {'SourceEngine': 'mysql'}},
                   {'kind': 'action', 'id': 'pause_capture', 'action': 'set_debezium_desired',
                    'desired': 0, 'stack': 'cdc', 'purpose': 'pause'})

    def test_reset_position_needs_reload(self):
        code, out, ran = self.run_apply(steps=self.PAUSE_STEPS, the_review=review('deploy'),
                                        reset_position=True)
        self.assertEqual((code, ran), (1, []))
        self.assertIn('needs a reload', out)

    def test_reset_position_with_reload_reaches_the_deployer(self):
        made = []
        code, out, ran = self.run_apply(steps=self.PAUSE_STEPS, the_review=review('deploy'),
                                        reset_position=True, reload=True, made=made)
        self.assertEqual((code, len(ran)), (0, 1))
        self.assertEqual((made[0]['reset_position'], made[0]['pg_slot_dropped']),
                         (True, False))
        self.assertIn('to record the current position', out)

    def test_reset_position_on_postgres_prints_the_slot_step(self):
        steps = (dict(self.PAUSE_STEPS[0], parameters={'SourceEngine': 'postgresql',
                                                       'PgSlotName': 'ecdl_demo'}),
                 self.PAUSE_STEPS[1])
        code, out, ran = self.run_apply(steps=steps, the_review=review('deploy'),
                                        reset_position=True, reload=True)
        self.assertEqual((code, ran), (1, []))
        self.assertIn("SELECT pg_drop_replication_slot('ecdl_demo');", out)
        made = []
        code, _out, ran = self.run_apply(steps=steps, the_review=review('deploy'),
                                         reset_position=True, reload=True,
                                         pg_slot_dropped=True, made=made)
        self.assertEqual((code, len(ran)), (0, 1))
        self.assertTrue(made[0]['pg_slot_dropped'])

    def test_without_the_flag_the_deployer_does_not_reset(self):
        made = []
        self.run_apply(steps=self.PAUSE_STEPS, the_review=review('deploy'), reload=True,
                       made=made)
        self.assertFalse(made[0]['reset_position'])



class DestroyTest(unittest.TestCase):  # Jira story F022
    """`ecdl destroy` names the final snapshot the broker leaves."""
    def run_destroy(self, follow_up):
        import ecdl_link.teardown as teardown
        class FakeDestroyer:
            def __init__(self, *a, **k):
                pass
            def broker_data(self, plan_doc):
                # No broker stack (Jira story F136 is tested in test_cli_mdeploy).
                return {'exists': False, 'at_risk': False}
            def run(self, plan_doc):
                fu = dict(plan_doc['follow_up'], **follow_up)
                return {'link': 'shop', 'deleted': ['ecdl-shop-broker'], 'skipped': [],
                        'cluster_deleted': False, 'kept': plan_doc['keeps'], 'follow_up': fu}
        saved = (deploy.Clients, teardown.Destroyer)
        deploy.Clients, teardown.Destroyer = (lambda **k: None), FakeDestroyer
        args = argparse.Namespace(link='shop', stack=None, prefix='ecdl', dry_run=False,
                                  yes=True, region=None, profile=None, keep_cluster=False)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                CLI.cmd_destroy(args)
        finally:
            deploy.Clients, teardown.Destroyer = saved
        return out.getvalue()
    def test_the_preview_and_the_result_name_the_snapshot(self):
        out = self.run_destroy({
            'broker_final_snapshot': 'ecdl-shop-broker-final',
            'broker_snapshot_delete_command':
                'aws elasticache delete-snapshot --snapshot-name ecdl-shop-broker-final'})
        self.assertIn('FINAL SNAPSHOT (its offsets', out)
        self.assertIn("FINAL SNAPSHOT 'ecdl-shop-broker-final'", out)
        self.assertIn('delete-snapshot --snapshot-name ecdl-shop-broker-final', out)
        self.assertIn('restore_snapshot', out)
    def test_an_unnamed_snapshot_gets_the_find_command(self):
        out = self.run_destroy({'broker_final_snapshot': None})
        self.assertIn('could not be named here', out)
        self.assertIn('describe-snapshots --replication-group-id ecdl-shop-broker', out)
if __name__ == '__main__':
    unittest.main()


class LoadProgressTest(unittest.TestCase):
    """Jira story F104: the CLI shows a running load instead of hiding every poll."""

    def test_status_changes_and_a_periodic_line_are_shown(self):
        lines = []
        show = CLI.progress_printer(lines.append)
        for status in ['PROVISIONING', 'PENDING'] + ['RUNNING'] * 61:
            show({'event': 'load_waiting', 'step': 'run_load', 'table': 'orders',
                  'status': status})
        self.assertEqual(lines[0], '  load_waiting     table=orders status=PROVISIONING')
        self.assertEqual(lines[1], '  load_waiting     table=orders status=PENDING')
        self.assertEqual(lines[2], '  load_waiting     table=orders status=RUNNING')
        self.assertEqual(len(lines), 5)
        self.assertIn('still loading', lines[3])

    def test_other_waits_stay_quiet_and_events_are_printed(self):
        lines = []
        show = CLI.progress_printer(lines.append)
        show({'event': 'stack_waiting', 'stack': 's'})
        show({'event': 'load_started', 'table': 'orders', 'purpose': 'p'})
        self.assertEqual(lines, ['  load_started     table=orders',
                                 '                   -> p'])
