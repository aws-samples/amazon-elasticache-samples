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
from ecdl_link.preflight import PIPELINE_ARNS_ENV as ARNS_ENV  # noqa: E402

_ORIGINAL_CLIENTS = links.DeployClients

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

    def list_tasks(self, cluster=None, family=None, desiredStatus=None):
        return {'taskArns': []}


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
    real, real_verify = links.discover, links._verify_reconstruct
    links._verify_reconstruct = lambda clients, doc, region: []
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
        # Nothing set beyond the defaults, so no advanced section was invented.
        check('recon.no_advanced', doc.get('advanced'), None)
    finally:
        links.discover, links._verify_reconstruct = real, real_verify


class _SettingsCfn(FakeCfn):
    # The C1 link: key_prefix b beside another link, alarm email, arm64,
    # a dedicated CDC user, larger sink, and a named loader key.
    EXTRA = [('KeyPrefix', 'b'), ('AlarmEmail', 'ops@example.com'),
             ('AlarmsEnabled', 'true'), ('SinkLagAlarmSeconds', '300'),
             ('CaptureStaleAlarmSeconds', '600'), ('TableStaleSeconds', 'users=900'),
             ('CpuArchitecture', 'ARM64'), ('SinkWorkers', '32'), ('SinkMinCount', '2'),
             ('DebeziumCpu', '2048'), ('DebeziumImage', '')]

    def describe_stacks(self, StackName):
        if StackName == 'ecdl-uitest-cdc':
            stack = dict(CDC_STACK)
            stack['Parameters'] = CDC_STACK['Parameters'] + [
                {'ParameterKey': k, 'ParameterValue': v} for k, v in self.EXTRA]
            return {'Stacks': [stack]}
        if StackName == 'ecdl-uitest-loader':
            return {'Stacks': [{'Parameters': [
                {'ParameterKey': 'Tables', 'ParameterValue': 'users'},
                {'ParameterKey': 'PrimaryKeys', 'ParameterValue': 'user_id'},
                {'ParameterKey': 'LoaderWorkers', 'ParameterValue': '16'}]}]}
        if StackName == 'ecdl-uitest-broker':
            return {'Stacks': [{'Parameters': [
                {'ParameterKey': 'ClusterName', 'ParameterValue': 'ecdl-uitest-broker'},
                {'ParameterKey': 'RestoreSnapshotName', 'ParameterValue': 'ecdl-uitest-final'}]}]}
        return super().describe_stacks(StackName)


def test_reconstruct_keeps_every_setting():
    # Lifecycle C1: an editor round trip reset all of these to defaults.
    real, real_verify = links.discover, links._verify_reconstruct
    links._verify_reconstruct = lambda clients, doc, region: []
    try:
        _patch_discover([SHOP_SOURCE], [CACHE_TARGET])
        doc, warnings = links.reconstruct_link(FakeClients(cfn=_SettingsCfn()), 'uitest')
        adv = doc['advanced']
        check('settings.prefix', adv['key_prefix'], 'b')
        check('settings.alarms', adv['alarms'], {'email': 'ops@example.com',
                                              'capture_stale_seconds': 600,
                                              'table_stale_seconds': {'users': 900}})
        check('settings.arch', adv['cpu_architecture'], 'arm64')
        check('settings.sink', adv['sink'], {'workers': 32})
        check('settings.loader_keys', adv['loader'], {'primary_keys': {'users': 'user_id'}})
        check('settings.restore_snapshot', adv['broker'], {'restore_snapshot': 'ecdl-uitest-final'})
        check_true('settings.no_debezium_default', 'debezium' not in adv)
        check_true('settings.no_images', 'images' not in adv)
        check('settings.user', doc['source'].get('username'), None)
        check('settings.warnings', warnings, [])
        # It is still a valid link file.
        check('settings.valid', [x for x in links.problems_for(doc)
                                 if not x.path.endswith('arn')], [])
    finally:
        links.discover, links._verify_reconstruct = real, real_verify


def test_reconstruct_verification_fills_derived_values_and_warns_on_the_rest():
    import copy
    real_resolve, real_review = links.resolve_link, links._review_update
    calls = []

    def review(resolution, doc, clients, assign_public_ip=None):
        calls.append(copy.deepcopy(doc))
        drift = [{'stack': 'cdc', 'parameter': 'SourceDBUser', 'from': 'cdc', 'to': 'admin'},
                 {'stack': 'cdc', 'parameter': 'Mystery', 'from': 'a', 'to': 'b'}]
        if len(calls) > 1:
            drift = drift[1:]
        changes = [{'field': d['parameter'], 'parameter': d['parameter'], 'from': d['from'],
                    'to': d['to'], 'severity': 'recreate', 'consequence': 'c', 'action': 'a'}
                   for d in drift]
        return ({'diff': {'changed': True, 'changes': changes, 'severity': 'recreate'},
                 'drift': drift, 'refuse': True, 'needs_ack': False}, None, [], None)
    try:
        links.resolve_link = lambda doc, region=None: {}
        links._review_update = review
        doc = {'name': 'uitest', 'source': {}}
        warnings = links._verify_reconstruct(FakeClients(), doc, None)
        check('verify.user_filled', doc['source'].get('username'), 'cdc')
        check('verify.two_passes', len(calls), 2)
        check_true('verify.warns_rest', len(warnings) == 1 and 'Mystery' in warnings[0])
    finally:
        links.resolve_link, links._review_update = real_resolve, real_review


