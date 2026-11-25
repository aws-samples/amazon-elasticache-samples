"""List what a link could point at, so that users do not have to look up an ARN.

`resolve.py` answers "what is this ARN?". This module answers the question that
comes first: "what have I got?". Together they let a form ask for a source, a
credential and a target by name, and derive everything else.

Three rules, each of them the result of getting it wrong somewhere else first:

1. **Nothing usable is hidden.** An unsupported engine or a cluster that is still
   creating is returned with `usable: false` and a reason, not filtered out. A
   list that silently omits the database someone is looking for turns a plain
   answer ("Aurora Serverless v1 has no binlog we can read") into an unanswerable
   question ("why is my database not in the list?").

2. **No secret values are read.** Listing credentials uses `ListSecrets`, which
   returns names and ARNs only. The one call that could return a value is never
   made here. `resolve.py` holds the same rule and a test asserts it.

3. **The Aurora member instance risk is called out.** A member instance of an Aurora
   cluster has its own ARN and its own endpoint, and picking it looks like it
   works. It pins the pipeline to one instance, so a failover leaves capture
   pointed at a demoted node. Members are returned marked, with the cluster to
   use instead.
"""

import os

from .resolve import (
    ENGINE_FACTS,
    Clients,
    ResolutionError,
    engine_family,
    parse_arn,
)

# A page cap so a large account cannot turn a dropdown into a several-megabyte
# response. Everything here also takes a `contains` filter, which is the better
# tool once an account is big enough for the cap to apply.
DEFAULT_LIMIT = 200


def _pages(callable_, key, **kwargs):
    """Walk a Marker/NextMarker paginated describe call.

    Written out rather than using boto3's paginators because `Clients` accepts
    injected fakes for testing, and a fake would then have to implement
    `get_paginator` as well as the call itself.
    """
    marker = None
    while True:
        params = dict(kwargs)
        if marker:
            params['Marker'] = marker
        page = callable_(**params)
        for item in page.get(key) or []:
            yield item
        marker = page.get('Marker') or page.get('NextMarker')
        if not marker:
            return


def _token_pages(callable_, key, **kwargs):
    """Walk a NextToken paginated call. Secrets Manager uses this shape."""
    token = None
    while True:
        params = dict(kwargs)
        if token:
            params['NextToken'] = token
        page = callable_(**params)
        for item in page.get(key) or []:
            yield item
        token = page.get('NextToken')
        if not token:
            return


def _matches(text, contains):
    if not contains:
        return True
    return contains.lower() in str(text or '').lower()


def _engine_info(raw):
    """Engine family and label, or a reason the engine cannot be used."""
    try:
        family = engine_family(raw)
    except ResolutionError as exc:
        return None, None, exc.message
    return family, ENGINE_FACTS[family]['label'], None


# ── sources ────────────────────────────────────────────────────────────────

def list_sources(clients, contains=None, limit=DEFAULT_LIMIT):
    """Every RDS cluster and instance that could be a link source.

    Clusters come first because for Aurora the cluster is almost always the right
    answer: its endpoint follows failover, while a member instance's does not.
    """
    items = []
    problems = []

    try:
        for cluster in _pages(clients.rds.describe_db_clusters, 'DBClusters'):
            item = _source_from_cluster(cluster)
            if _source_matches(item, contains):
                items.append(item)
    except Exception as exc:  # noqa: BLE001
        problems.append(_list_problem('database clusters', exc))

    try:
        for instance in _pages(clients.rds.describe_db_instances, 'DBInstances'):
            item = _source_from_instance(instance)
            if _source_matches(item, contains):
                items.append(item)
    except Exception as exc:  # noqa: BLE001
        problems.append(_list_problem('database instances', exc))

    # Usable first, then clusters before instances, then by name, so the entry
    # someone most likely wants is not below thirty they cannot use.
    items.sort(key=lambda s: (not s['usable'], s['kind'] != 'cluster',
                              s['identifier']))
    truncated = len(items) > limit
    return {
        'sources': items[:limit],
        'count': min(len(items), limit),
        'total': len(items),
        'truncated': truncated,
        'problems': problems,
    }


def _source_matches(item, contains):
    if not contains:
        return True
    return (_matches(item['identifier'], contains)
            or _matches(item.get('engine_label'), contains)
            or _matches(item.get('database'), contains))


