"""Source-schema preflight for a link config.

Nothing between "type a table name" and "deployed" ever asked the source whether
the configured tables -- and the columns the transforms reference -- actually
exist. `validate` is schema-only, `resolve` reads AWS metadata, and Debezium starts
capturing a table that is not there without reporting an error, so the
stack reports success while the cache stays empty. This module asks the source, and
turns that silent gap into a rejected config with a precise message.

Two layers, split so the comparison is testable without a database:

  * ``check_catalogue(document, catalogue)`` -- PURE. Given a link document and the
    result of ``tables.list_tables()``, returns a list of ``Finding``.
  * ``preflight(document, ...)`` -- fetches the catalogue via ``list_tables()`` and
    runs the check. This is the one that touches the network.

A ``Finding`` carries a level:

  * ``block`` -- a configured table, or a column a transform references, is
    definitively ABSENT from a source we successfully read. A deploy should stop.
  * ``warn``  -- the source could NOT be read (different VPC, no driver, no
    credential). The control plane usually cannot reach a customer's database, so
    this is an ordinary situation, not a defect: it is surfaced and the deploy
    proceeds. A preflight that blocked on unreachability would block every
    control plane that is doing the normal thing.

Only column references that can be resolved WITHOUT executing SQL are checked:
``columns``, ``filter.field``, ``rename`` sources, ``denormalize.join_field``
(on the child) and ``denormalize.fields`` (on the parent). A computed field's
expression is deliberately NOT parsed for column names -- guessing wrong would
block a valid config, which is worse than the gap this closes.
"""

import difflib
import fnmatch
import os
import re
from collections import namedtuple

from .link import bare_table, normalize

# A single preflight result. `level` is BLOCK or WARN; `path` mirrors the config
# path style used by validate (e.g. "transforms.tables.orders.columns") so the UI
# can annotate the same field.
Finding = namedtuple('Finding', ('level', 'path', 'message'))

BLOCK = 'block'
WARN = 'warn'


def _fold(name):
    """Case- and whitespace-folded key for matching a name.

    RDS MySQL defaults to lower_case_table_names=1 and PostgreSQL folds unquoted
    identifiers to lower case, so a case difference is almost never a real
    "missing" and blocking on one would be a false alarm. Matching is therefore
    case-insensitive; a genuine case-sensitivity problem is rare enough to accept.
    """
    return str(name).strip().lower()


def _closest(name, candidates):
    """A near-miss suggestion, or None. Turns "not found" into "did you mean"."""
    matches = difflib.get_close_matches(_fold(name),
                                        [_fold(c) for c in candidates], n=1, cutoff=0.6)
    if not matches:
        return None
    fold_to_actual = {_fold(c): c for c in candidates}
    return fold_to_actual.get(matches[0])


def _index(catalogue):
    """{folded bare table: {'name': actual, 'cols': {folded: actual}, 'entry'}}."""
    present = {}
    for entry in (catalogue or {}).get('tables') or []:
        name = entry.get('table') if isinstance(entry, dict) else None
        if not isinstance(name, str):
            continue
        cols = {}
        for col in entry.get('columns') or []:
            cname = col.get('name') if isinstance(col, dict) else None
            if isinstance(cname, str):
                cols[_fold(cname)] = cname
        present[_fold(name)] = {'name': name, 'cols': cols, 'entry': entry}
    return present


def _foreign_namespace(table, catalogue):
    """The schema (or MySQL database) a configured table names, when it is not
    the one the catalogue was read from; None otherwise (Jira story F131).

    A catalogue that does not say which namespace it holds matches every
    table by bare name, as before."""
    parts = str(table).split('.')
    own = _fold((catalogue or {}).get('namespace') or '')
    if len(parts) < 2 or not own or _fold(parts[-2]) == own:
        return None
    return parts[-2]


def _catalogue_for(table, catalogue):
    """(catalogue the table's namespace was read into, or None, namespace)."""
    ns = _foreign_namespace(table, catalogue)
    if ns is None:
        return catalogue, None
    other = ((catalogue or {}).get('other_namespaces') or {}).get(_fold(ns))
    return (other if other and other.get('listed') else None), ns


