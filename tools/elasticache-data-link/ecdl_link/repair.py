"""What changed between two versions of a link, and what to do about it.

A link is edited: a table added, a filter tightened, a computed field added. The
question is never just "redeploy" -- it is what redeploying LEAVES BEHIND.

  * A table added to a running link captures only changes made afterwards.
    Every row that already existed is never sent, and the cache is permanently
    missing them. Needs an incremental snapshot.
  * A transform changed on a table means every row ALREADY in the cache has the
    old shape. New writes get the new shape, so the cache holds two versions of
    the same object indefinitely. Needs a re-snapshot of that table.
  * A filter widened admits rows that were previously excluded, and those rows are
    not in the cache at all. Needs a re-snapshot. Narrowed is worse: rows that
    should now be gone are still there, and no future event will remove them.
  * A table removed leaves its keys behind. Nothing will ever clean them up.

None of this is visible from the pipeline: it keeps processing changes and reports itself
healthy, because from its point of view nothing is wrong. So the diff has to name
the consequence, not just the field that changed.

This module decides; it does not act. It returns a plan of stack updates, signals
and cleanups, so the customer can see what will happen to their cache before it
does.
"""

from __future__ import annotations

import json

from .link import bare_table, canonical_json, normalize

# How urgent a change is, and therefore how prominently to report it.
#   none      nothing to do
#   deploy    a stack update is enough; no data consequence
#   backfill  a stack update AND an incremental snapshot, or rows will be missing
#   cleanup   a stack update AND keys to remove, or stale keys linger for ever
#   recreate  cannot be done in place
SEVERITY_ORDER = ('none', 'deploy', 'backfill', 'cleanup', 'recreate')


def _worst(*levels):
    return max(levels, key=SEVERITY_ORDER.index)


class RepairError(Exception):
    def __init__(self, message, hint=None):
        super(RepairError, self).__init__(message)
        self.hint = hint


