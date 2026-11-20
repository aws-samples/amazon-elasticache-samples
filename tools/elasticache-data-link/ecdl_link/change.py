"""One change path (decision D7): what an update would change on the deployed link.

Every way of changing a running link -- `ecdl apply`, POST /api/links/deploy,
POST /api/links/repair and the UI editor -- plans an update and then calls
`review_update` before anything is sent to CloudFormation.

WHAT WAS WRONG
  * `ecdl apply` and /deploy chose between the first-deploy plan and the update
    plan from stack states alone and never compared the new document with what
    is deployed. A new target or source ARN was applied in place: the new cache
    received only future changes and the deploy said "deployed".
  * /repair compared two documents the CLIENT sent. The UI sent the document it
    had reconstructed, which left out advanced.*, so both sides lacked
    key_prefix, alarms, sizing, images and the CDC user; the diff saw no change
    and the update reset every one of them to its default. On a link with
    key_prefix "b" beside another link, the sink started writing the other
    link's keys.

THE RULE
Both sides of the comparison come from stack PARAMETERS: what the deployed
stacks hold, and what the planned update would send (the template's default
where the plan sends nothing). The parameters are what actually changes, so
nothing the document leaves out can change unnoticed. Each parameter belongs to
one field of the link file; a parameter that would change is reported as a
change of that field and classified by repair.diff. Changes nothing in the file
controls (the source or target moved, the identity keys) are refused.
"""

from __future__ import annotations

import json

from .repair import SEVERITY_ORDER, diff as doc_diff

# The source and target the link reads and writes. A different value means the
# stored log position or the cache contents belong to something else.
IDENTITY = {
    'SourceEngine': 'source',
    'SourceDBHost': 'source',
    'SourceDBPort': 'source',
    'SourceDBName': 'source.database',
    'TargetHost': 'target',
    'TargetPort': 'target',
    'TargetCluster': 'target',
    'TopicPrefix': 'name',
    'OffsetKey': 'name',
    'SchemaHistoryKey': 'name',
}

# Parameter -> the section of the link file that sets it.
SECTION = {
    'KeyPrefix': 'advanced.key_prefix',
    'AlarmsEnabled': 'advanced.alarms',
    'AlarmEmail': 'advanced.alarms',
    'SinkLagAlarmSeconds': 'advanced.alarms',
    'CaptureStoppedAlarmMinutes': 'advanced.alarms',
    'CaptureStaleAlarmSeconds': 'advanced.alarms',
    'TableStaleSeconds': 'advanced.alarms',
    # Follows the load timeout and the number of tables (Jira story F103).
    'CapturePausedAlarmHours': 'advanced.loader',
    'DebeziumImage': 'advanced.images',
    'SinkImage': 'advanced.images',
    'LoaderImage': 'advanced.images',
    'CpuArchitecture': 'advanced.cpu_architecture',
    'DebeziumCpu': 'advanced.debezium',
    'DebeziumMemory': 'advanced.debezium',
    'SnapshotMode': 'advanced.debezium',
    'ServerId': 'advanced.debezium',
    'SignalCollection': 'advanced.debezium',
    'SignalTopicPattern': 'advanced.debezium',
    'SinkWorkers': 'advanced.sink',
    'SinkBatchSize': 'advanced.sink',
    'SinkDesiredCount': 'advanced.sink',
    'SinkMinCount': 'advanced.sink',
    'SinkMaxCount': 'advanced.sink',
    'SinkTargetCpuPercent': 'advanced.sink',
    'LoaderWorkers': 'advanced.loader',
    'TaskCpu': 'advanced.loader',
    'TaskMemory': 'advanced.loader',
    'PrimaryKeys': 'advanced.loader',
    'SourceTLS': 'source.tls',
    'FetchBatch': 'advanced.loader',
    'NodeType': 'advanced.broker',
    'NodeCount': 'advanced.broker',
    'AssignPublicIp': 'advanced.network',
    'VpcId': 'advanced.network',
    'SubnetIds': 'advanced.network',
    'ClusterName': 'advanced.network',
    'SourceDBUser': 'source.username',
    'DBSecretArn': 'source.secret_arn',
    'DBSecretJsonKey': 'source.secret_json_key',
    'TargetSecretArn': 'target.secret_arn',
    'NodeType': 'advanced.broker',
    # Set by the control plane, not the file; a new value is shown, not refused.
    'MonitorSecurityGroupId': 'control plane',
    'PermissionsBoundaryArn': 'control plane',
}

