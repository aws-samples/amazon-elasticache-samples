"""Turn a resolved link into an ordered deployment plan.

`resolve_link()` says what the customer's infrastructure IS. This says what we
will CREATE, in what order, with which parameters. It is a pure function of the
resolution -- no AWS calls -- so the same plan can be shown for review, diffed,
and executed, and so it can be tested without an account.

WHY A PLAN AND NOT JUST A PARAMETER DICT
Provisioning is not one stack. A cdc link needs a broker created first and its
endpoint, port, security group and generated auth token fed into the CDC stack, so
some parameter values do not exist until an earlier stack has finished. Those are
declared here as `from_outputs` wiring and filled in by the executor. And an
initial load is not a stack at all: it is a task that runs once and exits, which
has to happen between two service state changes.

WHAT THIS REPLACES
The control plane could not deploy this architecture at all. `_deploy_ecs` in
backend/app.py returned "ECS Fargate deployment requires CloudFormation. Use
Download Package" and the generator built a DIFFERENT architecture (Debezium
Connect + Kafka/MSK + a Python sink) as an emitted script for a human to run.
The CLI's old `ecdl deploy` targeted an EC2/Kafka template, also a different
architecture (both since removed). Neither deployed infra/ecdl-cdc-ecs.yaml,
which is the thing every benchmark measures.
"""

from __future__ import annotations

import os
import re

BROKER_TEMPLATE = 'ecdl-broker-quickstart.yaml'
CDC_TEMPLATE = 'ecdl-cdc-ecs.yaml'
LOADER_TEMPLATE = 'ecdl-loader-ecs.yaml'

# The CDC and loader stacks create IAM roles, so they need an IAM capability.
# CAPABILITY_IAM is enough, not CAPABILITY_NAMED_IAM: neither template sets
# RoleName, so CloudFormation generates the names. Confirmed by asking it --
# validate-template reports ["CAPABILITY_IAM"] for both.
#
# The capability exists so that the narrower one can be requested. If a
# template later gained a NAMED role, requesting only CAPABILITY_IAM makes
# CloudFormation refuse and say so; requesting NAMED_IAM up front would let a
# fixed-name role -- which can collide across stacks and regions -- be created
# without any warning. Declared per stack so the broker stack, which creates no IAM at
# all, is granted nothing.
IAM_CAPABILITIES = ['CAPABILITY_IAM']

# The permissions boundary for the task roles the link stacks create. Set on the
# control plane by its CloudFormation template; see plan().
TASK_ROLE_BOUNDARY_ENV = 'ECDL_TASK_ROLE_BOUNDARY_ARN'


class PlanError(Exception):
    """The resolution cannot be turned into a deployable plan.

    Distinct from ResolutionError: that means we could not read the customer's
    infrastructure, this means we read it fine and still cannot proceed.
    """

    def __init__(self, message, hint=None):
        super(PlanError, self).__init__(message)
        self.hint = hint


def stack_names(link_name, prefix='ecdl'):
    """Stack names for a link.

    Derived from the link name so two links never collide and so a stack can be
    traced back to the link that owns it without a lookup table.
    """
    base = '{}-{}'.format(prefix, link_name)
    return {
        'broker': '{}-broker'.format(base),
        'cdc': '{}-cdc'.format(base),
        'loader': '{}-loader'.format(base),
    }


def cluster_name(link_name, prefix='ecdl'):
    """ECS cluster for a link's tasks."""
    return '{}-{}'.format(prefix, link_name)