def diff(current, desired):
    """Compare two link documents. Both are normalised first.

    Normalising means a reordered file or a re-quoted value is not reported as a
    change, which matters because a diff that reports false changes gets ignored.
    """
    current = normalize(current or {})
    desired = normalize(desired or {})

    changes = []

    # ── things that cannot change in place ────────────────────────────────
    for field, why in (
            ('name', 'the link name is in every stack name, the broker state keys '
                     'and the MySQL server id'),
            ):
        if current.get(field) != desired.get(field):
            changes.append({
                'kind': 'identity',
                'field': field,
                'from': current.get(field),
                'to': desired.get(field),
                'severity': 'recreate',
                'consequence': 'renaming would orphan the existing stacks, broker '
                               'state and cache keys rather than move them; {}'.format(why),
                'action': 'deploy the new name as a new link, then delete the old one',
            })

    cur_src = current.get('source') or {}
    des_src = desired.get('source') or {}
    for field in ('arn', 'database'):
        if cur_src.get(field) != des_src.get(field):
            changes.append({
                'kind': 'source',
                'field': 'source.{}'.format(field),
                'from': cur_src.get(field),
                'to': des_src.get(field),
                'severity': 'recreate',
                'consequence': 'the stored log position belongs to the old source, '
                               'so resuming against a different one would replay '
                               'or skip an unknowable amount',
                'action': 'deploy as a new link against the new source',
            })

    cur_tgt = current.get('target') or {}
    des_tgt = desired.get('target') or {}
    if cur_tgt.get('arn') != des_tgt.get('arn'):
        changes.append({
            'kind': 'target',
            'field': 'target.arn',
            'from': cur_tgt.get('arn'),
            'to': des_tgt.get('arn'),
            'severity': 'recreate',
            'consequence': 'the new cache is empty; pointing CDC at it would give '
                           'it only future changes, with no history',
            'action': 'deploy as a new link, which runs an initial load into the '
                      'new cache',
        })

    if current.get('mode') != desired.get('mode'):
        changes.append(_mode_change(current.get('mode'), desired.get('mode')))

    # ── tables ───────────────────────────────────────────────────────────
    cur_tables = list(current.get('tables') or [])
    des_tables = list(desired.get('tables') or [])
    cur_bare = {bare_table(t): t for t in cur_tables}
    des_bare = {bare_table(t): t for t in des_tables}

    for name in sorted(set(des_bare) - set(cur_bare)):
        changes.append({
            'kind': 'table_added',
            'field': 'tables',
            'table': des_bare[name],
            'to': des_bare[name],
            'severity': 'backfill',
            'consequence': 'a newly captured table yields only changes made after '
                           'the update, so every row that already exists would '
                           'never reach the cache',
            'action': 'update the stack, then request an incremental snapshot of '
                      'this table',
        })

    for name in sorted(set(cur_bare) - set(des_bare)):
        changes.append({
            'kind': 'table_removed',
            'field': 'tables',
            'table': cur_bare[name],
            'from': cur_bare[name],
            'severity': 'cleanup',
            'consequence': 'the keys this table wrote stay in the cache, and with '
                           'capture stopped nothing will ever delete or refresh '
                           'them -- they become permanently stale',
            'action': 'update the stack, then delete this table\'s keys from the '
                      'cache if they are no longer wanted',
        })

    # ── transforms, per table ────────────────────────────────────────────
    cur_tf = ((current.get('transforms') or {}).get('tables') or {})
    des_tf = ((desired.get('transforms') or {}).get('tables') or {})
    for name in sorted(set(cur_tf) | set(des_tf)):
        # A table being added is already reported; its transform is part of that.
        if name in des_bare and name not in cur_bare:
            continue
        if name in cur_bare and name not in des_bare:
            continue
        before = cur_tf.get(name)
        after = des_tf.get(name)
        if canonical_json(before or {}) == canonical_json(after or {}):
            continue
        changes.append(_transform_change(name, before, after))

    # ── everything else under advanced: a redeploy and nothing more ──────
    cur_adv = current.get('advanced') or {}
    des_adv = desired.get('advanced') or {}
    for section in sorted(set(cur_adv) | set(des_adv)):
        before = cur_adv.get(section)
        after = des_adv.get(section)
        if canonical_json(before or {}) == canonical_json(after or {}):
            continue
        changes.append({
            'kind': 'advanced',
            'field': 'advanced.{}'.format(section),
            'from': before,
            'to': after,
            'severity': 'deploy',
            'consequence': 'affects how the pipeline runs, not what is in the cache',
            'action': 'update the stack',
        })

    severity = _worst('none', *[c['severity'] for c in changes]) if changes else 'none'
    return {
        'changed': bool(changes),
        'severity': severity,
        'changes': changes,
        'current': current,
        'desired': desired,
    }


def _mode_change(before, after):
    """Mode transitions are not symmetric, so each is described on its own."""
    load_modes = ('initial_load', 'initial_load_and_cdc')
    cdc_modes = ('cdc', 'initial_load_and_cdc')
    gained_cdc = after in cdc_modes and before not in cdc_modes
    lost_cdc = before in cdc_modes and after not in cdc_modes

    if gained_cdc:
        return {
            'kind': 'mode', 'field': 'mode', 'from': before, 'to': after,
            'severity': 'backfill',
            'consequence': 'capture starts from the current log position, so any '
                           'change made between the last load and now is missing '
                           'from the cache',
            'action': 'deploy the capture components, then request an incremental '
                      'snapshot of every table to close the gap',
        }
    if lost_cdc:
        return {
            'kind': 'mode', 'field': 'mode', 'from': before, 'to': after,
            'severity': 'cleanup',
            'consequence': 'with capture removed the cache stops tracking the '
                           'source and goes stale from that moment, silently',
            'action': 'remove the capture components; be sure a stale cache is '
                      'acceptable, or keep cdc',
        }
    # initial_load_and_cdc -> cdc: the load will not run again.
    return {
        'kind': 'mode', 'field': 'mode', 'from': before, 'to': after,
        'severity': 'deploy',
        'consequence': 'changes which components exist, not what is in the cache',
        'action': 'update the deployment',
    }


