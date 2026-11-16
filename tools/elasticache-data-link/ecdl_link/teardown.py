"""Delete a link's own stacks, and nothing else.

A link owns up to three CloudFormation stacks, all named `ecdl-<link>-*`:

    ecdl-<link>-cdc      Debezium + sink ECS services, IAM roles, log groups
    ecdl-<link>-loader   the one-shot loader (initial-load links only)
    ecdl-<link>-broker   the broker Valkey, its auth secret, and this link's
                         offsets / schema-history / streams

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

import time

from .deploy import Clients, DeployError  # noqa: F401  (Clients re-exported)
from .provision import (cluster_name, offset_key, pg_slot_name, schema_history_key,
                        stack_names)
from .signals import signal_table_name

# Reverse of the deploy order in provision.plan(): consumers first (cdc, then
# loader), then the broker those consumers read from.
_DELETE_ORDER = ('cdc', 'loader', 'broker')


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
        },
    }


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
    return '\n'.join(lines)


def _noop(event):
    pass


class Destroyer(object):
    """Deletes the stacks in a destroy_plan, in order, and waits for each."""

    def __init__(self, clients, on_progress=None, poll_seconds=10,
                 timeout_seconds=1800, sleep=time.sleep, delete_cluster=True):
        self.clients = clients
        self.on_progress = on_progress or _noop
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.sleep = sleep
        self.delete_cluster = delete_cluster

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

        # Resolve the ECS cluster to clean up BEFORE deleting the cdc stack,
        # because the authoritative name is that stack's ClusterName parameter
        # and it is gone once the stack is deleted.
        cluster = self._cluster_to_clean(plan_doc)

        deleted, skipped = [], []
        for step in plan_doc['stacks']:
            name = step['name']
            if name not in owned:
                raise DeployError(
                    'refusing to delete {!r}: it is not a stack owned by link '
                    '{!r} (owned: {})'.format(
                        name, link, ', '.join(sorted(owned))))
            state = self._stack_state(name)
            if state is None:
                self._emit('stack_absent', id=step['id'], stack=name)
                skipped.append(name)
                continue
            self._emit('stack_delete_started', id=step['id'], stack=name,
                       state=state)
            self.clients.cloudformation.delete_stack(StackName=name)
            self._wait_for_delete(name)
            self._emit('stack_deleted', id=step['id'], stack=name)
            deleted.append(name)

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
            'follow_up': plan_doc.get('follow_up', {}),
        }

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