def check_catalogue(document, catalogue):
    """Compare a link document to a ``list_tables()`` catalogue. Pure.

    Returns a list of ``Finding``. An empty list means everything the document
    references exists on the source (or there was nothing to check). When the
    catalogue could not be read it returns exactly one WARN and never a BLOCK.
    """
    doc = normalize(document) if isinstance(document, dict) else {}
    tables = [t for t in (doc.get('tables') or []) if isinstance(t, str)]

    if not catalogue or not catalogue.get('listed'):
        info = catalogue or {}
        reason = info.get('error') or 'the source could not be read from the control plane'
        hint = info.get('hint') or ('Deployment is not blocked. Confirm the tables '
                                     'exist on the source yourself.')
        return [Finding(WARN, 'tables',
                        'could not verify tables against the source: {} -- {}'.format(reason, hint))]

    # bare table name (folded) -> {'name': actual, 'cols': {folded: actual}}
    present = _index(catalogue)
    findings = []

    # 1. Every configured table must exist on the source, in the schema (or
    #    MySQL database) it names (Jira story F131). A table in another one used to be
    #    looked up by its bare name in the default one: a valid link was
    #    blocked, or a same-named table there passed for a missing one. That
    #    namespace is checked when preflight() could read it, and is a WARN
    #    when it could not.
    missing, unverified = set(), set()
    found = {}   # folded bare name -> the index entry of the table itself
    kind = catalogue.get('namespace_kind') or 'schema'
    for entry in tables:
        key = _fold(bare_table(entry))
        other, ns = _catalogue_for(entry, catalogue)
        if other is None:
            unverified.add(key)
            why = ((((catalogue.get('other_namespaces') or {}).get(_fold(ns)) or {})
                    .get('error')) or 'only {} {!r} was read'.format(
                        kind, catalogue.get('namespace')))
            findings.append(Finding(WARN, 'tables', (
                "could not verify table '{}': it is in {} '{}', which the preflight "
                'could not read ({}). Confirm it exists yourself.'.format(
                    entry, kind, ns, why))))
            continue
        index = present if other is catalogue else _index(other)
        if key not in index:
            missing.add(key)
            msg = "table '{}' does not exist on the source".format(entry)
            hit = _closest(bare_table(entry), [e['name'] for e in index.values()])
            if hit:
                msg += " (did you mean '{}'?)".format(hit)
            findings.append(Finding(BLOCK, 'tables', msg))
        else:
            found[key] = index[key]

    # 2. Columns each transform references must exist, but only for tables that
    #    do exist -- a missing table is already reported, and adding column errors
    #    for it would make the actual problem harder to find.
    transforms = doc.get('transforms')
    t_tables = transforms.get('tables') if isinstance(transforms, dict) else None
    if isinstance(t_tables, dict):
        for tname in sorted(t_tables):
            spec = t_tables[tname]
            if not isinstance(spec, dict):
                continue
            key = _fold(bare_table(tname))
            hit = found.get(key) or (None if key in unverified else present.get(key))
            if key in missing or hit is None:
                continue
            cols = hit['cols']
            actual = hit['name']
            path = 'transforms.tables.{}'.format(tname)

            computed_names = set()
            computed = spec.get('computed')
            if isinstance(computed, list):
                for item in computed:
                    if isinstance(item, dict) and isinstance(item.get('name'), str):
                        computed_names.add(_fold(item['name']))

            def known(col):
                # A column the sink can see: a real source column or one this
                # transform computes.
                return _fold(col) in cols or _fold(col) in computed_names

            selected = spec.get('columns')
            if isinstance(selected, list):
                for col in selected:
                    if isinstance(col, str) and _fold(col) not in cols:
                        msg = "selects column '{}', which {} does not have".format(col, actual)
                        hit = _closest(col, list(cols.values()))
                        if hit:
                            msg += " (did you mean '{}'?)".format(hit)
                        findings.append(Finding(BLOCK, path + '.columns', msg))

            filt = spec.get('filter')
            if isinstance(filt, dict) and isinstance(filt.get('field'), str):
                if not known(filt['field']):
                    findings.append(Finding(BLOCK, path + '.filter.field',
                        "filters on column '{}', which {} does not have".format(filt['field'], actual)))

            rename = spec.get('rename')
            if isinstance(rename, dict):
                for src in sorted(rename):
                    if not known(src):
                        findings.append(Finding(BLOCK, '{}.rename.{}'.format(path, src),
                            "renames column '{}', which {} does not have".format(src, actual)))

            denorm = spec.get('denormalize')
            if isinstance(denorm, list):
                for i, rule in enumerate(denorm):
                    if not isinstance(rule, dict):
                        continue
                    jf = rule.get('join_field')
                    if isinstance(jf, str) and _fold(jf) not in cols:
                        findings.append(Finding(BLOCK, '{}.denormalize[{}].join_field'.format(path, i),
                            "joins on column '{}', which {} does not have".format(jf, actual)))
                    parent = rule.get('parent_table')
                    fields = rule.get('fields')
                    if isinstance(parent, str) and isinstance(fields, list):
                        pkey = _fold(bare_table(parent))
                        # A missing parent is already blocked; an unverified
                        # one cannot be checked.
                        phit = found.get(pkey) or (None if pkey in unverified
                                                   else present.get(pkey))
                        if phit is not None:
                            pcols = phit['cols']
                            for field in fields:
                                if isinstance(field, str) and _fold(field) not in pcols:
                                    findings.append(Finding(BLOCK, '{}.denormalize[{}].fields'.format(path, i),
                                        "embeds column '{}' from '{}', which that table does not have".format(field, parent)))

    return findings