def _transform_change(table, before, after):
    """Why a transform change needs a re-snapshot, for this specific change.

    The general reason is that a transform decides the SHAPE and the MEMBERSHIP of
    what is cached, and it is applied at write time. Rows written under the old
    rules keep the old shape for ever, because nothing rewrites a cache entry until
    that row changes again.
    """
    before = before or {}
    after = after or {}
    reasons = []

    bf = before.get('filter')
    af = after.get('filter')
    if canonical_json(bf or {}) != canonical_json(af or {}):
        if bf and not af:
            reasons.append('the filter was removed, so rows it excluded should now '
                           'be cached and none of them are')
        elif af and not bf:
            reasons.append('a filter was added, so rows that no longer qualify are '
                           'still in the cache and nothing will remove them')
        else:
            reasons.append('the filter changed, so which rows belong in the cache '
                           'changed in both directions')

    if canonical_json(before.get('columns') or []) != canonical_json(after.get('columns') or []):
        reasons.append('the selected columns changed, so cached rows have fields '
                       'that should be gone or are missing fields that should be there')
    if canonical_json(before.get('rename') or {}) != canonical_json(after.get('rename') or {}):
        reasons.append('a field was renamed, so cached rows still carry the old '
                       'field name alongside the new one')
    if canonical_json(before.get('computed') or []) != canonical_json(after.get('computed') or []):
        reasons.append('a computed field changed, so cached rows hold values '
                       'computed by the old expression')
    if canonical_json(before.get('denormalize') or []) != canonical_json(after.get('denormalize') or []):
        reasons.append('denormalization changed, so embedded parent fields and the '
                       'reverse indexes are inconsistent with the new rules')

    if not reasons:
        reasons.append('the transform changed')

    return {
        'kind': 'transform_changed',
        'field': 'transforms.tables.{}'.format(table),
        'table': table,
        'from': before or None,
        'to': after or None,
        'severity': 'backfill',
        'consequence': '; '.join(reasons),
        'action': 'update the stack, then request an incremental snapshot of this '
                  'table so every row is rewritten under the new rules',
    }


# ── turning a diff into steps ─────────────────────────────────────────────

