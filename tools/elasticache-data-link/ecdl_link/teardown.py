"""Delete a link's own stacks, and nothing else.

A link owns up to three CloudFormation stacks, all named `ecdl-<link>-*`:

    ecdl-<link>-cdc      Debezium + sink ECS services, IAM roles, log groups
    ecdl-<link>-loader   the one-shot loader (initial-load links only)
    ecdl-<link>-broker   the broker Valkey, its auth secret, and this link's
                         offsets / schema-history / streams

The broker's DeletionPolicy is Snapshot, so deleting its stack leaves a final
snapshot of it: the only copy of the offsets, schema history, unapplied changes
and dead letters, from which the link can be deployed again (operations.md,
"Restoring the broker from a snapshot"). ElastiCache charges for it until it is
deleted. The teardown reports its name and never deletes it.

The source database and the target cache are NOT in this list. They are
referenced by endpoint and ARN, shared across links, and created outside the
link's own stacks, so a teardown must not change them. The safety is
structural rather than a check added afterwards: every name this module
produces comes from `provision.stack_names(link, prefix)`, which can only ever
spell `ecdl-<link>-{broker,cdc,loader}`. There is no path by which a source or
target stack name is generated, so none can be deleted.

Deletion order is the reverse of creation: the cdc and loader stacks are the
consumers and go first, then the broker they read from.

Clients are injected (see deploy.Clients) so the interesting cases -- a stack
that is already gone, one stuck in DELETE_FAILED, an ECS cluster still in use by
another link -- are testable without an AWS account.
"""

from __future__ import annotations

import re
import time

from .deploy import Clients, DeployError  # noqa: F401  (Clients re-exported)
from .provision import (broker_replication_group_id, cluster_name, offset_key,
                        pg_slot_name, schema_history_key, stack_names)
from .signals import signal_table_name

# Reverse of the deploy order in provision.plan(): consumers first (cdc, then
# loader), then the broker those consumers read from.
_DELETE_ORDER = ('cdc', 'loader', 'broker')

# Where the sink keeps what it has not applied (Jira story F136). The consumer group is
# the CDC template's CONSUMER_GROUP; dead letters go to DLQ_STREAM_PREFIX +
# the source stream (the Go sink's default, also backend/links.py _DLQ_PREFIX).
SINK_GROUP = 'ecdl-sink'
DLQ_PREFIX = 'dlq:'
# Debezium's heartbeat stream has no consumer, so it is never backlog.
_HEARTBEAT_PREFIX = 'data:__debezium-heartbeat.'


def _text(value):
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else value


def _unapplied(client, stream, group=SINK_GROUP):
    """Entries of one stream the sink has not acknowledged.

    The group's lag (not yet delivered) plus its pending entries (delivered,
    not acknowledged). With no group, the sink never read the stream, so all
    of it. When the broker cannot compute the lag (entries deleted out of
    order), the stream's length is used: an upper bound, never an undercount.
    """
    try:
        groups = client.xinfo_groups(stream)
    except Exception as exc:  # noqa: BLE001
        if 'no such key' in str(exc).lower():
            return 0
        raise
    for info in groups or []:
        info = {_text(k): v for k, v in info.items()}
        if _text(info.get('name')) != group:
            continue
        lag = info.get('lag')
        if lag is None:
            return int(client.xlen(stream) or 0)
        return int(lag) + int(info.get('pending') or 0)
    return int(client.xlen(stream) or 0)


def broker_backlog(client, streams):
    """What deleting the broker would take with it that is not in the cache.

    Returns {'backlog': {stream: unapplied entries}, 'dead_letters':
    {dlq stream: entries}} with only non-zero counts. `streams` are the CDC
    stack's streams, or None to scan the broker for them (the CDC stack is
    already gone). Every dead-letter stream on the broker is counted: the
    broker belongs to this link alone.
    """
    if streams is None:
        streams = sorted(_text(k) for k in client.scan_iter(match='data:*', _type='STREAM')
                         if not _text(k).startswith(_HEARTBEAT_PREFIX))
    backlog = {}
    for stream in streams:
        count = _unapplied(client, stream)
        if count:
            backlog[stream] = count
    dead = {}
    for key in client.scan_iter(match=DLQ_PREFIX + '*', _type='STREAM'):
        count = int(client.xlen(_text(key)) or 0)
        if count:
            dead[_text(key)] = count
    return {'backlog': backlog, 'dead_letters': dead}