# Compared through the document instead (tables and transforms are classified
# per table by repair.diff).
# SourceTables is the loader's Tables qualified by schema or database, so it
# follows from the same tables[] entries.
BY_DOCUMENT = {'Tables', 'SourceTables', 'Streams', 'TransformsJson'}

# Follow from the resolved resources or from earlier stacks; never a decision of
# the link file. A changed security group of the same database is legitimate.
# ProgressKeyBase follows from the link name, which names the stack itself.
DERIVED = {
    'AuroraSecurityGroupId', 'SourceSecurityGroupId', 'TargetSecurityGroupId',
    'TargetSSL', 'BrokerHost', 'BrokerPort', 'BrokerSecurityGroupId',
    'BrokerSecretArn', 'BrokerSSL', 'BrokerReplicationGroupId', 'ProgressKeyBase',
}

# Kept from the stack on every update (the deployer sends UsePreviousValue).
#  * DebeziumDesiredCount: sending 1 on an update resumed capture under a
#    running load; an update now never moves capture (see ensure_capture).
#  * PgSlotName: a new slot starts at the current WAL position.
#  * OffsetCommitted: state the deployer records, not a setting of the link.
#  * PgPublicationName: a running link keeps the publication it reads.
KEPT = {'DebeziumDesiredCount', 'PgSlotName', 'OffsetCommitted', 'PgPublicationName'}

# Used only when the stack creates the resource. A new value on a deployed link
# would replace the broker (the planner keeps the deployed value instead), so
# it is refused with the way to do it; removing the setting changes nothing.
CREATE_ONLY = {'RestoreSnapshotName': 'advanced.broker.restore_snapshot'}
# Properties of the control plane, not of the link: kept when the planner has
# no value (the CLI on a laptop knows neither), so one CLI update no longer
# drops the broker's monitor ingress or the task-role boundary.
CONTROL_PLANE = {'MonitorSecurityGroupId', 'PermissionsBoundaryArn'}


def _norm(value):
    return '' if value is None else str(value)


def effective_parameters(step, template_defaults):
    """What a stack would hold after the update: the plan's values, and the
    template default for every parameter the plan does not send."""
    out = {k: _norm(v) for k, v in (template_defaults or {}).items()}
    out.update({k: _norm(v) for k, v in (step.get('parameters') or {}).items()})
    return out


def parameter_drift(step, deployed, old_defaults, new_defaults):
    """[(param, deployed, planned)] for every parameter the update would change.

    A parameter the plan does not send takes the template default. If the stack
    also held its template's default, the change is a release upgrade (new
    pinned images, new defaults) and not reported.
    """
    planned = effective_parameters(step, new_defaults)
    sent = set((step.get('parameters') or {}).keys())
    wired = set((step.get('from_outputs') or {}).keys())
    # The deployer sends the service's live count for these (Jira story F139), so the
    # planned value is not what the update would change it to. And it keeps the
    # stack's value for keep_previous ones (UsePreviousValue): the broker's node
    # type and count when the link does not set them. A create-only value is
    # still reviewed (CREATE_ONLY).
    live = set((step.get('keep_live_count') or {}).keys())
    kept = set(step.get('keep_previous') or []) - set(CREATE_ONLY)
    out = []
    for p in sorted(set(planned) | set(deployed)):
        if p in wired or p in DERIVED or p in KEPT or p in live or p in kept:
            continue
        d, n = _norm(deployed.get(p)), planned.get(p, '')
        if d == n:
            continue
        if p in CONTROL_PLANE and not n:
            continue
        if p in CREATE_ONLY and not n:
            continue
        if p not in sent and d == _norm((old_defaults or {}).get(p)):
            continue
        out.append((p, d, n))
    return out


def _spec(text):
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return (parsed.get('tables') or {}) if isinstance(parsed, dict) else {}


def _tables(text):
    return [t.strip() for t in (text or '').split(',') if t.strip()]


def deployed_mode(present):
    """The mode the deployed stacks implement ({component: params})."""
    if 'cdc' in present:
        return 'initial_load_and_cdc' if 'loader' in present else 'cdc'
    if 'loader' in present:
        return 'initial_load'
    return None


