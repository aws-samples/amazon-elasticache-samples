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
does. The update itself goes through the one change path (change.py, /deploy,
`ecdl apply`), which compares it with the deployed stacks first.
"""

from __future__ import annotations

import json

from .link import bare_table, canonical_json, normalize

# How urgent a change is, and therefore how prominently to report it.
#   none      nothing to do
#   deploy    a stack update is enough; no data consequence
#   backfill  a stack update AND an incremental snapshot, or rows will be missing
#   cleanup   a stack update AND keys to remove, or stale keys linger indefinitely
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
    # kind, engine, host and port identify an external source the way arn
    # identifies an AWS one (Jira story F126: they were not compared at all).
    for field in ('arn', 'database', 'kind', 'engine', 'host', 'port'):
        if _source_field(cur_src, field) != _source_field(des_src, field):
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
    # A different credential for the same source or target (Jira story F126).
    for side, field, before, after in (
            ('source', 'secret_arn', cur_src.get('secret_arn'), des_src.get('secret_arn')),
            ('source', 'username', cur_src.get('username'), des_src.get('username')),
            ('target', 'secret_arn', cur_tgt.get('secret_arn'), des_tgt.get('secret_arn'))):
        if before != after:
            changes.append({
                'kind': 'credential',
                'field': '{}.{}'.format(side, field),
                'from': before,
                'to': after,
                'severity': 'deploy',
                'consequence': 'the pipeline tasks restart with the new credential. '
                               'It must be readable by them (tagged ecdl:link, or in '
                               'the control plane\'s PipelineSecretArns) and, for the '
                               'source, allowed to replicate, or capture stops at the '
                               'restart',
                'action': 'check the credential, then update the stack',
            })

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

    # The same table name read from another schema or database (Jira story F126). A
    # source.schema change moves every unqualified table; an explicit
    # qualifier can move one. The keys stay the same (<table>:<pk>), so the
    # cache keeps the old table's rows under the new table's keys: a remove
    # of the old and an add of the new, in that order.
    cur_schema, des_schema = _schema_of(cur_src), _schema_of(des_src)
    for name in sorted(set(cur_bare) & set(des_bare)):
        before = _qualified(cur_bare[name], cur_schema)
        after = _qualified(des_bare[name], des_schema)
        if before is None and after is None:
            continue
        if before is None or after is None:
            # One side names no schema: moved only if source.schema changed.
            if cur_schema == des_schema:
                continue
        elif before == after:
            continue
        shown_from = before or '<default schema>.{}'.format(name)
        shown_to = after or '<default schema>.{}'.format(name)
        mysql_note = ('' if cur_schema == des_schema or des_src.get('engine') else
                      " (MySQL does not use source.schema; on a MySQL source this "
                      'moves nothing)')
        changes.append({
            'kind': 'table_moved',
            'field': 'tables',
            'table': des_bare[name],
            'from': shown_from,
            'to': shown_to,
            'severity': 'cleanup',
            'consequence': ('{} is now read from {} instead of {}. Its keys do not '
                            'change, so the cache keeps the rows of {} under them: '
                            'rows only that table has are never deleted, and the rows '
                            '{} already has are never sent{}'.format(
                                name, shown_to, shown_from, shown_from, shown_to,
                                mysql_note)),
            'action': 'update the stack, delete this table\'s keys, then request an '
                      'incremental snapshot of it',
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

    # ── the key prefix decides every cache key ───────────────────────────
    cur_adv = current.get('advanced') or {}
    des_adv = desired.get('advanced') or {}
    old_prefix = _prefix(cur_adv.get('key_prefix'))
    new_prefix = _prefix(des_adv.get('key_prefix'))
    if old_prefix != new_prefix:
        # It used to be reported as "deploy" like any other setting. After the
        # update the sink writes new keys; rows that do not change exist only
        # under the old prefix and are never maintained again, so readers on
        # the new prefix miss most rows and readers on the old one see frozen
        # data.
        changes.append({
            'kind': 'key_prefix',
            'field': 'advanced.key_prefix',
            'from': old_prefix or None,
            'to': new_prefix or None,
            'severity': 'cleanup',
            'consequence': 'every cache key moves from {!r} to {!r}: rows are '
                           'written under the new prefix only when they change, '
                           'and the old keys are never maintained again'.format(
                               old_prefix + '<table>:', new_prefix + '<table>:'),
            'action': 'update the stack, snapshot every table so all rows exist '
                      'under the new prefix, then delete the keys under the old '
                      'prefix',
        })

    # ── everything else under advanced: a redeploy and nothing more ──────
    for section in sorted(set(cur_adv) | set(des_adv)):
        if section == 'key_prefix':
            continue
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


def _source_field(src, field):
    """A source field as compared by diff: kind defaults to aws, and an
    external source's port to its engine's standard one."""
    if field == 'kind':
        return src.get('kind') or 'aws'
    if field == 'port' and (src.get('kind') or 'aws') == 'external':
        if src.get('port'):
            return int(src['port'])
        try:
            from .resolve import ENGINE_FACTS
            return ENGINE_FACTS[src.get('engine')]['port']
        except (ImportError, KeyError):
            return None
    return src.get(field)


def _schema_of(src):
    """source.schema as diff compares it, lowercased; None when unset or when
    the source is a MySQL one, which does not use it."""
    if src.get('engine') == 'mysql':
        return None
    schema = str(src.get('schema') or '').strip().lower()
    return schema or None


def _qualified(entry, schema):
    """'<schema>.<table>' (lowercased) a tables[] entry reads, or None when
    neither the entry nor source.schema names one."""
    parts = str(entry).lower().split('.')
    if len(parts) > 1:
        return '.'.join(parts[-2:])
    return '{}.{}'.format(schema, parts[0]) if schema else None


def _prefix(raw):
    """advanced.key_prefix as '<prefix>:' or '' (the sink's and loader's form)."""
    raw = str(raw or '').strip().rstrip(':')
    return raw + ':' if raw else ''


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
        # Refused in place. Nothing removed the capture stacks, so an
        # "initial_load" link kept capturing, and its load ran UNPAUSED under
        # live CDC -- the stale-overwrite race the pause exists for.
        return {
            'kind': 'mode', 'field': 'mode', 'from': before, 'to': after,
            'severity': 'recreate',
            'consequence': 'removing capture from a running link cannot be done '
                           'in place: the capture stacks would keep running, and '
                           'a load would run under live capture and overwrite '
                           'newer values with older ones',
            'action': 'destroy the link (ecdl destroy), then deploy it again in '
                      'the new mode; or keep a mode with cdc',
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
    rules keep the old shape indefinitely, because nothing rewrites a cache entry until
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


def repair_plan(diff_doc, resolved_desired, pg_publication=None):
    """The ordered work that applies a diff and leaves the cache correct.

    `resolved_desired` is resolve_link() output for the DESIRED document, needed
    for the qualified table names a snapshot signal has to use and for the
    signalling collection.

    `pg_publication` is the publication a PostgreSQL link's Debezium reads
    (known from the deployed stack). An added table must be put in it, or
    PostgreSQL never sends its changes and the snapshot is never requested.
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

    # 0. PostgreSQL: an added table must be in the link's publication. Done
    #    first, and in any case before the snapshot: PostgreSQL sends Debezium
    #    nothing for a table outside it, so the table's changes would never be
    #    captured.
    # A moved table is new to the publication too (Jira story F126).
    added = [c for c in diff_doc['changes'] if c['kind'] in ('table_added', 'table_moved')]
    if added and (resolved_desired.get('source') or {}).get('engine') == 'postgresql':
        by_name = {bare_table(t['input']): t['qualified']
                   for t in resolved_desired.get('tables') or []}
        new_tables = [by_name[bare_table(c.get('table') or '')] for c in added
                      if bare_table(c.get('table') or '') in by_name]
        if new_tables and pg_publication:
            from .signals import pg_publication_sql
            steps.append({
                'kind': 'source_sql',
                'id': 'publication',
                'tables': new_tables,
                'statement': pg_publication_sql(pg_publication, new_tables),
                'purpose': 'add the new table(s) to the publication {} before the '
                           'snapshot: PostgreSQL sends Debezium nothing for a table '
                           'outside it, so its changes would never be '
                           'captured'.format(pg_publication),
            })
        elif new_tables:
            notes.append(
                'PostgreSQL: add {} to the publication this link reads (ALTER '
                'PUBLICATION <publication> ADD TABLE ...) before the snapshot, or '
                'its changes are never captured. `ecdl signal ddl` prints the '
                'statement.'.format(', '.join(new_tables)))

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
    old_prefix = _prefix(((diff_doc.get('current') or {}).get('advanced') or {})
                         .get('key_prefix'))

    # 1b. A table read from another schema keeps its keys, which hold the old
    #     table's rows: they are deleted BEFORE the snapshot rewrites the
    #     table, or the snapshot's rows would be deleted with them (Jira story F126).
    moved_tables = [bare_table(c.get('table') or '') for c in diff_doc['changes']
                    if c['kind'] == 'table_moved']
    if moved_tables:
        steps.append({
            'kind': 'cleanup',
            'id': 'moved_table_keys',
            'tables': moved_tables,
            'key_patterns': ['{}{}:*'.format(old_prefix, t) for t in moved_tables],
            'confirm': True,
            'purpose': 'these keys hold rows of the table the link read before; delete '
                       'them after the stack update and before the snapshot below, '
                       'which writes the rows of the table it reads now',
        })

    backfill = []
    for change in diff_doc['changes']:
        if change['kind'] in ('table_added', 'transform_changed', 'table_moved'):
            name = bare_table(change.get('table') or '')
            if name in qualified:
                backfill.append(qualified[name])
            else:
                notes.append(
                    'cannot request a snapshot of {}: it is not in the resolved '
                    'table list'.format(change.get('table')))
        elif change['kind'] == 'mode' and change['severity'] == 'backfill':
            backfill.extend(qualified.values())
        elif change['kind'] == 'key_prefix':
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
    # The patterns carry the link's key prefix. They used to be '<table>:*',
    # which on a prefixed link beside an unprefixed one names the OTHER link's
    # keys.
    removed = [c['table'] for c in diff_doc['changes']
               if c['kind'] == 'table_removed']
    moved = next((c for c in diff_doc['changes'] if c['kind'] == 'key_prefix'), None)
    if moved:
        kept = [bare_table(t['input']) for t in resolved_desired.get('tables') or []]
        steps.append({
            'kind': 'cleanup',
            'id': 'old_prefix_keys',
            'tables': kept,
            'key_patterns': ['{}{}:*'.format(old_prefix, t) for t in kept],
            'confirm': True,
            'purpose': 'the keys under the old prefix are no longer maintained; '
                       'delete them once the snapshot has finished and nothing '
                       'reads them',
        })
    if removed:
        prefixes = ['{}{}:*'.format(old_prefix, bare_table(t)) for t in removed]
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
        if step.get('statement'):
            lines.append('       run on the source: {}'.format(step['statement']))
    for note in plan_doc['notes']:
        lines.append('  ! {}'.format(note))
    return '\n'.join(lines)


def signal_for(step):
    """Build the actual signal row for a `signal` repair step."""
    from .signals import snapshot_signal
    if step.get('signal') != 'execute-snapshot':
        raise RepairError('unsupported signal step {!r}'.format(step.get('signal')))
    return snapshot_signal(step['collections'])


def outstanding_steps(plan_doc, engine=None):
    """Public name of _outstanding: the steps a person still has to perform."""
    return _outstanding(plan_doc, engine)


def _outstanding(plan_doc, engine=None):
    """The steps a human still has to perform, each with what to run."""
    items = []
    for step in plan_doc['steps']:
        if step['kind'] == 'source_sql':
            items.append({
                'kind': 'source_sql',
                'id': step['id'],
                'why': step['purpose'],
                'tables': step.get('tables'),
                'statement': step['statement'],
                'reason_not_automatic':
                    'this changes your database, and the control plane holds '
                    'no database credential by design',
            })
        elif step['kind'] == 'signal':
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
