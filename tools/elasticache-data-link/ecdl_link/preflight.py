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
    present = {}
    for entry in catalogue.get('tables') or []:
        name = entry.get('table') if isinstance(entry, dict) else None
        if not isinstance(name, str):
            continue
        cols = {}
        for col in entry.get('columns') or []:
            cname = col.get('name') if isinstance(col, dict) else None
            if isinstance(cname, str):
                cols[_fold(cname)] = cname
        present[_fold(name)] = {'name': name, 'cols': cols}

    all_names = [e['name'] for e in present.values()]
    findings = []

    # 1. Every configured table must exist on the source.
    missing = set()
    for entry in tables:
        key = _fold(bare_table(entry))
        if key not in present:
            missing.add(key)
            msg = "table '{}' does not exist on the source".format(entry)
            hit = _closest(bare_table(entry), all_names)
            if hit:
                msg += " (did you mean '{}'?)".format(hit)
            findings.append(Finding(BLOCK, 'tables', msg))

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
            if key in missing or key not in present:
                continue
            cols = present[key]['cols']
            actual = present[key]['name']
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
                        if pkey in present:  # if parent is missing it is already blocked
                            pcols = present[pkey]['cols']
                            for field in fields:
                                if isinstance(field, str) and _fold(field) not in pcols:
                                    findings.append(Finding(BLOCK, '{}.denormalize[{}].fields'.format(path, i),
                                        "embeds column '{}' from '{}', which that table does not have".format(field, parent)))

    return findings


def summarize(findings, checked):
    """Turn a list of Findings into the JSON the API returns."""
    return {
        'checked': bool(checked),
        'blocks': sum(1 for f in findings if f.level == BLOCK),
        'warnings': sum(1 for f in findings if f.level == WARN),
        'findings': [f._asdict() for f in findings],
    }


def preflight(document, region=None, clients=None, driver=None):
    """Fetch the source catalogue and check the document against it.

    Returns ``summarize(...)``: ``{checked, blocks, warnings, findings}``.
    ``checked`` is True only when the source catalogue was actually read; every
    other outcome (external source, no ARN/secret, unreachable, no driver) comes
    back as a single WARN with ``checked`` False, because none of those is a
    reason to block a deploy.
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

    findings = check_catalogue(doc, catalogue)
    findings.extend(prerequisite_warnings(catalogue))
    return summarize(findings, checked=bool(catalogue.get('listed')))


def prerequisite_warnings(catalogue):
    """Capture prerequisites that FAILED, as warnings on the deploy preflight.

    They were only shown on the table picker, so a deploy went ahead with no
    binlog retention -- the setting that decides whether a paused or stopped
    capture loses changes -- without anyone being told. Warnings, not blocks: a
    deliberate setup (a short retention on a dev source) is the operator's decision.
    """
    out = []
    for check in (catalogue or {}).get('prerequisites') or []:
        if isinstance(check, dict) and check.get('ok') is False:
            msg = '{}: {}'.format(check.get('name'), check.get('detail'))
            if check.get('fix'):
                msg += ' -- {}'.format(check['fix'])
            out.append(Finding(WARN, 'source.prerequisites', msg))
    return out