def describe_broker_data(data):
    """One line for a broker_data() result, for the CLI and the API."""
    if not data or not data.get('exists'):
        return 'the link has no broker stack'
    if not data.get('readable'):
        return ('could not read the broker ({}), so whether it holds changes not yet '
                'written to the cache, or dead letters, is unknown. They are kept in '
                "the broker's final snapshot.".format(data.get('reason')))
    if not data.get('at_risk'):
        return 'the broker holds no unapplied changes and no dead letters'
    return ('the broker holds {} change(s) not yet written to the cache and {} dead '
            "letter(s). Deleting it keeps them only in the broker's final snapshot, "
            'from which the link can be deployed again.'.format(
                data['unapplied'], data['dead_letters_total']))


# Jira story F140. Deleting a stack deletes its log groups, which used to leave nothing
# to diagnose a link after its teardown. Before each stack goes, the error
# and warning lines of its log groups from the last day are copied into the
# teardown result (the control plane keeps that in the deployment record; the
# CLI writes a file). Retaining the groups instead would also mean retaining
# each stack's KMS key (they are encrypted with it, about $1 a month each,
# indefinitely), and a retained group makes the next deploy of the same link
# fail on "log group already exists".
#
# Names as the templates set them: ecdl-cdc-ecs.yaml /ecs/<stack>/debezium and
# /sink, ecdl-loader-ecs.yaml /ecs/<stack>, ecdl-broker-quickstart.yaml
# /ecs/<stack>/broker.
_LOG_GROUPS = {'cdc': ('/ecs/{}/debezium', '/ecs/{}/sink', '/ecs/{}/restart'),
               'loader': ('/ecs/{}',),
               'broker': ('/ecs/{}/broker',)}
_KEPT_LOG_PATTERN = '?ERROR ?FATAL ?WARN ?Exception'
_KEPT_LOG_HOURS = 24
_KEPT_LOG_LINES = 50          # per group, the newest
_KEPT_LOG_LINE_CHARS = 500
_KEPT_LOG_PAGES = 10          # FilterLogEvents pages read per window, at most


def log_groups_of(step_id, stack_name):
    """The log groups a link stack creates (and deletes with it)."""
    return [pattern.format(stack_name) for pattern in _LOG_GROUPS.get(step_id, ())]


def destroy_plan(link_name, prefix='ecdl'):
    """The ordered set of stacks to delete for one link, plus what is kept.

    Pure: it reads nothing and deletes nothing, so a caller (and a test) can see
    exactly what a teardown would touch before anything happens. The `follow_up`
    names resources that are in the customer's own source DB or on a shared
    broker and are therefore NOT removed by deleting a stack.
    """
    if not link_name or not str(link_name).strip():
        raise ValueError('a link name is required')
    link_name = str(link_name).strip()

    names = stack_names(link_name, prefix)
    stacks = [{'id': sid, 'name': names[sid]} for sid in _DELETE_ORDER]

    return {
        'link': link_name,
        'prefix': prefix,
        'cluster_name': cluster_name(link_name, prefix),
        'stacks': stacks,
        # Named so the operator can see the shared resources are safe.
        'keeps': [
            'the source database (referenced by endpoint/secret ARN, not owned '
            'by this link)',
            'the target ElastiCache / Valkey (referenced by endpoint, shared '
            'across links)',
        ],
        # Lives outside this link's stacks, so it is reported, not deleted.
        'follow_up': {
            'signal_table': signal_table_name(link_name),
            'broker_offset_key': offset_key(link_name),
            'broker_schema_history_key': schema_history_key(link_name),
            # PostgreSQL sources only. Deleting the stack stops Debezium but
            # leaves its replication slot, and an INACTIVE slot makes the source
            # retain WAL forever -- the disk fills. Drop it once the link is gone
            # (or ecdl_slot for a link created before slots were per link).
            'pg_replication_slot': pg_slot_name(link_name),
            'pg_drop_slot_sql': "SELECT pg_drop_replication_slot('{}');".format(
                pg_slot_name(link_name)),
            # Kept by the broker's DeletionPolicy. Its name is chosen by
            # CloudFormation, so the Destroyer reads it after the delete
            # (broker_final_snapshot); these find it if that read failed.
            'broker_replication_group': broker_replication_group_id(link_name, prefix),
            'broker_snapshot_find_command': _find_snapshots_command(
                broker_replication_group_id(link_name, prefix)),
        },
    }


