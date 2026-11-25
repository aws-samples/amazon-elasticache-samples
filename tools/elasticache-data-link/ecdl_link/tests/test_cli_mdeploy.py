"""`ecdl` CLI: the load timeout flag (Jira story F068), the preflight `apply` runs (Jira story F143),
crash paths (Jira story F143) and `destroy` safety (Jira story F136). AWS is faked."""

import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from ecdl_link.preflight import PIPELINE_ARNS_ENV as ARNS_ENV  # noqa: E402
from ecdl_link.tests import test_cli_apply as _apply  # noqa: E402

CLI = _apply.CLI


class LoadTimeoutFlagTest(_apply.ApplyTest):
    """Reuses ApplyTest's harness; its own tests run in test_cli_apply."""

    def test_flag_reaches_the_deployer_in_seconds(self):
        made = []
        code, _out, _ran = self.run_apply(state='new', made=made, load_timeout=30)
        self.assertEqual(code, 0)
        self.assertEqual(made[0]['load_timeout_seconds'], 1800)

    def test_without_the_flag_the_link_or_default_decides(self):
        made = []
        self.run_apply(state='new', made=made)
        self.assertIsNone(made[0]['load_timeout_seconds'])

    def test_minutes_must_be_a_positive_whole_number(self):
        self.assertEqual(CLI._positive_minutes('45'), 45)
        for bad in ('0', '-5', 'ten', '1.5'):
            with self.assertRaises(argparse.ArgumentTypeError):
                CLI._positive_minutes(bad)


def _summary(*findings, checked=True):
    return {'checked': checked, 'blocks': sum(1 for f in findings if f['level'] == 'block'),
            'warnings': sum(1 for f in findings if f['level'] != 'block'),
            'findings': list(findings)}


MISSING = {'level': 'block', 'path': 'tables', 'message': "table 'ghost' does not exist"}
RETENTION = {'level': 'block', 'path': 'source.log_retention',
             'message': 'the bulk load holds capture paused for an estimated 30.0h'}
GTID_WARN = {'level': 'warn', 'path': 'source.prerequisites', 'message': 'retention 12h'}


class ApplyPreflightTest(_apply.ApplyTest):  # Jira story F143
    """`ecdl apply` refuses what the control plane's preflight refuses."""

    def test_a_missing_table_refuses_the_apply(self):
        code, out, ran = self.run_apply(state='new', preflight=_summary(MISSING))
        self.assertEqual((code, ran), (1, []))
        self.assertIn("x tables: table 'ghost' does not exist", out)
        self.assertIn('--ignore-preflight', out)

    def test_ignore_preflight_deploys_and_still_prints_it(self):
        code, out, ran = self.run_apply(state='new', preflight=_summary(MISSING),
                                        ignore_preflight=True)
        self.assertEqual((code, len(ran)), (0, 1))
        self.assertIn("! tables: table 'ghost' does not exist", out)

    def test_warnings_are_printed_and_deploy(self):
        code, out, ran = self.run_apply(state='new', preflight=_summary(GTID_WARN))
        self.assertEqual((code, len(ran)), (0, 1))
        self.assertIn('! source.prerequisites: retention 12h', out)

    def test_an_unreachable_source_is_said_and_deploys(self):
        unread = _summary({'level': 'warn', 'path': 'source',
                           'message': 'could not verify tables against the source: '
                                      'could not reach MySQL'}, checked=False)
        code, out, ran = self.run_apply(state='new', preflight=unread)
        self.assertEqual((code, len(ran)), (0, 1))
        self.assertIn('could not be read from this machine', out)
        self.assertIn('could not reach MySQL', out)

    def test_a_retention_block_refuses_a_deploy_that_pauses(self):
        code, out, ran = self.run_apply(state='new', steps=self.PAUSE_STEPS,
                                        preflight=_summary(RETENTION))
        self.assertEqual((code, ran), (1, []))
        self.assertEqual(out.count('estimated 30.0h'), 1)

    def test_a_retention_block_is_moot_for_an_update(self):
        code, out, ran = self.run_apply(steps=self.CDC_STEPS, the_review=_apply.review('deploy'),
                                        preflight=_summary(RETENTION))
        self.assertEqual((code, len(ran)), (0, 1))
        self.assertTrue(ran[0].get('for_update'))
        self.assertNotIn('estimated 30.0h', out)

    def test_a_retention_block_is_overridable(self):
        code, out, ran = self.run_apply(state='new', steps=self.PAUSE_STEPS,
                                        preflight=_summary(RETENTION), ignore_preflight=True)
        self.assertEqual((code, len(ran)), (0, 1))
        self.assertIn('! source.log_retention', out)