def _source_from_cluster(cluster):
    family, label, engine_problem = _engine_info(cluster.get('Engine'))
    status = cluster.get('Status')
    blockers = []
    if engine_problem:
        blockers.append(engine_problem)
    if status != 'available':
        blockers.append('the cluster is {}, not available'.format(status))

    # Serverless v1 is a different product from v2 and exposes no binlog to read,
    # so CDC cannot work against it. v2 is an ordinary Aurora cluster and fine.
    if (cluster.get('EngineMode') or '') == 'serverless':
        blockers.append(
            'Aurora Serverless v1 does not expose a replication log, so change '
            'capture cannot read it (Serverless v2 can)')

    master_secret = (cluster.get('MasterUserSecret') or {}).get('SecretArn')

    return {
        'arn': cluster.get('DBClusterArn'),
        'identifier': cluster.get('DBClusterIdentifier'),
        'kind': 'cluster',
        'engine_raw': cluster.get('Engine'),
        'engine': family,
        'engine_label': label,
        'engine_version': cluster.get('EngineVersion'),
        'endpoint': cluster.get('Endpoint'),
        'port': cluster.get('Port'),
        'username': cluster.get('MasterUsername'),
        # Aurora usually reports this as null, which is exactly
        # why `source.database` is a required field in the link schema instead of
        # something we claim to discover.
        'database': cluster.get('DatabaseName') or None,
        'status': status,
        'multi_az': bool(cluster.get('MultiAZ')),
        'serverless_v2': bool(cluster.get('ServerlessV2ScalingConfiguration')),
        # When RDS manages the admin password it names the secret outright, so
        # the credential step can be pre-answered instead of asked.
        'master_user_secret_arn': master_secret,
        'usable': not blockers,
        'blockers': blockers,
    }


def _source_from_instance(instance):
    family, label, engine_problem = _engine_info(instance.get('Engine'))
    status = instance.get('DBInstanceStatus')
    endpoint = instance.get('Endpoint') or {}
    cluster_id = instance.get('DBClusterIdentifier')

    blockers = []
    if engine_problem:
        blockers.append(engine_problem)
    if status != 'available':
        blockers.append('the instance is {}, not available'.format(status))

    notes = []
    # Review finding M7: resolve refuses both, so they are blockers here too.
    if cluster_id:
        blockers.append(
            'this is a member of Aurora cluster {}: point the link at the '
            'cluster, so capture follows a failover rather than staying on a '
            'demoted instance'.format(cluster_id))
    replica_of = (instance.get('ReadReplicaSourceDBInstanceIdentifier')
                  or instance.get('ReadReplicaSourceDBClusterIdentifier'))
    if replica_of:
        blockers.append('this is a read replica of {}: change capture reads the '
                        'primary'.format(replica_of))

    master_secret = (instance.get('MasterUserSecret') or {}).get('SecretArn')

    return {
        'arn': instance.get('DBInstanceArn'),
        'identifier': instance.get('DBInstanceIdentifier'),
        'kind': 'instance',
        'engine_raw': instance.get('Engine'),
        'engine': family,
        'engine_label': label,
        'engine_version': instance.get('EngineVersion'),
        'endpoint': endpoint.get('Address'),
        'port': endpoint.get('Port'),
        'username': instance.get('MasterUsername'),
        'database': instance.get('DBName') or None,
        'status': status,
        'multi_az': bool(instance.get('MultiAZ')),
        'serverless_v2': False,
        'master_user_secret_arn': master_secret,
        'cluster_identifier': cluster_id,
        'aurora_member': bool(cluster_id),
        'usable': not blockers,
        'blockers': blockers,
        'notes': notes,
    }


# ── targets ────────────────────────────────────────────────────────────────

def list_targets(clients, contains=None, limit=DEFAULT_LIMIT):
    """Every ElastiCache replication group a link could write to.

    Each entry carries the two facts that change what else the form must ask:
    whether auth is enabled (so a target credential is required) and whether
    cluster mode is on (so the sink and loader need cluster-aware clients).
    """
    items = []
    problems = []
    try:
        for group in _pages(clients.elasticache.describe_replication_groups,
                            'ReplicationGroups'):
            item = _target_from_group(group)
            if (_matches(item['identifier'], contains)
                    or _matches(item.get('engine'), contains)):
                items.append(item)
    except Exception as exc:  # noqa: BLE001
        problems.append(_list_problem('cache replication groups', exc))

    items.sort(key=lambda t: (not t['usable'], t['identifier']))
    return {
        'targets': items[:limit],
        'count': min(len(items), limit),
        'total': len(items),
        'truncated': len(items) > limit,
        'problems': problems,
    }