def without_path(summary, path):
    """A summarize() result with the findings at `path` removed, recounted."""
    summary = dict(summary or {})
    kept = [f for f in summary.get('findings') or [] if f.get('path') != path]
    dropped = [f for f in summary.get('findings') or [] if f.get('path') == path]
    summary['findings'] = kept
    summary['blocks'] = max(0, summary.get('blocks', 0) - sum(
        1 for f in dropped if f.get('level') == BLOCK))
    summary['warnings'] = max(0, summary.get('warnings', 0) - sum(
        1 for f in dropped if f.get('level') != BLOCK))
    return summary


def summarize(findings, checked):
    """Turn a list of Findings into the JSON the API returns."""
    return {
        'checked': bool(checked),
        'blocks': sum(1 for f in findings if f.level == BLOCK),
        'warnings': sum(1 for f in findings if f.level == WARN),
        'findings': [f._asdict() for f in findings],
    }


def preflight(document, region=None, clients=None, driver=None, publication=None):
    """Fetch the source catalogue and check the document against it.

    Returns ``summarize(...)``: ``{checked, blocks, warnings, findings}``.
    ``checked`` is True only when the source catalogue was actually read; every
    other outcome (external source, no ARN/secret, unreachable, no driver) comes
    back as a single WARN with ``checked`` False, because none of those is a
    reason to block a deploy.

    ``publication`` is the PostgreSQL publication the link's Debezium reads,
    when the caller knows it (``provision.deployed_pg_publication``). Unknown
    (None), the link's own name is checked and a gap is a WARN, not a BLOCK.
    """
    doc = normalize(document) if isinstance(document, dict) else {}
    source = doc.get('source') or {}
    source_arn = source.get('arn')
    secret_arn = source.get('secret_arn')

    if not source_arn or not secret_arn:
        return summarize([Finding(WARN, 'source',
            'could not verify tables against the source: it is not an AWS source '
            'with a secret_arn, so the control plane cannot connect to list its '
            'tables. Confirm the tables exist yourself.')], checked=False)

    # tables.py pulls in the DB drivers; import locally so a control plane without
    # them still loads this module.
    from .tables import list_tables

    try:
        catalogue = list_tables(source_arn=source_arn, secret_arn=secret_arn,
                                database=source.get('database'),
                                schema=source.get('schema'),
                                region=region, clients=clients, driver=driver)
    except Exception as exc:  # noqa: BLE001
        return summarize([Finding(WARN, 'source',
            'could not verify tables against the source: {}'.format(exc))], checked=False)

    _read_other_namespaces(doc, catalogue, source_arn, secret_arn, region, clients,
                           driver, list_tables)
    findings = check_catalogue(doc, catalogue)
    findings.extend(prerequisite_warnings(catalogue))
    findings.extend(pause_findings(doc, catalogue))
    findings.extend(replica_identity_findings(doc, catalogue))
    findings.extend(keyless_findings(doc, catalogue))
    findings.extend(publication_findings(doc, catalogue, publication))
    return summarize(findings, checked=bool(catalogue.get('listed')))


def _read_other_namespaces(doc, catalogue, source_arn, secret_arn, region, clients,
                           driver, list_tables):
    """Read the catalogue of every other schema (or MySQL database) the link's
    tables name, into catalogue['other_namespaces'] (Jira story F131). One that cannot be
    read is kept with its error, which check_catalogue reports as a WARN."""
    if not catalogue.get('listed'):
        return
    source = doc.get('source') or {}
    wanted = {}
    for table in doc.get('tables') or []:
        ns = _foreign_namespace(table, catalogue) if isinstance(table, str) else None
        if ns is not None:
            wanted.setdefault(_fold(ns), ns)
    others = {}
    for folded, ns in sorted(wanted.items()):
        if catalogue.get('qualify_with') == 'database':
            where = {'database': ns, 'schema': source.get('schema')}
        else:
            where = {'database': source.get('database'), 'schema': ns}
        try:
            others[folded] = list_tables(source_arn=source_arn, secret_arn=secret_arn,
                                         region=region, clients=clients, driver=driver,
                                         **where)
        except Exception as exc:  # noqa: BLE001
            others[folded] = {'listed': False, 'error': str(exc)}
    if others:
        catalogue['other_namespaces'] = others