def test_reconstruct_verification_failure_is_a_warning_not_silence():
    real_resolve = links.resolve_link
    try:
        def boom(doc, region=None):
            raise RuntimeError('no access')
        links.resolve_link = boom
        warnings = links._verify_reconstruct(FakeClients(), {'name': 'x'}, None)
        check_true('verify.fail_warns', warnings and 'could not be checked' in warnings[0])
    finally:
        links.resolve_link = real_resolve


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
    def __init__(self, states, tags=None):
        self.states = states
        self.tags = tags or {}

    def describe_stacks(self, StackName):
        if StackName not in self.states:
            raise RuntimeError('Stack with id {} does not exist'.format(StackName))
        return {'Stacks': [{'StackStatus': self.states[StackName], 'Parameters': [],
                            'Tags': self.tags.get(StackName, [])}]}

    def get_template_summary(self, **_):
        return {'Parameters': []}


_CDC_PLAN = {'link': 'demo', 'mode': 'initial_load_and_cdc', 'cluster_name': 'ecdl-demo',
             'steps': [{'kind': 'stack', 'id': 'broker', 'name': 'ecdl-demo-broker'},
                       {'kind': 'stack', 'id': 'cdc', 'name': 'ecdl-demo-cdc'},
                       {'kind': 'action', 'id': 'pause_capture'},
                       {'kind': 'stack', 'id': 'loader', 'name': 'ecdl-demo-loader'}]}


def _with_deploy_fakes(states, body, build=None, deployer_run=None, preflight=None,
                       plan=None, cfn=None, made=None):
    """POST /deploy with planning, clients and the deployer faked.

    `made`, a list, receives the keyword arguments each Deployer was built with.
    """
    saved = (links._plan_for, links.DeployClients, links.Deployer,
             links.describe_plan, links.build_plan, links.control_plane_security_group)
    built = []
    first_plan = plan or _CDC_PLAN
    cfn = cfn or _StateCfn(states)

    class _Deployer:
        def __init__(self, *a, **k):
            if made is not None:
                made.append(k)

        def run(self, plan_doc):
            if deployer_run:
                return deployer_run(plan_doc)
            return {'completed': [], 'outputs': {}}
    try:
        links._plan_for = lambda doc, region=None, assign_public_ip=None: (
            ({'preflight': json.loads(json.dumps(preflight or {})), 'link': 'demo'},
             json.loads(json.dumps(first_plan))), None)
        links.DeployClients = lambda region=None: FakeClients(cfn=cfn)
        links.Deployer = _Deployer
        links.describe_plan = lambda plan_doc: 'plan summary'
        links.control_plane_security_group = lambda: None

        def fake_build(resolution, **kw):
            built.append(kw)
            return dict(_CDC_PLAN, for_update=kw.get('for_update', False),
                        steps=[s for s in _CDC_PLAN['steps']
                               if s['id'] != 'pause_capture'])
        links.build_plan = build or fake_build
        res = _client(FakeClients(cfn=cfn)).post('/api/links/deploy', json=body)
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
    # The update plan is built only to compare with the deployed stacks; the
    # deployment itself still runs the full plan with the pause and the load.
    ids = [s['id'] for s in res.get_json()['plan']['steps']]
    check_true('reload.full_plan', 'pause_capture' in ids)


# A plan that really pauses capture for the load (the pause step _CDC_PLAN
# carries only names its id).
_PAUSING_PLAN = dict(_CDC_PLAN, steps=[
    {'kind': 'stack', 'id': 'broker', 'name': 'ecdl-demo-broker'},
    {'kind': 'stack', 'id': 'cdc', 'name': 'ecdl-demo-cdc'},
    {'kind': 'action', 'id': 'pause_capture', 'action': 'set_debezium_desired',
     'desired': 0, 'stack': 'cdc'},
    {'kind': 'stack', 'id': 'loader', 'name': 'ecdl-demo-loader'},
    {'kind': 'action', 'id': 'resume_capture', 'action': 'set_debezium_desired',
     'desired': 1, 'stack': 'cdc'}])