def _find_snapshots_command(group):
    return ('aws elasticache describe-snapshots --replication-group-id {} '
            '--query "Snapshots[].SnapshotName"'.format(group))


def _delete_snapshot_command(name):
    return 'aws elasticache delete-snapshot --snapshot-name {}'.format(name)


def describe_destroy_plan(plan_doc):
    """Human-readable preview, for `ecdl destroy --dry-run` and the confirm step."""
    lines = ['destroy pipeline {!r} -> delete {} stack(s), in order:'.format(
        plan_doc['link'], len(plan_doc['stacks']))]
    for step in plan_doc['stacks']:
        lines.append('    {:<7} {}'.format(step['id'], step['name']))
    lines.append('  ECS cluster {} is deleted only if it is empty afterwards.'
                 .format(plan_doc['cluster_name']))
    lines.append('  KEPT (never deleted):')
    for item in plan_doc['keeps']:
        lines.append('    - {}'.format(item))
    group = (plan_doc.get('follow_up') or {}).get('broker_replication_group')
    if group:
        lines.append('  The broker {} is deleted with a FINAL SNAPSHOT (its offsets, schema'
                     .format(group))
        lines.append('  history, unapplied changes and dead letters). ElastiCache keeps it,')
        lines.append('  and charges for it, until you delete it; the link can be deployed')
        lines.append('  again from it (advanced.broker.restore_snapshot).')
    return '\n'.join(lines)


def _noop(event):
    pass


