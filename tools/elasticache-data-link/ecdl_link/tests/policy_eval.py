"""Evaluate rendered IAM policy statements against one request.

policy_render.render() turns a template's policy into the JSON IAM sees; this
answers "would IAM allow this call?" for it, including the condition
operators the ECDL templates use. It is the identity-policy part of IAM's
logic only: an explicit Deny wins, otherwise any matching Allow allows, and a
request matched by no statement is denied. Condition keys absent from the
request fail every operator except Null (and the ...IfExists forms), as in IAM.
"""
import fnmatch


def _as_list(value):
    if value is None:
        return []
    return [value] if isinstance(value, (str, dict)) else list(value)


def _like(value, pattern):
    return fnmatch.fnmatchcase(value, pattern)


_OPERATORS = {
    'StringEquals': lambda v, w: v == w,
    'StringLike': _like,
    'ArnEquals': lambda v, w: v == w,
    'ArnLike': _like,
}


def _condition_holds(condition, context):
    for op, checks in (condition or {}).items():
        base = op[:-len('IfExists')] if op.endswith('IfExists') else op
        for key, wanted in checks.items():
            wanted = [str(w) for w in _as_list(wanted)]
            present = key in context
            if base == 'Null':
                # "Null": "true" holds when the key is absent.
                if (wanted[0].lower() == 'true') == present:
                    return False
                continue
            if not present:
                if op.endswith('IfExists'):
                    continue
                return False
            test = _OPERATORS[base]
            values = _as_list(context[key])
            if not any(test(v, w) for v in values for w in wanted):
                return False
    return True


def _matches(statement, action, resource, context):
    if 'NotAction' in statement:
        if any(_like(action, a) for a in _as_list(statement['NotAction'])):
            return False
    elif not any(_like(action, a) for a in _as_list(statement.get('Action'))):
        return False
    if 'NotResource' in statement:
        if any(_like(resource, r) for r in _as_list(statement['NotResource'])):
            return False
    elif not any(_like(resource, r) for r in _as_list(statement.get('Resource'))):
        return False
    return _condition_holds(statement.get('Condition'), context)


def allows(statements, action, resource, context=None):
    """True when these statements allow `action` on `resource`."""
    context = context or {}
    allowed = False
    for st in statements:
        if not _matches(st, action, resource, context):
            continue
        if st['Effect'] == 'Deny':
            return False
        allowed = True
    return allowed


def statements_of(documents):
    """All statements of one or more rendered policy documents."""
    out = []
    for doc in documents:
        out.extend(_as_list(doc.get('Statement')))
    return out