_RETENTION_BLOCK = {'checked': True, 'blocks': 1, 'warnings': 1, 'findings': [
    {'level': 'block', 'path': 'source.log_retention',
     'message': 'the bulk load holds capture paused for an estimated 20.3h, longer '
                'than the source keeps its binary logs (12h).'},
    {'level': 'warn', 'path': 'source.prerequisites', 'message': 'something else'}]}


def test_retention_block_refuses_a_deploy_that_pauses():  # Jira story F056
    res, _ = _with_deploy_fakes({}, {'link': {'name': 'demo'}},
                                preflight=_RETENTION_BLOCK, plan=_PAUSING_PLAN)
    body = res.get_json()
    check('retention.status', res.status_code, 409)
    check('retention.stage', body['stage'], 'preflight')
    check_true('retention.message', '20.3h' in body['message'])
    check_true('retention.not_started', 'deployment_id' not in body)


def test_retention_block_refuses_a_reload_of_a_deployed_link():  # Jira story F056
    res, _ = _with_deploy_fakes(ALL_DEPLOYED, {'link': {'name': 'demo'},
                                               'confirm_redeploy': True, 'reload': True},
                                preflight=_RETENTION_BLOCK, plan=_PAUSING_PLAN)
    check('retention.reload.status', res.status_code, 409)
    check('retention.reload.stage', res.get_json()['stage'], 'preflight')


def test_retention_block_is_moot_for_an_update_that_does_not_pause():  # Jira story F056
    res, _ = _with_deploy_fakes(ALL_DEPLOYED, {'link': {'name': 'demo'},
                                               'confirm_redeploy': True},
                                preflight=_RETENTION_BLOCK, plan=_PAUSING_PLAN)
    body = res.get_json()
    check('retention.update.status', res.status_code, 202)
    check('retention.update.findings', [f['path'] for f in body['preflight']['findings']],
          ['source.prerequisites'])
    check('retention.update.counts', (body['preflight']['blocks'],
                                      body['preflight']['warnings']), (0, 1))


def test_retention_block_is_bypassed_by_ignore_preflight():  # Jira story F056
    res, _ = _with_deploy_fakes({}, {'link': {'name': 'demo'}, 'ignore_preflight': True},
                                preflight=_RETENTION_BLOCK, plan=_PAUSING_PLAN)
    check('retention.ignore.status', res.status_code, 202)


def test_other_blocks_still_refuse_before_the_state_is_read():  # Jira story F056
    pre = dict(_RETENTION_BLOCK, blocks=2, findings=_RETENTION_BLOCK['findings'] + [
        {'level': 'block', 'path': 'tables', 'message': "table 'x' does not exist"}])
    res, _ = _with_deploy_fakes(ALL_DEPLOYED, {'link': {'name': 'demo'},
                                               'confirm_redeploy': True},
                                preflight=pre, plan=_PAUSING_PLAN)
    check('retention.other.status', res.status_code, 409)
    check_true('retention.other.message', "'x'" in res.get_json()['message'])


_INCOMPLETE = {'ecdl-demo-loader': [{'Key': 'ecdl:initial-load', 'Value': 'incomplete'}]}


def test_an_unfinished_load_refuses_a_plain_redeploy():  # Jira story F059
    res, built = _with_deploy_fakes(ALL_DEPLOYED, {'link': {'name': 'demo'},
                                                   'confirm_redeploy': True},
                                    cfn=_StateCfn(ALL_DEPLOYED, _INCOMPLETE))
    body = res.get_json()
    check('marker.refuse.status', res.status_code, 409)
    check('marker.refuse.stage', body['stage'], 'initial_load')
    check_true('marker.refuse.message', 'did not complete' in body['message'])
    check_true('marker.refuse.hint', '"reload": true' in body['hint'])
    check_true('marker.refuse.not_started', 'deployment_id' not in body)


def test_an_unfinished_load_allows_a_reload():  # Jira story F059
    res, _ = _with_deploy_fakes(ALL_DEPLOYED, {'link': {'name': 'demo'},
                                               'confirm_redeploy': True, 'reload': True},
                                cfn=_StateCfn(ALL_DEPLOYED, _INCOMPLETE))
    check('marker.reload.status', res.status_code, 202)
    ids = [s['id'] for s in res.get_json()['plan']['steps']]
    check_true('marker.reload.full_plan', 'pause_capture' in ids)


def test_a_complete_load_updates_as_before():  # Jira story F059
    done = {'ecdl-demo-loader': [{'Key': 'ecdl:initial-load', 'Value': 'complete'}]}
    res, built = _with_deploy_fakes(ALL_DEPLOYED, {'link': {'name': 'demo'},
                                                   'confirm_redeploy': True},
                                    cfn=_StateCfn(ALL_DEPLOYED, done))
    check('marker.complete.status', res.status_code, 202)
    check('marker.complete.for_update', built and built[0].get('for_update'), True)


