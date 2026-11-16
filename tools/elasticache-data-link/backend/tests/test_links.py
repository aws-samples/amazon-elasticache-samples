#!/usr/bin/env python3
"""Unit tests for the deployment enrichment in links.py.
Pure-logic only: fake CloudFormation/ECS clients, no AWS.
Run: python3 test_links.py   (exit 0 = all pass)

The gap these cover: the Deployments page showed only a link name and "3 of 3
steps", and a click on a discovered deployment 404'd, because the detail endpoint
only knew in-memory tracked runs. So a live pipeline's wiring -- what it reads
from, the broker, the target, the streams, and whether its tasks are running --
was invisible in the UI and had to be found with the CLI.

The wiring comes from the CDC stack's parameters and outputs, and the live status
from describe_services, so faking those two clients exercises the whole path.
"""
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, _BACKEND)                   # backend/  (import links, app, ...)
sys.path.insert(0, os.path.dirname(_BACKEND))  # repo root (backend modules import ecdl_link)
# Keep test deployments out of backend/data; persistence is tested with a temp path.
os.environ.setdefault('ECDL_DEPLOY_STATE_PATH', '')

import links  # noqa: E402

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append('{}: got {!r}, want {!r}'.format(name, got, want))


def check_true(name, cond):
    if not cond:
        FAILURES.append('{}: expected true'.format(name))


# ── fakes ──────────────────────────────────────────────────────────────────

CDC_STACK = {
    'Parameters': [
        {'ParameterKey': 'SourceEngine', 'ParameterValue': 'mysql'},
        {'ParameterKey': 'SourceDBHost', 'ParameterValue': 'aurora.example'},
        {'ParameterKey': 'SourceDBPort', 'ParameterValue': '3306'},
        {'ParameterKey': 'SourceDBName', 'ParameterValue': 'Inventory'},
        {'ParameterKey': 'SourceDBUser', 'ParameterValue': 'admin'},
        {'ParameterKey': 'Tables', 'ParameterValue': 'Inventory.users'},
        {'ParameterKey': 'BrokerHost', 'ParameterValue': 'broker.example'},
        {'ParameterKey': 'BrokerPort', 'ParameterValue': '6379'},
        {'ParameterKey': 'BrokerSSL', 'ParameterValue': 'true'},
        {'ParameterKey': 'OffsetKey', 'ParameterValue': 'ecdl:uitest:offsets'},
        {'ParameterKey': 'SchemaHistoryKey',
         'ParameterValue': 'ecdl:uitest:schema_history'},
        {'ParameterKey': 'TargetHost', 'ParameterValue': 'target.example'},
        {'ParameterKey': 'TargetPort', 'ParameterValue': '6379'},
        {'ParameterKey': 'TargetSSL', 'ParameterValue': 'false'},
        {'ParameterKey': 'TargetCluster', 'ParameterValue': 'true'},
        {'ParameterKey': 'Streams',
         'ParameterValue': 'data:uitest.Inventory.users,data:uitest.Inventory.orders'},
        {'ParameterKey': 'TopicPrefix', 'ParameterValue': 'uitest'},
        {'ParameterKey': 'ClusterName', 'ParameterValue': 'ecdl-uitest'},
        {'ParameterKey': 'DBSecretArn',
         'ParameterValue': 'arn:aws:secretsmanager:us-east-2:1:secret:db-x'},
        {'ParameterKey': 'TransformsJson',
         'ParameterValue': '{"tables":{"users":{"columns":["id","name"]}}}'},
    ],
    'Outputs': [
        {'OutputKey': 'DebeziumService', 'OutputValue': 'ecdl-uitest-cdc-debezium'},
        {'OutputKey': 'SinkService', 'OutputValue': 'ecdl-uitest-cdc-sink'},
        {'OutputKey': 'SinkScaling', 'OutputValue': 'min=1 desired=2 max=10'},
    ],
}

LIST_PAGE = {'StackSummaries': [
    {'StackName': 'ecdl-uitest-broker', 'StackStatus': 'CREATE_COMPLETE',
     'CreationTime': '2026-09-15T03:49:13Z'},
    {'StackName': 'ecdl-uitest-cdc', 'StackStatus': 'CREATE_COMPLETE',
     'CreationTime': '2026-09-15T03:56:35Z'},
    {'StackName': 'ecdl-uitest-loader', 'StackStatus': 'CREATE_COMPLETE',
     'CreationTime': '2026-09-15T03:59:06Z'},
    # A non-ecdl stack and an unrelated kind, to prove the filter holds.
    {'StackName': 'some-other-stack', 'StackStatus': 'CREATE_COMPLETE',
     'CreationTime': '2026-01-01T00:00:00Z'},
]}