def review_update(update_plan, desired_doc, deployed, old_defaults, new_defaults):
    """Compare a planned update with the deployed stacks.

    update_plan  provision.plan(..., for_update=True) of the desired document
    desired_doc  the desired link document (for name and mode)
    deployed     {'broker'|'cdc'|'loader': {param: value}} for stacks that exist
    old_defaults {component: {param: default}} of the DEPLOYED templates
    new_defaults {component: {param: default}} of the templates being sent

    Returns {'diff': repair.diff output with the parameter changes added,
    'drift': [...], 'refuse': bool, 'needs_ack': bool}. A refusal is a change
    that cannot be made in place; needs_ack means the cache needs a backfill or
    a cleanup after the update, which the caller must confirm and is told how.
    """
    steps = {s['id']: s for s in update_plan.get('steps') or [] if s.get('kind') == 'stack'}
    drift = []
    for comp, step in sorted(steps.items()):
        if comp not in deployed:
            continue
        for p, d, n in parameter_drift(step, deployed[comp],
                                       (old_defaults or {}).get(comp),
                                       (new_defaults or {}).get(comp)):
            drift.append({'stack': comp, 'parameter': p, 'from': d, 'to': n})

    # Both documents are built from parameters, so equal settings compare equal
    # however the file spells them (qualified names, filled defaults).
    def side(params_of, mode):
        doc = {'name': desired_doc.get('name'), 'mode': mode,
               'source': {'arn': 'same', 'database': 'same'},
               'target': {'arn': 'same'}}
        primary = params_of.get('cdc') or params_of.get('loader') or {}
        # Qualified names, so a table read from another schema is a change
        # (Jira story F126): the CDC stack's Tables are qualified; the loader's are bare
        # and its SourceTables qualified (absent on stacks that predate it).
        doc['tables'] = _tables(primary.get('SourceTables') or primary.get('Tables'))
        spec = _spec(primary.get('TransformsJson'))
        if spec:
            doc['transforms'] = {'tables': spec}
        if primary.get('KeyPrefix'):
            doc['advanced'] = {'key_prefix': primary['KeyPrefix']}
        return doc

    planned_params = {c: effective_parameters(s, (new_defaults or {}).get(c))
                      for c, s in steps.items()}
    current = side(deployed, deployed_mode(deployed))
    desired = side(planned_params, update_plan.get('mode') or desired_doc.get('mode'))
    # A component the desired mode no longer has is reported by the mode change.
    result = doc_diff(current, desired)
    changes = list(result['changes'])

    seen = set()
    for item in drift:
        p = item['parameter']
        if p in BY_DOCUMENT or p == 'KeyPrefix':
            continue  # already classified above
        # The CDC and loader stacks share most parameters; report each once.
        key = (p, item['from'], item['to'])
        if key in seen:
            continue
        seen.add(key)
        if p in IDENTITY:
            changes.append({
                'kind': 'identity', 'field': IDENTITY[p], 'parameter': p,
                'stack': item['stack'], 'from': item['from'], 'to': item['to'],
                'severity': 'recreate',
                'consequence': ('the deployed link reads and writes a different '
                                '{}: its stored log position and its cache '
                                'contents belong to that one, so changing it in '
                                'place would skip or replay an unknowable amount, '
                                'or leave the new cache with only future changes'
                                .format(IDENTITY[p])),
                'action': 'deploy this as a new link (it runs a full load), then '
                          'delete the old one',
            })
        elif p in CREATE_ONLY:
            changes.append({
                'kind': 'create_only', 'field': CREATE_ONLY[p], 'parameter': p,
                'stack': item['stack'], 'from': item['from'], 'to': item['to'],
                'severity': 'recreate',
                'consequence': ('a broker is restored from a snapshot only when it '
                                'is created; this broker already exists and holds '
                                'the link\'s offsets, so it is not replaced'),
                'action': ('to restore it, delete the link (its broker leaves a '
                           'final snapshot), then deploy it again with '
                           'advanced.broker.restore_snapshot (docs/operations.md, '
                           '"Restoring the broker from a snapshot")'),
            })
        elif p in SECTION:
            changes.append({
                'kind': 'setting', 'field': SECTION[p], 'parameter': p,
                'stack': item['stack'], 'from': item['from'], 'to': item['to'],
                'severity': 'deploy',
                'consequence': 'changes how the pipeline runs, not what is in '
                               'the cache',
                'action': 'update the stack',
            })
        else:
            changes.append({
                'kind': 'unexplained', 'field': p, 'parameter': p,
                'stack': item['stack'], 'from': item['from'], 'to': item['to'],
                'severity': 'recreate',
                'consequence': 'no field of the link file sets this parameter, so '
                               'the change cannot be reviewed',
                'action': 'report this; it is refused rather than applied unchecked',
            })

    severity = max([c['severity'] for c in changes] or ['none'], key=SEVERITY_ORDER.index)
    result.update(changed=bool(changes), changes=changes, severity=severity)
    out = {
        'diff': result,
        'drift': drift,
        'refuse': severity == 'recreate',
        'needs_ack': severity in ('backfill', 'cleanup'),
    }
    cdc = deployed.get('cdc')
    if cdc and cdc.get('SourceEngine') == 'postgresql':
        # The publication the running link reads (kept on update), for the
        # ALTER PUBLICATION an added table needs.
        from .provision import pg_publication_in_use
        out['pg_publication'] = pg_publication_in_use(desired_doc.get('name'), cdc)
    return out