class _LoaderTagCfn(FakeCfn):
    def __init__(self, marker=None, error=False):
        super().__init__()
        self.marker, self.error = marker, error

    def describe_stacks(self, StackName):
        if StackName == 'ecdl-uitest-loader':
            if self.error:
                raise RuntimeError('throttled')
            tags = ([{'Key': 'ecdl:initial-load', 'Value': self.marker}]
                    if self.marker else [])
            return {'Stacks': [{'StackStatus': 'CREATE_COMPLETE', 'Tags': tags}]}
        return super().describe_stacks(StackName)


def test_discovered_item_says_the_initial_load_is_incomplete():  # Jira story F059
    item = links._discovered_item(FakeClients(cfn=_LoaderTagCfn('incomplete')),
                                  'uitest', STACKS)
    check('enrich.state', item['initial_load'], 'incomplete')
    check('enrich.flag', item.get('initial_load_incomplete'), True)
    check_true('enrich.note', 'initial load incomplete' in item['initial_load_note'])
    for marker, error in (('complete', False), (None, False), (None, True)):
        item = links._discovered_item(FakeClients(cfn=_LoaderTagCfn(marker, error)),
                                      'uitest', STACKS)
        check('enrich.other.{}.{}'.format(marker, error),
              (item['initial_load'], 'initial_load_incomplete' in item),
              (marker, False))
    # No loader stack: nothing to say.
    item = links._discovered_item(FakeClients(cfn=_LoaderTagCfn('incomplete')), 'uitest',
                                  {k: v for k, v in STACKS.items() if k != 'loader'})
    check('enrich.noloader', item['initial_load'], None)


def test_tracked_records_carry_the_marker_in_the_list_and_detail():  # Jira story F059
    saved = dict(links._DEPLOYMENTS)
    try:
        links._DEPLOYMENTS.clear()
        links._DEPLOYMENTS['deploy-uitest-1'] = {
            'id': 'deploy-uitest-1', 'link': 'uitest', 'status': 'failed',
            'events': []}
        client = _client(FakeClients(cfn=_LoaderTagCfn('incomplete')))
        rows = client.get('/api/links/deployments').get_json()['deployments']
        row = next(r for r in rows if r['id'] == 'deploy-uitest-1')
        check('tracked.list', row.get('initial_load_incomplete'), True)
        detail = client.get('/api/links/deployments/deploy-uitest-1').get_json()
        check('tracked.detail', detail.get('initial_load_incomplete'), True)
    finally:
        links._DEPLOYMENTS.clear()
        links._DEPLOYMENTS.update(saved)
        links.DeployClients = _ORIGINAL_CLIENTS


def _pg_plan():
    plan_doc = json.loads(json.dumps(_PAUSING_PLAN))
    plan_doc['steps'][1]['parameters'] = {'SourceEngine': 'postgresql',
                                          'PgSlotName': 'ecdl_demo'}
    return plan_doc


def test_reset_position_needs_reload():  # Jira story F057
    res, _ = _with_deploy_fakes(ALL_DEPLOYED, {'link': {'name': 'demo'},
                                               'confirm_redeploy': True,
                                               'reset_position': True},
                                plan=_PAUSING_PLAN)
    body = res.get_json()
    check('reset.noreload.status', res.status_code, 409)
    check('reset.noreload.stage', body['stage'], 'reset_position')
    check_true('reset.noreload.message', 'needs a reload' in body['message'])


def test_reset_position_with_reload_reaches_the_deployer():  # Jira story F057
    made = []
    res, _ = _with_deploy_fakes(ALL_DEPLOYED, {'link': {'name': 'demo'},
                                               'confirm_redeploy': True, 'reload': True,
                                               'reset_position': True},
                                plan=_PAUSING_PLAN, made=made)
    check('reset.ok.status', res.status_code, 202)
    for _ in range(50):
        if made:
            break
        time.sleep(0.02)
    check('reset.ok.flag', made and (made[0].get('reset_position'),
                                     made[0].get('pg_slot_dropped')), (True, False))
    rec = links._DEPLOYMENTS[res.get_json()['deployment_id']]
    check('reset.ok.recorded', rec.get('reset_position'), True)


def test_reset_position_on_postgres_needs_the_slot_dropped():  # Jira story F057
    body = {'link': {'name': 'demo'}, 'confirm_redeploy': True, 'reload': True,
            'reset_position': True}
    res, _ = _with_deploy_fakes(ALL_DEPLOYED, body, plan=_pg_plan())
    check('reset.pg.status', res.status_code, 409)
    check_true('reset.pg.sql', "pg_drop_replication_slot('ecdl_demo')" in res.get_json()['hint'])
    made = []
    res, _ = _with_deploy_fakes(ALL_DEPLOYED, dict(body, pg_slot_dropped=True),
                                plan=_pg_plan(), made=made)
    check('reset.pg.ok', res.status_code, 202)
    for _ in range(50):
        if made:
            break
        time.sleep(0.02)
    check('reset.pg.flag', made and made[0].get('pg_slot_dropped'), True)