SERVICES = [
    {'serviceName': 'ecdl-uitest-cdc-debezium', 'status': 'ACTIVE',
     'desiredCount': 1, 'runningCount': 1},
    {'serviceName': 'ecdl-uitest-cdc-sink', 'status': 'ACTIVE',
     'desiredCount': 2, 'runningCount': 1},
]


class FakeCfn:
    def __init__(self, describe_error=False):
        self.describe_error = describe_error

    def describe_stacks(self, StackName):
        if self.describe_error:
            raise RuntimeError('boom')
        if StackName == 'ecdl-uitest-cdc':
            return {'Stacks': [CDC_STACK]}
        raise RuntimeError('no such stack: {}'.format(StackName))

    def list_stacks(self, **kwargs):
        return dict(LIST_PAGE)  # single page, no NextToken


class FakeEcs:
    def __init__(self, services=None, error=False):
        self.services = SERVICES if services is None else services
        self.error = error

    def describe_services(self, cluster, services):
        if self.error:
            raise RuntimeError('ecs down')
        return {'services': self.services}


class FakeClients:
    def __init__(self, cfn=None, ecs=None):
        self.cloudformation = cfn or FakeCfn()
        self.ecs = ecs or FakeEcs()


STACKS = {
    'broker': {'stack': 'ecdl-uitest-broker', 'status': 'CREATE_COMPLETE',
               'updated': '2026-09-15 03:49:13'},
    'cdc': {'stack': 'ecdl-uitest-cdc', 'status': 'CREATE_COMPLETE',
            'updated': '2026-09-15 03:56:35'},
    'loader': {'stack': 'ecdl-uitest-loader', 'status': 'CREATE_COMPLETE',
               'updated': '2026-09-15 03:59:06'},
}


# ── _cdc_wiring ──────────────────────────────────────────────────────────────

def test_cdc_wiring_maps_every_component():
    w = links._cdc_wiring(FakeClients(), 'ecdl-uitest-cdc')
    check('wiring.source', w['source'], {
        'engine': 'mysql', 'host': 'aurora.example', 'port': '3306',
        'database': 'Inventory', 'user': 'admin', 'tables': 'Inventory.users'})
    check('wiring.broker.host', w['broker']['host'], 'broker.example')
    check('wiring.broker.tls', w['broker']['tls'], 'true')
    check('wiring.broker.offset', w['broker']['offset_key'], 'ecdl:uitest:offsets')
    check('wiring.target.host', w['target']['host'], 'target.example')
    check('wiring.target.cluster', w['target']['cluster_mode'], 'true')
    check('wiring.topic_prefix', w['topic_prefix'], 'uitest')
    check('wiring.runtime.cluster', w['runtime']['cluster'], 'ecdl-uitest')
    check('wiring.runtime.debezium', w['runtime']['debezium_service'],
          'ecdl-uitest-cdc-debezium')


def test_cdc_wiring_splits_streams_and_derives_dlq():
    w = links._cdc_wiring(FakeClients(), 'ecdl-uitest-cdc')
    check('streams.list', [s['stream'] for s in w['streams']],
          ['data:uitest.Inventory.users', 'data:uitest.Inventory.orders'])
    check('streams.dlq', w['streams'][0]['dlq'],
          'dlq:data:uitest.Inventory.users')


def test_cdc_wiring_returns_none_on_describe_failure():
    check('wiring.describe_fail',
          links._cdc_wiring(FakeClients(cfn=FakeCfn(describe_error=True)),
                            'ecdl-uitest-cdc'), None)


# ── _service_status ──────────────────────────────────────────────────────────

def test_service_status_reports_running_and_desired():
    out = links._service_status(FakeClients(), 'ecdl-uitest',
                                ['ecdl-uitest-cdc-debezium', 'ecdl-uitest-cdc-sink'])
    check('svc.counts', {s['name']: (s['running'], s['desired']) for s in out}, {
        'ecdl-uitest-cdc-debezium': (1, 1),
        'ecdl-uitest-cdc-sink': (1, 2)})