def repair_plan(diff_doc, resolved_desired):
    """The ordered work that applies a diff and leaves the cache correct.

    `resolved_desired` is resolve_link() output for the DESIRED document, needed
    for the qualified table names a snapshot signal has to use and for the
    signalling collection.
    """
    if not diff_doc['changed']:
        return {'severity': 'none', 'steps': [], 'notes': ['nothing changed'],
                'blocked': False}

    blocking = [c for c in diff_doc['changes'] if c['severity'] == 'recreate']
    if blocking:
        return {
            'severity': 'recreate',
            'steps': [],
            'blocked': True,
            'notes': ['this change cannot be applied to the running link: {}'
                      .format('; '.join(c['field'] for c in blocking))],
            'changes': blocking,
        }

    steps = []
    notes = []

    # 1. The stack update always comes first. A snapshot requested before the
    #    connector knows about the table would be rejected as an unknown
    #    collection, and a snapshot requested before the new transform is live
    #    would rewrite every row using the OLD rules -- doing the work and leaving
    #    it just as wrong.
    steps.append({
        'kind': 'deploy',
        'id': 'update_stacks',
        'purpose': 'apply the new table list, transforms and settings',
    })

    # 2. Tables needing a backfill: added tables, and tables whose transform
    #    changed. Both are the same operation for the same reason -- every row has
    #    to be re-read and rewritten under the current rules.
    qualified = {bare_table(t['input']): t['qualified']
                 for t in resolved_desired.get('tables') or []}
    backfill = []
    for change in diff_doc['changes']:
        if change['kind'] in ('table_added', 'transform_changed'):
            name = bare_table(change.get('table') or '')
            if name in qualified:
                backfill.append(qualified[name])
            else:
                notes.append(
                    'cannot request a snapshot of {}: it is not in the resolved '
                    'table list'.format(change.get('table')))
        elif change['kind'] == 'mode' and change['severity'] == 'backfill':
            backfill.extend(qualified.values())

    # De-duplicated, order preserved: one signal naming several tables is one
    # snapshot, and asking twice for the same table wastes a full re-read.
    seen = set()
    unique = [t for t in backfill if not (t in seen or seen.add(t))]
    if unique:
        steps.append({
            'kind': 'signal',
            'id': 'backfill',
            'signal': 'execute-snapshot',
            'collections': unique,
            'purpose': 'incrementally re-read these tables so every row is '
                       'present and in the current shape',
            'signal_collection': (resolved_desired.get('debezium') or {})
                                 .get('signal_collection'),
        })

    # 3. Removed tables leave keys nothing will ever touch again. Deliberately a
    #    separate step and NOT done automatically: deleting cache keys is
    #    destructive and only the customer knows whether something still reads them.
    removed = [c['table'] for c in diff_doc['changes']
               if c['kind'] == 'table_removed']
    if removed:
        prefixes = ['{}:*'.format(bare_table(t)) for t in removed]
        steps.append({
            'kind': 'cleanup',
            'id': 'stale_keys',
            'tables': removed,
            'key_patterns': prefixes,
            'confirm': True,
            'purpose': 'these keys are no longer maintained by any capture and '
                       'will never be refreshed or deleted on their own',
        })
        notes.append('key deletion is not performed automatically: it is '
                     'destructive, and only you know whether anything still '
                     'reads {}'.format(', '.join(prefixes)))

    if not (resolved_desired.get('debezium') or {}).get('signal_collection') and unique:
        notes.append(
            'no signalling collection is configured, so the backfill cannot be '
            'requested and the affected rows would stay wrong. Create the '
            'signalling table and redeploy first.')

    return {
        'severity': diff_doc['severity'],
        'steps': steps,
        'notes': notes,
        'blocked': False,
        'changes': diff_doc['changes'],
    }


def describe_diff(diff_doc):
    if not diff_doc['changed']:
        return 'no changes'
    lines = ['{} change(s), worst is "{}"'.format(
        len(diff_doc['changes']), diff_doc['severity'])]
    for change in diff_doc['changes']:
        lines.append('  [{}] {}'.format(change['severity'], change['field']))
        if change.get('table'):
            lines.append('      table: {}'.format(change['table']))
        lines.append('      why:  {}'.format(change['consequence']))
        lines.append('      do:   {}'.format(change['action']))
    return '\n'.join(lines)


def describe_repair(plan_doc):
    if plan_doc.get('blocked'):
        lines = ['BLOCKED: this change cannot be applied in place']
        for note in plan_doc['notes']:
            lines.append('  {}'.format(note))
        for change in plan_doc.get('changes') or []:
            lines.append('  {}: {}'.format(change['field'], change['action']))
        return '\n'.join(lines)
    if not plan_doc['steps']:
        return 'nothing to do'
    lines = ['{} step(s), severity "{}"'.format(
        len(plan_doc['steps']), plan_doc['severity'])]
    for i, step in enumerate(plan_doc['steps'], 1):
        head = '  {}. {}'.format(i, step['kind'])
        if step.get('collections'):
            head += ' ({})'.format(', '.join(step['collections']))
        if step.get('key_patterns'):
            head += ' ({})'.format(', '.join(step['key_patterns']))
        if step.get('confirm'):
            head += '  [needs confirmation]'
        lines.append(head)
        lines.append('       {}'.format(step['purpose']))
    for note in plan_doc['notes']:
        lines.append('  ! {}'.format(note))
    return '\n'.join(lines)