def _is_ecdl_broker(group, prefix='ecdl'):
    """Whether a replication group is a broker ECDL provisioned for itself.

    Two signals, both already present in describe_replication_groups, so this
    costs no extra API call. Tags would be the simplest signal -- the broker
    template sets Project=ECDL and Role=broker-cdc -- but tags are not included in
    that response, and ListTagsForResource per group is both an extra round trip
    each and an extra IAM action the control plane does not currently hold.

    Neither signal alone is enough:
      * the id test misses a broker whose name does not end that way, and the
        deployed demo broker is exactly that case ('ecdl-demo-broker-cache');
      * the description test misses a broker created before the template set one,
        or one whose description was edited.
    """
    identifier = (group.get('ReplicationGroupId') or '').lower()
    description = (group.get('Description') or '').lower()
    named_like_one = (identifier.startswith(prefix.lower() + '-')
                      and identifier.endswith('-broker'))
    describes_itself = 'ecdl cdc broker' in description
    return named_like_one or describes_itself


def _target_from_group(group):
    status = group.get('Status')
    node_groups = group.get('NodeGroups') or []
    cluster_mode = bool(group.get('ClusterEnabled'))


    if cluster_mode:
        endpoint = (group.get('ConfigurationEndpoint') or {}).get('Address')
    else:
        first = node_groups[0] if node_groups else {}
        endpoint = (first.get('PrimaryEndpoint') or {}).get('Address')

    user_groups = [u for u in group.get('UserGroupIds') or [] if u]
    if user_groups:
        auth_mode = 'rbac'
    elif group.get('AuthTokenEnabled'):
        auth_mode = 'token'
    else:
        auth_mode = 'none'

    blockers = []
    if status != 'available':
        blockers.append('the cache is {}, not available'.format(status))
    if not endpoint:
        blockers.append(
            'it reports no {} yet'.format(
                'configuration endpoint' if cluster_mode else 'primary endpoint'))
    if _is_ecdl_broker(group):
        # A broker is ECDL's own queue, not somewhere a link may write. Choosing
        # one as a target would write CDC rows into the broker's keyspace next to the
        # stream and the Debezium offset and schema-history keys that pipeline
        # depends on -- so it would corrupt a working link rather than fail.
        blockers.append(
            'this is an ECDL broker, not a target: it holds the change stream '
            'and the capture offsets for a pipeline, so writing cached rows into '
            'it would corrupt that pipeline')

    notes = []
    if auth_mode != 'none':
        # Stated as a requirement rather than left to fail at first write. A sink
        # with no credential against an authenticated cache used to connect,
        # report itself healthy, and dead-letter everything.
        notes.append(
            'this cache requires authentication ({}), so the link needs '
            'target.secret_arn'.format(auth_mode))
    if not group.get('TransitEncryptionEnabled'):
        notes.append('in-transit encryption is off, so cache traffic is '
                     'unencrypted inside the VPC')

    return {
        'arn': group.get('ARN'),
        'identifier': group.get('ReplicationGroupId'),
        'description': group.get('Description'),
        'endpoint': endpoint,
        'cluster_mode': cluster_mode,
        'shards': len(node_groups) or 1,
        'node_type': group.get('CacheNodeType'),
        'engine': group.get('Engine'),
        'engine_version': group.get('EngineVersion'),
        'tls': bool(group.get('TransitEncryptionEnabled')),
        'auth_mode': auth_mode,
        'needs_secret': auth_mode != 'none',
        'status': status,
        'usable': not blockers,
        'blockers': blockers,
        'notes': notes,
    }


# ── credentials ────────────────────────────────────────────────────────────

def list_secrets(clients, contains=None, limit=DEFAULT_LIMIT):
    """Secrets Manager entries, by name and ARN only.

    `ListSecrets` returns metadata; it cannot return a value. That is why this
    is a listing endpoint rather than anything richer: the control
    plane hands the ARN to ECS, which reads the value at task start. Nothing in
    the control plane ever needs to see it.
    """
    items = []
    problems = []
    try:
        for secret in _token_pages(clients.secretsmanager.list_secrets,
                                   'SecretList'):
            name = secret.get('Name')
            if not (_matches(name, contains)
                    or _matches(secret.get('Description'), contains)):
                continue
            items.append({
                'arn': secret.get('ARN'),
                'name': name,
                'description': secret.get('Description'),
                # RDS-managed admin passwords are named `rds!cluster-...` and
                # are the correct choice when present, so they are worth marking.
                'rds_managed': str(name or '').startswith('rds!'),
                'rotation_enabled': bool(secret.get('RotationEnabled')),
                # A secret pending deletion still lists, and using one produces a
                # task that cannot start.
                'deleted_pending': bool(secret.get('DeletedDate')),
            })
    except Exception as exc:  # noqa: BLE001
        problems.append(_list_problem('secrets', exc))

    items.sort(key=lambda s: (s['deleted_pending'], str(s['name'] or '')))
    return {
        'secrets': items[:limit],
        'count': min(len(items), limit),
        'total': len(items),
        'truncated': len(items) > limit,
        'problems': problems,
    }


