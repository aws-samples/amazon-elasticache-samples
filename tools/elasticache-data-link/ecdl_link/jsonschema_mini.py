"""A small interpreter for the subset of JSON Schema that link.schema.json uses.

Why not the `jsonschema` package: this module is imported by both the Flask API
and the CLI, and neither currently depends on it. Adding a third-party dependency
in two places is exactly how the surfaces drifted apart in the first place. The
subset in use is small enough to interpret directly, which keeps the schema file
the single definition rather than documentation next to separately coded checks.

Everything here is stdlib. The schema file remains valid JSON Schema 2020-12, so
standard tooling (editors, `check-jsonschema`) can still consume it.

Supported keywords:
    type (object array string integer number boolean), required, properties,
    additionalProperties (false | schema), enum, const, items, pattern,
    minLength, minItems, maxItems, minimum, maximum

A key starting with '_' is a comment, and skipped, in an object whose keys are
fixed (additionalProperties false). In a map (additionalProperties a schema)
it is an ordinary key and is validated like any other.

Plus one extension:
    x-rejected -- a map of property name to an explanation. A key listed here
    produces that explanation instead of a generic "unknown property". This is
    the purpose of the extension: a field that used to be silently dropped now
    says what to do instead.
"""

import difflib
import re


class Problem(object):
    """One validation failure, located by a human-readable path."""

    __slots__ = ('path', 'message')

    def __init__(self, path, message):
        self.path = path
        self.message = message

    def __str__(self):
        return '{}: {}'.format(self.path or '(root)', self.message)

    def __repr__(self):
        return 'Problem({!r}, {!r})'.format(self.path, self.message)

    def __eq__(self, other):
        return (isinstance(other, Problem)
                and self.path == other.path
                and self.message == other.message)


def _join(path, key):
    return '{}.{}'.format(path, key) if path else str(key)


def _index(path, i):
    return '{}[{}]'.format(path, i)


def _type_name(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    return type(value).__name__


def _matches_type(value, expected):
    if expected == 'object':
        return isinstance(value, dict)
    if expected == 'array':
        return isinstance(value, list)
    if expected == 'string':
        return isinstance(value, str)
    if expected == 'boolean':
        return isinstance(value, bool)
    if expected == 'integer':
        # bool is a subclass of int in Python; `enabled: true` is not a count.
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == 'number':
        return (isinstance(value, (int, float))
                and not isinstance(value, bool))
    return True


def _suggest(key, candidates):
    """'Did you mean' for a near-miss key. Typos are the common case."""
    if not candidates:
        return ''
    close = difflib.get_close_matches(key, sorted(candidates), n=1, cutoff=0.7)
    if close and close[0] != key:
        return " Did you mean '{}'?".format(close[0])
    return ''


def validate(value, schema, path=''):
    """Return a list of Problem. Empty list means the document is valid.

    Collects every problem rather than stopping at the first, because a customer
    fixing a config file wants the whole list, not one error per run.
    """
    problems = []

    expected = schema.get('type')
    if expected is not None and not _matches_type(value, expected):
        problems.append(Problem(
            path, 'expected {}, got {}'.format(expected, _type_name(value))))
        # Everything below assumes the type held, so stop on this branch.
        return problems

    if 'const' in schema and value != schema['const']:
        problems.append(Problem(
            path, 'must be {!r}'.format(schema['const'])))

    if 'enum' in schema and value not in schema['enum']:
        allowed = ', '.join(repr(v) for v in schema['enum'])
        problems.append(Problem(
            path, 'must be one of {} (got {!r})'.format(allowed, value)))

    if isinstance(value, str):
        problems.extend(_check_string(value, schema, path))
    elif isinstance(value, list):
        problems.extend(_check_array(value, schema, path))
    elif isinstance(value, dict):
        problems.extend(_check_object(value, schema, path))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        problems.extend(_check_number(value, schema, path))

    return problems


def _check_string(value, schema, path):
    problems = []
    min_length = schema.get('minLength')
    if min_length is not None and len(value) < min_length:
        if min_length == 1:
            problems.append(Problem(path, 'must not be empty'))
        else:
            problems.append(Problem(
                path, 'must be at least {} characters'.format(min_length)))
    pattern = schema.get('pattern')
    if pattern is not None and not re.search(pattern, value):
        # A raw regex is not an error message a customer can act on. Fields with
        # a non-obvious format carry x-format-hint and use that instead.
        hint = schema.get('x-format-hint')
        if hint:
            problems.append(Problem(
                path, '{!r} is not valid here. {}'.format(value, hint)))
        else:
            problems.append(Problem(
                path, '{!r} does not match the required format ({})'.format(
                    value, pattern)))
    return problems


def _check_number(value, schema, path):
    problems = []
    minimum = schema.get('minimum')
    if minimum is not None and value < minimum:
        problems.append(Problem(path, 'must be >= {}'.format(minimum)))
    maximum = schema.get('maximum')
    if maximum is not None and value > maximum:
        problems.append(Problem(path, 'must be <= {}'.format(maximum)))
    return problems


def _check_array(value, schema, path):
    problems = []
    min_items = schema.get('minItems')
    if min_items is not None and len(value) < min_items:
        problems.append(Problem(
            path, 'needs at least {} entr{}'.format(
                min_items, 'y' if min_items == 1 else 'ies')))
    max_items = schema.get('maxItems')
    if max_items is not None and len(value) > max_items:
        problems.append(Problem(
            path, 'accepts at most {} entries'.format(max_items)))
    item_schema = schema.get('items')
    if item_schema:
        for i, item in enumerate(value):
            problems.extend(validate(item, item_schema, _index(path, i)))
    return problems


def _check_object(value, schema, path):
    problems = []
    properties = schema.get('properties') or {}
    rejected = schema.get('x-rejected') or {}
    additional = schema.get('additionalProperties', True)

    for key in schema.get('required') or []:
        if key not in value:
            problems.append(Problem(
                _join(path, key), 'is required'))

    for key in sorted(value.keys()):
        child_path = _join(path, key)
        if key in properties:
            problems.extend(validate(value[key], properties[key], child_path))
            continue
        # JSON has no comments, so an underscore prefix is the convention for
        # one. data-plane/transforms.json already uses `_doc` this way. This is
        # an explicit opt-in by the author, not a silent tolerance for typos.
        #
        # Only in an object with a fixed set of keys (additionalProperties
        # false). In a map -- table names under transforms.tables, column names
        # under rename, loader.primary_keys -- every key is data, and '_' is a
        # legal first character of a table or column name. Skipping those keys
        # left them unvalidated while the planner still emitted them.
        if key.startswith('_') and additional is False:
            continue
        if key in rejected:
            # This extension exists to say what to do instead.
            problems.append(Problem(child_path, rejected[key]))
            continue
        if additional is False:
            problems.append(Problem(
                child_path,
                'is not a recognised setting.{}'.format(
                    _suggest(key, list(properties) + list(rejected)))))
        elif isinstance(additional, dict):
            problems.extend(validate(value[key], additional, child_path))

    return problems


def defaults_for(schema):
    """Structural defaults declared in the schema, one level deep.

    Deliberately shallow. Deployment defaults (batch sizes, node types) are
    resolved at deploy time, not written into a customer's config file -- once
    they are in the file you can no longer tell what the customer chose from what
    we chose for them.
    """
    out = {}
    for key, sub in (schema.get('properties') or {}).items():
        if isinstance(sub, dict) and 'default' in sub:
            out[key] = sub['default']
    return out