def test_an_interrupted_reset_is_judged_by_its_last_capture_step():  # Jira story F057
    def rec(*steps):
        return {'events': [{'event': 'action_complete', 'step': s} for s in steps]}
    check('reset.paused.after_repause', links.capture_left_paused(
        rec('pause_capture', 'reset_resume_capture', 'reset_pause_capture')), True)
    check('reset.paused.during_brief_resume', links.capture_left_paused(
        rec('pause_capture', 'reset_resume_capture')), False)
    check('reset.paused.done', links.capture_left_paused(
        rec('pause_capture', 'reset_resume_capture', 'reset_pause_capture',
            'resume_capture')), False)


def _review(severity, field='target', refuse=None, ack=None, outstanding=None):
    change = {'field': field, 'parameter': 'TargetHost', 'from': 'a', 'to': 'b',
              'severity': severity, 'consequence': 'c', 'action': 'do it'}
    review = {'diff': {'changed': severity != 'none', 'severity': severity,
                       'changes': [change] if severity != 'none' else []},
              'drift': [], 'refuse': severity == 'recreate' if refuse is None else refuse,
              'needs_ack': severity in ('backfill', 'cleanup') if ack is None else ack}
    return lambda *a, **k: (review, {'steps': [], 'blocked': False, 'notes': [],
                                     'severity': severity}, outstanding or [], None)


def _with_review(review_fn, states, body):
    saved = links._review_update
    try:
        links._review_update = review_fn
        return _with_deploy_fakes(states, body)[0]
    finally:
        links._review_update = saved


def test_deploy_refuses_a_change_that_cannot_be_made_in_place():
    # Lifecycle H1: a new target used to be applied in place.
    res = _with_review(_review('recreate'), ALL_DEPLOYED,
                       {'link': {'name': 'demo'}, 'confirm_redeploy': True})
    body = res.get_json()
    check('refuse.status', res.status_code, 409)
    check('refuse.stage', body['stage'], 'review')
    check_true('refuse.summary', 'TargetHost' in body['review']['summary'])
    check_true('refuse.not_started', 'deployment_id' not in body)


def test_deploy_needs_acknowledgement_for_a_backfill_and_carries_the_steps():
    steps = [{'kind': 'signal', 'statement': 'INSERT ...'}]
    res = _with_review(_review('backfill', 'tables', outstanding=steps), ALL_DEPLOYED,
                       {'link': {'name': 'demo'}, 'confirm_redeploy': True})
    check('ack.status', res.status_code, 409)
    check('ack.stage', res.get_json()['stage'], 'acknowledge')
    check('ack.steps', res.get_json()['outstanding'], steps)
    res = _with_review(_review('backfill', 'tables', outstanding=steps), ALL_DEPLOYED,
                       {'link': {'name': 'demo'}, 'confirm_redeploy': True,
                        'acknowledge': True})
    check('ack.ok.status', res.status_code, 202)
    rec = links._DEPLOYMENTS[res.get_json()['deployment_id']]
    check('ack.recorded', rec['outstanding'], steps)
    check('ack.review.severity', rec['review']['severity'], 'backfill')


def test_deploy_settings_change_needs_no_acknowledgement():
    res = _with_review(_review('deploy', 'advanced.sink'), ALL_DEPLOYED,
                       {'link': {'name': 'demo'}, 'confirm_redeploy': True})
    check('settings.status', res.status_code, 202)


def test_deploy_refuses_while_a_load_of_the_link_runs():
    # Lifecycle H3: every stack reads COMPLETE during a load, so by stacks alone
    # the link looked deployed and another update ran under the load.
    class LoadingEcs(FakeEcs):
        def list_tasks(self, cluster=None, family=None, desiredStatus=None):
            return {'taskArns': ['arn:task/1'] if family == 'ecdl-demo-loader' else []}
    saved = (links._plan_for, links.DeployClients, links.describe_plan,
             links.control_plane_security_group)
    try:
        clients = FakeClients(cfn=_StateCfn(ALL_DEPLOYED), ecs=LoadingEcs())
        links._plan_for = lambda doc, region=None, assign_public_ip=None: (
            ({'preflight': {}, 'link': 'demo'}, json.loads(json.dumps(_CDC_PLAN))), None)
        links.describe_plan = lambda plan_doc: 'plan summary'
        links.control_plane_security_group = lambda: None
        res = _client(clients).post('/api/links/deploy',
                                    json={'link': {'name': 'demo'}, 'confirm_redeploy': True})
    finally:
        (links._plan_for, links.DeployClients, links.describe_plan,
         links.control_plane_security_group) = saved
    body = res.get_json()
    check('load.status', res.status_code, 409)
    check('load.stage', body['stage'], 'in_progress')
    check('load.tasks', body['loader_tasks'], ['arn:task/1'])
    check_true('load.message', 'a load is running' in body['message'])