def test_service_status_swallows_errors():
    check('svc.error', links._service_status(
        FakeClients(ecs=FakeEcs(error=True)), 'ecdl-uitest', ['x']), [])


def test_service_status_empty_without_cluster_or_names():
    check('svc.nocluster', links._service_status(FakeClients(), None, ['x']), [])
    check('svc.nonames', links._service_status(FakeClients(), 'c', []), [])


# ── _discovered_item / discovered_one / _stack_index ─────────────────────────

def test_discovered_item_carries_components_and_live_services():
    item = links._discovered_item(FakeClients(), 'uitest', STACKS)
    check('item.id', item['id'], 'stacks-uitest')
    check('item.status', item['status'], 'succeeded')
    check('item.engine', item['components']['source']['engine'], 'mysql')
    check('item.services',
          [s['name'] for s in item['components']['runtime']['services']],
          ['ecdl-uitest-cdc-debezium', 'ecdl-uitest-cdc-sink'])


def test_discovered_item_without_live_skips_ecs():
    item = links._discovered_item(FakeClients(), 'uitest', STACKS, with_live=False)
    check('item.nolive', item['components']['runtime']['services'], [])


def test_discovered_one_finds_and_misses():
    clients = FakeClients()
    check('one.found', links.discovered_one(clients, 'uitest')['link'], 'uitest')
    check('one.missing', links.discovered_one(clients, 'does-not-exist'), None)


def test_stack_index_ignores_non_ecdl_and_unknown_kinds():
    found, problem = links._stack_index(FakeClients())
    check('index.problem', problem, None)
    check('index.links', set(found), {'uitest'})
    check('index.kinds', set(found['uitest']), {'broker', 'cdc', 'loader'})


# ── the detail endpoint serves discovered deployments ────────────────────────

def _client(clients=None):
    from flask import Flask
    links.DeployClients = lambda region=None: clients or FakeClients()
    app = Flask(__name__)
    app.register_blueprint(links.links_bp)
    return app.test_client()


def test_detail_endpoint_serves_discovered_instead_of_404():
    real = links.DeployClients
    try:
        client = _client()
        res = client.get('/api/links/deployments/stacks-uitest')
        check('detail.status', res.status_code, 200)
        body = res.get_json()
        check('detail.link', body['link'], 'uitest')
        check('detail.broker', body['components']['broker']['host'], 'broker.example')
        check('detail.running',
              body['components']['runtime']['services'][0]['running'], 1)
    finally:
        links.DeployClients = real


def test_detail_endpoint_still_404s_for_unknown():
    real = links.DeployClients
    try:
        client = _client()
        res = client.get('/api/links/deployments/stacks-nope')
        check('detail.404', res.status_code, 404)
    finally:
        links.DeployClients = real


def test_detail_endpoint_enriches_a_tracked_run_with_components():
    real = links.DeployClients
    links._DEPLOYMENTS['deploy-uitest-abc'] = {
        'id': 'deploy-uitest-abc', 'link': 'uitest', 'mode': 'cdc',
        'status': 'succeeded', 'steps': ['broker', 'cdc'],
        'completed': ['broker', 'cdc'], 'events': [],
    }
    try:
        client = _client()
        body = client.get('/api/links/deployments/deploy-uitest-abc').get_json()
        check('tracked.enriched', body['components']['source']['engine'], 'mysql')
    finally:
        links.DeployClients = real
        del links._DEPLOYMENTS['deploy-uitest-abc']


# ── reconstruct_link (edit an existing pipeline without pasting YAML) ─────────

def _patch_discover(sources, targets):
    links.discover = lambda region=None: {'sources': sources, 'targets': targets}


SHOP_SOURCE = {'arn': 'arn:aws:rds:us-east-2:1:cluster:shop',
               'identifier': 'shop', 'endpoint': 'aurora.example'}
CACHE_TARGET = {'arn': 'arn:aws:elasticache:us-east-2:1:replicationgroup:cache',
                'identifier': 'cache', 'endpoint': 'target.example'}