def signal_for(step):
    """Build the actual signal row for a `signal` repair step."""
    from .signals import snapshot_signal
    if step.get('signal') != 'execute-snapshot':
        raise RepairError('unsupported signal step {!r}'.format(step.get('signal')))
    return snapshot_signal(step['collections'])


def apply_repair(plan_doc, resolved_desired, deployer, prefix='ecdl',
                 assign_public_ip=None):
    """Carry out the parts of a repair plan that the control plane may do itself.

    Which is the `deploy` step, and only that. The other two steps are left to the
    operator on purpose, and the reasons are different for each:

      * `signal` writes a row to the CUSTOMER'S DATABASE. The control plane never
        holds a database credential -- resolution confirms the secret exists with
        DescribeSecret and never reads its value -- so it has nothing to connect
        with, and giving it one only to save the customer a copy-paste would add more risk than
        it removes. The
        exact statement is returned instead.
      * `cleanup` deletes cache keys, so it requires `confirm`; step 3 of
        repair_plan gives the reason.

    Returns what was done and what is still outstanding, so a caller can show the
    difference between "applied" and "finished".
    """
    engine = (resolved_desired.get('source') or {}).get('engine')

    if plan_doc.get('blocked'):
        raise RepairError(
            'this change cannot be applied to the running link',
            '; '.join(plan_doc.get('notes') or []) or None)

    deploy_steps = [s for s in plan_doc['steps'] if s['kind'] == 'deploy']
    if not deploy_steps:
        return {'deployed': False, 'result': None,
                'outstanding': _outstanding(plan_doc, engine),
                'notes': ['no stack change was needed']}

    # for_update: update the stacks, leave capture running, do not re-load.
    from .provision import plan as build_plan
    from .discover import control_plane_security_group
    update_plan = build_plan(resolved_desired, prefix=prefix,
                             assign_public_ip=assign_public_ip,
                             for_update=True,
                             # Carried on an update too, or repairing a link would
                             # silently drop the broker's monitor ingress.
                             monitor_security_group_id=control_plane_security_group())
    result = deployer.run(update_plan)
    return {
        'deployed': True,
        'result': result,
        'plan': update_plan,
        'outstanding': _outstanding(plan_doc, engine),
        'notes': list(plan_doc.get('notes') or []),
    }


def _outstanding(plan_doc, engine=None):
    """The steps a human still has to perform, each with what to run."""
    items = []
    for step in plan_doc['steps']:
        if step['kind'] == 'signal':
            signal = signal_for(step)
            collection = step.get('signal_collection')
            # The signal dict alone is not actionable -- an operator needs the
            # statement to run. Parameterised, because `data` is JSON full of
            # quotes and braces and a table name out of a link file is not
            # something to concatenate into SQL.
            statement, values = (None, None)
            if collection:
                from .signals import insert_statement
                statement, values = insert_statement(
                    engine or 'mysql', collection, signal)
            items.append({
                'kind': 'signal',
                'id': step['id'],
                'why': step['purpose'],
                'collections': step['collections'],
                'signal_collection': collection,
                'signal': signal,
                'statement': statement,
                'values': list(values) if values else None,
                'blocked_reason': None if collection else (
                    'no signalling collection is configured, so this backfill '
                    'cannot be requested at all -- create the signalling table '
                    'and redeploy first'),
                'reason_not_automatic':
                    'this writes to your database, and the control plane holds '
                    'no database credential by design',
            })
        elif step['kind'] == 'cleanup':
            items.append({
                'kind': 'cleanup',
                'id': step['id'],
                'why': step['purpose'],
                'tables': step['tables'],
                'key_patterns': step['key_patterns'],
                'reason_not_automatic':
                    'deleting cache keys is destructive and only you know '
                    'whether anything still reads them',
            })
    return items


def _unused_json_guard():
    # json is imported for callers that serialise a diff; referenced so linters do
    # not strip the import.
    return json