def _repair(body, states, review_fn):
    saved = (links._plan_for, links.DeployClients, links._review_update, links._deploy)
    sent = []
    try:
        links._plan_for = lambda doc, region=None, assign_public_ip=None, preflight_check=True: (
            ({'preflight': {}, 'link': 'demo', 'source': {'engine': 'mysql'}, 'tables': [],
              'debezium': {}}, json.loads(json.dumps(_CDC_PLAN))), None)
        links.DeployClients = lambda region=None: FakeClients(cfn=_StateCfn(states))
        links._review_update = review_fn

        def fake_deploy(body, **_kw):
            sent.append(body)
            return {'deployed': True, 'deployment_id': 'deploy-demo-x',
                    'poll': '/api/links/deployments/deploy-demo-x'}, 202
        links._deploy = fake_deploy
        res = _client(FakeClients(cfn=_StateCfn(states))).post('/api/links/repair', json=body)
        return res, sent
    finally:
        (links._plan_for, links.DeployClients, links._review_update, links._deploy) = saved


DESIRED = {'name': 'demo', 'mode': 'initial_load_and_cdc',
           'source': {'arn': 'arn:aws:rds:us-east-2:111122223333:cluster:shop',
                      'database': 'Inventory',
                      'secret_arn': 'arn:aws:secretsmanager:us-east-2:111122223333:secret:db-AbCdEf'},
           'target': {'arn': 'arn:aws:elasticache:us-east-2:111122223333:replicationgroup:cache'},
           'tables': ['orders']}


def test_repair_compares_with_the_deployed_stacks_not_the_client_current():
    # Lifecycle C1: "current" from the client equalled "desired", so the diff
    # saw nothing while the update reset key_prefix.
    res, sent = _repair({'current': DESIRED, 'desired': DESIRED},
                        ALL_DEPLOYED, _review('cleanup', 'advanced.key_prefix'))
    body = res.get_json()
    check('repair.compared', body['compared_with'], 'deployed')
    check('repair.changed', body['changed'], True)
    check('repair.ack', body['needs_acknowledge'], True)
    check('repair.not_applied', sent, [])


def test_repair_apply_goes_through_the_deploy_path():
    # Lifecycle H7: it ran synchronously, unrecorded, with no lock or checks.
    res, sent = _repair({'desired': DESIRED, 'apply': True, 'acknowledge': True},
                        ALL_DEPLOYED, _review('cleanup', 'advanced.key_prefix'))
    check('repair.apply.status', res.status_code, 202)
    check('repair.apply.id', res.get_json()['deployment_id'], 'deploy-demo-x')
    check('repair.apply.flags', (sent[0]['confirm_redeploy'], sent[0]['acknowledge']),
          (True, True))


def test_repair_apply_passes_ignore_preflight_through():
    # Live: an edit of a link whose source had a preflight block (GTIDs off)
    # was refused with 409 even with ignore_preflight, because /repair
    # dropped the flag on its way to the deploy path.
    for flag in (True, False):
        _res, sent = _repair({'desired': DESIRED, 'apply': True, 'acknowledge': True,
                              'ignore_preflight': flag},
                             ALL_DEPLOYED, _review('cleanup', 'advanced.key_prefix'))
        check('repair.ignore_preflight.%s' % flag, sent[0].get('ignore_preflight'), flag)
    _res, sent = _repair({'desired': DESIRED, 'apply': True, 'acknowledge': True},
                         ALL_DEPLOYED, _review('cleanup', 'advanced.key_prefix'))
    check('repair.ignore_preflight.default', sent[0].get('ignore_preflight'), False)


def test_repair_apply_of_an_undeployed_link_is_refused():
    res, sent = _repair({'current': DESIRED, 'desired': dict(DESIRED, tables=['orders', 'x']),
                         'apply': True}, {}, _review('none'))
    check('repair.undeployed.ok', res.get_json()['ok'], False)
    check('repair.undeployed.compared', res.get_json()['compared_with'], 'documents')
    check('repair.undeployed.sent', sent, [])


def test_repair_without_current_on_an_undeployed_link_says_so():
    res, _ = _repair({'desired': DESIRED}, {}, _review('none'))
    check_true('repair.nocurrent', 'not deployed' in res.get_json()['error'])