class SourcePreflightTest(unittest.TestCase):  # Jira story F143
    """_source_preflight calls the control plane's preflight."""

    def run_it(self, resolution, behaviour=None, publication=('ecdl_demo_pub', None)):
        import ecdl_link.preflight as pre
        import ecdl_link.resolve as resolve
        calls = []

        def fake(document, region=None, clients=None, driver=None, publication=None):
            calls.append({'document': document, 'region': region, 'clients': clients,
                          'publication': publication})
            if behaviour:
                return behaviour()
            return {'checked': True, 'blocks': 0, 'warnings': 0, 'findings': []}
        saved = (pre.preflight, resolve.Clients, CLI._link_publication)
        pre.preflight = fake
        resolve.Clients = lambda **k: ('clients', k)
        CLI._link_publication = lambda args, name: publication
        try:
            out = CLI._source_preflight(argparse.Namespace(region='us-east-2', profile='p'),
                                        resolution)
        finally:
            pre.preflight, resolve.Clients, CLI._link_publication = saved
        return out, calls

    RES = {'name': 'demo', 'link': {'name': 'demo', 'tables': ['orders']},
           'source': {'engine': 'mysql'}, 'target': {}}

    def test_runs_on_the_link_document_with_this_machines_credentials(self):
        _out, calls = self.run_it(self.RES)
        self.assertEqual(calls[0]['document'], self.RES['link'])
        self.assertEqual(calls[0]['clients'], ('clients', {'region': 'us-east-2', 'profile': 'p'}))
        self.assertIsNone(calls[0]['publication'])

    def test_postgres_gets_the_deployed_publication_or_none(self):
        pg = dict(self.RES, source={'engine': 'postgresql'})
        _out, calls = self.run_it(pg)
        self.assertEqual(calls[0]['publication'], 'ecdl_demo_pub')
        _out, calls = self.run_it(pg, publication=('ecdl_demo_pub', 'AccessDenied'))
        self.assertIsNone(calls[0]['publication'])

    def test_a_failure_is_a_warning(self):
        def boom():
            raise RuntimeError('NoRegionError')
        out, _calls = self.run_it(self.RES, behaviour=boom)
        self.assertEqual((out['checked'], out['blocks'], out['warnings']), (False, 0, 1))
        self.assertIn('NoRegionError', out['findings'][0]['message'])

    def test_secret_findings_are_added(self):
        res = dict(self.RES, source={'engine': 'mysql', 'secret': {
            'arn': 'arn:aws:secretsmanager:us-east-2:1:secret:db', 'name': 'db',
            'tag_keys': []}})
        saved = os.environ.get('ECDL_PIPELINE_SECRET_ARNS')
        os.environ[ARNS_ENV] = ''
        try:
            out, _calls = self.run_it(res)
        finally:
            if saved is None:
                os.environ.pop('ECDL_PIPELINE_SECRET_ARNS', None)
            else:
                os.environ['ECDL_PIPELINE_SECRET_ARNS'] = saved
        self.assertEqual(out['warnings'], 1)
        self.assertEqual(out['findings'][0]['path'], 'source.secret_arn')