# ── will the source keep its log for as long as the load holds capture? ──
#
# initial_load_and_cdc pauses Debezium and loads every table, one after the
# other, before resuming. Changes made meanwhile exist only in the source's log,
# so a load that outlasts the log's retention loses them, with no error at the
# time. The estimate below is deliberately pessimistic:
#
#   LOAD_ROWS_PER_SECOND  rows the loader writes per second. Measured runs did
#                         140-250K rows/s; 20K leaves room for wide rows,
#                         denormalisation, a small task and a busy source.
#   LOAD_TABLE_OVERHEAD_SECONDS  per table: a Fargate task start (image pull,
#                         network interface, connections) and the deployer's
#                         polling.
#   LOAD_PAUSE_FIXED_SECONDS  once per load: Debezium has to stop and start
#                         again, and the deployer polls both. The loader stack
#                         is created before the pause (Jira story F086), so this is now a
#                         generous allowance; kept, as the estimate is meant to
#                         be pessimistic.
#
# Row counts are the catalogue's estimates (MySQL TABLE_ROWS, PostgreSQL
# reltuples), which can be off by tens of percent; the low rate covers that.
LOAD_ROWS_PER_SECOND = 20000
LOAD_TABLE_OVERHEAD_SECONDS = 300
LOAD_PAUSE_FIXED_SECONDS = 900
# Warn once the estimate uses more than this share of the retention.
PAUSE_WARN_FRACTION = 0.5
RETENTION_PATH = 'source.log_retention'


def estimate_pause(tables, catalogue):
    """How long the load holds capture paused, from the catalogue. Pure.

    Returns {'seconds', 'rows', 'tables', 'unknown'}: `seconds` counts only the
    tables whose row count is known (so it is a lower bound when `unknown`
    names any), plus the fixed costs for every table.
    """
    rows, unknown = 0, []
    for name in tables:
        key = _fold(bare_table(name))
        # The table's own schema's catalogue (Jira story F131); unread is an unknown count.
        own, ns = _catalogue_for(name, catalogue)
        if own is None:
            if ns is not None:
                unknown.append(name)
            continue
        counts = {_fold(e['table']): e.get('row_estimate')
                  for e in own.get('tables') or []
                  if isinstance(e, dict) and isinstance(e.get('table'), str)}
        if key not in counts:
            continue    # a missing table is already a BLOCK of its own
        try:
            count = int(counts[key])
        except (TypeError, ValueError):
            count = -1
        if count < 0:
            unknown.append(name)
        else:
            rows += count
    seconds = (rows / float(LOAD_ROWS_PER_SECOND)
               + LOAD_TABLE_OVERHEAD_SECONDS * len(tables)
               + LOAD_PAUSE_FIXED_SECONDS)
    return {'seconds': seconds, 'rows': rows, 'tables': len(tables),
            'unknown': unknown}


def _hours(seconds):
    return '{:.1f}h'.format(seconds / 3600.0)


def _raise_retention_fix(origin, needed_hours):
    if origin == 'rds':
        return ("CALL mysql.rds_set_configuration('binlog retention hours', {});"
                .format(needed_hours))
    return 'SET PERSIST binlog_expire_logs_seconds = {};'.format(needed_hours * 3600)