def test_repair_apply_with_no_change_deploys_nothing():
    res, sent = _repair({'desired': DESIRED, 'apply': True}, ALL_DEPLOYED, _review('none'))
    check('repair.nochange.sent', sent, [])
    check('repair.nochange.summary', res.get_json()['plan_summary'], 'nothing to do')


def test_status_from_stacks_reports_a_rolled_back_update_as_failed():
    # Lifecycle M5.
    def st(*states):
        return links._status_from_stacks({str(i): {'status': x} for i, x in enumerate(states)})
    check('status.rollback', st('CREATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'), 'failed')
    check('status.rollback_failed', st('UPDATE_ROLLBACK_FAILED'), 'failed')
    check('status.ok', st('CREATE_COMPLETE', 'UPDATE_COMPLETE'), 'succeeded')
    check('status.running', st('UPDATE_IN_PROGRESS'), 'running')
    check('status.rollback_in_progress', st('UPDATE_ROLLBACK_IN_PROGRESS'), 'running')
    check('status.deleting', st('DELETE_IN_PROGRESS', 'CREATE_COMPLETE'), 'deleting')


def test_deploy_settings_from_the_environment():
    # D1: both set, neither set, or refused.
    saved = {k: os.environ.get(k) for k in ('ECDL_TEMPLATE_URL_BASE', 'ECDL_DEPLOY_ROLE_ARN')}
    try:
        for k in saved:
            os.environ.pop(k, None)
        check('d1.none', links._deploy_settings(), {})
        os.environ['ECDL_TEMPLATE_URL_BASE'] = 'https://b.s3.us-east-2.amazonaws.com/templates/0.2.20'
        try:
            links._deploy_settings()
            FAILURES.append('d1.half: expected a refusal')
        except links.DeployError:
            pass
        os.environ['ECDL_DEPLOY_ROLE_ARN'] = 'arn:aws:iam::1:role/ECDLDeploy-x'
        got = links._deploy_settings()
        check('d1.role', got['role_arn'], 'arn:aws:iam::1:role/ECDLDeploy-x')
        check('d1.url', got['template_url']('ecdl-cdc-ecs.yaml'),
              'https://b.s3.us-east-2.amazonaws.com/templates/0.2.20/ecdl-cdc-ecs.yaml')
        try:
            got['template_url']('../x.yaml')
            FAILURES.append('d1.path: expected a refusal')
        except links.DeployError:
            pass
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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
        def broker_data(self, plan_doc):
            # Nothing unapplied (Jira story F136; tested in test_links_destroy.py).
            return {'exists': True, 'readable': True, 'at_risk': False,
                    'unapplied': 0, 'dead_letters_total': 0}

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


# ── D6: the target eviction policy is part of the deploy preflight ──────────
def _plan_with_target(target, source=None):
    names = ('problems_for', 'resolve_link', 'build_plan', 'add_load_step',
             'control_plane_security_group', 'preflight_source',
             '_check_key_conflicts', '_check_target_memory')
    saved = {n: getattr(links, n) for n in names}
    try:
        links.problems_for = lambda doc: []
        links.resolve_link = lambda doc, region=None: {'target': dict(target),
                                                       'source': dict(source or {})}
        links.build_plan = lambda resolution, **k: {'steps': []}
        links.add_load_step = lambda plan: plan
        links.control_plane_security_group = lambda: None
        links.preflight_source = lambda doc, region=None: {
            'checked': True, 'blocks': 0, 'warnings': 0, 'findings': []}
        links._check_key_conflicts = lambda r, region: None
        links._check_target_memory = lambda r, region: None
        ok, verdict = links._plan_for({'name': 'demo'})
        return ok[0]['preflight']
    finally:
        for n, v in saved.items():
            setattr(links, n, v)


def test_plan_blocks_an_evicting_target():
    pre = _plan_with_target({'maxmemory_policy': 'allkeys-lru', 'parameter_group': 'pg'})
    check('evict.allkeys.blocks', pre['blocks'], 1)
    check_true('evict.allkeys.msg', 'allkeys-lru' in pre['findings'][0]['message'])
    check('evict.allkeys.path', pre['findings'][0]['path'], 'target')


def test_plan_warns_on_volatile_and_passes_noeviction():
    pre = _plan_with_target({'maxmemory_policy': 'volatile-lru', 'parameter_group': 'pg'})
    check('evict.volatile', (pre['blocks'], pre['warnings']), (0, 1))
    pre = _plan_with_target({'maxmemory_policy': 'noeviction', 'parameter_group': 'pg'})
    check('evict.noeviction', (pre['blocks'], pre['warnings'], pre['findings']), (0, 0, []))