# Words that appear in so many secret and database names that matching on them
# says nothing. Without this, every `*-db-password` in an account looks like a
# match for every database.
_WEAK_TOKENS = frozenset((
    'db', 'database', 'password', 'passwd', 'pass', 'secret', 'credentials',
    'credential', 'creds', 'user', 'username', 'master', 'admin', 'rds',
    'cluster', 'dbcluster', 'dbinstance', 'instance', 'writer', 'reader',
    'primary', 'prod', 'dev', 'test', 'staging', 'token', 'auth', 'key',
))


def _tokens(text):
    """Split a name into comparable words, dropping the ones that carry no signal."""
    raw = []
    current = ''
    for char in str(text or '').lower():
        if char.isalnum():
            current += char
        else:
            if current:
                raw.append(current)
            current = ''
    if current:
        raw.append(current)
    return {t for t in raw if len(t) >= 3 and t not in _WEAK_TOKENS}


def secrets_for_source(secrets, source):
    """Rank secrets by how likely they are to be the source's credential.

    An account can hold hundreds of secrets and only one of them is the
    credential for this database. Ordering by likelihood is the difference between a dropdown that
    answers the question and one that merely contains the answer.

    Scored on shared words, not substrings. Substring matching was the first
    attempt and it failed against real data: a CloudFormation-created database is
    called something like `ecdl-demo-source-dbcluster-xxxxxxxxxxxx`, its secret is
    called `ecdl-demo-db-password`, and neither name contains the other. Since
    almost every database in a real account is created by a stack, that is the
    common case rather than the edge one. Shared words find it -- `ecdl` and
    `demo` -- while the random suffix matches nothing.

    `likely` means "the best match, and there is a match", so it stays useful in
    an account where every secret shares a project prefix.
    """
    source = source or {}
    managed = source.get('master_user_secret_arn')
    wanted = _tokens(source.get('identifier'))
    username = str(source.get('username') or '').lower()

    scored = []
    for secret in secrets:
        entry = dict(secret)                # never mutate the caller's list
        name = str(entry.get('name') or '')
        shared = wanted & _tokens(name)

        if managed and entry.get('arn') == managed:
            # RDS states outright that this secret holds this database's password.
            # A declared match takes priority over any inferred one.
            entry['match_reason'] = 'RDS manages this database\'s password with it'
            entry['_rank'] = (0, 0)
        elif shared:
            entry['match_reason'] = 'shares {} with the database name'.format(
                ', '.join(sorted(shared)))
            entry['_rank'] = (1, -len(shared))
        elif username and username in _tokens(name):
            entry['match_reason'] = 'names the database user {!r}'.format(username)
            entry['_rank'] = (2, 0)
        else:
            entry['match_reason'] = None
            entry['_rank'] = (3, 0)
        scored.append(entry)

    scored.sort(key=lambda s: (s['_rank'], str(s.get('name') or '')))

    best = scored[0]['_rank'] if scored else None
    for entry in scored:
        # Declared takes priority over inferred, and among inferred only the strongest match is
        # worth pointing at -- a "likely" badge on four entries is not a hint.
        entry['likely'] = (entry['_rank'] == best and entry['_rank'][0] < 3)
        del entry['_rank']
    return scored


def _list_problem(what, exc):
    """A listing failure that should not stop the rest of the page from loading.

    Listing four kinds of thing means four chances to be denied one of them. A
    missing permission on secrets should not stop someone choosing a source, so
    each failure is reported against its own list and the rest still render.
    """
    name = type(exc).__name__
    text = str(exc)
    if 'AccessDenied' in name or 'AccessDenied' in text or 'not authorized' in text:
        return {
            'what': what,
            'error': 'not permitted to list {}'.format(what),
            'hint': 'The control plane role needs the matching describe or list '
                    'permission. Everything else on this page still works.',
        }
    return {'what': what,
            'error': 'could not list {}: {}'.format(what, text),
            'hint': None}


# ── the whole picture in one call ──────────────────────────────────────────

