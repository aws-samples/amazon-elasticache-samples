"""Load, validate and normalise an ECDL link configuration.

This module is the one place a link config is interpreted. The Flask API and the
`ecdl` CLI both import it, so a document that one accepts is a document the other
accepts -- which is the requirement this whole module exists to satisfy.

Two rules it enforces that the previous surfaces did not:

1. Unknown keys are reported as errors, not ignored. The old CLI read `target.endpoint`,
   `target.tls` and `performance.poll_interval_ms` into a dict and never passed
   them anywhere; the old UI collected an `auth_username` there was no column for.
   Both looked like they worked.

2. Settings that are discovered rather than chosen cannot be set at all.
   `target.cluster_mode` is the most dangerous one: a plain client connected to a sharded
   cluster connects successfully and then fails `MOVED` on most keys, so a wrong
   value degrades into partial data loss rather than an error.
"""

import json
import os

from . import jsonschema_mini as mini
from .exprcheck import expr_problem
from .jsonschema_mini import Problem

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'link.schema.json')

# Which advanced sections describe components that a given mode does not create.
# Setting one is not harmless -- it means the author believes something is running
# that is not, so it is reported rather than ignored.
_IRRELEVANT_ADVANCED = {
    'initial_load': {
        'sink': 'mode initial_load runs the parallel loader and finishes; no sink is created',
        'broker': 'mode initial_load does not create a broker',
        'debezium': 'mode initial_load does not run Debezium',
        # The alarms live in the CDC stack, which this mode does not create.
        'alarms': 'mode initial_load creates no CDC stack, and the alarms belong to it',
    },
    'cdc': {
        'loader': 'mode cdc does not run the initial loader; use initial_load_and_cdc to backfill',
    },
    'initial_load_and_cdc': {},
}

# The same for single images: each belongs to one component.
_IRRELEVANT_IMAGES = {
    'initial_load': {
        'debezium': 'mode initial_load does not run Debezium',
        'sink': 'mode initial_load runs the parallel loader and finishes; no sink is created',
    },
    'cdc': {
        'loader': 'mode cdc does not run the initial loader',
    },
}

_MODES = ('initial_load', 'cdc', 'initial_load_and_cdc')


class LinkValidationError(ValueError):
    """Raised with every problem found, not just the first."""

    def __init__(self, problems, source=None):
        self.problems = list(problems)
        self.source = source
        super(LinkValidationError, self).__init__(self.format())

    def format(self):
        where = ' in {}'.format(self.source) if self.source else ''
        lines = ['{} problem{} found{}:'.format(
            len(self.problems),
            '' if len(self.problems) == 1 else 's',
            where)]
        for p in self.problems:
            lines.append('  {}'.format(p))
        return '\n'.join(lines)


_schema_cache = None


def schema():
    """The parsed schema. Cached; the file does not change at runtime."""
    global _schema_cache
    if _schema_cache is None:
        with open(SCHEMA_PATH) as fh:
            _schema_cache = json.load(fh)
    return _schema_cache


# ── loading ────────────────────────────────────────────────────────────────

def load_file(path):
    """Read a link config from JSON or YAML. Returns the raw document."""
    with open(path) as fh:
        text = fh.read()
    return loads(text, source=path)


def loads(text, source=None):
    """Parse JSON or YAML text into a raw document."""
    stripped = text.lstrip()
    if stripped.startswith('{'):
        try:
            return json.loads(text)
        except ValueError as exc:
            raise LinkValidationError(
                [Problem('', 'not valid JSON: {}'.format(exc))], source)
    try:
        import yaml
    except ImportError:
        raise LinkValidationError(
            [Problem('', 'PyYAML is required to read YAML configs '
                         '(pip install PyYAML), or write the file as JSON')],
            source)
    try:
        from ecdl_link.safe_yaml import check_plain
        check_plain(text, allow_aliases=True)
        doc = yaml.safe_load(text)
    except Exception as exc:
        raise LinkValidationError(
            [Problem('', 'not valid YAML: {}'.format(exc))], source)
    if doc is None:
        raise LinkValidationError([Problem('', 'file is empty')], source)
    return doc


# ── normalisation ──────────────────────────────────────────────────────────

def _prune(value):
    """Drop nulls and empty containers, recursively.

    The UI posts explicit nulls and empty strings for fields the operator left
    blank; a file omits them. Both must reach the same document or
    "the UI and the file accept the same things" is not testable.
    """
    if isinstance(value, dict):
        out = {}
        for key, sub in value.items():
            pruned = _prune(sub)
            if pruned is None:
                continue
            out[key] = pruned
        return out or None
    if isinstance(value, list):
        out = [_prune(v) for v in value]
        out = [v for v in out if v is not None]
        return out or None
    if value is None:
        return None
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    return value