def test_reconstruct_builds_link_from_stacks():
    real = links.discover
    try:
        _patch_discover([SHOP_SOURCE], [CACHE_TARGET])
        doc, warnings = links.reconstruct_link(FakeClients(), 'uitest')
        check('recon.name', doc['name'], 'uitest')
        # broker + cdc + loader all present -> load-then-cdc.
        check('recon.mode', doc['mode'], 'initial_load_and_cdc')
        check('recon.source.arn', doc['source']['arn'],
              'arn:aws:rds:us-east-2:1:cluster:shop')
        check('recon.source.db', doc['source']['database'], 'Inventory')
        check('recon.source.secret', doc['source']['secret_arn'],
              'arn:aws:secretsmanager:us-east-2:1:secret:db-x')
        check('recon.target.arn', doc['target']['arn'],
              'arn:aws:elasticache:us-east-2:1:replicationgroup:cache')
        check('recon.tables', doc['tables'], ['Inventory.users'])
        check('recon.transforms',
              doc['transforms']['tables']['users']['columns'], ['id', 'name'])
        check('recon.no_warnings', warnings, [])
    finally:
        links.discover = real


def test_reconstruct_matches_arn_when_endpoint_carries_a_port():
    # The stored host has no port; a discovered endpoint may. Host-only match.
    real = links.discover
    try:
        _patch_discover(
            [dict(SHOP_SOURCE, endpoint='aurora.example:3306')],
            [dict(CACHE_TARGET, endpoint='target.example:6379')])
        doc, _ = links.reconstruct_link(FakeClients(), 'uitest')
        check('recon.port.source', doc['source']['arn'].endswith('cluster:shop'),
              True)
        check('recon.port.target',
              doc['target']['arn'].endswith('replicationgroup:cache'), True)
    finally:
        links.discover = real


def test_reconstruct_warns_when_arn_not_matched():
    real = links.discover
    try:
        _patch_discover([], [])  # nothing in the account matches the endpoints
        doc, warnings = links.reconstruct_link(FakeClients(), 'uitest')
        check_true('recon.no_source_arn', 'arn' not in doc['source'])
        check_true('recon.no_target_arn', 'arn' not in doc.get('target', {}))
        check_true('recon.warns_source',
                   any('source endpoint' in w for w in warnings))
        check_true('recon.warns_target',
                   any('target endpoint' in w for w in warnings))
    finally:
        links.discover = real


def test_reconstruct_missing_link_returns_none():
    real = links.discover
    try:
        _patch_discover([], [])
        doc, warnings = links.reconstruct_link(FakeClients(), 'does-not-exist')
        check('recon.missing.doc', doc, None)
        check_true('recon.missing.warn',
                   any('no deployed stacks' in w for w in warnings))
    finally:
        links.discover = real


# ── deploy refuses a config whose tables do not exist (preflight block) ──────

def test_deploy_refuses_a_preflight_block():
    """A deploy whose source-schema preflight found a definitive miss (a table or
    column that does not exist on the source) must be refused, not started. This
    is the gap that let a link naming a nonexistent table deploy as successful."""
    real_plan = links._plan_for
    try:
        preflight = {'checked': True, 'blocks': 1, 'warnings': 0,
                     'findings': [{'level': 'block', 'path': 'tables',
                                   'message': "table 'customers' does not exist "
                                              "on the source"}]}
        links._plan_for = lambda doc, region=None, assign_public_ip=None: (
            ({'preflight': preflight, 'link': 'demo'},
             {'link': 'demo', 'mode': 'cdc', 'steps': [{'id': 'broker'}]}),
            None)
        client = _client()
        res = client.post('/api/links/deploy', json={'link': {'name': 'demo'}})
        check('deploy.block.status', res.status_code, 409)
        body = res.get_json()
        check('deploy.block.stage', body['stage'], 'preflight')
        check('deploy.block.deployed', body['deployed'], False)
        check('deploy.block.preflight', body['preflight']['blocks'], 1)
    finally:
        links._plan_for = real_plan