def _cdc_parameters(resolved, images, sink, network, debezium, debezium_desired):
    """Everything infra/ecdl-cdc-ecs.yaml needs that is knowable before deploy.

    Broker values are absent on purpose -- they come from the broker stack's
    outputs, see the caller's `from_outputs`.
    """
    src = resolved['source']
    tgt = resolved['target']
    dbz = resolved['debezium']
    tables = resolved['tables']

    params = {
        'ClusterName': network['cluster_name'],
        'VpcId': network['vpc_id'],
        # CloudFormation List<> parameters are supplied comma-joined.
        'SubnetIds': ','.join(network['subnet_ids']),
        'AssignPublicIp': network['assign_public_ip'],

        # Source. The engine drives QUARKUS_PROFILE inside the Debezium image, so
        # this single value selects the whole connector configuration.
        'SourceEngine': src['engine'],
        'SourceDBHost': src['endpoint'],
        'SourceDBPort': str(src['port']),
        'SourceDBName': src['database'],
        'SourceDBUser': src['username'],
        'DBSecretArn': src['secret']['arn'],
        'DBSecretJsonKey': _db_secret_json_key(src),
        'KeyPrefix': _link_key_prefix(resolved).rstrip(':'),
        # An external source has no AWS security group to add pipeline ingress
        # to -- the operator opens their own firewall over the DX/VPN link -- so
        # this is empty and the template skips the ingress rule for it.
        'AuroraSecurityGroupId': ('' if src.get('external')
                                  else _one_security_group(src, 'source')),

        # Target. cluster_mode is READ FROM THE ARN, never chosen: a plain client
        # connected to a sharded cluster gets MOVED errors on most writes.
        'TargetHost': tgt['endpoint'],
        'TargetPort': str(tgt['port']),
        'TargetSSL': _bool(tgt['tls']),
        'TargetCluster': _bool(tgt['cluster_mode']),
        'TargetSecurityGroupId': _one_security_group(tgt, 'target'),
        'TargetSecretArn': (tgt.get('secret') or {}).get('arn', ''),

        # CDC scope.
        'Tables': dbz['table_include_list'],
        'TopicPrefix': dbz['topic_prefix'],
        'SnapshotMode': dbz['snapshot_mode'],
        'Streams': ','.join(t['stream'] for t in tables),

        # Per-link state keys, so two links on one broker cannot overwrite each
        # other's position or schema history.
        'OffsetKey': offset_key(resolved['name']),
        'SchemaHistoryKey': schema_history_key(resolved['name']),

        # Day-2 signalling. Without SignalCollection the connector never reads the
        # signalling table, so adding a table later can only capture its future
        # changes and never backfills the rows that already exist.
        'SignalCollection': dbz['signal_collection'],
        'SignalTopicPattern': dbz['signal_topic_pattern'],

        'DebeziumDesiredCount': str(debezium_desired),
        'DebeziumCpu': str(debezium['cpu']),
        'DebeziumMemory': str(debezium['memory']),
        'SinkWorkers': str(sink['workers']),
        'SinkBatchSize': str(sink['batch_size']),
        'SinkDesiredCount': str(sink['desired_count']),
        'SinkMinCount': str(sink['min_count']),
        'SinkMaxCount': str(sink['max_count']),
        'SinkTargetCpuPercent': str(sink['target_cpu_percent']),
    }

    # Images are only sent when the link overrides them, so the template's own
    # pinned defaults apply otherwise. Sending '' would override the pinned digest
    # with an empty string and produce a task definition with no image -- caught
    # by a test, because the failure would only surface at deploy time.
    if images.get('debezium'):
        params['DebeziumImage'] = images['debezium']
    if images.get('sink'):
        params['SinkImage'] = images['sink']

    arch = _cpu_architecture(resolved)
    if arch:
        params['CpuArchitecture'] = arch

    # MySQL only. Sending an empty ServerId would override the template default
    # with nothing, so the key is omitted entirely for the other engines.
    if dbz.get('server_id'):
        params['ServerId'] = str(dbz['server_id'])

    # PostgreSQL only: a per-link replication slot. See pg_slot_name.
    if src['engine'] == 'postgresql':
        params['PgSlotName'] = pg_slot_name(resolved['name'])

    transforms = _transforms_json(resolved)
    if transforms:
        params['TransformsJson'] = transforms

    # Alarms are on unless the link turns them off; only overrides are sent so
    # the template's defaults apply otherwise.
    alarms = ((resolved.get('link') or {}).get('advanced') or {}).get('alarms') or {}
    if alarms.get('enabled') is False:
        params['AlarmsEnabled'] = 'false'
    if alarms.get('email'):
        params['AlarmEmail'] = alarms['email']
    if alarms.get('sink_lag_seconds'):
        params['SinkLagAlarmSeconds'] = str(alarms['sink_lag_seconds'])
    if alarms.get('capture_stopped_minutes'):
        params['CaptureStoppedAlarmMinutes'] = str(alarms['capture_stopped_minutes'])
    return params