def describe_review(review):
    """Plain text of a review, for the CLI and for error messages."""
    d = review['diff']
    if not d['changed']:
        return 'no change to the deployed link'
    lines = []
    for c in d['changes']:
        what = c['field']
        if c.get('parameter') and c['parameter'] not in (c['field'],):
            what = '{} ({} {!r} -> {!r})'.format(c['field'], c['parameter'],
                                                c.get('from'), c.get('to'))
        elif c.get('table'):
            what = '{} {}'.format(c['field'], c['table'])
        lines.append('[{}] {}'.format(c['severity'], what))
        lines.append('    why: {}'.format(c['consequence']))
        lines.append('    do:  {}'.format(c['action']))
    return '\n'.join(lines)


def template_body_defaults(body):
    """{param: default} of a template's text, read locally. (GetTemplateSummary
    with a TemplateBody is not scoped to a stack, which the control plane's
    policy does not grant.) Intrinsic tags (!Ref, !Sub) are read as plain
    values; only Parameters.*.Default is used."""
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor('!', lambda loader, suffix, node: None)
    doc = yaml.load(body, Loader=Loader) or {}  # nosec B506 - SafeLoader subclass
    out = {}
    for name, spec in (doc.get('Parameters') or {}).items():
        if isinstance(spec, dict) and 'Default' in spec:
            value = spec['Default']
            out[name] = ('true' if value is True else 'false' if value is False
                         else '' if value is None else str(value))
    return out


def template_defaults(cloudformation, **where):
    """{param: default} of a deployed stack's template (StackName=...)."""
    summary = cloudformation.get_template_summary(**where)
    return {p['ParameterKey']: p.get('DefaultValue', '')
            for p in summary.get('Parameters') or []}


def deployed_parameters(cloudformation, stack_names):
    """{component: {param: value}} for every one of the link's stacks that exists
    and is not deleted. Raises on any describe error other than "does not exist"."""
    out = {}
    for comp, name in stack_names.items():
        try:
            described = cloudformation.describe_stacks(StackName=name)
        except Exception as exc:  # noqa: BLE001
            if 'does not exist' in str(exc):
                continue
            raise
        items = described.get('Stacks') or []
        if not items or items[0].get('StackStatus') == 'DELETE_COMPLETE':
            continue
        out[comp] = {p['ParameterKey']: p.get('ParameterValue', '')
                     for p in items[0].get('Parameters') or []}
    return out



def review_deployed(cloudformation, update_plan, desired_doc, template_body):
    """review_update against the live stacks of the plan's link.

    template_body(filename) -> the template text being deployed (its defaults
    are what an omitted parameter becomes). Returns None when none of the
    link's stacks exist (a first deploy has nothing to compare with).
    """
    from .provision import stack_names
    names = stack_names(update_plan['link'], update_plan.get('prefix') or 'ecdl')
    deployed = deployed_parameters(cloudformation, names)
    if not deployed:
        return None
    old_defaults = {c: template_defaults(cloudformation, StackName=names[c]) for c in deployed}
    new_defaults = {}
    for step in update_plan.get('steps') or []:
        if step.get('kind') == 'stack' and step.get('template'):
            new_defaults[step['id']] = template_body_defaults(template_body(step['template']))
    return review_update(update_plan, desired_doc, deployed, old_defaults, new_defaults)


def follow_up(review, resolution):
    """The repair plan for a reviewed update, and the steps left for a person
    (the snapshot statement to run, the keys to delete)."""
    from .repair import outstanding_steps, repair_plan
    plan = repair_plan(review['diff'], resolution,
                       pg_publication=review.get('pg_publication'))
    engine = (resolution.get('source') or {}).get('engine')
    return plan, ([] if plan.get('blocked') else outstanding_steps(plan, engine))


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _thresholds(v):
    """'orders=3600,items=600' -> {'orders': 3600, 'items': 600}; None if malformed."""
    out = {}
    for part in str(v or '').split(','):
        if not part.strip():
            continue
        name, _sep, secs = part.partition('=')
        n = _int(secs)
        if not name.strip() or n is None:
            return None
        out[name.strip()] = n
    return out or None