def bare_table(name):
    """'Inventory.dbo.orders' -> 'orders'. Transforms are keyed by bare name."""
    return str(name).split('.')[-1]


def normalize(doc):
    """Canonicalise a raw document. Does not validate; call validate_link for that.

    Applies only structural defaults. Deployment defaults (batch sizes, node
    types, image tags) are resolved at deploy time and deliberately not written
    back into the config; see jsonschema_mini.defaults_for for the reason.
    """
    if not isinstance(doc, dict):
        return doc

    out = _prune(doc) or {}

    for key, default in mini.defaults_for(schema()).items():
        out.setdefault(key, default)

    if isinstance(out.get('name'), str):
        out['name'] = out['name'].strip().lower()

    tables = out.get('tables')
    if isinstance(tables, list):
        seen = set()
        deduped = []
        for entry in tables:
            if not isinstance(entry, str):
                deduped.append(entry)
                continue
            trimmed = entry.strip()
            if trimmed in seen:
                continue
            seen.add(trimmed)
            deduped.append(trimmed)
        out['tables'] = deduped

    transforms = out.get('transforms')
    if isinstance(transforms, dict):
        # `version` exists only for compatibility with older transforms.json
        # files and is not used, so it is dropped rather than carried forward.
        transforms.pop('version', None)
        # Deliberately no `setdefault('tables', {})` here. An earlier version did
        # that and then dropped the whole transforms block when tables came back
        # empty -- which also discarded any invalid sibling key, so a rejected
        # setting like `aggregations` passed validation silently. _prune already
        # removes a transforms block that holds nothing; anything left is real
        # and must be validated.
        if not transforms:
            out.pop('transforms', None)

    return out


def canonical_json(doc):
    """Stable serialisation, for comparing two documents byte for byte."""
    return json.dumps(doc, sort_keys=True, indent=2, separators=(',', ': '))


# The order a link file reads best in: what it is, where it comes from, where it
# goes, what it carries, then the optional shaping. Alphabetical would put
# `advanced` first and `tables` after `transforms`, which is not a useful reading
# order. Anything not listed keeps its own order after these.
_KEY_ORDER = ('schema_version', 'name', 'mode', 'source', 'target', 'tables',
              'transforms', 'advanced')
_SOURCE_ORDER = ('kind', 'arn', 'engine', 'host', 'port', 'tls', 'database',
                 'schema', 'username', 'secret_arn')
_TARGET_ORDER = ('arn', 'secret_arn')
_TRANSFORM_ORDER = ('columns', 'rename', 'computed', 'filter', 'denormalize')


def _ordered(doc, order):
    """Re-key a dict so `order` comes first, then whatever else it holds."""
    if not isinstance(doc, dict):
        return doc
    out = {}
    for key in order:
        if key in doc:
            out[key] = doc[key]
    for key in doc:
        if key not in out:
            out[key] = doc[key]
    return out


def for_display(doc):
    """The document with its keys in reading order, nothing added or removed.

    Ordering only. It must not change what the document MEANS, or the text a
    person reviews would differ from the document that gets deployed.
    """
    if not isinstance(doc, dict):
        return doc
    out = _ordered(doc, _KEY_ORDER)
    if isinstance(out.get('source'), dict):
        out['source'] = _ordered(out['source'], _SOURCE_ORDER)
    if isinstance(out.get('target'), dict):
        out['target'] = _ordered(out['target'], _TARGET_ORDER)
    transforms = out.get('transforms')
    if isinstance(transforms, dict) and isinstance(transforms.get('tables'), dict):
        transforms = dict(transforms)
        transforms['tables'] = {
            table: _ordered(spec, _TRANSFORM_ORDER)
            for table, spec in transforms['tables'].items()
        }
        out['transforms'] = transforms
    return out


def to_yaml(doc):
    """Render a document as the YAML text of a link file.

    Exists so a form can produce a file. The alternative was serialising in the
    browser, which would mean the text someone reviews and commits was written by
    a different implementation from the one that reads it -- the same file-versus-
    form split this whole schema exists to remove. The same code writes and
    reads the file.
    """
    try:
        import yaml
    except ImportError:
        raise LinkValidationError(
            [Problem('', 'PyYAML is required to write a link file')], None)
    return yaml.safe_dump(
        for_display(doc),
        # Insertion order, which for_display has just made reading order.
        sort_keys=False,
        # Block style: a link file is meant to be read and diffed, and flow style
        # collapses tables and transforms onto one line.
        default_flow_style=False,
        # Long ARNs must not be wrapped. A wrapped ARN still parses, but it reads
        # as broken, and someone may edit it and break it.
        width=10000,
        allow_unicode=True)