_CPU_ARCHITECTURES = {'x86_64': 'X86_64', 'arm64': 'ARM64'}


def _cpu_architecture(resolved):
    """The CloudFormation CpuArchitecture for this link, or None for the default.

    Only sent when the link asks for one, so the templates' own default (X86_64)
    applies otherwise -- the same rule as the image overrides.
    """
    advanced = (resolved.get('link') or {}).get('advanced') or {}
    value = advanced.get('cpu_architecture')
    return _CPU_ARCHITECTURES.get(value) if value else None


def _link_key_prefix(resolved):
    """advanced.key_prefix as '<prefix>:' (or ''), the sink's and loader's form."""
    raw = str((((resolved.get('link') or {}).get('advanced') or {})
               .get('key_prefix')) or '').strip().rstrip(':')
    return raw + ':' if raw else ''


def _db_secret_json_key(src):
    """Which key of the source secret holds the password, or '' for plaintext.

    An RDS-managed admin secret is JSON ({"username": ..., "password": ...}).
    Injected whole, Debezium and the loader sent that JSON as the password and
    MySQL answered "Access denied". The shape is known from the secret's
    metadata alone (resolve.confirm_secret), so the value is never read here.
    """
    return 'password' if (src.get('secret') or {}).get('managed_by_rds') else ''


def pg_slot_name(link_name):
    """PostgreSQL replication slot for this link.

    The slot used to be the constant ecdl_slot, so a second link on the same
    PostgreSQL source failed to start ("replication slot is active"), and the
    teardown of one link could not tell whose slot it was. Slot names allow only
    lowercase letters, digits and underscores, up to 63 characters.
    """
    slot = 'ecdl_' + re.sub(r'[^a-z0-9_]', '_', str(link_name).lower())
    return slot[:63]


def offset_key(link_name):
    """Broker key holding Debezium's committed position for this link."""
    return 'ecdl:{}:offsets'.format(link_name)


def schema_history_key(link_name):
    return 'ecdl:{}:schema_history'.format(link_name)


def _transforms_json(resolved):
    """The transform spec, in the shape the Go sink parses.

    Taken from the link document rather than rebuilt, so what executes is what was
    validated.
    """
    link = resolved.get('link') or {}
    transforms = link.get('transforms') or {}
    tables = transforms.get('tables') or {}
    if not tables:
        return ''
    import copy
    import json
    from .link import bare_table
    tables = copy.deepcopy(tables)
    # Fill the documented denormalize default. The schema says parent_key_prefix
    # defaults to '<parent_table>:', but nothing wrote it in, and both engines
    # read the field verbatim -- so an omitted prefix produced the cache key
    # '<join_value>' instead of '<parent_table>:<join_value>' and embedded
    # nothing. The prefix is the parent's bare table name (the loader keys rows
    # as '<bare table>:<pk>' and the sink as 'Source.Table:<pk>'), so a
    # schema-qualified parent_table is reduced to its bare name here. Filling it
    # at the single point that emits the executable spec fixes the loader and the
    # sink together and keeps the two byte-identical.
    for ts in tables.values():
        if not isinstance(ts, dict):
            continue
        for rule in ts.get('denormalize') or []:
            if isinstance(rule, dict) and not rule.get('parent_key_prefix'):
                rule['parent_key_prefix'] = '{}{}:'.format(
                    _link_key_prefix(resolved),
                    bare_table(rule.get('parent_table') or ''))
    return json.dumps({'tables': tables}, sort_keys=True, separators=(',', ':'))


