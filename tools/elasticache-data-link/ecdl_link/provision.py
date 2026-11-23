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


def broker_replication_group_id(link_name, prefix='ecdl'):
    """The broker's ElastiCache replication group id (its ClusterName)."""
    return '{}-{}-broker'.format(prefix, link_name)


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
        # false on a first deploy and on a reload (a position reset: the reload
        # re-reads the tables, so starting at the current position is right).
        # The deployer sets it to true once it has seen an offset committed, and
        # an update keeps the stack's value (keep_previous). With true, Debezium
        # refuses to start without its stored offset instead of silently
        # starting at the current log position (Jira story F005).
        'OffsetCommitted': 'false',

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

    # PostgreSQL only: a per-link replication slot and publication. See
    # pg_slot_name and pg_publication_name.
    if src['engine'] == 'postgresql':
        params['PgSlotName'] = pg_slot_name(resolved['name'])
        params['PgPublicationName'] = pg_publication_name(resolved['name'])

    # Debezium verifies TLS to the source unless the link turns it off, which
    # only an external source can (as for the loader, _loader_parameters).
    if src.get('external') and src.get('tls') is False:
        params['SourceTLS'] = 'false'

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
    if alarms.get('capture_stale_seconds'):
        params['CaptureStaleAlarmSeconds'] = str(alarms['capture_stale_seconds'])
    if alarms.get('table_stale_seconds'):
        params['TableStaleSeconds'] = table_stale_parameter(alarms['table_stale_seconds'])
    hours = capture_paused_hours(resolved)
    if hours:
        params['CapturePausedAlarmHours'] = str(hours)
    return params