# ── validation ─────────────────────────────────────────────────────────────

def _source_problems(source):
    """The arn-vs-endpoint rules the schema cannot express.

    The validator has no oneOf, so "an AWS source is given by an ARN, an external
    source by an engine and host, never both" lives here. Kept as an error rather
    than a silent preference because this schema exists so that a
    field which can never take effect is a mistake, not something to ignore: an `engine` on an
    AWS source, or an `arn` on an external one, means the author believes
    something that is not true about their own pipeline.
    """
    if not isinstance(source, dict):
        return []
    kind = source.get('kind') or 'aws'
    problems = []

    if kind == 'aws':
        if not source.get('arn'):
            problems.append(Problem(
                'source.arn',
                "is required for an AWS source (source.kind 'aws')"))
        for field in ('engine', 'host', 'port', 'tls'):
            if source.get(field) is not None:
                problems.append(Problem(
                    'source.{}'.format(field),
                    "applies only to an external source (source.kind "
                    "'external'); for an AWS source it is read from source.arn"))
    else:  # external
        if source.get('arn'):
            problems.append(Problem(
                'source.arn',
                "is not used for an external source (source.kind 'external'); "
                "give source.engine and source.host instead"))
        for field in ('engine', 'host'):
            if not source.get(field):
                problems.append(Problem(
                    'source.{}'.format(field),
                    "is required for an external source (source.kind "
                    "'external')"))
        if not source.get('username'):
            problems.append(Problem(
                'source.username',
                "is required for an external source: there is no RDS "
                "MasterUsername to fall back to"))
        # tls: false is accepted since 0.2.34: both stacks have a SourceTLS
        # parameter (Debezium and the loader), set from it. It used to be
        # refused because nothing passed it on.

    return problems


def _semantic_problems(doc):
    """Cross-field rules the schema cannot express on its own."""
    problems = _source_problems(doc.get('source'))

    tables = doc.get('tables') if isinstance(doc.get('tables'), list) else []
    table_names = [t for t in tables if isinstance(t, str)]
    bare = {}
    for entry in table_names:
        key = bare_table(entry)
        bare.setdefault(key, []).append(entry)

    for key, entries in sorted(bare.items()):
        if len(entries) > 1:
            problems.append(Problem(
                'tables',
                "'{}' is listed more than once, as {}. Cache keys use the "
                "bare table name (<table>:<pk>), so these would write the "
                "same keys. Keep one, or put the other in its own link with "
                "a different advanced.key_prefix.".format(
                    key, ' and '.join(repr(e) for e in entries))))

    transforms = doc.get('transforms')
    if isinstance(transforms, dict):
        t_tables = transforms.get('tables')
        if isinstance(t_tables, dict):
            # The executable spec is keyed by bare name, so 'public.users' and
            # 'users' would be one entry and one of them silently dropped.
            seen = {}
            for name in sorted(t_tables):
                seen.setdefault(bare_table(name), []).append(name)
            for key, names in sorted(seen.items()):
                if len(names) > 1:
                    problems.append(Problem(
                        'transforms.tables',
                        "'{}' has more than one block, as {}. Keep one.".format(
                            key, ' and '.join(repr(n) for n in names))))
            for name in sorted(t_tables):
                spec = t_tables[name]
                path = 'transforms.tables.{}'.format(name)
                if bare_table(name) not in bare:
                    problems.append(Problem(
                        path,
                        "has no matching entry in 'tables', so it would never "
                        "be applied. Add '{}' to tables or remove this "
                        "block.".format(name)))
                if not isinstance(spec, dict):
                    continue
                problems.extend(_transform_problems(spec, path, bare))

    advanced = doc.get('advanced')
    mode = doc.get('mode')
    if isinstance(advanced, dict) and mode in _IRRELEVANT_ADVANCED:
        for section, reason in sorted(_IRRELEVANT_ADVANCED[mode].items()):
            if section in advanced:
                problems.append(Problem(
                    'advanced.{}'.format(section),
                    'has no effect: {}'.format(reason)))
        images = advanced.get('images')
        if isinstance(images, dict):
            for image, reason in sorted(_IRRELEVANT_IMAGES.get(mode, {}).items()):
                if image in images:
                    problems.append(Problem(
                        'advanced.images.{}'.format(image),
                        'has no effect: {}'.format(reason)))

    if isinstance(advanced, dict):
        problems.extend(_advanced_problems(advanced))
        stale = (advanced.get('alarms') or {}).get('table_stale_seconds') \
            if isinstance(advanced.get('alarms'), dict) else None
        if isinstance(stale, dict):
            seen = {}
            for name in sorted(stale):
                if bare_table(name) not in bare:
                    problems.append(Problem(
                        'advanced.alarms.table_stale_seconds.{}'.format(name),
                        "'{}' is not one of this link's tables, so the alarm would "
                        "watch nothing".format(name)))
                seen.setdefault(bare_table(name).lower(), []).append(name)
            for key, names in sorted(seen.items()):
                if len(names) > 1:
                    problems.append(Problem(
                        'advanced.alarms.table_stale_seconds',
                        "'{}' is listed more than once, as {}".format(
                            key, ' and '.join(repr(n) for n in names))))

    return problems