def pause_findings(document, catalogue):
    """Compare the load's pause with the source's log retention. Pure.

    Only for initial_load_and_cdc, the one mode that pauses capture for a load.
    MySQL: the estimate against binary log retention -- over half of it is a
    WARN, over all of it a BLOCK (the changes made during the load would be
    purged before capture resumes). PostgreSQL keeps WAL for the replication
    slot, so there is no time limit unless max_slot_wal_keep_size is set; that
    limit is a size, so it is a WARN with the estimate. Anything unknown
    (retention, row counts) is a WARN only, except that the tables whose counts
    ARE known can already prove the pause too long.
    """
    doc = normalize(document) if isinstance(document, dict) else {}
    if doc.get('mode') != 'initial_load_and_cdc':
        return []
    if not catalogue or not catalogue.get('listed'):
        return []   # check_catalogue already warned that nothing could be read
    tables = [t for t in (doc.get('tables') or []) if isinstance(t, str)]
    if not tables:
        return []
    est = estimate_pause(tables, catalogue)
    basis = ('{:,} rows across {} table(s) at a conservative {:,} rows/s, plus '
             '{} min per table and {} min for pausing and resuming').format(
        est['rows'], est['tables'], LOAD_ROWS_PER_SECOND,
        LOAD_TABLE_OVERHEAD_SECONDS // 60, LOAD_PAUSE_FIXED_SECONDS // 60)
    lead = 'the bulk load holds capture paused for an estimated {}{} ({})'.format(
        'at least ' if est['unknown'] else '', _hours(est['seconds']), basis)
    out = []
    if est['unknown']:
        out.append(Finding(WARN, RETENTION_PATH,
            'the row count of {} is not known (run ANALYZE on the table), so how '
            'long the bulk load holds capture paused could only be estimated for '
            'the other tables. Make sure the source keeps its log for longer than '
            'the whole load.'.format(', '.join(est['unknown']))))

    checks = {c.get('key'): c for c in (catalogue.get('prerequisites') or [])
              if isinstance(c, dict) and c.get('key')}
    engine = catalogue.get('engine')
    if engine == 'postgresql':
        keep = (checks.get('slot_wal_keep') or {}).get('slot_wal_keep_size')
        if keep:
            out.append(Finding(WARN, RETENTION_PATH,
                '{}. PostgreSQL invalidates the replication slot once it holds '
                'more than max_slot_wal_keep_size={} of WAL, and the source writes '
                'WAL for every change made during the load. If that much can be '
                'written in this time the changes are lost: set '
                'max_slot_wal_keep_size=-1, or large enough, before '
                'deploying.'.format(lead, keep)))
        return out
    if engine != 'mysql':
        return out
    check = checks.get('log_retention')
    retention = (check or {}).get('retention_hours')
    if retention is None:
        out.append(Finding(WARN, RETENTION_PATH,
            '{}, and the binary log retention could not be read. Make sure the '
            'source keeps its binary logs for longer than that.'.format(lead)))
        return out
    retention_s = float(retention) * 3600.0
    # At least twice the estimate, never under the general minimum.
    from .tables import MIN_LOG_RETENTION_HOURS
    needed = max(MIN_LOG_RETENTION_HOURS, int(-(-2 * est['seconds'] // 3600)))
    fix = _raise_retention_fix(check.get('origin'), needed)
    if est['seconds'] > retention_s:
        out.append(Finding(BLOCK, RETENTION_PATH,
            '{}, longer than the source keeps its binary logs ({:g}h). Changes '
            'made during the load would be purged before capture resumes, and '
            'lost. Raise the retention first: {}'.format(lead, float(retention), fix)))
    elif est['seconds'] > PAUSE_WARN_FRACTION * retention_s:
        out.append(Finding(WARN, RETENTION_PATH,
            '{}, more than half of the binary log retention ({:g}h). A slower load '
            'than estimated would lose the changes made during it. Consider '
            'raising the retention: {}'.format(lead, float(retention), fix)))
    return out


EVICTION_FIX = ('Set maxmemory-policy to noeviction in a custom cache parameter '
                'group and apply it to the target (ElastiCache console: Parameter '
                'groups, then Modify the cache). A full cache then refuses writes, '
                'which the pipeline holds and retries, instead of deleting keys.')


def target_findings(target):
    """Target requirements, from the resolved target. Pure.

    The eviction policy decides whether a full cache drops data:

      * ``noeviction``: writes fail when full; the sink holds and retries them
        and the loader stops. Nothing is lost. No finding.
      * ``volatile-*``: evicts only keys with a TTL. The delete markers
        (tombstones) are the only keys ECDL gives a TTL, so an eviction can let
        a late change bring a deleted row back. Rows themselves are kept, and
        the cache shares this policy with whatever else uses it, so a WARN.
      * ``allkeys-*`` (or anything else): evicts loaded rows without any
        error, so the cache silently stops matching the source. A BLOCK.

    A policy that could not be read is a WARN: the deploy goes ahead and the
    sink checks the policy again when it starts.
    """
    target = target or {}
    policy = (target.get('maxmemory_policy') or '').strip()
    group = target.get('parameter_group') or '?'
    if policy == 'noeviction':
        return []
    if not policy:
        why = target.get('maxmemory_policy_problem') or 'it was not reported'
        return [Finding(WARN, 'target',
                        'could not check the target eviction policy ({}). ECDL '
                        'needs maxmemory-policy noeviction; confirm it '
                        'yourself.'.format(why))]
    if policy.startswith('volatile-'):
        return [Finding(WARN, 'target',
                        "the target's maxmemory-policy is {} (parameter group {}). "
                        'When the cache is full it evicts keys that have a TTL, '
                        'and the delete markers ECDL writes are such keys, so a '
                        'late change can bring a deleted row back. {}'.format(
                            policy, group, EVICTION_FIX))]
    return [Finding(BLOCK, 'target',
                    "the target's maxmemory-policy is {} (parameter group {}). "
                    'When the cache is full it deletes cached rows without any '
                    'error, so the cache silently stops matching the source. '
                    '{}'.format(policy, group, EVICTION_FIX))]


# The control plane's task-role boundary (TaskRoleBoundary) lets pipeline tasks
# read a secret only when it is listed in PipelineSecretArns, is one of ECDL's
# own, or carries this tag (with or without a list). The control plane
# passes the list as ECDL_PIPELINE_SECRET_ARNS (possibly empty). Unset means no
# boundary applies (the CLI deploys without one), so there is nothing to check.
PIPELINE_READ_TAG = 'ecdl:link'
PIPELINE_ARNS_ENV = 'ECDL_PIPELINE_SECRET_ARNS'
_OWN_NAME_PREFIXES = ('ecdl-', 'BrokerAuthSecret-')


def pipeline_secret_list(environ=None):
    """PipelineSecretArns as the control plane passed it, or None if unset."""
    raw = (os.environ if environ is None else environ).get(PIPELINE_ARNS_ENV)
    if raw is None:
        return None
    return [x.strip() for x in raw.split(',') if x.strip()]


def secret_findings(resolution, listed):
    """A WARN for each link secret the pipeline tasks will not be allowed to
    read, so their tasks would fail to start (ResourceInitializationError).
    Pure. `listed` is pipeline_secret_list(): None skips the check."""
    if listed is None:
        return []
    out = []
    for what in ('source', 'target'):
        secret = ((resolution or {}).get(what) or {}).get('secret')
        if not secret:
            continue
        arn, name = secret.get('arn') or '', secret.get('name') or ''
        if name.startswith(_OWN_NAME_PREFIXES):
            continue
        if PIPELINE_READ_TAG in (secret.get('tag_keys') or []):
            continue
        if listed and any(fnmatch.fnmatchcase(arn, entry) for entry in listed):
            continue
        message = ('the {w} secret {a} has no {t} tag{l}, so the pipeline tasks '
                   'cannot read it and will not start. Tag it (aws '
                   'secretsmanager tag-resource --secret-id {a} --tags '
                   'Key={t},Value=<link>), or list it in the control plane\'s '
                   'PipelineSecretArns.'.format(
                       w=what, a=arn, t=PIPELINE_READ_TAG,
                       l=' and is not in the control plane\'s PipelineSecretArns' if listed else ''))
        out.append(Finding(WARN, '{}.secret_arn'.format(what), message))
    return out


def prerequisite_warnings(catalogue):
    """Capture prerequisites that FAILED, as findings on the deploy preflight.

    They were only shown on the table picker, so a deploy went ahead with no
    binlog retention -- the setting that decides whether a paused or stopped
    capture loses changes -- without anyone being told. Warnings, as a rule: a
    deliberate setup (a short retention on a dev source) is the operator's decision.
    A check marked ``'block': True`` is a BLOCK instead: MySQL GTID mode off,
    which makes a change of writer skip changes without an error (Jira story F008). Only a
    check that read a value and failed counts; one that could not read is neither.
    """
    out = []
    for check in (catalogue or {}).get('prerequisites') or []:
        if isinstance(check, dict) and check.get('ok') is False:
            msg = '{}: {}'.format(check.get('name'), check.get('detail'))
            if check.get('fix'):
                msg += ' -- {}'.format(check['fix'])
            level = BLOCK if check.get('block') else WARN
            out.append(Finding(level, 'source.prerequisites', msg))
    return out


# ── PostgreSQL: replica identity ───────────────────────────────────────────
def _pg_ident(name):
    """A PostgreSQL identifier as SQL text: bare when PostgreSQL would read it
    back unchanged, else double-quoted. The name comes from the catalogue, so
    it is the exact relname, and quoting it keeps its case."""
    name = str(name)
    if re.match(r'[a-z_][a-z0-9_$]*\Z', name):
        return name
    return '"{}"'.format(name.replace('"', '""'))


def _pg_qualified(schema, table):
    return '{}.{}'.format(_pg_ident(schema), _pg_ident(table))


def _link_catalogue_tables(doc, catalogue):
    """[(configured name, catalogue entry)] for the link's tables in the
    catalogue's schema. A table qualified with another schema is not in this
    catalogue and is skipped rather than matched by its bare name."""
    namespace = _fold(catalogue.get('namespace') or '')
    present = {_fold(e.get('table')): e for e in catalogue.get('tables') or []
               if isinstance(e, dict) and isinstance(e.get('table'), str)}
    out = []
    for entry in doc.get('tables') or []:
        if not isinstance(entry, str):
            continue
        parts = entry.split('.')
        if len(parts) > 1 and _fold(parts[-2]) != namespace:
            continue
        hit = present.get(_fold(bare_table(entry)))
        if hit is not None:
            out.append((entry, hit))
    return out


def replica_identity_findings(document, catalogue):
    """REPLICA IDENTITY of each PostgreSQL link table. Pure.
      * FULL: every column of the old row is logged. Nothing to report.
      * DEFAULT with a primary key, or USING INDEX: acceptable. If the table has
        columns that can be TOASTed, a WARN: an UPDATE that leaves one unchanged
        does not carry its value (Debezium sends a placeholder, which the sink
        skips so the cached value stays), so a filter, computed field or
        denormalize rule reading that column sees no value for that change.
      * NOTHING, or DEFAULT without a primary key: the table has no replica
        identity. Once it is in a publication PostgreSQL refuses every UPDATE
        and DELETE on it ("cannot update table ... because it does not have a
        replica identity and publishes updates"), and the change events would
        carry no key. A BLOCK.
    A table whose identity could not be read is not reported.
    """
    if (catalogue or {}).get('engine') != 'postgresql' or not catalogue.get('listed'):
        return []
    doc = normalize(document) if isinstance(document, dict) else {}
    schema = catalogue.get('namespace') or 'public'
    findings = []
    for configured, entry in _link_catalogue_tables(doc, catalogue):
        ident = entry.get('replica_identity')
        if not ident:
            continue
        name = _pg_qualified(schema, entry['table'])
        full_fix = 'ALTER TABLE {} REPLICA IDENTITY FULL;'.format(name)
        if ident == 'n' or (ident == 'd' and not entry.get('primary_key')):
            why = ('REPLICA IDENTITY NOTHING' if ident == 'n' else
                   'REPLICA IDENTITY DEFAULT and no primary key')
            findings.append(Finding(BLOCK, 'tables', (
                "table '{}' has {}, so it has no replica identity. Once it is in "
                'the publication, PostgreSQL refuses every UPDATE and DELETE on '
                'it, and its change events would carry no key. Fix: {} (or add '
                'a primary key and keep DEFAULT)'.format(configured, why, full_fix))))
            continue
        toastable = entry.get('toastable_columns') or []
        if ident in ('d', 'i') and toastable:
            findings.append(Finding(WARN, 'tables', (
                "table '{}' is REPLICA IDENTITY {} and has columns that can be "
                'stored out of line (TOAST): {}. An UPDATE that leaves one of them '
                'unchanged does not carry its value. The sink keeps the cached '
                'value, but a filter, computed field or denormalize rule that '
                'reads the column sees no value for that change. To have every '
                'UPDATE carry it: {} (each UPDATE then writes the whole old row '
                'to the WAL)'.format(configured,
                                     'DEFAULT' if ident == 'd' else 'USING INDEX',
                                     ', '.join(toastable), full_fix))))
    return findings


def keyless_findings(document, catalogue):
    """Link tables the sink cannot key, for a link that streams changes. Pure.

    The sink keys each row by the change event's key (the primary key) and,
    without one, by an `id` column. A table with neither turns every change
    into a dead letter, and a busy one fills the dead-letter stream (Jira story F025).
    A BLOCK. PostgreSQL tables already blocked for their replica identity
    (NOTHING, or DEFAULT without a primary key) are not reported twice, and a
    table whose primary key was not read is not reported.
    """
    doc = normalize(document) if isinstance(document, dict) else {}
    if doc.get('mode') not in ('cdc', 'initial_load_and_cdc'):
        return []
    if not (catalogue or {}).get('listed'):
        return []
    pg = catalogue.get('engine') == 'postgresql'
    findings = []
    for configured, entry in _link_catalogue_tables(doc, catalogue):
        if 'primary_key' not in entry or entry.get('primary_key'):
            continue
        if pg and entry.get('replica_identity') in ('n', 'd'):
            continue
        cols = {_fold(c.get('name')) for c in entry.get('columns') or []
                if isinstance(c, dict) and isinstance(c.get('name'), str)}
        if 'id' in cols:
            continue
        findings.append(Finding(BLOCK, 'tables', (
            "table '{}' has no primary key and no 'id' column, so the sink has "
            'no key to cache its rows under and would dead-letter every change. '
            'Add a primary key, or leave the table out of the link'.format(configured))))
    return findings


# ── PostgreSQL: publication and signalling table ───────────────────────────


def _signal_collection(doc):
    """The link's signalling collection (schema.table), or None."""
    from .signals import SignalError, signal_collection
    source = doc.get('source') or {}
    override = ((doc.get('advanced') or {}).get('debezium') or {}).get('signal_collection')
    try:
        return signal_collection('postgresql', doc.get('name') or '',
                                 database=source.get('database'),
                                 schema=source.get('schema'), override=override)
    except SignalError:
        return None


def publication_findings(document, catalogue, publication=None):
    """Is every link table, and the signalling table, in the publication? Pure.

    PostgreSQL sends Debezium only the tables in the publication it reads. A
    link table outside it is never captured; a signalling table outside it
    means every signal is ignored, so a backfill silently never happens. Both
    are a BLOCK with the statement that fixes them, when the publication is
    known. With ``publication`` None (the deployed stack could not be read) the
    link's own publication is checked and a gap is a WARN, because a link
    deployed before per-link publications reads dbz_pub instead.

    Also: a signalling table that does not exist is a BLOCK, and one the
    link's user cannot INSERT into is a WARN (Debezium writes its snapshot
    window markers there).
    """
    from .provision import pg_publication_name
    from .signals import SignalError, pg_publication_sql, pg_signal_grant_sql
    if (catalogue or {}).get('engine') != 'postgresql' or not catalogue.get('listed'):
        return []
    doc = normalize(document) if isinstance(document, dict) else {}
    if not doc.get('name'):
        return []
    certain = publication is not None
    publication = publication or pg_publication_name(doc['name'])
    level = BLOCK if certain else WARN
    unsure = ('' if certain else
              ' (The deployed stack could not be read, so this assumes the link '
              'reads {}; a link deployed before per-link publications reads dbz_pub.)'
              .format(publication))
    schema = catalogue.get('namespace') or 'public'
    present = {_fold(e.get('table')): e for e in catalogue.get('tables') or []
               if isinstance(e, dict) and isinstance(e.get('table'), str)}
    findings = []

    # Every member the publication needs, as schema.table SQL text.
    needed = []
    for entry in doc.get('tables') or []:
        if not isinstance(entry, str):
            continue
        parts = entry.split('.')
        if len(parts) == 1 or _fold(parts[-2]) == _fold(schema):
            hit = present.get(_fold(parts[-1]))
            if hit is None:
                continue  # already blocked by check_catalogue
            needed.append(_pg_qualified(schema, hit['table']))
        else:
            needed.append('.'.join(_pg_ident(p) for p in parts[-2:]))
    signal = _signal_collection(doc)
    if signal:
        sig_schema, sig_table = signal.rsplit('.', 1)
        if _fold(sig_schema) == _fold(schema):
            hit = present.get(_fold(sig_table))
            if hit is None:
                findings.append(Finding(BLOCK, 'advanced.debezium.signal_collection', (
                    "the signalling table '{}' does not exist on the source, so "
                    'no backfill can ever be requested and Debezium cannot run an '
                    'incremental snapshot. `ecdl signal ddl <link file>` prints '
                    'the CREATE TABLE, the publication statement and the '
                    'grant'.format(signal))))
            elif hit.get('insertable') is False:
                findings.append(Finding(WARN, 'advanced.debezium.signal_collection', (
                    "the source user {} cannot INSERT into the signalling table "
                    "'{}'. Debezium writes its snapshot window markers there, so a "
                    'backfill would fail as it starts. Fix: {}'.format(
                        catalogue.get('user') or '?', signal,
                        pg_signal_grant_sql(signal, catalogue.get('user'))))))
        needed.append(signal)

    publications = catalogue.get('publications')
    if publications is None:
        return findings + [Finding(WARN, 'source', (
            'could not read the publications on the source. Confirm that {} '
            'contains {}.'.format(publication, ', '.join(needed) or 'the link tables')))]
    if not needed:
        return findings
    try:
        if publication not in publications:
            findings.append(Finding(level, 'source', (
                'the publication {} does not exist on the source. Debezium reads '
                'only the tables in it and does not create it. Fix: {}{}'.format(
                    publication, pg_publication_sql(publication, needed, create=True),
                    unsure))))
            return findings
        # Review finding M8: what the publication sends, and how it names a
        # partitioned table's changes.
        options = (catalogue.get('publication_options') or {}).get(publication) or {}
        dropped = [op for op in ('insert', 'update', 'delete') if options.get(op) is False]
        if dropped:
            findings.append(Finding(level, 'source', (
                'the publication {} does not publish {}: those changes never reach '
                'Debezium, and the cache keeps the old rows. Fix: ALTER PUBLICATION {} '
                "SET (publish = 'insert, update, delete, truncate');{}".format(
                    publication, ', '.join(dropped), publication, unsure))))
        partitioned = [t for t in needed
                       if (present.get(_fold(t.split('.')[-1].replace('"', ''))) or {}).get('partitioned')]
        if partitioned and options.get('via_root') is not True:
            findings.append(Finding(level, 'source', (
                '{} {} partitioned. Without publish_via_partition_root PostgreSQL '
                "sends each partition's changes under the partition's name, which "
                'the link does not capture. Fix: ALTER PUBLICATION {} SET '
                '(publish_via_partition_root = true); (PostgreSQL 13 or later){}'.format(
                    ', '.join(partitioned), 'is' if len(partitioned) == 1 else 'are',
                    publication, unsure))))
        members = {_fold(m) for m in publications[publication]}
        # A partitioned table is listed under its partitions' names unless the
        # publication publishes via the root; that is the finding above.
        missing = [t for t in needed if _fold(t.replace('"', '')) not in members
                   and not (t in partitioned and options.get('via_root') is not True)]
        if missing:
            tables_missing = [t for t in missing if t != signal]
            why = []
            if tables_missing:
                why.append('PostgreSQL sends Debezium nothing for {}, so its changes '
                           'are never captured'.format(', '.join(tables_missing)))
            if signal in missing:
                why.append('signals written to {} never reach Debezium, so every '
                           'backfill request is silently ignored'.format(signal))
            findings.append(Finding(level, 'source', (
                'the publication {} does not include {}: {}. Fix: {}{}'.format(
                    publication, ', '.join(missing), '; '.join(why),
                    pg_publication_sql(publication, missing), unsure))))
    except SignalError as exc:
        findings.append(Finding(WARN, 'source',
                                'could not check the publication: {}'.format(exc)))
    return findings