def discover(clients=None, contains=None, limit=DEFAULT_LIMIT, region=None,
             profile=None):
    """Sources, targets and credentials together.

    One call because a form needs all three to render, and three round trips to
    populate one page is three chances to be half-loaded.
    """
    if clients is None:
        clients = Clients(region=region, profile=profile)

    sources = list_sources(clients, contains=contains, limit=limit)
    targets = list_targets(clients, contains=contains, limit=limit)
    # Credentials are not filtered by `contains`: the filter is there to find a
    # database by name, and the secret for it rarely shares that name.
    secrets = list_secrets(clients, limit=limit)

    return {
        'sources': sources['sources'],
        'targets': targets['targets'],
        'secrets': secrets['secrets'],
        'counts': {
            'sources': sources['total'],
            'targets': targets['total'],
            'secrets': secrets['total'],
        },
        'truncated': {
            'sources': sources['truncated'],
            'targets': targets['truncated'],
            'secrets': secrets['truncated'],
        },
        'problems': (sources['problems'] + targets['problems']
                     + secrets['problems']),
    }


def region_of(arn):
    """The region of an ARN, or None. Used to keep a link in one region."""
    try:
        return parse_arn(arn)['region'] or None
    except ResolutionError:
        return None


# ---------------------------------------------------------------------------
# The control plane's own security group
# ---------------------------------------------------------------------------
#
# Needed so a deployed broker will admit the process that has to read it. The
# broker holds every queue-shaped figure the dashboard reports -- stream depth,
# consumer-group lag, pending entries, DLQ length, Debezium's committed offset --
# and the CDC stack grants only the pipeline's own task security group. Without
# this the pipeline is healthy and unobservable at the same time, which is the
# most misleading state it can be in.
#
# Explicit configuration wins, because it always works and can be reviewed. The
# IMDS lookup exists so that the common case (control plane on EC2) needs no
# configuration at all -- a manual step to look up the security group id
# is easy to miss.

_MONITOR_SG_ENV = 'ECDL_MONITOR_SECURITY_GROUP_ID'
_IMDS_HOST = '169.254.169.254'
_IMDS_PREFIX = '/latest'
_IMDS_TIMEOUT = 1.0
_monitor_sg_cache = []


def _imds_security_group_ids(timeout=_IMDS_TIMEOUT):
    """Security group ids of this EC2 instance, via IMDSv2. [] if not on EC2.

    Short timeout and total silence on failure: this runs on the deploy path, and
    not being on EC2 is a normal outcome, not an error worth surfacing.
    """
    try:
        import http.client

        # A plain HTTP connection to the fixed link-local IMDS address, not a URL
        # opener: there is no scheme to parse, so nothing can turn this into a
        # file:// or custom-scheme read.
        def fetch(path, headers=None, method='GET'):
            conn = http.client.HTTPConnection(_IMDS_HOST, timeout=timeout)
            try:
                conn.request(method, _IMDS_PREFIX + path, headers=headers or {})
                resp = conn.getresponse()
                body = resp.read().decode('utf-8').strip()
                if resp.status != 200:
                    raise OSError('IMDS {} returned {}'.format(path, resp.status))
                return body
            finally:
                conn.close()

        # IMDSv2 requires a token, and instances are commonly configured to
        # REQUIRE it, so v1 is not attempted.
        token = fetch('/api/token', method='PUT',
                      headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'})
        auth = {'X-aws-ec2-metadata-token': token}
        mac = fetch('/meta-data/network/interfaces/macs/', headers=auth)
        mac = mac.splitlines()[0].strip().rstrip('/')
        # security-group-ids, not security-groups: the latter returns NAMES, and
        # a name is not what CloudFormation needs.
        raw = fetch('/meta-data/network/interfaces/macs/{}/security-group-ids'
                    .format(mac), headers=auth)
        return [line.strip() for line in raw.splitlines() if line.strip()]
    except Exception:                                     # noqa: BLE001
        return []


def control_plane_security_group(env=None):
    """The security group a deployed broker should admit, or ''.

    Order: explicit environment variable, then IMDS. Cached, because it cannot
    change while the process is running and the deploy path should not make a
    metadata request per link.

    Returns '' rather than raising when it cannot be determined: a link that
    deploys with reduced observability is better than a link that refuses to
    deploy, and plan() already attaches a warning saying exactly what will be
    missing.
    """
    if _monitor_sg_cache:
        return _monitor_sg_cache[0]
    environ = os.environ if env is None else env
    configured = (environ.get(_MONITOR_SG_ENV) or '').strip()
    if not configured:
        ids = _imds_security_group_ids()
        # One interface can carry several groups and any of them grants access,
        # so any of them works and the first is used. Deterministic for reviewability.
        configured = sorted(ids)[0] if ids else ''
    _monitor_sg_cache.append(configured)
    return configured


def reset_control_plane_security_group_cache():
    """Drop the cached lookup. For tests."""
    del _monitor_sg_cache[:]
