"""Does another link already write these cache keys into this target?

Keys are <table>:<pk> (or <key_prefix>:<table>:<pk>). Two links that copy a
table of the same name into the same cache therefore write the same keys: each
overwrites the other's rows, and whichever bulk load runs second can fill the
cache with a table that was not meant to be there. Found deploying a second link
from a different database into the target of a running one -- the load failed
with "OOM command not allowed" and the first link's rows were mixed with rows
from another source. No check had reported that the two would collide.

Read from CloudFormation, which is the record of what is deployed: every
ecdl-<link>-cdc and ecdl-<link>-loader stack names its target and its tables in
its parameters. The link being planned is excluded, so redeploying a link is
not a conflict with itself.
"""

ACTIVE = (
    'CREATE_IN_PROGRESS', 'CREATE_COMPLETE', 'ROLLBACK_IN_PROGRESS',
    'ROLLBACK_FAILED', 'ROLLBACK_COMPLETE', 'DELETE_FAILED',
    'UPDATE_IN_PROGRESS', 'UPDATE_COMPLETE_CLEANUP_IN_PROGRESS',
    'UPDATE_COMPLETE', 'UPDATE_FAILED', 'UPDATE_ROLLBACK_IN_PROGRESS',
    'UPDATE_ROLLBACK_FAILED', 'UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS',
    'UPDATE_ROLLBACK_COMPLETE', 'IMPORT_COMPLETE',
)
_SUFFIXES = ('-cdc', '-loader')


def _link_of(stack_name):
    if not stack_name.startswith('ecdl-'):
        return None
    for suffix in _SUFFIXES:
        if stack_name.endswith(suffix):
            return stack_name[len('ecdl-'):-len(suffix)] or None
    return None


def key_prefixes(tables_param, key_prefix=''):
    """The key prefixes a stack's Tables and KeyPrefix parameters produce."""
    lead = str(key_prefix or '').strip().rstrip(':')
    lead = lead + ':' if lead else ''
    out = set()
    for table in str(tables_param or '').split(','):
        table = table.strip()
        if table:
            out.add('{}{}:'.format(lead, table.split('.')[-1]))
    return out


def deployed_key_owners(cloudformation, exclude_link=None):
    """{(target_host, key_prefix): {link, ...}} for every deployed link."""
    owners = {}
    paginator_kwargs = {'StackStatusFilter': list(ACTIVE)}
    token = None
    names = []
    while True:
        if token:
            paginator_kwargs['NextToken'] = token
        page = cloudformation.list_stacks(**paginator_kwargs)
        names += [s.get('StackName') or '' for s in page.get('StackSummaries') or []]
        token = page.get('NextToken')
        if not token:
            break
    for name in names:
        link = _link_of(name)
        if not link or link == exclude_link:
            continue
        try:
            stack = cloudformation.describe_stacks(StackName=name)['Stacks'][0]
        except Exception:  # noqa: BLE001 -- deleted between list and describe
            continue
        params = {p.get('ParameterKey'): p.get('ParameterValue')
                  for p in stack.get('Parameters') or []}
        host = (params.get('TargetHost') or '').strip().lower()
        if not host:
            continue
        for prefix in key_prefixes(params.get('Tables'), params.get('KeyPrefix')):
            owners.setdefault((host, prefix), set()).add(link)
    return owners


def key_conflicts(cloudformation, link_name, target_host, prefixes):
    """Other links that already write any of `prefixes` into `target_host`.

    Returns [{'link': name, 'key_prefixes': [...]}], sorted, empty when clear.
    """
    owners = deployed_key_owners(cloudformation, exclude_link=link_name)
    host = (target_host or '').strip().lower()
    clash = {}
    for prefix in prefixes:
        for other in owners.get((host, prefix), ()):
            clash.setdefault(other, set()).add(prefix)
    return [{'link': other, 'key_prefixes': sorted(found)}
            for other, found in sorted(clash.items())]


def describe_conflict(conflict, target_host):
    return ('pipeline {!r} already writes {} into this cache ({}). Both would '
            'write the same keys and overwrite each other\'s rows.'.format(
                conflict['link'],
                ', '.join(p + '*' for p in conflict['key_prefixes']),
                target_host))


CONFLICT_HINT = ('Use a different target cache, or set advanced.key_prefix on '
                 'this link (keys become <prefix>:<table>:<pk>).')


def resolution_conflicts(cloudformation, resolution):
    """Conflicts for a resolved link, as plain dicts ready to report."""
    link = resolution.get('link') or {}
    target = resolution.get('target') or {}
    prefixes = [t.get('key_prefix') for t in resolution.get('tables') or []
                if t.get('key_prefix')]
    return key_conflicts(cloudformation, link.get('name'),
                         target.get('endpoint'), prefixes)