def _load_ordered_tables(resolved):
    """The loader's tables, ordered so a denorm parent loads before its children.

    The loader embeds a parent's fields onto a child by reading the parent back
    from the cache, and the initial load does no parent-side fan-out, so a child
    loaded before its parent would embed empty values with nothing to correct it.
    The loader runs one table per task in the order of this list, so returning
    parents first is what makes the embed find its parent. Tables with no denorm
    relationship keep their original order; a cycle degrades to best-effort
    (leftovers appended) rather than dropping a table.
    """
    from .link import bare_table
    tables = list(resolved.get('tables') or [])
    link = resolved.get('link') or {}
    tspec = (link.get('transforms') or {}).get('tables') or {}

    parents = {}  # child bare name -> set(parent bare names)
    for child, ts in tspec.items():
        if not isinstance(ts, dict):
            continue
        for rule in ts.get('denormalize') or []:
            p = rule.get('parent_table') if isinstance(rule, dict) else None
            if p:
                parents.setdefault(bare_table(child), set()).add(bare_table(p))
    if not parents:
        return tables

    in_load = {bare_table(t['table']) for t in tables}
    ordered, placed, remaining = [], set(), tables[:]
    progressed = True
    while remaining and progressed:
        progressed = False
        still = []
        for t in remaining:
            need = parents.get(bare_table(t['table']), set()) & in_load
            if need <= placed:
                ordered.append(t)
                placed.add(bare_table(t['table']))
                progressed = True
            else:
                still.append(t)
        remaining = still
    ordered.extend(remaining)  # any cycle: keep them, never drop
    return ordered


def _one_security_group(described, what):
    """The security group to add pipeline ingress to.

    The template takes a single SG id. A resource with several is not a failure we
    can hide by picking one: which one carries the rule decides whether the
    pipeline can connect at all, so it has to be an explicit choice.
    """
    groups = described.get('security_group_ids') or []
    if not groups:
        raise PlanError(
            'the {} has no security group, so the pipeline tasks cannot be '
            'granted access to it'.format(what),
            'Attach a security group to the {} and resolve again.'.format(what))
    if len(groups) > 1:
        raise PlanError(
            'the {} has {} security groups ({}), so it is ambiguous which one '
            'should allow the pipeline in'.format(what, len(groups),
                                                  ', '.join(groups)),
            'Set advanced.network.{}_security_group_id to choose one.'.format(what))
    return groups[0]


def _bool(value):
    """CloudFormation string parameters take 'true'/'false', not Python bools."""
    return 'true' if value else 'false'


DEFAULT_SINK = {
    'workers': 8,
    'batch_size': 2000,
    'desired_count': 2,
    # 2, matching the template default and its reasoning (burst headroom while a
    # scale-out alarm takes ~3 minutes to fire). This said 1 while the template
    # said 2, so a link deployed through the control plane silently got half the
    # minimum the template documents.
    'min_count': 2,
    'max_count': 10,
    'target_cpu_percent': 60,
}

# Debezium is ONE log reader, so extra vCPU do not parallelise capture. These
# match the template's own defaults; the benchmarks used 8192/16384 to find the
# ceiling, which is not what a normal link needs.
DEFAULT_DEBEZIUM = {
    'cpu': '2048',
    'memory': '4096',
}

DEFAULT_LOADER = {
    'workers': 16,
    'cpu': '2048',
    'memory': '4096',
}

DEFAULT_IMAGES = {
    # Left empty so the template's own pinned defaults apply unless the link
    # overrides them. Writing a version in here would be a second place that has
    # to be updated on every release, and the two would drift.
    'debezium': '',
    'sink': '',
    'loader': '',
}