class Destroyer(object):
    """Deletes the stacks in a destroy_plan, in order, and waits for each."""

    def __init__(self, clients, on_progress=None, poll_seconds=10,
                 timeout_seconds=1800, sleep=time.sleep, delete_cluster=True,
                 role_arn=None, delete_backlog=False, broker_reader=None):
        self.clients = clients
        # The CloudFormation service role (D1). A stack created with a role is
        # deleted with it; the control plane's own credentials cannot delete
        # the resources.
        self.role_arn = role_arn
        self.on_progress = on_progress or _noop
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.sleep = sleep
        # The final snapshot can take a few seconds to be listed after the
        # broker stack is gone (measured: about 40 s).
        self.snapshot_wait_tries = 9
        self.snapshot_wait_seconds = 10
        self.delete_cluster = delete_cluster
        # Jira story F136. The broker is deleted with whatever the sink has not applied
        # and with its dead letters; without this the delete is refused when
        # it holds any. broker_reader(outputs, streams) -> broker_backlog();
        # injectable for tests, by default a valkey client from the outputs.
        self.delete_backlog = delete_backlog
        self.broker_reader = broker_reader
        # How long to wait for a running load task to stop.
        self.stop_wait_seconds = 300
        self.now = time.time

    def run(self, plan_doc):
        link = plan_doc['link']
        prefix = plan_doc.get('prefix', 'ecdl')
        # The guarantee, enforced not just documented: refuse to delete anything
        # that is not one of this link's own stacks, even if a manually constructed plan
        # includes another name. stack_names() is the sole source of truth for
        # what a link owns.
        owned = set(stack_names(link, prefix).values())

        self._emit('destroy_started', link=link,
                   stacks=[s['name'] for s in plan_doc['stacks']])
        # Checked for every stack before any is read or touched.
        for step in plan_doc['stacks']:
            self._check_owned(step['name'], link, owned)

        # Refused while another operation changes a stack (Jira story F136): deleting
        # under a running deploy leaves both half done. A delete already in
        # progress is waited for below.
        busy = {s['name']: st for s in plan_doc['stacks']
                for st in [self._stack_state(s['name'])]
                if st and st.endswith('_IN_PROGRESS') and st != 'DELETE_IN_PROGRESS'}
        if busy:
            raise DeployError(
                'pipeline {!r} is being changed right now ({}); nothing was deleted'.format(
                    link, ', '.join('{} {}'.format(n, st) for n, st in sorted(busy.items()))),
                'Wait for it to finish, then delete again.')

        # Resolve the ECS cluster to clean up BEFORE deleting the cdc stack,
        # because the authoritative name is that stack's ClusterName parameter
        # and it is gone once the stack is deleted.
        cluster = self._cluster_to_clean(plan_doc)

        data = self.broker_data(plan_doc)
        self._emit('broker_data', **{k: v for k, v in data.items() if k != 'exists'})
        if data.get('at_risk') and not self.delete_backlog:
            raise DeployError(
                'nothing was deleted: {}'.format(describe_broker_data(data)),
                'Let the sink catch up, or replay the dead letters (docs/operations.md, '
                '"Dead letters"), then delete again; or delete anyway with '
                '`ecdl destroy --delete-backlog` ("delete_backlog": true).')

        # A load task still running would keep writing to the target after its
        # stack, roles and log group are gone (Jira story F136).
        self._stop_loads(plan_doc, cluster)

        follow_up = dict(plan_doc.get('follow_up') or {})
        deleted, skipped = [], []
        for step in plan_doc['stacks']:
            name = step['name']
            self._check_owned(name, link, owned)
            state = self._stack_state(name)
            if state is None:
                self._emit('stack_absent', id=step['id'], stack=name)
                skipped.append(name)
                continue
            kept = self._keep_log_lines(step['id'], name)
            if kept:
                follow_up.setdefault('kept_log_lines', {}).update(kept)
            self._emit('stack_delete_started', id=step['id'], stack=name,
                       state=state)
            self.clients.cloudformation.delete_stack(
                StackName=name, **({'RoleARN': self.role_arn} if self.role_arn else {}))
            self._wait_for_delete(name)
            self._emit('stack_deleted', id=step['id'], stack=name)
            deleted.append(name)
            if step['id'] == 'broker' and follow_up.get('broker_replication_group'):
                follow_up.update(self._final_snapshot(follow_up['broker_replication_group']))

        cluster_deleted = False
        if self.delete_cluster:
            cluster_deleted = self._maybe_delete_cluster(cluster)

        self._emit('destroy_complete', link=link, deleted=deleted,
                   skipped=skipped, cluster_deleted=cluster_deleted)
        return {
            'link': link,
            'deleted': deleted,
            'skipped': skipped,
            'cluster_deleted': cluster_deleted,
            'kept': plan_doc.get('keeps', []),
            'follow_up': follow_up,
        }

    @staticmethod
    def _check_owned(name, link, owned):
        if name not in owned:
            raise DeployError(
                'refusing to delete {!r}: it is not a stack owned by link '
                '{!r} (owned: {})'.format(name, link, ', '.join(sorted(owned))))

    # ── What the broker holds (Jira story F136) ─────────────────────────────────────
    def broker_data(self, plan_doc):
        """What deleting the broker would delete that is not in the cache.

        {'exists', 'readable', 'reason', 'backlog', 'dead_letters',
        'unapplied', 'dead_letters_total', 'at_risk'}. Never raises: a broker
        that cannot be read from here (the CLI on a laptop) is reported as
        unreadable, not as empty, and does not block the delete.
        """
        names = {s['id']: s['name'] for s in plan_doc['stacks']}
        broker = names.get('broker')
        try:
            stacks = self.clients.cloudformation.describe_stacks(StackName=broker).get(
                'Stacks') or [] if broker else []
        except Exception as exc:  # noqa: BLE001
            if 'does not exist' in str(exc):
                return {'exists': False, 'at_risk': False}
            return self._unread('the broker stack could not be read ({})'.format(exc))
        if not stacks:
            return {'exists': False, 'at_risk': False}
        outputs = {o['OutputKey']: o['OutputValue'] for o in stacks[0].get('Outputs') or []}
        streams = None
        if names.get('cdc'):
            try:
                cdc = (self.clients.cloudformation.describe_stacks(StackName=names['cdc'])
                       .get('Stacks') or [{}])[0]
                params = {p['ParameterKey']: p.get('ParameterValue')
                          for p in cdc.get('Parameters') or []}
                if params.get('Streams') is not None:
                    streams = [x.strip() for x in params['Streams'].split(',') if x.strip()]
            except Exception:  # noqa: BLE001 -- gone already: scan the broker instead
                streams = None
        try:
            found = (self.broker_reader or self._read_broker)(outputs, streams)
        except Exception as exc:  # noqa: BLE001
            return self._unread(str(exc))
        if found is None:
            return self._unread('the broker stack has no endpoint output')
        backlog, dead = found.get('backlog') or {}, found.get('dead_letters') or {}
        unapplied, dead_total = sum(backlog.values()), sum(dead.values())
        return {'exists': True, 'readable': True, 'backlog': backlog, 'dead_letters': dead,
                'unapplied': unapplied, 'dead_letters_total': dead_total,
                'at_risk': bool(unapplied or dead_total)}

    @staticmethod
    def _unread(reason):
        return {'exists': True, 'readable': False, 'reason': reason, 'at_risk': False}

    def _read_broker(self, outputs, streams):
        from .deploy import broker_client
        if not outputs.get('PrimaryEndpoint'):
            return None
        client = broker_client(self.clients.secrets(), outputs)
        if client is None:
            return None
        return broker_backlog(client, streams)

    # ── Load tasks (Jira story F136) ──────────────────────────────────────────────────
    def _stop_loads(self, plan_doc, cluster):
        """Stop this link's running loader tasks and wait until they stop."""
        family = stack_names(plan_doc['link'], plan_doc.get('prefix', 'ecdl'))['loader']
        try:
            listed = self.clients.ecs.list_tasks(cluster=cluster, family=family,
                                                 desiredStatus='RUNNING')
        except Exception as exc:  # noqa: BLE001
            if 'ClusterNotFound' in str(exc) or 'not found' in str(exc).lower():
                return
            raise DeployError(
                'could not check for a running load of {!r} ({}); nothing was '
                'deleted'.format(plan_doc['link'], exc),
                'Check the caller can run ecs:ListTasks, then delete again.')
        arns = list(listed.get('taskArns') or [])
        if not arns:
            return
        for arn in arns:
            self._emit('load_stopping', task=arn)
            try:
                self.clients.ecs.stop_task(cluster=cluster, task=arn,
                                           reason='ecdl destroy: the link is being deleted')
            except Exception as exc:  # noqa: BLE001
                if 'not found' not in str(exc).lower():
                    raise DeployError(
                        'could not stop the load task {} ({}); nothing was deleted'.format(
                            arn, exc), 'Stop it, then delete again.')
        waited = 0
        while True:
            tasks = self.clients.ecs.describe_tasks(cluster=cluster, tasks=arns).get(
                'tasks') or []
            alive = [t for t in tasks if t.get('lastStatus') != 'STOPPED']
            if not alive:
                self._emit('loads_stopped', tasks=arns)
                return
            if waited >= self.stop_wait_seconds:
                raise DeployError(
                    'a load task was still {} after {}s; nothing was deleted'.format(
                        alive[0].get('lastStatus'), self.stop_wait_seconds),
                    'Wait for it to stop, then delete again.')
            self.sleep(self.poll_seconds)
            waited += (self.poll_seconds or 1)

    # ── Broker final snapshot ─────────────────────────────────────────────
    def _final_snapshot(self, group):
        """The snapshot the broker's DeletionPolicy left, by name.

        Read after the stack is gone: CloudFormation names it, and the
        automatic snapshots are deleted with the replication group, so a
        manual snapshot of this group is the final one (the newest, if an
        operator also took some). A failed read is reported, not raised: the
        stacks are already deleted, and the find command still works.
        """
        try:
            found = self._snapshots_of(self.clients.elasticache(), group)
            # CloudFormation may still be finishing the snapshot it named.
            for _ in range(self.snapshot_wait_tries):
                if found:
                    break
                self.sleep(self.snapshot_wait_seconds)
                found = self._snapshots_of(self.clients.elasticache(), group)
        except Exception as exc:  # noqa: BLE001
            self._emit('broker_snapshot_unknown', replication_group=group, error=str(exc),
                       find=_find_snapshots_command(group))
            return {'broker_final_snapshot': None}

        def created(snap):
            # A snapshot still being written may have no time yet: newest.
            times = [n.get('SnapshotCreateTime') for n in snap.get('NodeSnapshots') or []
                     if n.get('SnapshotCreateTime')]
            return (0, max(times)) if times else (1, 0)
        found = sorted(found, key=created)
        if not found:
            self._emit('broker_snapshot_missing', replication_group=group)
            return {'broker_final_snapshot': None}
        name = found[-1].get('SnapshotName')
        self._emit('broker_snapshot_kept', replication_group=group, snapshot=name,
                   status=found[-1].get('SnapshotStatus'))
        return {'broker_final_snapshot': name,
                'broker_snapshot_delete_command': _delete_snapshot_command(name),
                'broker_other_snapshots': [s.get('SnapshotName') for s in found[:-1]]}

    @staticmethod
    def _snapshots_of(client, group):
        """Manual snapshots of a broker replication group.

        A cluster-mode-disabled group's snapshot is recorded against its NODE
        (CacheClusterId <group>-001, ReplicationGroupId empty), so a
        ReplicationGroupId filter misses the final snapshot. Every manual
        snapshot is listed and matched by group or by node name.
        """
        out, marker = [], None
        for _ in range(50):
            kwargs = {'SnapshotSource': 'user'}
            if marker:
                kwargs['Marker'] = marker
            page = client.describe_snapshots(**kwargs)
            for snap in page.get('Snapshots') or []:
                node = snap.get('CacheClusterId') or ''
                # Node names are <group>-001, <group>-002; a longer group name
                # that starts with this one is another link's broker.
                if snap.get('ReplicationGroupId') == group or re.fullmatch(
                        re.escape(group) + r'-\d{3}', node):
                    out.append(snap)
            marker = page.get('Marker')
            if not marker:
                break
        return out

    # ── Log lines kept for diagnosis (Jira story F140) ───────────────────────────────
    def _keep_log_lines(self, step_id, stack_name):
        """{group: [lines]} of the newest error and warning lines of a stack's
        log groups. Best effort: a teardown is never stopped because its logs
        could not be read, but it says so."""
        groups = log_groups_of(step_id, stack_name)
        if not groups:
            return {}
        try:
            client = self.clients.logs()
        except Exception as exc:  # noqa: BLE001
            self._emit('log_lines_not_kept', stack=stack_name, error=str(exc))
            return {}
        out = {}
        for group in groups:
            try:
                # The last day; when that is more than a few pages, the last
                # hour instead, so what is kept is the newest.
                for hours in (_KEPT_LOG_HOURS, 1):
                    lines, complete = self._read_log_lines(client, group, hours)
                    if complete:
                        break
            except Exception as exc:  # noqa: BLE001
                if 'ResourceNotFound' in type(exc).__name__ or 'does not exist' in str(exc):
                    continue            # no such group: nothing ever ran
                self._emit('log_lines_not_kept', stack=stack_name, group=group,
                           error=str(exc))
                continue
            out[group] = lines
            self._emit('log_lines_kept', stack=stack_name, group=group,
                       lines=len(lines), hours=hours,
                       **({} if complete else {'only_part_read': True}))
        return out

    def _read_log_lines(self, client, group, hours):
        """(newest lines, whether every page was read) of one group."""
        kwargs = {'logGroupName': group, 'filterPattern': _KEPT_LOG_PATTERN,
                  'startTime': int((self.now() - hours * 3600) * 1000)}
        lines = []
        for _ in range(_KEPT_LOG_PAGES):
            resp = client.filter_log_events(**kwargs)
            for event in resp.get('events') or []:
                text = (event.get('message') or '').rstrip()
                stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                      time.gmtime((event.get('timestamp') or 0) / 1000))
                lines.append('{} {} {}'.format(stamp, event.get('logStreamName', ''),
                                               text[:_KEPT_LOG_LINE_CHARS]))
            lines = lines[-_KEPT_LOG_LINES:]
            if not resp.get('nextToken'):
                return lines, True
            kwargs['nextToken'] = resp['nextToken']
        return lines, False

    # ── ECS cluster ────────────────────────────────────────────────────────
    def _cluster_to_clean(self, plan_doc):
        """The ECS cluster this link's tasks actually ran in.

        Deploys disagree on the name: `ecdl apply` uses `ecdl-<link>`, while
        quickstart uses `ecdl-<prefix>-cluster`. Deriving it would clean the
        wrong one (and orphan the real, now-empty cluster), so read the cdc
        stack's ClusterName parameter -- the authoritative value -- before that stack
        is deleted. Fall back to the planned name when the stack is already gone
        or carries no such parameter.
        """
        cdc_name = next((s['name'] for s in plan_doc['stacks']
                         if s['id'] == 'cdc'), None)
        if cdc_name:
            try:
                described = self.clients.cloudformation.describe_stacks(
                    StackName=cdc_name)
                stacks = described.get('Stacks') or []
                params = {p['ParameterKey']: p['ParameterValue']
                          for p in (stacks[0].get('Parameters') if stacks else [])
                          or []}
                if params.get('ClusterName'):
                    self._emit('cluster_resolved', cluster=params['ClusterName'],
                               source='cdc-stack')
                    return params['ClusterName']
            except Exception:  # noqa: BLE001
                pass
        return plan_doc['cluster_name']

    def _maybe_delete_cluster(self, name):
        """Delete the per-link ECS cluster, but only when nothing is left in it.

        The cluster is created by the deployer, not by a stack, so deleting the
        stacks leaves it behind empty. It is named per link (`ecdl-<link>`), so
        it is this link's to remove -- unless something is still registered in
        it, in which case leaving it is the safe choice. An empty ECS cluster
        costs nothing.
        """
        try:
            described = self.clients.ecs.describe_clusters(clusters=[name])
        except Exception as exc:  # noqa: BLE001
            self._emit('cluster_check_skipped', cluster=name, error=str(exc))
            return False
        clusters = [c for c in described.get('clusters') or []
                    if c.get('status') not in (None, 'INACTIVE')]
        if not clusters:
            return False
        cluster = clusters[0]
        busy = ((cluster.get('activeServicesCount') or 0)
                + (cluster.get('runningTasksCount') or 0)
                + (cluster.get('pendingTasksCount') or 0))
        if busy:
            self._emit('cluster_kept', cluster=name,
                       active_services=cluster.get('activeServicesCount') or 0,
                       running_tasks=cluster.get('runningTasksCount') or 0)
            return False
        try:
            self.clients.ecs.delete_cluster(cluster=name)
        except Exception as exc:  # noqa: BLE001
            self._emit('cluster_delete_skipped', cluster=name, error=str(exc))
            return False
        self._emit('cluster_deleted', cluster=name)
        return True

    # ── CloudFormation ───────────────────────────────────────────────────────
    def _stack_state(self, name):
        """The stack's status, or None when it does not exist."""
        try:
            described = self.clients.cloudformation.describe_stacks(StackName=name)
        except Exception as exc:  # noqa: BLE001
            if 'does not exist' in str(exc):
                return None
            raise
        items = described.get('Stacks') or []
        return items[0].get('StackStatus') if items else None

    def _wait_for_delete(self, name):
        deadline = self.timeout_seconds
        while deadline > 0:
            state = self._stack_state(name)
            if state is None:
                return
            if state == 'DELETE_FAILED':
                raise DeployError(
                    'stack {} could not be deleted (DELETE_FAILED)'.format(name),
                    'Something in it is still in use or is protected. The '
                    'CloudFormation console names the resource that blocked the delete.')
            self._emit('stack_deleting', stack=name, state=state)
            self.sleep(self.poll_seconds)
            # `or 1` so a zero poll interval (tests) still advances the deadline
            # instead of looping forever.
            deadline -= (self.poll_seconds or 1)
        raise DeployError(
            'stack {} was still deleting after {}s'.format(
                name, self.timeout_seconds),
            'It may still be deleting; check the CloudFormation console.')

    def _emit(self, event, **fields):
        payload = {'event': event}
        payload.update(fields)
        self.on_progress(payload)