def test_deploy_block_is_bypassed_by_ignore_preflight():
    """The override: the same block deploys when the caller confirms the
    tables will exist by the time capture starts."""
    real_plan = links._plan_for
    real_clients = links.DeployClients
    real_deployer = links.Deployer
    real_describe = links.describe_plan
    try:
        preflight = {'checked': True, 'blocks': 1, 'warnings': 0, 'findings': []}
        links._plan_for = lambda doc, region=None, assign_public_ip=None: (
            ({'preflight': preflight, 'link': 'demo'},
             {'link': 'demo', 'mode': 'cdc', 'steps': [{'id': 'broker'}]}),
            None)
        links.DeployClients = lambda region=None: FakeClients()
        links.describe_plan = lambda plan_doc: 'plan summary'

        class _NoopDeployer:
            def __init__(self, *a, **k):
                pass

            def run(self, plan_doc):
                return {'completed': [], 'outputs': {}}

        links.Deployer = _NoopDeployer
        client = _client()
        res = client.post('/api/links/deploy',
                          json={'link': {'name': 'demo'}, 'ignore_preflight': True,
                                'confirm_redeploy': True})
        check('deploy.bypass.status', res.status_code, 202)
        check('deploy.bypass.deployed', res.get_json()['deployed'], True)
    finally:
        links._plan_for = real_plan
        links.DeployClients = real_clients
        links.Deployer = real_deployer
        links.describe_plan = real_describe


# ── redeploy safety: in-progress refusal, update plans, lock, persistence ────

class _StateCfn:
    def __init__(self, states):
        self.states = states

    def describe_stacks(self, StackName):
        if StackName not in self.states:
            raise RuntimeError('Stack with id {} does not exist'.format(StackName))
        return {'Stacks': [{'StackStatus': self.states[StackName]}]}


_CDC_PLAN = {'link': 'demo', 'mode': 'initial_load_and_cdc', 'cluster_name': 'ecdl-demo',
             'steps': [{'kind': 'stack', 'id': 'broker', 'name': 'ecdl-demo-broker'},
                       {'kind': 'stack', 'id': 'cdc', 'name': 'ecdl-demo-cdc'},
                       {'kind': 'action', 'id': 'pause_capture'},
                       {'kind': 'stack', 'id': 'loader', 'name': 'ecdl-demo-loader'}]}


def _with_deploy_fakes(states, body, build=None, deployer_run=None):
    """POST /deploy with planning, clients and the deployer faked."""
    saved = (links._plan_for, links.DeployClients, links.Deployer,
             links.describe_plan, links.build_plan, links.control_plane_security_group)
    built = []

    class _Deployer:
        def __init__(self, *a, **k):
            pass

        def run(self, plan_doc):
            if deployer_run:
                return deployer_run(plan_doc)
            return {'completed': [], 'outputs': {}}
    try:
        links._plan_for = lambda doc, region=None, assign_public_ip=None: (
            ({'preflight': {}, 'link': 'demo'}, json.loads(json.dumps(_CDC_PLAN))), None)
        links.DeployClients = lambda region=None: FakeClients(cfn=_StateCfn(states))
        links.Deployer = _Deployer
        links.describe_plan = lambda plan_doc: 'plan summary'
        links.control_plane_security_group = lambda: None

        def fake_build(resolution, **kw):
            built.append(kw)
            return dict(_CDC_PLAN, for_update=kw.get('for_update', False),
                        steps=[s for s in _CDC_PLAN['steps']
                               if s['id'] != 'pause_capture'])
        links.build_plan = build or fake_build
        res = _client(FakeClients(cfn=_StateCfn(states))).post(
            '/api/links/deploy', json=body)
        return res, built
    finally:
        (links._plan_for, links.DeployClients, links.Deployer, links.describe_plan,
         links.build_plan, links.control_plane_security_group) = saved


ALL_DEPLOYED = {'ecdl-demo-broker': 'CREATE_COMPLETE',
                'ecdl-demo-cdc': 'UPDATE_COMPLETE',
                'ecdl-demo-loader': 'CREATE_COMPLETE'}


def test_deploy_refuses_while_a_stack_is_mid_operation():
    """Another deploy (from any process) is running: never start while it is running,
    even with confirm_redeploy."""
    res, _ = _with_deploy_fakes(dict(ALL_DEPLOYED, **{'ecdl-demo-cdc': 'UPDATE_IN_PROGRESS'}),
                                {'link': {'name': 'demo'}, 'confirm_redeploy': True})
    check('inprog.status', res.status_code, 409)
    check('inprog.stage', res.get_json()['stage'], 'in_progress')


def test_redeploy_of_a_deployed_cdc_link_is_an_update_plan():
    """The finding: a redeploy re-ran the pause and the full bulk load."""
    res, built = _with_deploy_fakes(ALL_DEPLOYED,
                                    {'link': {'name': 'demo'}, 'confirm_redeploy': True})
    check('update.status', res.status_code, 202)
    check('update.for_update', built and built[0].get('for_update'), True)
    ids = [s['id'] for s in res.get_json()['plan']['steps']]
    check_true('update.no_pause', 'pause_capture' not in ids)