def plan(resolved, prefix='ecdl', assign_public_ip=None, for_update=False,
         monitor_security_group_id=None, task_role_boundary_arn=None):
    """The ordered steps that deploy this link.

    Returns a dict with `steps`, each one either a `stack` (create or update a
    CloudFormation stack) or an `action` (something the executor does directly,
    like holding capture at zero while the loader runs).

    `for_update=True` plans a change to a link that is ALREADY RUNNING, and omits
    the bulk load and the capture pause that exists only to protect it. This is
    the correct mechanism, not only an optimisation:

      * the bulk load reads whole tables and writes them straight to the target,
        so running it again re-reads tables that did not change
      * it can only run safely with capture paused, so it interrupts a pipeline
        that is otherwise healthy
      * and it does not fix the thing a change actually breaks -- rows already in
        the cache under an old transform, or rows that predate a newly added
        table. An incremental snapshot fixes exactly those, in chunks, WHILE
        streaming continues.

    So a first deployment loads in bulk and a later change backfills by signal.
    `repair.repair_plan` names those two steps in that order.

    `task_role_boundary_arn` is the permissions boundary put on every task role
    the CDC and loader stacks create. The control plane's IAM policy refuses to
    create a role without it, so the control plane passes it from
    ECDL_TASK_ROLE_BOUNDARY_ARN; None reads that variable, '' sets no boundary.
    """
    if task_role_boundary_arn is None:
        task_role_boundary_arn = os.environ.get(TASK_ROLE_BOUNDARY_ENV, '')
    boundary = (task_role_boundary_arn or '').strip()
    mode = resolved['mode']
    components = resolved['components']
    name = resolved['name']
    names = stack_names(name, prefix)
    link = resolved.get('link') or {}
    advanced = link.get('advanced') or {}

    images = dict(DEFAULT_IMAGES)
    images.update({k: v for k, v in (advanced.get('images') or {}).items() if v})
    sink = dict(DEFAULT_SINK)
    sink.update(advanced.get('sink') or {})
    loader = dict(DEFAULT_LOADER)
    loader.update(advanced.get('loader') or {})
    debezium = dict(DEFAULT_DEBEZIUM)
    # Only cpu and memory are task sizing; the rest of advanced.debezium is
    # connector configuration and belongs to the resolver.
    for key in ('cpu', 'memory'):
        value = (advanced.get('debezium') or {}).get(key)
        if value:
            debezium[key] = value

    steps = []
    notes = []
    warnings = list(resolved.get('warnings') or [])

    net_cfg = advanced.get('network') or {}
    network = {
        'vpc_id': resolved['network']['vpc_id'],
        'subnet_ids': resolved['network']['subnet_ids'],
        'cluster_name': net_cfg.get('cluster_name') or cluster_name(name, prefix),
        # Decided from the subnets' actual routing -- see _public_ip.
        'assign_public_ip': _public_ip(net_cfg, assign_public_ip,
                                       resolved['network'].get('egress'),
                                       warnings),
        # Passed in by the caller rather than read from the environment here,
        # because this planner is pure and because it is a property of the
        # CONTROL PLANE, not of the link -- putting it in the link document would
        # mean repeating one machine's security group in every customer's file.
        'monitor_sg': monitor_security_group_id or '',
    }

    if not network['monitor_sg'] and components.get('broker'):
        warnings.append(
            'no control-plane security group was supplied, so the broker will '
            'only admit the pipeline itself. The pipeline will run, but the '
            'dashboard cannot read stream depth, consumer-group lag, pending '
            'entries, DLQ length or the committed offset. Set '
            'ECDL_MONITOR_SECURITY_GROUP_ID on the control plane to fix this.')

    if not network['vpc_id'] or not network['subnet_ids']:
        raise PlanError(
            'the resolution has no VPC or subnets to deploy into',
            'Resolve the link first; the network comes from the source and '
            'target subnet groups.')

    # The loader writes straight to the target and never touches the broker, so
    # an initial_load link needs no broker, no Debezium and no sink at all.
    if components['broker']:
        steps.append({
            'kind': 'stack',
            'id': 'broker',
            'name': names['broker'],
            'template': BROKER_TEMPLATE,
            'capabilities': [],
            'purpose': 'the broker Valkey that buffers change events, with TLS '
                       'and a generated auth token',
            'parameters': {
                'ClusterName': '{}-{}-broker'.format(prefix, name),
                'VpcId': network['vpc_id'],
                'SubnetIds': ','.join(network['subnet_ids']),
                # Lets the control plane open a socket to the broker. Without it
                # the only grant is to the pipeline's own task security group, so
                # the pipeline runs correctly and is UNOBSERVABLE: stream depth,
                # consumer-group lag, pending entries, DLQ length and Debezium's
                # committed offset all live in the broker. The failure is quiet
                # and misleading, because ECS still reports the tasks healthy --
                # Debezium and Sink show "1/1 tasks" beside blank throughput.
                'MonitorSecurityGroupId': network['monitor_sg'],
            },
            'from_outputs': {},
            'depends_on': [],
        })

    if components['debezium'] or components['sink']:
        # For a combined load-and-cdc link, capture must be RUNNING for this
        # first deploy (a first start with no stored offset would begin at the
        # current log position and skip the load window) and is paused
        # afterwards, by the hold_capture step below.
        cdc = {
            'kind': 'stack',
            'id': 'cdc',
            'name': names['cdc'],
            'template': CDC_TEMPLATE,
            'capabilities': list(IAM_CAPABILITIES),
            'purpose': 'Debezium (one task) and the sink (autoscaled) on Fargate',
            'parameters': dict(_cdc_parameters(resolved, images, sink, network,
                                               debezium, debezium_desired=1),
                               PermissionsBoundaryArn=boundary),
            # Values that only exist once the broker stack has finished.
            'from_outputs': {
                'BrokerHost': ['broker', 'PrimaryEndpoint'],
                'BrokerPort': ['broker', 'Port'],
                'BrokerSecurityGroupId': ['broker', 'SecurityGroupId'],
                'BrokerSecretArn': ['broker', 'AuthSecretArn'],
                'BrokerSSL': ['broker', 'TransitEncryption'],
                # For the broker memory alarm in the CDC stack.
                'BrokerReplicationGroupId': ['broker', 'ReplicationGroupId'],
            },
            'depends_on': ['broker'],
            'needs_cluster': network['cluster_name'],
            # Parameters that must never change on an existing stack: changing
            # the replication slot would start a NEW slot at the current WAL
            # position (skipping everything since) and orphan the old one, which
            # pins WAL until the disk fills. On update the deployer keeps the
            # stack's existing value -- and a stack created before the parameter
            # existed keeps the old shared slot.
            'keep_previous': ['PgSlotName'],
        }
        steps.append(cdc)

    if components['loader']:
        loader_step = {
            'kind': 'stack',
            'id': 'loader',
            'name': names['loader'],
            'template': LOADER_TEMPLATE,
            'capabilities': list(IAM_CAPABILITIES),
            'purpose': 'a run-once Fargate task that bulk-loads the current '
                       'table contents straight into the target',
            'parameters': dict(_loader_parameters(resolved, images, loader, network),
                               PermissionsBoundaryArn=boundary),
            'from_outputs': {},
            'depends_on': [],
            'needs_cluster': network['cluster_name'],
        }

        if for_update:
            # Updating a running link: keep the loader STACK consistent with the new
            # table list so a later deliberate load is correct, but do not run it
            # and do not touch capture. Nothing here writes to the target, so the
            # pause has nothing to protect.
            loader_step['depends_on'] = ['cdc'] if components['debezium'] else []
            steps.append(loader_step)
            notes.append(
                'capture is left running: this updates the stacks only. Rows '
                'already in the cache, and rows in a newly added table, are '
                'corrected by an incremental snapshot rather than by re-running '
                'the bulk load')
        elif mode == 'initial_load_and_cdc':
            # THE ORDERING INVARIANT. Capture is paused across the load, then
            # resumed. Without the pause, CDC can apply a change to a row and the
            # loader can then overwrite it with the older value it read earlier;
            # CDC never replays that event, so the key stays stale until the row
            # next changes.
            loader_step['depends_on'] = ['cdc']
            steps.append({
                'kind': 'action',
                'id': 'pause_capture',
                'action': 'set_debezium_desired',
                'desired': 0,
                'stack': 'cdc',
                'purpose': 'pause change capture so the load cannot be '
                           'overwritten by a concurrent CDC write',
                'depends_on': ['cdc'],
            })
            steps.append(loader_step)
            steps.append({
                'kind': 'action',
                'id': 'resume_capture',
                'action': 'set_debezium_desired',
                'desired': 1,
                'stack': 'cdc',
                'purpose': 'resume capture; it replays every change since before '
                           'the load, so the load window is not lost',
                'depends_on': ['loader'],
            })
            notes.append(
                'capture is started, paused for the load, then resumed -- it '
                'resumes from a position recorded before the load, so changes '
                'made during the load are replayed')
        else:
            steps.append(loader_step)

    if not steps:
        raise PlanError(
            'mode {!r} produces nothing to deploy'.format(mode))

    if mode == 'initial_load' and not for_update:
        notes.append('one-shot load: nothing is left running afterwards, and '
                     'the cache goes stale from the moment the load finishes')

    return {
        'link': name,
        'mode': mode,
        'prefix': prefix,
        'cluster_name': network['cluster_name'],
        'steps': steps,
        'notes': notes,
        'warnings': warnings,
        # Carried on the document so `add_load_step` cannot put the load back.
        # Two places deciding whether a load runs is how they end up disagreeing.
        'for_update': bool(for_update),
    }


