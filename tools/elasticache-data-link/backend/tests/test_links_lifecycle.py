#!/usr/bin/env python3
"""Deploy, delete and list safety in links.py. Fakes only, no AWS.

Jira story F103  capture paused is kept on the record, outside the capped events, and a
      failed save is logged
Jira story F085  a restart while capture was being paused is flagged as possibly paused
Jira story F135  a healthy link with Debezium at 0 is listed as paused
Jira story F136  deleting a broker with unapplied changes or dead letters needs consent
Jira story F160  a key clash with another link is not overridden by ignore_preflight
Jira story F163  one audit line per deploy, destroy and repair, and per finished run

Run: python3 tests/test_links_lifecycle.py   (exit 0 = all pass)
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))
os.environ.setdefault('ECDL_DEPLOY_STATE_PATH', '')

import links  # noqa: E402
import test_links as base  # noqa: E402

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append('{}: got {!r}, want {!r}'.format(name, got, want))


def check_true(name, cond):
    if not cond:
        FAILURES.append('{}: expected true'.format(name))


@contextlib.contextmanager
def audit_lines():
    """Capture the [audit] lines printed while the block runs."""
    out = io.StringIO()
    lines = []
    with contextlib.redirect_stdout(out):
        yield lines
    for line in out.getvalue().splitlines():
        if line.startswith(links._AUDIT_PREFIX):
            lines.append(json.loads(line[len(links._AUDIT_PREFIX):]))


def wait_for(record_id, status='running'):
    for _ in range(200):
        rec = links._DEPLOYMENTS.get(record_id) or {}
        if rec.get('status') != status:
            return rec
        time.sleep(0.01)
    return links._DEPLOYMENTS.get(record_id) or {}


# ── Jira stories F103 / F085: the capture state on the record ────────────────────────────
def _restart_with(events, extra=None):
    """Write a running record with `events`, restart, return the record."""
    saved_path, saved = links._STATE_PATH, dict(links._DEPLOYMENTS)
    tmp = tempfile.mkdtemp()
    try:
        links._STATE_PATH = os.path.join(tmp, 'deployments.json')
        links._DEPLOYMENTS.clear()
        links._record('deploy-demo-1', id='deploy-demo-1', link='demo', status='running',
                      cluster_name='ecdl-demo', debezium_service='ecdl-demo-cdc-debezium',
                      started_at=1, **(extra or {}))
        for event in events:
            links._append_event('deploy-demo-1', event)
        links._DEPLOYMENTS.clear()
        links._restore_deployments()
        return dict(links._DEPLOYMENTS['deploy-demo-1'])
    finally:
        links._STATE_PATH = saved_path
        links._DEPLOYMENTS.clear()
        links._DEPLOYMENTS.update(saved)


def test_pause_state_survives_the_event_cap():
    # The record keeps capture_pause itself; 600 later events push the pause
    # event out of the 500 kept, as on a long load.
    noise = [{'event': 'load_waiting', 'table': 'orders'}] * 600
    rec = _restart_with(noise, extra={'capture_pause': 'paused', 'capture_paused_at': 5})
    check('cap.events_capped', len(rec['events']), 500)
    check('cap.paused', rec.get('capture_paused'), True)
    check_true('cap.hint', rec['hint'].startswith('CAPTURE WAS LEFT PAUSED.'))


def test_old_records_are_read_from_their_events():
    rec = _restart_with([{'event': 'action_complete', 'step': 'pause_capture'}])
    check('old.paused', rec.get('capture_paused'), True)
    rec = _restart_with([{'event': 'action_complete', 'step': 'pause_capture'},
                         {'event': 'action_complete', 'step': 'resume_capture'}])
    check('old.resumed', rec.get('capture_paused'), None)
    check_true('old.resumed.hint', rec['hint'].startswith('Deploy again'))


def test_a_restart_during_the_pause_step_is_possibly_paused():  # Jira story F085
    rec = _restart_with([{'event': 'action_started', 'step': 'pause_capture'}])
    check('pausing.flag', rec.get('capture_paused'), True)
    check_true('pausing.hint', rec['hint'].startswith('CAPTURE MAY HAVE BEEN LEFT PAUSED'))
    check_true('pausing.hint.cmd', 'describe-services --cluster ecdl-demo --services '
                                   'ecdl-demo-cdc-debezium' in rec['hint'])


def test_transitions():
    t = links.capture_pause_transition
    check('t.start', t({'event': 'action_started', 'step': 'pause_capture'}), 'pausing')
    check('t.done', t({'event': 'action_complete', 'step': 'reset_pause_capture'}), 'paused')
    check('t.resume', t({'event': 'action_complete', 'step': 'resume_capture'}), 'resumed')
    check('t.after_failure', t({'event': 'capture_resumed_after_failure'}), 'resumed')
    check('t.unconfirmed', t({'event': 'capture_resume_unconfirmed'}), 'resumed')
    check('t.resume_started', t({'event': 'action_started', 'step': 'resume_capture'}), None)
    check('t.other', t({'event': 'stack_complete', 'step': 'cdc'}), None)
    check('state.field_wins', links.capture_pause_state(
        {'capture_pause': None, 'events': [{'event': 'action_complete',
                                            'step': 'pause_capture'}]}), None)


def test_the_deploy_records_the_pause_on_the_record():
    def run(plan_doc):
        progress = made[0]['on_progress']
        progress({'event': 'action_started', 'step': 'pause_capture'})
        progress({'event': 'action_complete', 'step': 'pause_capture'})
        snapshot.append(dict(links._DEPLOYMENTS[dep[0]]))
        progress({'event': 'action_complete', 'step': 'resume_capture'})
        return {'completed': [], 'outputs': {}}
    made, snapshot, dep = [], [], []
    real = links._record

    def record(deployment_id, **fields):
        if not dep:
            dep.append(deployment_id)
        return real(deployment_id, **fields)
    links._record = record
    try:
        with audit_lines():
            res, _ = base._with_deploy_fakes({}, {'link': {'name': 'demo'}},
                                             deployer_run=run, made=made)
        rec = wait_for(res.get_json()['deployment_id'])
    finally:
        links._record = real
    check('rec.paused_mid_run', snapshot and snapshot[0].get('capture_pause'), 'paused')
    check_true('rec.paused_at', snapshot and snapshot[0].get('capture_paused_at'))
    check('rec.resumed', rec.get('capture_pause'), None)
    check('rec.paused_at_cleared', rec.get('capture_paused_at'), None)


def test_a_failed_save_is_logged_once_and_its_recovery_too():
    import logging
    saved_path = links._STATE_PATH
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    links._LOG.addHandler(handler)
    blocker = tempfile.mkdtemp()
    open(os.path.join(blocker, 'file'), 'w').close()
    try:
        # A path under a FILE cannot be created.
        links._STATE_PATH = os.path.join(blocker, 'file', 'deployments.json')
        with links._DEPLOY_LOCK:
            links._persist_locked()
            links._persist_locked()
        check('persist.logged_once', stream.getvalue().count('could not save'), 1)
        links._STATE_PATH = os.path.join(blocker, 'ok.json')
        with links._DEPLOY_LOCK:
            links._persist_locked()
        check_true('persist.recovered', 'being saved to' in stream.getvalue())
    finally:
        links._LOG.removeHandler(handler)
        links._STATE_PATH = saved_path
        links._PERSIST_ERROR[0] = None


# ── Jira story F135: Debezium at 0 is shown as paused ──────────────────────────────────
def _services(dbz_desired):
    return [{'serviceName': 'ecdl-uitest-cdc-debezium', 'status': 'ACTIVE',
             'desiredCount': dbz_desired, 'runningCount': dbz_desired},
            {'serviceName': 'ecdl-uitest-cdc-sink', 'status': 'ACTIVE',
             'desiredCount': 2, 'runningCount': 2}]


def test_a_paused_link_is_listed_as_paused():
    clients = base.FakeClients(ecs=base.FakeEcs(services=_services(0)))
    item = links._discovered_item(clients, 'uitest', base.STACKS)
    check('paused.status', item['status'], 'paused')
    check('paused.flag', item.get('capture_paused'), True)
    check_true('paused.note', 'desired count is 0' in item.get('capture_paused_note', ''))


def test_a_running_link_is_still_succeeded():
    clients = base.FakeClients(ecs=base.FakeEcs(services=_services(1)))
    item = links._discovered_item(clients, 'uitest', base.STACKS)
    check('running.status', item['status'], 'succeeded')
    check('running.flag', item.get('capture_paused'), None)


def test_a_failed_link_stays_failed_and_unknown_services_change_nothing():
    stacks = dict(base.STACKS, cdc=dict(base.STACKS['cdc'], status='UPDATE_ROLLBACK_COMPLETE'))
    clients = base.FakeClients(ecs=base.FakeEcs(services=_services(0)))
    check('failed.status', links._discovered_item(clients, 'uitest', stacks)['status'], 'failed')
    clients = base.FakeClients(ecs=base.FakeEcs(error=True))
    check('unknown.status', links._discovered_item(clients, 'uitest', base.STACKS)['status'],
          'succeeded')


def test_the_list_shows_a_tracked_run_of_a_paused_link_as_paused():
    from flask import Flask
    saved = (links.discovered_deployments, dict(links._DEPLOYMENTS))
    links._DEPLOYMENTS.clear()
    links._DEPLOYMENTS['deploy-uitest-1'] = {'id': 'deploy-uitest-1', 'link': 'uitest',
                                             'status': 'succeeded', 'events': []}
    links.discovered_deployments = lambda region=None: ([
        {'link': 'uitest', 'status': 'paused', 'capture_paused': True,
         'capture_paused_note': 'n', 'updated': ''}], None)
    try:
        app = Flask(__name__)
        app.register_blueprint(links.links_bp)
        rows = app.test_client().get('/api/links/deployments').get_json()['deployments']
        row = next(r for r in rows if r['id'] == 'deploy-uitest-1')
        check('list.status', row['status'], 'paused')
        check('list.run_status', row.get('run_status'), 'succeeded')
        check('list.stored_unchanged', links._DEPLOYMENTS['deploy-uitest-1']['status'],
              'succeeded')
    finally:
        links.discovered_deployments = saved[0]
        links._DEPLOYMENTS.clear()
        links._DEPLOYMENTS.update(saved[1])


# ── Jira story F160: a key clash is not overridden ─────────────────────────────────────
CLASH = {'checked': True, 'blocks': 1, 'warnings': 0, 'findings': [
    {'level': 'block', 'path': 'target', 'kind': links.KEY_CONFLICT,
     'message': "pipeline 'other' already writes orders:* into this cache"}]}


def test_a_key_clash_refuses_even_with_ignore_preflight():
    for ignore in (False, True):
        made = []
        with audit_lines() as lines:
            res, _ = base._with_deploy_fakes({}, {'link': {'name': 'demo'},
                                                  'ignore_preflight': ignore},
                                             preflight=CLASH, made=made)
        body = res.get_json()
        check('clash.status.{}'.format(ignore), res.status_code, 409)
        check('clash.stage.{}'.format(ignore), body['stage'], 'key_conflict')
        check_true('clash.hint.{}'.format(ignore), 'does not override' in body['hint'])
        check('clash.nothing_ran.{}'.format(ignore), made, [])
        check('clash.audit.{}'.format(ignore),
              [(x['action'], x['result'], x.get('stage')) for x in lines],
              [('deploy', 'refused', 'key_conflict')])


def test_other_blocks_are_still_overridable():
    other = {'checked': True, 'blocks': 1, 'warnings': 0, 'findings': [
        {'level': 'block', 'path': 'tables', 'message': "table 'x' does not exist"}]}
    with audit_lines():
        res, _ = base._with_deploy_fakes({}, {'link': {'name': 'demo'},
                                              'ignore_preflight': True}, preflight=other)
    check('other.status', res.status_code, 202)
    wait_for(res.get_json()['deployment_id'])


def test_the_conflict_check_marks_its_findings():
    import ecdl_link.conflicts  # noqa: F401
    saved = links.resolution_conflicts
    links.resolution_conflicts = lambda cfn, resolution: [
        {'link': 'other', 'key_prefixes': ['orders:']}]
    try:
        resolution = {'target': {'endpoint': 'cache.example'}}
        import types
        sys.modules.setdefault('boto3', types.ModuleType('boto3'))
        boto3 = sys.modules['boto3']
        real_client = getattr(boto3, 'client', None)
        boto3.client = lambda *a, **k: object()
        try:
            links._check_key_conflicts(resolution, None)
        finally:
            if real_client is not None:
                boto3.client = real_client
        finding = resolution['preflight']['findings'][0]
        check('mark.kind', finding.get('kind'), links.KEY_CONFLICT)
        check('mark.level', finding['level'], 'block')
    finally:
        links.resolution_conflicts = saved


# ── Jira story F163: audit lines ───────────────────────────────────────────────────────
def test_a_deploy_writes_accepted_then_succeeded():
    with audit_lines() as lines:
        res, _ = base._with_deploy_fakes({}, {'link': {'name': 'demo'}})
        dep = res.get_json()['deployment_id']
        wait_for(dep)
        time.sleep(0.05)
    results = [(x['action'], x['link'], x['result']) for x in lines]
    check('audit.deploy', sorted(results), [('deploy', 'demo', 'accepted'),
                                           ('deploy', 'demo', 'succeeded')])
    for line in lines:
        check('audit.id.{}'.format(line['result']), line.get('deployment_id'), dep)
        check('audit.source.{}'.format(line['result']), line['source'].get('path'),
              '/api/links/deploy')
        check_true('audit.when.{}'.format(line['result']), line['at'].endswith('Z'))


def test_a_failed_run_is_audited_with_its_error():
    def fail(plan_doc):
        raise links.DeployError('stack ecdl-demo-cdc ended in CREATE_FAILED', step='cdc')
    with audit_lines() as lines:
        res, _ = base._with_deploy_fakes({}, {'link': {'name': 'demo'}}, deployer_run=fail)
        wait_for(res.get_json()['deployment_id'])
        time.sleep(0.05)
    failed = [x for x in lines if x['result'] == 'failed']
    check('audit.failed.count', len(failed), 1)
    check('audit.failed.step', failed and failed[0].get('failed_step'), 'cdc')


def test_audit_never_carries_the_token_or_the_body():
    from flask import Flask
    app = Flask(__name__)

    @app.route('/x', methods=['POST'])
    def x():
        links._audit('deploy', 'demo', 'accepted')
        return 'ok'
    with audit_lines() as lines:
        app.test_client().post('/x', json={'link': {'secret_arn': 'arn:secret'}},
                               headers={'Authorization': 'Bearer s3cr3t-token',
                                        'X-Request-Id': 'req-42',
                                        'X-Forwarded-For': '10.1.2.3, 10.0.0.1'})
    text = json.dumps(lines)
    check_true('audit.no_token', 's3cr3t' not in text and 'Bearer' not in text)
    check_true('audit.no_body', 'arn:secret' not in text)
    check('audit.request_id', lines[0]['source'].get('request_id'), 'req-42')
    check('audit.forwarded', lines[0]['source'].get('forwarded_for'), '10.1.2.3')


def test_a_dry_run_is_not_audited():
    with audit_lines() as lines:
        base._with_deploy_fakes({}, {'link': {'name': 'demo'}, 'dry_run': True})
    check('audit.dry_run', lines, [])


def test_repair_apply_is_audited_as_repair():
    with audit_lines() as lines:
        res, _sent = base._repair({'desired': base.DESIRED, 'apply': True}, base.ALL_DEPLOYED,
                                  base._review('deploy', 'advanced.sink'))
    check('audit.repair.status', res.status_code, 202)
    check('audit.repair', [(x['action'], x['result']) for x in lines],
          [('repair', 'accepted')])


# ── Jira story F136 + Jira story F163: deleting ───────────────────────────────────────────────────
def _with_broker(data, fn):
    """Destroy endpoints with the broker holding `data`."""
    real = links.Destroyer

    class _Destroyer(real):
        def broker_data(self, plan_doc):
            return dict(data)

        def run(self, plan_doc):
            ran.append(self.delete_backlog)
            return {'deleted': [], 'skipped': [], 'kept': [], 'follow_up': {},
                    'cluster_deleted': False}
    ran = []

    def inner(client, _ran):
        links.Destroyer = _Destroyer
        try:
            return fn(client, ran)
        finally:
            links.Destroyer = real
    return base._with_destroy_fakes(base._LIVE, inner)


AT_RISK = {'exists': True, 'readable': True, 'at_risk': True, 'unapplied': 7,
           'dead_letters_total': 2, 'backlog': {'data:a': 7}, 'dead_letters': {'dlq:a': 2}}


def test_destroy_plan_reports_what_the_broker_holds():
    def run(client, ran):
        body = client.get('/api/links/destroy-plan?link=shop').get_json()
        check('dplan.broker', body['broker']['unapplied'], 7)
        check_true('dplan.summary', '7 change(s) not yet written' in body['broker_summary'])
        check('dplan.nothing_ran', ran, [])
    _with_broker(AT_RISK, run)


def test_destroy_with_a_backlog_needs_delete_backlog():
    def run(client, ran):
        with audit_lines() as lines:
            res = client.post('/api/links/destroy', json={'link': 'shop', 'confirm': 'shop'})
        check('dback.status', res.status_code, 409)
        check('dback.stage', res.get_json()['stage'], 'backlog')
        check_true('dback.counts', '7 change(s)' in res.get_json()['error']
                   and '2 dead letter' in res.get_json()['error'])
        check('dback.nothing_ran', ran, [])
        check('dback.audit', [(x['action'], x['result'], x.get('stage')) for x in lines],
              [('destroy', 'refused', 'backlog')])
        with audit_lines() as lines:
            res = client.post('/api/links/destroy', json={'link': 'shop', 'confirm': 'shop',
                                                          'delete_backlog': True})
            check('dback.ok', res.status_code, 202)
            wait_for(res.get_json()['destroy_id'])
            time.sleep(0.05)
        check('dback.ran', ran, [True])
        check('dback.audit.ok', sorted(x['result'] for x in lines), ['accepted', 'succeeded'])
        done = [x for x in lines if x['result'] == 'succeeded'][0]
        check('dback.audit.counts', (done.get('unapplied'), done.get('dead_letters')), (7, 2))
    _with_broker(AT_RISK, run)


def test_destroy_with_nothing_at_risk_needs_no_flag():
    def run(client, ran):
        with audit_lines():
            res = client.post('/api/links/destroy', json={'link': 'shop', 'confirm': 'shop'})
            wait_for(res.get_json()['destroy_id'])
        check('dclean.status', res.status_code, 202)
    _with_broker({'exists': True, 'readable': False, 'at_risk': False, 'reason': 'x'}, run)


def test_destroy_refusals_are_audited():
    def run(client, ran):
        with audit_lines() as lines:
            client.post('/api/links/destroy', json={'link': 'shop', 'confirm': 'sho'})
            client.post('/api/links/destroy', json={'link': 'gone', 'confirm': 'gone'})
        check('drefuse.audit', [(x['link'], x['result'], x['status']) for x in lines],
              [('shop', 'refused', 400), ('gone', 'refused', 404)])
    base._with_destroy_fakes(base._LIVE, run)


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append('{}: raised {}: {}'.format(t.__name__, type(exc).__name__, exc))
    if FAILURES:
        print('FAIL ({} problem(s)):'.format(len(FAILURES)))
        for f in FAILURES:
            print('  -', f)
        return 1
    print('OK ({} tests)'.format(len(tests)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