def test_reload_true_keeps_the_full_plan():
    res, built = _with_deploy_fakes(ALL_DEPLOYED, {'link': {'name': 'demo'},
                                                   'confirm_redeploy': True,
                                                   'reload': True})
    check('reload.status', res.status_code, 202)
    check('reload.not_replanned', built, [])


def test_a_second_deploy_of_the_same_link_is_refused_while_one_runs():
    import threading as _t
    gate = _t.Event()

    def slow(plan_doc):
        gate.wait(5)
        return {'completed': [], 'outputs': {}}
    try:
        first, _ = _with_deploy_fakes({}, {'link': {'name': 'demo'}}, deployer_run=slow)
        check('lock.first', first.status_code, 202)
        second, _ = _with_deploy_fakes({}, {'link': {'name': 'demo'}})
        check('lock.second', second.status_code, 409)
        check('lock.second.points_at_first', second.get_json().get('deployment_id'),
              first.get_json()['deployment_id'])
    finally:
        gate.set()
        for _ in range(50):
            if 'demo' not in links._RUNNING_LINKS:
                break
            time.sleep(0.05)
    check_true('lock.released', 'demo' not in links._RUNNING_LINKS)


def test_records_persist_and_an_interrupted_pause_is_flagged():
    import tempfile
    saved_path, saved = links._STATE_PATH, dict(links._DEPLOYMENTS)
    tmp = tempfile.mkdtemp()
    try:
        links._STATE_PATH = os.path.join(tmp, 'deployments.json')
        links._DEPLOYMENTS.clear()
        links._record('deploy-demo-1', id='deploy-demo-1', link='demo', status='running',
                      cluster_name='ecdl-demo', debezium_service='ecdl-demo-cdc-debezium',
                      started_at=1)
        links._append_event('deploy-demo-1', {'event': 'action_complete',
                                              'step': 'pause_capture'})
        check_true('persist.file', os.path.exists(links._STATE_PATH))
        links._DEPLOYMENTS.clear()                       # simulate a restart
        check('persist.restored', links._restore_deployments(), 1)
        rec = links._DEPLOYMENTS['deploy-demo-1']
        check('persist.status', rec['status'], 'interrupted')
        check('persist.paused', rec.get('capture_paused'), True)
        check_true('persist.hint', 'update-service' in rec['hint']
                   and 'ecdl-demo-cdc-debezium' in rec['hint'])
    finally:
        links._STATE_PATH = saved_path
        links._DEPLOYMENTS.clear()
        links._DEPLOYMENTS.update(saved)


# ── deleting a pipeline from the UI ──────────────────────────────────────────
def _with_destroy_fakes(states, fn):
    saved = (links.DeployClients, links.Destroyer, links._STATE_PATH)
    ran = []

    class _Destroyer:
        def __init__(self, clients, on_progress=None, **k):
            self.on_progress = on_progress

        def run(self, plan_doc):
            ran.append(plan_doc)
            for s in plan_doc['stacks']:
                self.on_progress({'event': 'stack_deleted', 'id': s['id'],
                                  'stack': s['name']})
            return {'deleted': [s['name'] for s in plan_doc['stacks']],
                    'skipped': [], 'kept': plan_doc['keeps'],
                    'follow_up': plan_doc['follow_up'], 'cluster_deleted': True}

    from flask import Flask
    links.DeployClients = lambda region=None: FakeClients(cfn=_StateCfn(states))
    links.Destroyer = _Destroyer
    links._STATE_PATH = None
    app = Flask(__name__)
    app.register_blueprint(links.links_bp)
    try:
        return fn(app.test_client(), ran)
    finally:
        links.DeployClients, links.Destroyer, links._STATE_PATH = saved


_LIVE = {'ecdl-shop-broker': 'CREATE_COMPLETE', 'ecdl-shop-cdc': 'CREATE_COMPLETE',
         'ecdl-shop-loader': 'CREATE_COMPLETE'}


def test_destroy_plan_lists_stacks_and_changes_nothing():
    def run(client, ran):
        body = client.get('/api/links/destroy-plan?link=shop').get_json()
        check('dplan.order', [s['id'] for s in body['stacks']], ['cdc', 'loader', 'broker'])
        check('dplan.status', body['stacks'][0]['status'], 'CREATE_COMPLETE')
        check('dplan.exists', body['exists'], True)
        check('dplan.nothing_ran', ran, [])
    _with_destroy_fakes(_LIVE, run)


