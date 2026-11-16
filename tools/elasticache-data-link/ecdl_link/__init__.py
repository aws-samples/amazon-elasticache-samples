"""Shared definition of an ECDL link configuration.

Imported by both the control-plane API (`backend/`) and the CLI (`ecdl-cli/ecdl`)
so that a config accepted by one is accepted by the other. `link.schema.json` is
the single definition; everything else here interprets it.

    from ecdl_link import validate_link, LinkValidationError

    try:
        link = validate_link(document)
    except LinkValidationError as exc:
        print(exc.format())
"""

from .jsonschema_mini import Problem
from .link import (
    LinkValidationError,
    bare_table,
    canonical_json,
    for_display,
    load_file,
    loads,
    normalize,
    problems_for,
    schema,
    to_yaml,
    validate_file,
    validate_link,
)

__all__ = [
    'LinkValidationError',
    'Problem',
    'bare_table',
    'canonical_json',
    'for_display',
    'load_file',
    'loads',
    'normalize',
    'problems_for',
    'schema',
    'to_yaml',
    'validate_file',
    'validate_link',
]