def _transform_problems(spec, path, bare_tables):
    problems = []

    columns = spec.get('columns')
    computed = spec.get('computed')
    computed_names = set()
    if isinstance(computed, list):
        for item in computed:
            if isinstance(item, dict) and isinstance(item.get('name'), str):
                computed_names.add(item['name'])

    # The sink and the loader refuse to start with an expression outside their
    # grammar (Jira story F077), so say so here, before a deploy.
    if isinstance(computed, list):
        for i, item in enumerate(computed):
            if isinstance(item, dict) and 'expr' in item:
                why = expr_problem(item['expr'])
                if why:
                    problems.append(Problem('{}.computed[{}].expr'.format(path, i), why))
    # A rename whose source column is neither selected nor computed can never
    # fire. Silently doing nothing is the failure mode this schema exists to
    # remove, so it is an error rather than ignored.
    rename = spec.get('rename')
    if isinstance(rename, dict) and isinstance(columns, list) and columns:
        selectable = set(columns) | computed_names
        for src in sorted(rename):
            if src not in selectable:
                problems.append(Problem(
                    '{}.rename.{}'.format(path, src),
                    "renames a column that 'columns' does not select and "
                    "'computed' does not create, so it would never apply"))
    # Two columns renamed to one name: the sink and the loader refuse it.
    if isinstance(rename, dict):
        targets = {}
        for src in sorted(rename):
            if isinstance(rename[src], str):
                targets.setdefault(rename[src], []).append(src)
        for dst, srcs in sorted(targets.items()):
            if len(srcs) > 1:
                problems.append(Problem(
                    '{}.rename'.format(path),
                    "renames {} all to '{}'; only one column can have a name".format(
                        ' and '.join(repr(s) for s in srcs), dst)))

    denorm = spec.get('denormalize')
    if isinstance(denorm, list):
        for i, rule in enumerate(denorm):
            if not isinstance(rule, dict):
                continue
            parent = rule.get('parent_table')
            if isinstance(parent, str) and bare_table(parent) not in bare_tables:
                problems.append(Problem(
                    '{}.denormalize[{}].parent_table'.format(path, i),
                    "'{}' is not one of this link's tables, so it is never "
                    "materialised in the cache and there would be nothing to "
                    "embed from".format(parent)))

    return problems


def _advanced_problems(advanced):
    problems = []

    sink = advanced.get('sink')
    if isinstance(sink, dict):
        lo, want, hi = (sink.get('min_count'), sink.get('desired_count'),
                        sink.get('max_count'))
        if isinstance(lo, int) and isinstance(hi, int) and lo > hi:
            problems.append(Problem(
                'advanced.sink.min_count',
                'is above max_count ({} > {})'.format(lo, hi)))
        if isinstance(want, int) and isinstance(hi, int) and want > hi:
            problems.append(Problem(
                'advanced.sink.desired_count',
                'is above max_count ({} > {})'.format(want, hi)))
        if isinstance(want, int) and isinstance(lo, int) and want < lo:
            problems.append(Problem(
                'advanced.sink.desired_count',
                'is below min_count ({} < {})'.format(want, lo)))

    return problems


def problems_for(doc):
    """Every problem with a raw document, schema then semantic. Never raises."""
    if not isinstance(doc, dict):
        return [Problem('', 'expected an object at the top level, got {}'.format(
            mini._type_name(doc)))]
    normalised = normalize(doc)
    found = mini.validate(normalised, schema())
    # Semantic checks assume the shape held. Running them on a document that
    # failed the schema produces noise about fields that are the wrong
    # type, so they are gated.
    if not found:
        found = _semantic_problems(normalised)
    return found


def validate_link(doc, source=None):
    """Validate and return the normalised document. Raises LinkValidationError."""
    found = problems_for(doc)
    if found:
        raise LinkValidationError(found, source)
    return normalize(doc)


def validate_file(path):
    """Load, validate and return the normalised document from a file."""
    return validate_link(load_file(path), source=path)