def _public_ip(net_cfg, override, egress=None, warnings=None):
    """ENABLED or DISABLED, decided from how the subnets actually reach AWS.

    This used to return DISABLED whenever nothing said otherwise. DISABLED is the
    safer choice -- a Fargate task with a public IP is internet-reachable unless a
    security group blocks it, and this task holds a database credential -- but it is
    only *possible* when the subnets have another route to the AWS APIs.

    The default VPC here has no NAT gateway and no interface endpoints: its only
    egress is an internet gateway, which does nothing for a task with no public
    address. So the safe-by-default value produced tasks that could not fetch
    their own secrets, and the only symptom was CloudFormation timing out after
    twenty-five minutes with "Exceeded attempts to wait".

    Preference order, and why:
      1. an explicit override or config value -- the operator knows their network
      2. NAT or interface endpoints present -> DISABLED, the private and safer
         arrangement, now known to work rather than assumed to
      3. internet gateway only -> ENABLED, with a warning, because it is the only
         value that can start a task at all
      4. nothing at all -> PlanError, because deploying is guaranteed to hang
    """
    if override is not None:
        return 'ENABLED' if override else 'DISABLED'
    if net_cfg.get('assign_public_ip') is not None:
        return 'ENABLED' if net_cfg['assign_public_ip'] else 'DISABLED'

    kind = (egress or {}).get('kind')
    detail = (egress or {}).get('detail') or ''

    if kind in ('nat', 'endpoints'):
        return 'DISABLED'

    if kind == 'igw':
        if warnings is not None:
            warnings.append(
                'tasks will run with a public IP: ' + detail + '. They are still '
                'closed to inbound traffic by their security group, but a NAT '
                'gateway or interface endpoints for secretsmanager, ecr.api, '
                'ecr.dkr, logs and s3 would let them run with no public address '
                'at all.')
        return 'ENABLED'

    if kind == 'none':
        raise PlanError(
            'the deployment subnets have no route to the AWS APIs: ' + detail,
            'A Fargate task fetches its image and its secrets before any of our '
            'code runs, so without egress every task will fail to start and the '
            'stack will roll back after ~20 minutes. Add a NAT gateway, or '
            'interface endpoints for secretsmanager, ecr.api, ecr.dkr and logs '
            'plus the S3 gateway endpoint, or set '
            'advanced.network.assign_public_ip: true if these subnets are public.')

    # kind == 'unknown': routing could not be read. Keep the safer value and say
    # so, rather than silently choosing the one that happens to work here.
    if warnings is not None and egress:
        warnings.append(
            'could not verify how these subnets reach the AWS APIs ('
            + (detail or 'no detail')
            + '), so tasks are planned with no public IP. If they fail to start '
              'with a Secrets Manager or ECR timeout, that is why.')
    return 'DISABLED'