class CrashPathTest(unittest.TestCase):  # Jira story F143
    """Bad input ends with a message and exit 1, never a traceback."""

    def file(self, text, suffix='.yaml'):
        handle = tempfile.NamedTemporaryFile('w', suffix=suffix, delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def call(self, fn, **kw):
        out, code = io.StringIO(), 0
        try:
            with contextlib.redirect_stdout(out):
                fn(argparse.Namespace(region=None, profile=None, **kw))
        except SystemExit as exc:
            code = exc.code
        return code, out.getvalue()

    def test_validate_shows_an_external_source(self):
        path = self.file(
            'name: ext\nmode: cdc\n'
            'source: {kind: external, engine: postgresql, host: db.local, port: 6432,\n'
            '         database: shop, username: repl,\n'
            '         secret_arn: "arn:aws:secretsmanager:us-east-2:111122223333:secret:s-AbC"}\n'
            'target: {arn: "arn:aws:elasticache:us-east-2:111122223333:replicationgroup:c"}\n'
            'tables: [orders]\n')
        code, out = self.call(CLI.cmd_validate, file=path, show=False)
        self.assertEqual(code, 0)
        self.assertIn('source  postgresql db.local:6432 (external)', out)

    def test_broken_yaml_is_reported_by_every_command(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest('PyYAML is not installed')
        bad = self.file('name: x\nmode: [cdc\n')
        cases = [
            (CLI.cmd_validate, {'file': bad, 'show': False}),
            (CLI.cmd_resolve, {'file': bad, 'json': False}),
            (CLI.cmd_plan, {'file': bad, 'json': False}),
            (CLI.cmd_signal, {'file': bad, 'action': 'ddl', 'tables': None, 'wait': 0}),
            (CLI.cmd_diff, {'current': bad, 'desired': bad}),
        ]
        for fn, kw in cases:
            code, out = self.call(fn, **kw)
            self.assertEqual(code, 1, fn.__name__)
            self.assertIn('could not be read', out, fn.__name__)
            self.assertIn('not valid YAML', out, fn.__name__)

    def test_broken_json_and_a_missing_file(self):
        code, out = self.call(CLI.cmd_resolve, file=self.file('{"name": ', '.json'), json=False)
        self.assertEqual(code, 1)
        self.assertIn('not valid JSON', out)
        code, out = self.call(CLI.cmd_plan, file='/nonexistent/link.yaml', json=False)
        self.assertEqual(code, 1)
        self.assertIn('not found', out)


# Inherited ApplyTest tests run in test_cli_apply; not again here.
for _name in [n for n in dir(_apply.ApplyTest) if n.startswith('test_')]:
    if _name not in ApplyPreflightTest.__dict__:
        setattr(ApplyPreflightTest, _name, None)


class DestroyBacklogTest(unittest.TestCase):  # Jira story F136
    AT_RISK = {'exists': True, 'readable': True, 'at_risk': True, 'unapplied': 4,
               'dead_letters_total': 1, 'backlog': {'data:shop.a': 4},
               'dead_letters': {'dlq:data:shop.a': 1}}

    def run_destroy(self, data, answer=None, run_error=None, **flags):
        import builtins
        import ecdl_link.deploy as deploy
        import ecdl_link.teardown as teardown
        runs = []

        class FakeDestroyer:
            def __init__(self, *a, **k):
                self.kw = k

            def broker_data(self, plan_doc):
                return dict(data)

            def run(self, plan_doc):
                runs.append(self.kw.get('delete_backlog'))
                if run_error:
                    raise deploy.DeployError(run_error, 'wait')
                return {'link': 'shop', 'deleted': [], 'skipped': [], 'kept': [],
                        'cluster_deleted': False, 'follow_up': plan_doc['follow_up']}
        saved = (deploy.Clients, teardown.Destroyer, builtins.input)
        deploy.Clients, teardown.Destroyer = (lambda **k: None), FakeDestroyer
        builtins.input = lambda prompt='': answer
        args = argparse.Namespace(link='shop', stack=None, prefix='ecdl', dry_run=False,
                                  yes=answer is None, region=None, profile=None,
                                  keep_cluster=False, delete_backlog=False)
        for k, v in flags.items():
            setattr(args, k, v)
        out, code = io.StringIO(), 0
        try:
            with contextlib.redirect_stdout(out):
                CLI.cmd_destroy(args)
        except SystemExit as exc:
            code = exc.code
        finally:
            deploy.Clients, teardown.Destroyer, builtins.input = saved
        return code, out.getvalue(), runs

    def test_yes_alone_does_not_delete_a_backlog(self):
        code, out, runs = self.run_destroy(self.AT_RISK)
        self.assertEqual((code, runs), (1, []))
        self.assertIn('4 change(s) not yet written', out)
        self.assertIn('data:shop.a: 4 not yet written', out)
        self.assertIn('dlq:data:shop.a: 1 dead letter', out)
        self.assertIn('--delete-backlog', out)

    def test_delete_backlog_with_yes_deletes(self):
        code, _out, runs = self.run_destroy(self.AT_RISK, delete_backlog=True)
        self.assertEqual((code, runs), (0, [True]))

    def test_the_prompt_names_it_and_yes_confirms(self):
        code, out, runs = self.run_destroy(self.AT_RISK, answer='y')
        self.assertEqual((code, runs), (0, [True]))
        code, out, runs = self.run_destroy(self.AT_RISK, answer='n')
        self.assertEqual(runs, [])
        self.assertIn('Cancelled', out)

    def test_an_unreadable_broker_is_said_and_does_not_block(self):
        code, out, runs = self.run_destroy({'exists': True, 'readable': False,
                                            'at_risk': False, 'reason': 'timed out'})
        self.assertEqual((code, runs), (0, [False]))
        self.assertIn('could not read the broker (timed out)', out)

    def test_dry_run_reports_and_deletes_nothing(self):
        code, out, runs = self.run_destroy(self.AT_RISK, dry_run=True)
        self.assertIn(code, (0, None))
        self.assertEqual(runs, [])
        self.assertIn('4 change(s)', out)
        self.assertIn('nothing was deleted', out)

    def test_a_refused_run_exits_non_zero(self):
        code, out, _runs = self.run_destroy(
            {'exists': False, 'at_risk': False},
            run_error="pipeline 'shop' is being changed right now (ecdl-shop-cdc "
                      "UPDATE_IN_PROGRESS); nothing was deleted")
        self.assertEqual(code, 1)
        self.assertIn('being changed right now', out)


# ApplyTest's own tests are collected from test_cli_apply; do not run them twice.
for _name in [n for n in dir(_apply.ApplyTest) if n.startswith('test_')]:
    if _name not in LoadTimeoutFlagTest.__dict__:
        setattr(LoadTimeoutFlagTest, _name, None)


if __name__ == '__main__':
    unittest.main()