def capture_paused_hours(resolved):
    """Jira story F103: hours of pause after which the capture-paused alarm fires, or None
    for the template default (a link that never loads).
    A load holds capture paused while it loads every table, one after the
    other, each for at most the load timeout. Longer than that, nothing is
    going to resume capture. One hour of margin; at most a day, which is all
    CloudWatch evaluates.
    """
    from .deploy import DEFAULT_LOAD_TIMEOUT_SECONDS
    if not (resolved.get('components') or {}).get('loader'):
        return None
    advanced = (resolved.get('link') or {}).get('advanced') or {}
    minutes = int((advanced.get('loader') or {}).get('timeout_minutes')
                  or DEFAULT_LOAD_TIMEOUT_SECONDS // 60)
    tables = max(1, len(resolved.get('tables') or []))
    return min(24, -(-minutes * tables // 60) + 1)


def alarm_topic_warning(advanced):
    """Jira story F105: the warning for alarms that go to a topic nobody reads.

    The CDC stack's alarm topic gets a subscriber only from
    advanced.alarms.email; the guided wizard sets none, so a link deployed
    from the UI used to have its alarms publish to nobody. The planner cannot
    see subscriptions made outside the link (the Pipeline Monitor checks those
    and shows a banner), so this names both ways out. None when alarms are
    off or an email is set.
    """
    alarms = (advanced or {}).get('alarms') or {}
    if alarms.get('enabled') is False or alarms.get('email'):
        return None
    return ('no alarm email is set (advanced.alarms.email), so the pipeline '
            'alarms reach nobody unless something is already subscribed to the '
            "CDC stack's AlarmTopicArn output. Set an email (and confirm it from "
            'the message SNS sends), or subscribe your paging or chat endpoint '
            'to that topic.')


def table_stale_parameter(thresholds):
    """{'Inventory.orders': 3600} -> 'orders=3600', the sink's TABLE_STALE_SECONDS.

    Bare names, sorted, so the same setting always gives the same parameter
    (a reordered file is no change). The sink matches them to its streams'
    tables ignoring case.
    """
    from .link import bare_table
    return ','.join('{}={}'.format(bare_table(name), int(secs))
                    for name, secs in sorted(thresholds.items(),
                                             key=lambda kv: bare_table(kv[0]).lower()))


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
    Any other JSON secret cannot be told from a plaintext one without reading
    it, so the link names its key in source.secret_json_key (Jira story F015).
    """
    if src.get('secret_json_key'):
        return src['secret_json_key']
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


# The publication every PostgreSQL link used before publications were per link
# (the image default, application-postgresql.properties). A stack created then
# has no PgPublicationName and keeps using it.
LEGACY_PG_PUBLICATION = 'dbz_pub'


def pg_publication_name(link_name):
    """PostgreSQL publication for this link: ecdl_<link>_pub.

    It used to be the shared dbz_pub. A publication decides which tables
    PostgreSQL sends the connector, so with one shared publication adding a
    table to one link meant altering an object every other link also read, and
    nothing said which link needed which table. Derived like the slot name:
    lowercase letters, digits and underscores, at most 63 characters, with the
    _pub suffix always kept.
    """
    base = 'ecdl_' + re.sub(r'[^a-z0-9_]', '_', str(link_name).lower())
    return base[:63 - len('_pub')] + '_pub'


def pg_publication_in_use(link_name, deployed_cdc=None):
    """The publication a link's Debezium reads.

    deployed_cdc is the deployed CDC stack's {parameter: value}, or None when
    the link has no CDC stack yet. A new link gets its own publication. A
    deployed link keeps what it runs with: the update keeps PgPublicationName
    (keep_previous), so a stack created before the parameter existed, or with
    it empty, still reads dbz_pub.
    """
    if deployed_cdc is None:
        return pg_publication_name(link_name)
    return deployed_cdc.get('PgPublicationName') or LEGACY_PG_PUBLICATION


def deployed_pg_publication(cloudformation, link_name, prefix='ecdl'):
    """pg_publication_in_use from the live CDC stack. Raises when the stack
    cannot be described (the caller decides how much that matters)."""
    from .change import deployed_parameters
    deployed = deployed_parameters(cloudformation,
                                   {'cdc': stack_names(link_name, prefix)['cdc']})
    return pg_publication_in_use(link_name, deployed.get('cdc'))


def offset_key(link_name):
    """Broker key holding Debezium's committed position for this link."""
    return 'ecdl:{}:offsets'.format(link_name)


def schema_history_key(link_name):
    return 'ecdl:{}:schema_history'.format(link_name)
def progress_key_base(link_name):
    """Start of this link's bulk-load progress hashes, one per table (the
    loader appends ':<key prefix>'; see progress_key)."""
    return 'ecdl:{}:bulkload'.format(link_name)
def progress_key(link_name, table, key_prefix=''):
    """The hash the loader publishes one table's progress to and resumes from:
    ecdl:<link>:bulkload:[<advanced.key_prefix>:]<bare table>. The same rule
    as progress_key_for in data-plane/bulk-loader/bulk_loader.py, applied to
    the key prefix the loader writes the table's rows under."""
    from .link import bare_table
    prefix = str(key_prefix or '').strip().rstrip(':')
    return '{}:{}{}'.format(progress_key_base(link_name),
                            prefix + ':' if prefix else '', bare_table(table))


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
    #
    # The wizard writes the bare default '<parent>:' explicitly. With a link key
    # prefix that is the wrong key whenever the parent is one of this link's own
    # tables (the sink writes it as '<prefix>:<parent>:<pk>'), so the embed found
    # nothing. An explicit bare default for an in-link parent is therefore treated
    # as the default; any other explicit value is kept.
    link_prefix = _link_key_prefix(resolved)
    in_link = {bare_table(t.get('table') or '') for t in resolved.get('tables') or []}
    for ts in tables.values():
        if not isinstance(ts, dict):
            continue
        for rule in ts.get('denormalize') or []:
            if not isinstance(rule, dict):
                continue
            parent = bare_table(rule.get('parent_table') or '')
            given = rule.get('parent_key_prefix')
            if not given or (link_prefix and parent in in_link
                             and given == parent + ':'):
                rule['parent_key_prefix'] = '{}{}:'.format(link_prefix, parent)
            rule['parent_table'] = parent
    # Key by bare name. Both engines look a table up by its bare name (the
    # sink by the change's source table, the loader by the last part of the
    # table), so a validated block keyed 'public.users' never applied: every
    # column was written and the filter skipped.
    bare_tables = {}
    for name, ts in tables.items():
        key = bare_table(name)
        if key in bare_tables:
            raise PlanError(
                "transforms.tables has more than one block for '{}'".format(key),
                'Keep one block per table.')
        bare_tables[key] = ts
    return json.dumps({'tables': bare_tables}, sort_keys=True, separators=(',', ':'))


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

# The broker of a NEW link (Jira stories F023, F024). A running broker keeps its node type
# and node count unless advanced.broker sets them: the planner sends these with
# keep_previous, so an update never resizes a broker nobody asked to resize.
#   * cache.m7g.large, not the template's cache.t4g.small: a burstable node
#     runs out of CPU credits under exactly the sustained backlog the broker
#     exists to absorb, and holds about 15 minutes of it at 1,000 changes/s.
#   * 3 nodes (a primary and 2 replicas): with one replica, every failover or
#     node replacement leaves a single copy until the new replica has synced.
DEFAULT_BROKER = {
    'node_type': 'cache.m7g.large',
    'replicas': 2,
}

# maxmemory (GB) of common ElastiCache node types, for the buffer estimate in
# the plan. ElastiCache reserves 25% of it by default (reserved-memory-percent).
BROKER_NODE_MEMORY_GB = {
    'cache.t4g.micro': 0.5, 'cache.t4g.small': 1.37, 'cache.t4g.medium': 3.09,
    'cache.t3.small': 1.37, 'cache.t3.medium': 3.09,
    'cache.m6g.large': 6.38, 'cache.m6g.xlarge': 12.93, 'cache.m6g.2xlarge': 26.04,
    'cache.m7g.large': 6.38, 'cache.m7g.xlarge': 12.93, 'cache.m7g.2xlarge': 26.04,
    'cache.r6g.large': 13.07, 'cache.r6g.xlarge': 26.32, 'cache.r6g.2xlarge': 52.82,
    'cache.r7g.large': 13.07, 'cache.r7g.xlarge': 26.32, 'cache.r7g.2xlarge': 52.82,
}
BROKER_EVENT_BYTES = 1100      # a CDC event with before and after images
BROKER_REFERENCE_RATE = 1000   # changes/s the estimate is quoted at


def broker_buffer_note(node_type, nodes):
    """One plan line: how long the broker can absorb a backlog. Pure."""
    mem = BROKER_NODE_MEMORY_GB.get(node_type)
    text = 'broker {}, {} nodes'.format(node_type, nodes)
    if mem is None:
        return text + ' (no buffer estimate for this node type)'
    usable = mem * 0.75
    minutes = usable * 1e9 / BROKER_EVENT_BYTES / BROKER_REFERENCE_RATE / 60
    return ('{}: about {:.1f} GB for queued changes, about {:.0f} minutes of '
            'backlog at {:,} changes/s (about {:.1f} KB each)'.format(
                text, usable, minutes, BROKER_REFERENCE_RATE,
                BROKER_EVENT_BYTES / 1000.0))


def _broker_sizing(advanced, steps_broker, notes, warnings, for_update):
    """Node type and node count for the broker step (Jira stories F023, F024).

    Set in advanced.broker: sent as is (an in-place scale, not a replacement).
    Not set: the new-link default, kept from the stack on an update.
    """
    cfg = advanced.get('broker') or {}
    node_type = (cfg.get('node_type') or '').strip()
    replicas = cfg.get('replicas')
    params = steps_broker['parameters']
    params['NodeType'] = node_type or DEFAULT_BROKER['node_type']
    params['NodeCount'] = str((replicas if replicas else DEFAULT_BROKER['replicas']) + 1)
    if not node_type:
        steps_broker['keep_previous'].append('NodeType')
    if not replicas:
        steps_broker['keep_previous'].append('NodeCount')
    if for_update and not node_type and not replicas:
        notes.append('the broker keeps its deployed node type and node count '
                     '(set advanced.broker.node_type or replicas to change them)')
    else:
        notes.append(broker_buffer_note(params['NodeType'], int(params['NodeCount'])))
    if params['NodeType'].startswith(('cache.t2.', 'cache.t3.', 'cache.t4g.')):
        warnings.append(
            'advanced.broker.node_type {} is burstable: under a sustained backlog it '
            'runs out of CPU credits and slows to its baseline, so the broker drains '
            'slower exactly when it is fullest. Use cache.m7g.large or larger for '
            'production.'.format(params['NodeType']))


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
        # Always derived from the link. This read advanced.network.cluster_name,
        # which the schema rejects, so the override could never be set (Jira story F129).
        'cluster_name': cluster_name(name, prefix),
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
                'ClusterName': broker_replication_group_id(name, prefix),
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
            'keep_if_empty': ['MonitorSecurityGroupId'],
            # The snapshot a broker is created from applies only at creation.
            # Sending a new one to a running broker would replace it (and its
            # offsets), so an update always keeps the deployed value.
            'keep_previous': ['RestoreSnapshotName'],
        })
        restore = ((advanced.get('broker') or {}).get('restore_snapshot') or '').strip()
        steps[-1]['parameters']['RestoreSnapshotName'] = restore
        _broker_sizing(advanced, steps[-1], notes, warnings, for_update)

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
            # existed keeps the old shared slot. The publication likewise: a
            # running slot reading dbz_pub must not be switched to a
            # publication that may not exist yet, or lack the link's tables.
            'keep_previous': ['PgSlotName', 'PgPublicationName'] + (
                # An update never moves capture: sending 1 resumed it under a
                # running load. ensure_capture (below) decides instead. And an
                # update never forgets that the link has committed an offset.
                ['DebeziumDesiredCount', 'OffsetCommitted'] if for_update else []),
            'keep_if_empty': ['MonitorSecurityGroupId', 'PermissionsBoundaryArn'],
        }
        if for_update and 'desired_count' not in (advanced.get('sink') or {}):
            # Autoscaling owns the sink's task count once it runs. Sending the
            # default (2) on every update reset a sink scaled out to drain a
            # backlog back to 2 (Jira story F139). The deployer sends the service's live
            # count instead (deploy._keep_live_count). A link that sets
            # advanced.sink.desired_count still gets exactly that.
            cdc['keep_live_count'] = {'SinkDesiredCount': 'sink'}
        steps.append(cdc)
        unread = alarm_topic_warning(advanced)
        if unread:
            warnings.append(unread)

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
            'keep_if_empty': ['PermissionsBoundaryArn'],
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
            #
            # The loader STACK is created before the pause, not inside it
            # (Jira story F086): creating it takes minutes (IAM roles, a task definition,
            # log groups), and every minute capture is paused is a minute of
            # changes that exist only in the source's log. Only the load
            # itself (run_load, added after the pause by add_load_step) needs
            # capture paused.
            loader_step['depends_on'] = ['cdc']
            steps.append(loader_step)
            steps.append({
                'kind': 'action',
                'id': 'pause_capture',
                'action': 'set_debezium_desired',
                'desired': 0,
                'stack': 'cdc',
                'purpose': 'pause change capture so the load cannot be '
                           'overwritten by a concurrent CDC write',
                'depends_on': ['cdc', 'loader'],
            })
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

    if for_update and components['debezium']:
        steps.append({
            'kind': 'action',
            'id': 'ensure_capture',
            'action': 'ensure_capture',
            'stack': 'cdc',
            'purpose': 'capture must be running after an update: resume it if it '
                       'was left paused, unless a load is still running',
            'depends_on': [s['id'] for s in steps if s['kind'] == 'stack'],
        })

    if not steps:
        raise PlanError(
            'mode {!r} produces nothing to deploy'.format(mode))

    if mode == 'initial_load' and not for_update:
        notes.append('one-shot load: nothing is left running afterwards, and '
                     'the cache goes stale from the moment the load finishes')

    out = {
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
    # advanced.loader.timeout_minutes (Jira story F068): the deployer's budget for one
    # table's load. Absent, the deployer's own default (six hours) applies.
    if components['loader'] and loader.get('timeout_minutes'):
        out['load_timeout_seconds'] = int(loader['timeout_minutes']) * 60
    return out


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
        # The same tables as the source names them (PostgreSQL / SQL Server /
        # Oracle schema.table, MySQL database.table), positionally matched to
        # Tables. The loader queries this name; it used to query the bare one,
        # which in PostgreSQL resolved through search_path -- a table outside
        # `public` failed, or a same-named table in `public` was loaded instead.
        # Tables stays bare: it names the RunTask override and the cache keys.
        'SourceTables': ','.join(_source_table(t) for t in tables),
        'PrimaryKeys': ','.join(_pk_for(resolved, t) for t in tables),
        # Per link, so two links loading the same table into one cache keep
        # separate progress and neither resumes the other's load. The loader
        # adds the table's key prefix (see progress_key).
        'ProgressKeyBase': progress_key_base(resolved['name']),
        'LoaderWorkers': str(loader['workers']),
        'TaskCpu': str(loader['cpu']),
        'TaskMemory': str(loader['memory']),
    }
    if images.get('loader'):
        params['LoaderImage'] = images['loader']
    # The loader verifies TLS to the source unless the link turns it off,
    # which only an external source can. Sent only then, as an override.
    if src.get('external') and src.get('tls') is False:
        params['SourceTLS'] = 'false'
    # The sampled cache check (review finding C1) only means something while
    # capture keeps the cache up to date: a one-time load drifts by design.
    if resolved.get('mode') == 'initial_load':
        params['VerifyIntervalMinutes'] = '0'
    if src['engine'] == 'postgresql':
        params['PgSlotName'] = pg_slot_name(resolved['name'])
    if loader.get('fetch_batch'):
        params['FetchBatch'] = str(loader['fetch_batch'])
    # The load alarm (a load task that stops with an error) follows the
    # link's alarm settings, sent only as overrides, like the CDC stack's.
    alarms = ((resolved.get('link') or {}).get('advanced') or {}).get('alarms') or {}
    if alarms.get('enabled') is False:
        params['AlarmsEnabled'] = 'false'
    if alarms.get('email'):
        params['AlarmEmail'] = alarms['email']
    arch = _cpu_architecture(resolved)
    if arch:
        params['CpuArchitecture'] = arch
    transforms = _transforms_json(resolved)
    if transforms:
        params['TransformsJson'] = transforms
    return params


def _source_table(table_entry):
    """The schema- or database-qualified name the loader reads a table by."""
    return table_entry.get('qualified') or table_entry['table']
def _pk_for(resolved, table_entry):
    """The integer column the loader splits a table's read by, or '' for the
    first column of its primary key. Rows are keyed by the whole primary key
    whatever this is.
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