def _loader_parameters(resolved, images, loader, network):
    src = resolved['source']
    tgt = resolved['target']
    # Parent-before-child, so a denorm child finds its parent already in the cache.
    tables = _load_ordered_tables(resolved)

    params = {
        'ClusterName': network['cluster_name'],
        'VpcId': network['vpc_id'],
        'SubnetIds': ','.join(network['subnet_ids']),
        'AssignPublicIp': network['assign_public_ip'],

        'SourceEngine': src['engine'],
        'SourceDBHost': src['endpoint'],
        'SourceDBPort': str(src['port']),
        'SourceDBName': src['database'],
        'SourceDBUser': src['username'],
        'DBSecretArn': src['secret']['arn'],
        'DBSecretJsonKey': _db_secret_json_key(src),
        'KeyPrefix': _link_key_prefix(resolved).rstrip(':'),
        # Empty for an external source; see AuroraSecurityGroupId in
        # _cdc_parameters for the reason.
        'SourceSecurityGroupId': ('' if src.get('external')
                                  else _one_security_group(src, 'source')),

        'TargetHost': tgt['endpoint'],
        'TargetPort': str(tgt['port']),
        'TargetSSL': _bool(tgt['tls']),
        'TargetCluster': _bool(tgt['cluster_mode']),
        'TargetSecurityGroupId': _one_security_group(tgt, 'target'),
        'TargetSecretArn': (tgt.get('secret') or {}).get('arn', ''),

        # One table per loader run: the loader chunks a single table by primary
        # key range, so several tables are several runs. The executor iterates.
        'Tables': ','.join(t['table'] for t in tables),
        'PrimaryKeys': ','.join(_pk_for(resolved, t) for t in tables),
        'LoaderWorkers': str(loader['workers']),
        'TaskCpu': str(loader['cpu']),
        'TaskMemory': str(loader['memory']),
    }
    if images.get('loader'):
        params['LoaderImage'] = images['loader']
    arch = _cpu_architecture(resolved)
    if arch:
        params['CpuArchitecture'] = arch
    transforms = _transforms_json(resolved)
    if transforms:
        params['TransformsJson'] = transforms
    return params