def test_plan_warns_about_a_secret_the_task_boundary_will_not_admit():  # Jira story F171
    arn = 'arn:aws:secretsmanager:us-east-2:1:secret:shop-db-AbCdEf'
    untagged = {'secret': {'arn': arn, 'name': 'shop-db', 'tag_keys': []}}
    ok_target = {'maxmemory_policy': 'noeviction', 'parameter_group': 'pg'}
    saved = os.environ.get('ECDL_PIPELINE_SECRET_ARNS')
    try:
        os.environ[ARNS_ENV] = ''
        pre = _plan_with_target(ok_target, source=untagged)
        check('secret.untagged.warns', (pre['blocks'], pre['warnings']), (0, 1))
        check('secret.untagged.path', pre['findings'][0]['path'], 'source.secret_arn')
        check_true('secret.untagged.says_tag', 'ecdl:link' in pre['findings'][0]['message'])
        os.environ['ECDL_PIPELINE_SECRET_ARNS'] = arn
        pre = _plan_with_target(ok_target, source=untagged)
        check('secret.listed.quiet', pre['findings'], [])
        del os.environ['ECDL_PIPELINE_SECRET_ARNS']
        pre = _plan_with_target(ok_target, source=untagged)
        check('secret.no_boundary.quiet', pre['findings'], [])
    finally:
        if saved is None:
            os.environ.pop('ECDL_PIPELINE_SECRET_ARNS', None)
        else:
            os.environ['ECDL_PIPELINE_SECRET_ARNS'] = saved


def test_plan_keeps_the_source_findings_next_to_the_target_ones():
    pre = _plan_with_target({'maxmemory_policy': None,
                             'maxmemory_policy_problem': 'AccessDenied'})
    check('evict.unknown', (pre['checked'], pre['blocks'], pre['warnings']), (True, 0, 1))


# ── Jira story F007: the preflight checks the publication the link actually reads ──────
def _plan_publication(engine, lookup):
    names = ('problems_for', 'resolve_link', 'build_plan', 'add_load_step',
             'control_plane_security_group', 'preflight_source',
             '_check_key_conflicts', '_check_target_memory',
             'deployed_pg_publication')
    saved = {n: getattr(links, n) for n in names}
    seen = {}
    try:
        links.problems_for = lambda doc: []
        links.resolve_link = lambda doc, region=None: {
            'name': 'shop', 'source': {'engine': engine},
            'target': {'maxmemory_policy': 'noeviction'}}
        links.build_plan = lambda resolution, **k: {'steps': [], 'prefix': 'ecdl'}
        links.add_load_step = lambda plan: plan
        links.control_plane_security_group = lambda: None

        def fake_preflight(doc, region=None, **kw):
            seen.update(kw)
            return {'checked': True, 'blocks': 0, 'warnings': 0, 'findings': []}
        links.preflight_source = fake_preflight
        links._check_key_conflicts = lambda r, region: None
        links._check_target_memory = lambda r, region: None
        links.deployed_pg_publication = lookup
        # A client is created (no call is made) before the lookup. Faked, so
        # the test does not depend on this machine's AWS credentials.
        import boto3
        real_client = boto3.client
        boto3.client = lambda *a, **k: object()
        try:
            links._plan_for({'name': 'shop'}, region='us-east-2')
        finally:
            boto3.client = real_client
        return seen
    finally:
        for n, v in saved.items():
            setattr(links, n, v)


def test_postgres_preflight_gets_the_deployed_publication():
    seen = _plan_publication('postgresql', lambda cfn, name, prefix: 'dbz_pub')
    check('pub.deployed', seen, {'publication': 'dbz_pub'})


def test_postgres_preflight_publication_unknown_when_the_stack_is_unreadable():
    def boom(cfn, name, prefix):
        raise RuntimeError('AccessDenied')
    check('pub.unknown', _plan_publication('postgresql', boom), {'publication': None})


def test_other_engines_do_not_look_up_a_publication():
    def boom(cfn, name, prefix):
        raise AssertionError('looked up a publication for mysql')
    check('pub.mysql', _plan_publication('mysql', boom), {})



def test_the_record_keeps_what_the_deployer_reports():
    """capture_verified, offset_recorded and load_recorded reach the record:
    live, the events showed offset_recorded while the record said None."""
    import time as _t
    res, _built = _with_deploy_fakes(
        {}, {'link': {'name': 'demo'}},
        deployer_run=lambda plan_doc: {'completed': ['cdc'], 'outputs': {},
                                       'capture_verified': True, 'offset_recorded': True,
                                       'load_recorded': False})
    dep = res.get_json().get('deployment_id')
    rec = None
    for _ in range(100):
        rec = links._DEPLOYMENTS.get(dep)
        if rec and rec.get('status') == 'succeeded':
            break
        _t.sleep(0.02)
    check('record.status', rec and rec.get('status'), 'succeeded')
    check('record.flags', tuple((rec or {}).get(k) for k in
                                ('capture_verified', 'offset_recorded', 'load_recorded')),
          (True, True, False))


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