def test_destroy_refuses_without_the_typed_name():
    def run(client, ran):
        res = client.post('/api/links/destroy', json={'link': 'shop', 'confirm': 'sho'})
        check('destroy.confirm', res.status_code, 400)
        check('destroy.confirm.nothing', ran, [])
    _with_destroy_fakes(_LIVE, run)


def test_destroy_refuses_a_bad_name_and_a_missing_pipeline():
    def run(client, ran):
        check('destroy.badname', client.post(
            '/api/links/destroy', json={'link': '../x', 'confirm': '../x'}).status_code, 400)
        check('destroy.missing', client.post(
            '/api/links/destroy', json={'link': 'gone', 'confirm': 'gone'}).status_code, 404)
        check('destroy.bad.nothing', ran, [])
    _with_destroy_fakes(_LIVE, run)


def test_destroy_refuses_while_a_stack_is_changing():
    busy = dict(_LIVE, **{'ecdl-shop-cdc': 'UPDATE_IN_PROGRESS'})
    def run(client, ran):
        res = client.post('/api/links/destroy', json={'link': 'shop', 'confirm': 'shop'})
        check('destroy.busy', res.status_code, 409)
        check('destroy.busy.nothing', ran, [])
    _with_destroy_fakes(busy, run)


def test_destroy_runs_and_is_tracked_like_a_deployment():
    def run(client, ran):
        res = client.post('/api/links/destroy', json={'link': 'shop', 'confirm': 'shop'})
        check('destroy.accepted', res.status_code, 202)
        destroy_id = res.get_json()['destroy_id']
        for _ in range(100):
            if links._DEPLOYMENTS.get(destroy_id, {}).get('status') != 'running':
                break
            time.sleep(0.02)
        record = links._DEPLOYMENTS[destroy_id]
        check('destroy.status', record['status'], 'succeeded')
        check('destroy.completed', record['completed'], ['cdc', 'loader', 'broker'])
        check('destroy.kind', record['kind'], 'destroy')
        check('destroy.only_its_stacks', [s['name'] for s in ran[0]['stacks']],
              ['ecdl-shop-cdc', 'ecdl-shop-loader', 'ecdl-shop-broker'])
        del links._DEPLOYMENTS[destroy_id]
    _with_destroy_fakes(_LIVE, run)


def test_list_marks_records_of_deleted_pipelines_as_gone():
    from flask import Flask
    saved = (links.discovered_deployments, dict(links._DEPLOYMENTS))
    links._DEPLOYMENTS.clear()
    links._DEPLOYMENTS['deploy-old-1'] = {'id': 'deploy-old-1', 'link': 'old',
                                          'status': 'failed', 'events': []}
    links._DEPLOYMENTS['deploy-shop-1'] = {'id': 'deploy-shop-1', 'link': 'shop',
                                           'status': 'succeeded', 'events': []}
    app = Flask(__name__)
    app.register_blueprint(links.links_bp)
    try:
        links.discovered_deployments = lambda region=None: ([{'link': 'shop'}], None)
        rows = {d['id']: d for d in app.test_client().get(
            '/api/links/deployments').get_json()['deployments']}
        check('gone.deleted', rows['deploy-old-1'].get('gone'), True)
        check('gone.live', rows['deploy-shop-1'].get('gone'), None)
        # A stack list that could not be read must never mark anything gone.
        links.discovered_deployments = lambda region=None: ([], 'AccessDenied')
        rows = {d['id']: d for d in app.test_client().get(
            '/api/links/deployments').get_json()['deployments']}
        check('gone.unknown', rows['deploy-shop-1'].get('gone'), None)
    finally:
        links.discovered_deployments = saved[0]
        links._DEPLOYMENTS.clear()
        links._DEPLOYMENTS.update(saved[1])


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as exc:                           # noqa: BLE001
            FAILURES.append('{}: raised {}: {}'.format(
                t.__name__, type(exc).__name__, exc))
    if FAILURES:
        print('FAIL ({} problem(s)):'.format(len(FAILURES)))
        for f in FAILURES:
            print('  -', f)
        return 1
    print('OK ({} tests)'.format(len(tests)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