def _pk_for(resolved, table_entry):
    """The primary key column the loader chunks by.

    Defaults to `id` because that is what the loader's own default is; the link
    can name a different one per table.
    """
    link = resolved.get('link') or {}
    loader_cfg = (link.get('advanced') or {}).get('loader') or {}
    keys = loader_cfg.get('primary_keys') or {}
    named = keys.get(table_entry['table']) or keys.get(table_entry['input'])
    if named:
        return named
    # Empty means "the loader asks the source". It used to mean 'id', which was a
    # guess presented as a known value: the control plane frequently cannot introspect the
    # source at all (listing tables needs GetSecretValue on the credential), so
    # the operator types a table name manually and no key is ever discovered. The
    # loader has a working connection by definition, so it resolves this from
    # information_schema instead. See resolve_primary_key in bulk_loader.py.
    return ''


def describe_plan(plan_doc):
    """Human-readable plan, for `ecdl deploy --dry-run` and the UI review step."""
    lines = ['{} ({}) -> {} step(s)'.format(
        plan_doc['link'], plan_doc['mode'], len(plan_doc['steps']))]
    lines.append('  ECS cluster: {}'.format(plan_doc['cluster_name']))
    for i, step in enumerate(plan_doc['steps'], 1):
        if step['kind'] == 'stack':
            lines.append('  {}. stack {} ({})'.format(i, step['name'], step['template']))
            lines.append('       {}'.format(step['purpose']))
            if step.get('from_outputs'):
                wired = ', '.join(
                    '{}<-{}.{}'.format(p, s, o)
                    for p, (s, o) in sorted(step['from_outputs'].items()))
                lines.append('       from earlier stacks: {}'.format(wired))
        else:
            lines.append('  {}. {} (desired={})'.format(
                i, step['action'], step.get('desired')))
            lines.append('       {}'.format(step['purpose']))
    for note in plan_doc['notes']:
        lines.append('  - {}'.format(note))
    for warning in plan_doc['warnings']:
        lines.append('  ! {}'.format(warning))
    return '\n'.join(lines)