# Parameter -> (path in the link file, converter). Used to rebuild a deployed
# link's file (the UI editor), so an edit round-trips every setting.
INVERSE = {
    'KeyPrefix': (('advanced', 'key_prefix'), str),
    'AlarmEmail': (('advanced', 'alarms', 'email'), str),
    'AlarmsEnabled': (('advanced', 'alarms', 'enabled'), lambda v: str(v).lower() == 'true'),
    'SinkLagAlarmSeconds': (('advanced', 'alarms', 'sink_lag_seconds'), _int),
    'CaptureStoppedAlarmMinutes': (('advanced', 'alarms', 'capture_stopped_minutes'), _int),
    'CaptureStaleAlarmSeconds': (('advanced', 'alarms', 'capture_stale_seconds'), _int),
    'TableStaleSeconds': (('advanced', 'alarms', 'table_stale_seconds'), _thresholds),
    'DebeziumImage': (('advanced', 'images', 'debezium'), str),
    'SinkImage': (('advanced', 'images', 'sink'), str),
    'LoaderImage': (('advanced', 'images', 'loader'), str),
    'CpuArchitecture': (('advanced', 'cpu_architecture'), lambda v: str(v).lower()),
    'DebeziumCpu': (('advanced', 'debezium', 'cpu'), str),
    'DebeziumMemory': (('advanced', 'debezium', 'memory'), str),
    'SnapshotMode': (('advanced', 'debezium', 'snapshot_mode'), str),
    'ServerId': (('advanced', 'debezium', 'server_id'), _int),
    'SignalCollection': (('advanced', 'debezium', 'signal_collection'), str),
    'SinkWorkers': (('advanced', 'sink', 'workers'), _int),
    'SinkBatchSize': (('advanced', 'sink', 'batch_size'), _int),
    'SinkDesiredCount': (('advanced', 'sink', 'desired_count'), _int),
    'SinkMinCount': (('advanced', 'sink', 'min_count'), _int),
    'SinkMaxCount': (('advanced', 'sink', 'max_count'), _int),
    'SinkTargetCpuPercent': (('advanced', 'sink', 'target_cpu_percent'), _int),
    'LoaderWorkers': (('advanced', 'loader', 'workers'), _int),
    'TaskCpu': (('advanced', 'loader', 'cpu'), str),
    'TaskMemory': (('advanced', 'loader', 'memory'), str),
    'SourceDBUser': (('source', 'username'), str),
    'DBSecretJsonKey': (('source', 'secret_json_key'), str),
    'AssignPublicIp': (('advanced', 'network', 'assign_public_ip'),
                       lambda v: str(v).upper() == 'ENABLED'),
    # Broker stack. Kept in the file so an edit shows what the broker was
    # created from.
    'RestoreSnapshotName': (('advanced', 'broker', 'restore_snapshot'), str),
    'NodeType': (('advanced', 'broker', 'node_type'), str),
}

# Derived by the planner from the link itself; only written to the file when
# the deployed value differs from what the plan of the file would produce.
INVERSE_IF_DRIFTED = {'SnapshotMode', 'ServerId', 'SignalCollection', 'SourceDBUser',
                      'AssignPublicIp', 'DBSecretJsonKey'}


def _set(doc, path, value):
    node = doc
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def apply_settings(doc, params, defaults, only=None):
    """Write into `doc` every mapped setting of `params` that differs from
    `defaults` (the value the planner sends, or the template default).
    `only` limits it to those parameter names. Returns the names written."""
    written = []
    for p, (path, conv) in sorted(INVERSE.items()):
        if only is not None and p not in only:
            continue
        if only is None and p in INVERSE_IF_DRIFTED:
            continue
        if p not in params:
            continue
        value = _norm(params.get(p))
        if value == _norm((defaults or {}).get(p)):
            continue
        converted = conv(value)
        if converted is None or converted == '':
            continue
        _set(doc, path, converted)
        written.append(p)
    return written


def primary_keys_setting(loader_params):
    """advanced.loader.primary_keys from the loader stack ({table: pk}),
    only for tables whose key was named."""
    tables = _tables(loader_params.get('Tables'))
    keys = (loader_params.get('PrimaryKeys') or '').split(',')
    out = {t: k.strip() for t, k in zip(tables, keys) if k.strip()}
    return out
