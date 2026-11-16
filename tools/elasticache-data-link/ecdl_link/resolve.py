"""Turn a link config into everything a deployer needs, using only describe calls.

The customer supplies ARNs. This module reads the endpoints, ports, engine,
credentials owner, VPC, subnets, security groups, TLS state, cluster mode and
auth mode off them, so none of that is a question anyone has to answer.

Two rules held throughout:

1. **No secret values are read.** The username comes from RDS's `MasterUsername`
   and the target's auth mode from the replication group's own flags, so nothing
   here needs `GetSecretValue`. Secrets are confirmed to exist with
   `DescribeSecret` and otherwise not accessed -- a control plane that never holds a
   credential cannot leak one.

2. **Discovered values are reported, never asked.** `resolved.notes` records what
   was found and where, so the customer can see that "TLS: enabled" came from
   their cluster rather than from a default we picked.

The per-engine table below is the single source of truth for engine differences.
Getting one of these wrong is not a small mistake: the Oracle topic-naming rule
(no database segment) once cost a benchmark run where 2,000,000 events reached the
broker and zero reached the target, because the sink was subscribed to a stream
name Debezium was not writing to.
"""

import binascii
import re

from .link import bare_table, normalize

ARN_RE = re.compile(
    r'^arn:(?P<partition>aws[a-z-]*):(?P<service>[a-z0-9-]+):'
    r'(?P<region>[a-z0-9-]*):(?P<account>[0-9]{12}):(?P<rest>.+)$')


class ResolutionError(Exception):
    """Something about the referenced infrastructure prevents deployment."""

    def __init__(self, message, hint=None):
        self.hint = hint
        self.message = message
        text = message
        if hint:
            # Keep the two as one readable sentence pair rather than running
            # them together, since this string is what a customer sees.
            if not text.rstrip().endswith(('.', '!', '?', ':')):
                text = text.rstrip() + '.'
            text = '{} {}'.format(text, hint)
        super(ResolutionError, self).__init__(text)


# ── engine facts ───────────────────────────────────────────────────────────
#
# `topic_parts` is how Debezium composes a topic name after the prefix, and
# therefore how the broker stream is named. Verified against the four working
# properties files in data-plane/debezium-server/ and against live runs:
#   MySQL       data:<prefix>.<database>.<table>
#   PostgreSQL  data:<prefix>.<schema>.<table>          (database is per connector)
#   SQL Server  data:<prefix>.<database>.<schema>.<table>   (the only 4-part one)
#   Oracle      data:<prefix>.<SCHEMA>.<TABLE>          (no database segment)

ENGINE_FACTS = {
    'mysql': {
        'label': 'MySQL',
        'port': 3306,
        'connector_class': 'io.debezium.connector.mysql.MySqlConnector',
        # MySQL has no schema separate from the database, so the database is the
        # qualifier and there is no schema.include.list.
        'database_property': 'database.include.list',
        'qualify_with': 'database',
        'topic_parts': ('database', 'table'),
        'default_schema': None,
        'uppercase_identifiers': False,
        'needs_server_id': True,
        'needs_schema_history': True,
        # Kept as the legacy alias rather than the modern `no_data` because this
        # is the value proven on 3.0.6 in this repo's working configuration.
        'no_data_snapshot_mode': 'schema_only',
        'log_name': 'binlog',
    },
    'postgresql': {
        'label': 'PostgreSQL',
        'port': 5432,
        'connector_class': 'io.debezium.connector.postgresql.PostgresConnector',
        'database_property': 'database.dbname',
        'qualify_with': 'schema',
        'topic_parts': ('schema', 'table'),
        'default_schema': 'public',
        'uppercase_identifiers': False,
        'needs_server_id': False,
        'needs_schema_history': False,
        'no_data_snapshot_mode': 'no_data',
        'log_name': 'write-ahead log',
    },
    'sqlserver': {
        'label': 'SQL Server',
        'port': 1433,
        'connector_class': 'io.debezium.connector.sqlserver.SqlServerConnector',
        # The only engine using the plural form; it can capture several databases.
        'database_property': 'database.names',
        'qualify_with': 'schema',
        'topic_parts': ('database', 'schema', 'table'),
        'default_schema': 'dbo',
        'uppercase_identifiers': False,
        'needs_server_id': False,
        'needs_schema_history': True,
        'no_data_snapshot_mode': 'no_data',
        'log_name': 'transaction log',
    },
    'oracle': {
        'label': 'Oracle',
        'port': 1521,
        'connector_class': 'io.debezium.connector.oracle.OracleConnector',
        'database_property': 'database.dbname',
        'qualify_with': 'schema',
        'topic_parts': ('schema', 'table'),
        'default_schema': None,      # defaults to the uppercased username
        'uppercase_identifiers': True,
        'needs_server_id': False,
        'needs_schema_history': False,
        'no_data_snapshot_mode': 'no_data',
        'log_name': 'redo log',
    },
}

# RDS reports a family plus an edition. Normalised to the four Debezium
# connectors. `custom-` prefixes (RDS Custom) and Aurora variants map to the
# same connector as their engine family.
_ENGINE_PREFIXES = (
    ('aurora-mysql', 'mysql'),
    ('aurora-postgresql', 'postgresql'),
    ('mysql', 'mysql'),
    ('mariadb', 'mysql'),
    ('postgres', 'postgresql'),
    ('sqlserver', 'sqlserver'),
    ('custom-sqlserver', 'sqlserver'),
    ('oracle', 'oracle'),
    ('custom-oracle', 'oracle'),
)


def engine_family(raw):
    """'aurora-mysql' -> 'mysql'. Raises for an engine with no connector."""
    if not raw:
        raise ResolutionError('the source reports no engine')
    lowered = str(raw).lower()
    # Longest prefix first so 'aurora-postgresql' does not match 'postgres'.
    for prefix, family in sorted(_ENGINE_PREFIXES, key=lambda p: -len(p[0])):
        if lowered.startswith(prefix):
            return family
    raise ResolutionError(
        "engine '{}' is not supported".format(raw),
        'ECDL captures changes from MySQL, PostgreSQL, SQL Server and Oracle.')


def default_port(engine):
    """Default port for an engine name, raw or canonical.

    Exists so nothing else has to guess. The previous control plane defaulted
    every engine to 5432, so a MySQL source saved with a blank port was stored
    pointing at the PostgreSQL port.
    """
    try:
        family = engine_family(engine)
    except ResolutionError:
        return None
    return ENGINE_FACTS[family]['port']


def parse_arn(arn):
    """Split an ARN. Raises ResolutionError on anything unparseable."""
    match = ARN_RE.match(arn or '')
    if not match:
        raise ResolutionError('{!r} is not a well-formed ARN'.format(arn))
    parts = match.groupdict()
    rest = parts.pop('rest')
    # resource-type/resource-id or resource-type:resource-id
    if ':' in rest:
        parts['resource_type'], parts['resource_id'] = rest.split(':', 1)
    elif '/' in rest:
        parts['resource_type'], parts['resource_id'] = rest.split('/', 1)
    else:
        parts['resource_type'], parts['resource_id'] = '', rest
    return parts


# ── table naming ───────────────────────────────────────────────────────────

def resolve_tables(entries, family, database, schema):
    """Expand each table entry into every name the pipeline needs.

    Accepts bare ('orders'), schema-qualified ('public.orders') and, for SQL
    Server, database-qualified ('Inventory.dbo.orders'). A name the customer
    qualified themselves takes priority over the defaults.
    """
    facts = ENGINE_FACTS[family]
    upper = facts['uppercase_identifiers']
    out = []

    for entry in entries:
        pieces = [p for p in str(entry).split('.') if p]
        if len(pieces) == 1:
            t_database, t_schema, t_table = database, schema, pieces[0]
        elif len(pieces) == 2:
            if facts['qualify_with'] == 'database':
                t_database, t_schema, t_table = pieces[0], schema, pieces[1]
            else:
                t_database, t_schema, t_table = database, pieces[0], pieces[1]
        else:
            if len(facts['topic_parts']) < 3:
                raise ResolutionError(
                    "table {!r} has three parts, which {} does not use".format(
                        entry, facts['label']),
                    'Write it as {}.'.format(
                        'database.table' if facts['qualify_with'] == 'database'
                        else 'schema.table'))
            t_database, t_schema, t_table = pieces[0], pieces[1], pieces[2]

        if upper:
            t_database = (t_database or '').upper() or None
            t_schema = (t_schema or '').upper() or None
            t_table = t_table.upper()

        if facts['qualify_with'] == 'database':
            qualified = '{}.{}'.format(t_database, t_table)
        else:
            if not t_schema:
                raise ResolutionError(
                    "cannot qualify table {!r}: no schema is known".format(entry),
                    'Set source.schema, or write the table as schema.table.')
            qualified = '{}.{}'.format(t_schema, t_table)

        available = {'database': t_database, 'schema': t_schema,
                     'table': t_table}
        topic_suffix = '.'.join(available[part] for part in facts['topic_parts'])

        out.append({
            'input': entry,
            'table': t_table,
            'schema': t_schema,
            'database': t_database,
            # What goes in Debezium's table.include.list.
            'qualified': qualified,
            # Appended to the topic prefix to form the broker stream name.
            'topic_suffix': topic_suffix,
        })
    return out


def stream_key(topic_prefix, topic_suffix):
    """The broker stream a table's events are written to.

    The `data:` prefix comes from the RegexRouter in application.properties, which
    rewrites every topic to `data:$0`.
    """
    return 'data:{}.{}'.format(topic_prefix, topic_suffix)


def cache_key_prefix(table_name, key_prefix=''):
    """How the sink names this table's keys.

    The sink builds `evt.Source.Table + ":" + pk`, so the prefix is the table name
    exactly as the source reports it -- uppercase for Oracle.
    """
    base = '{}:'.format(table_name)
    return '{}:{}'.format(key_prefix, base) if key_prefix else base


def mysql_server_id(link_name):
    """A stable, per-link MySQL replication server id.

    Must be unique across everything reading that source's binlog; the shipped
    properties file hardcodes 88888, so two links against one MySQL source would
    collide and the server would disconnect one of them. Derived from the link
    name so it is stable across redeploys and different between links.
    """
    digest = binascii.crc32(link_name.encode('utf-8')) & 0xffffffff
    return 1000000 + (digest % 8000000)


# ── AWS lookups ────────────────────────────────────────────────────────────

class Clients(object):
    """The four AWS clients resolution needs. Injectable, so tests use fakes."""

    def __init__(self, rds=None, elasticache=None, secretsmanager=None,
                 ec2=None, session=None, region=None, profile=None):
        if session is None and (rds is None or elasticache is None):
            try:
                import boto3
            except ImportError:
                raise ResolutionError(
                    'boto3 is required to read configuration from ARNs',
                    'pip install boto3')
            session = self._open_session(boto3, region, profile)

        # Building a client is where a missing region surfaces, so the
        # conversion has to cover construction and not just the session.
        self.rds = rds if rds is not None else self._client(session, 'rds')
        self.elasticache = (elasticache if elasticache is not None
                            else self._client(session, 'elasticache'))
        self.secretsmanager = (secretsmanager if secretsmanager is not None
                               else self._client(session, 'secretsmanager'))
        self.ec2 = ec2 if ec2 is not None else (
            self._client(session, 'ec2') if session else None)

    @staticmethod
    def _open_session(boto3, region, profile):
        try:
            return boto3.session.Session(region_name=region,
                                         profile_name=profile)
        except Exception as exc:  # noqa: BLE001
            credential = _credential_error(exc)
            if credential:
                raise credential
            raise ResolutionError(
                'could not open an AWS session: {}'.format(exc))

    @staticmethod
    def _client(session, name):
        try:
            return session.client(name)
        except Exception as exc:  # noqa: BLE001
            credential = _credential_error(exc)
            if credential:
                raise credential
            raise ResolutionError(
                'could not create an AWS {} client: {}'.format(name, exc))


_CREDENTIAL_MARKERS = (
    'NoCredentialsError', 'CredentialRetrievalError', 'TokenRefreshError',
    'ExpiredToken', 'InvalidClientTokenId', 'UnrecognizedClientException',
    'AuthFailure', 'ProfileNotFound', 'NoRegionError',
)


def _credential_error(exc):
    """Recognise a missing or expired credential.

    Worth separating: it is an ordinary operational state, not a defect, so it
    should read as 'refresh your credentials' rather than as an internal error.
    """
    blob = '{} {}'.format(type(exc).__name__, exc)
    if not any(marker in blob for marker in _CREDENTIAL_MARKERS):
        return None
    if 'NoRegionError' in blob:
        return ResolutionError(
            'no AWS region configured',
            'Set a region on the request, in AWS_DEFAULT_REGION, or in the '
            'profile.')
    return ResolutionError(
        'no usable AWS credentials ({})'.format(type(exc).__name__),
        'The control plane needs credentials that can describe the referenced '
        'source, target and secret.')


def _access_error(what, arn, exc):
    credential = _credential_error(exc)
    if credential:
        return credential
    name = type(exc).__name__
    text = str(exc)
    if 'AccessDenied' in name or 'AccessDenied' in text or 'not authorized' in text:
        return ResolutionError(
            'not permitted to describe the {} {}'.format(what, arn),
            'The control plane role needs describe access to it.')
    if 'NotFound' in name or 'NotFound' in text:
        return ResolutionError(
            'the {} {} does not exist'.format(what, arn),
            'Check the ARN, the account and the region.')
    return ResolutionError(
        'could not describe the {} {}: {}'.format(what, arn, text))


def resolve_source(clients, source_cfg):
    """Source facts, from RDS for an AWS source or from the link for an external one.

    The single place the two source kinds diverge. Everything after it uses
    the returned dict, so the resolver's body does not branch on where the
    database is hosted.
    """
    if (source_cfg.get('kind') or 'aws') == 'external':
        return describe_external_source(source_cfg)
    return describe_source(clients, source_cfg['arn'])


def describe_external_source(cfg):
    """The shape describe_source returns, built from the link instead of RDS.

    A self-managed or on-premises database has no ARN, so the engine, host and
    port are stated in the link. The AWS-only facts -- VPC, security groups,
    backup retention, status -- have no answer here, so they come back empty and
    every downstream guard that depends on them (the VPC-match check, the status and
    retention warnings, the source security-group ingress rule) does not
    run. Reachability is the operator's responsibility over Direct Connect or a VPN, and
    the pipeline tasks run in the target cache's VPC.
    """
    family = engine_family(cfg.get('engine'))
    return {
        'endpoint': cfg.get('host'),
        'port': cfg.get('port') or ENGINE_FACTS[family]['port'],
        'engine_raw': cfg.get('engine'),
        'engine_version': None,
        'username': cfg.get('username'),
        'database_name': cfg.get('database'),
        'security_group_ids': [],
        'backup_retention_days': None,
        'subnet_group': None,
        'serverless': False,
        'status': None,
        'vpc_id': None,
        'subnet_ids': [],
        'arn': None,
        'identifier': cfg.get('host'),
        'kind': 'external',
        'tls': cfg.get('tls', True),
        'external': True,
    }


def describe_source(clients, arn):
    """Endpoint, engine, credentials owner, VPC and security groups for a source."""
    parsed = parse_arn(arn)
    identifier = parsed['resource_id']
    kind = parsed['resource_type']

    if kind == 'cluster':
        try:
            page = clients.rds.describe_db_clusters(
                DBClusterIdentifier=identifier)
        except Exception as exc:
            raise _access_error('database cluster', arn, exc)
        items = page.get('DBClusters') or []
        if not items:
            raise ResolutionError(
                'no database cluster matches {}'.format(arn))
        c = items[0]
        endpoint = c.get('Endpoint')
        port = c.get('Port')
        security_groups = [g['VpcSecurityGroupId']
                           for g in c.get('VpcSecurityGroups') or []
                           if g.get('VpcSecurityGroupId')]
        info = {
            'endpoint': endpoint,
            'port': port,
            'engine_raw': c.get('Engine'),
            'engine_version': c.get('EngineVersion'),
            'username': c.get('MasterUsername'),
            'database_name': c.get('DatabaseName') or None,
            'security_group_ids': security_groups,
            'backup_retention_days': c.get('BackupRetentionPeriod'),
            'subnet_group': c.get('DBSubnetGroup'),
            'serverless': bool(c.get('ServerlessV2ScalingConfiguration')),
            'status': c.get('Status'),
        }
    else:
        try:
            page = clients.rds.describe_db_instances(
                DBInstanceIdentifier=identifier)
        except Exception as exc:
            raise _access_error('database instance', arn, exc)
        items = page.get('DBInstances') or []
        if not items:
            raise ResolutionError(
                'no database instance matches {}'.format(arn))
        d = items[0]
        endpoint_block = d.get('Endpoint') or {}
        info = {
            'endpoint': endpoint_block.get('Address'),
            'port': endpoint_block.get('Port'),
            'engine_raw': d.get('Engine'),
            'engine_version': d.get('EngineVersion'),
            'username': d.get('MasterUsername'),
            'database_name': d.get('DBName') or None,
            'security_group_ids': [g['VpcSecurityGroupId']
                                   for g in d.get('VpcSecurityGroups') or []
                                   if g.get('VpcSecurityGroupId')],
            'backup_retention_days': d.get('BackupRetentionPeriod'),
            'subnet_group': d.get('DBSubnetGroup'),
            'serverless': False,
            'status': d.get('DBInstanceStatus'),
        }

    # RDS is inconsistent here, and it matters: describe_db_instances returns
    # DBSubnetGroup as an object carrying VpcId and Subnets, while
    # describe_db_clusters returns it as a bare string -- the group's name. For a
    # cluster the network has to be looked up separately.
    subnet_group = info.pop('subnet_group')
    if isinstance(subnet_group, str):
        info.update(describe_db_subnet_group(clients, subnet_group))
    else:
        group = subnet_group or {}
        info['vpc_id'] = group.get('VpcId')
        info['subnet_ids'] = [s['SubnetIdentifier']
                              for s in group.get('Subnets') or []
                              if s.get('SubnetIdentifier')]

    info['arn'] = arn
    info['identifier'] = identifier
    info['kind'] = kind or 'cluster'
    return info


def describe_db_subnet_group(clients, name):
    """VPC and subnets behind a DB subnet group, looked up by name."""
    if not name:
        return {'vpc_id': None, 'subnet_ids': []}
    try:
        page = clients.rds.describe_db_subnet_groups(DBSubnetGroupName=name)
    except Exception as exc:
        credential = _credential_error(exc)
        if credential:
            raise credential
        # Not fatal on its own: the target's subnet group is a workable
        # fallback, and advanced.network can override both.
        return {'vpc_id': None, 'subnet_ids': []}
    items = page.get('DBSubnetGroups') or []
    if not items:
        return {'vpc_id': None, 'subnet_ids': []}
    group = items[0]
    return {
        'vpc_id': group.get('VpcId'),
        'subnet_ids': [s['SubnetIdentifier']
                       for s in group.get('Subnets') or []
                       if s.get('SubnetIdentifier')],
    }


def describe_target(clients, arn):
    """Endpoint, topology, TLS and auth mode for a target replication group."""
    parsed = parse_arn(arn)
    identifier = parsed['resource_id']
    try:
        page = clients.elasticache.describe_replication_groups(
            ReplicationGroupId=identifier)
    except Exception as exc:
        raise _access_error('cache replication group', arn, exc)
    items = page.get('ReplicationGroups') or []
    if not items:
        raise ResolutionError('no replication group matches {}'.format(arn))
    g = items[0]

    node_groups = g.get('NodeGroups') or []
    cluster_mode = bool(g.get('ClusterEnabled'))
    config_endpoint = g.get('ConfigurationEndpoint') or {}

    if cluster_mode:
        # Cluster mode on: one configuration endpoint, and the client must be
        # cluster-aware so it can follow slot ownership.
        endpoint = config_endpoint.get('Address')
        port = config_endpoint.get('Port')
        reader_endpoint = None
        endpoint_source = 'ConfigurationEndpoint'
    else:
        primary = (node_groups[0] if node_groups else {}).get(
            'PrimaryEndpoint') or {}
        reader = (node_groups[0] if node_groups else {}).get(
            'ReaderEndpoint') or {}
        endpoint = primary.get('Address')
        port = primary.get('Port')
        reader_endpoint = reader.get('Address')
        endpoint_source = 'PrimaryEndpoint'

    if not endpoint:
        raise ResolutionError(
            'the replication group {} reports no {}'.format(
                identifier, endpoint_source),
            'It may still be creating; wait for status "available".')

    user_groups = [u for u in g.get('UserGroupIds') or [] if u]
    if user_groups:
        # RBAC supersedes AUTH on Redis/Valkey 6 and later, so it takes priority if both
        # are reported.
        auth_mode = 'rbac'
    elif g.get('AuthTokenEnabled'):
        auth_mode = 'token'
    else:
        auth_mode = 'none'

    return {
        'arn': arn,
        'identifier': identifier,
        'endpoint': endpoint,
        'endpoint_source': endpoint_source,
        'port': port or 6379,
        'reader_endpoint': reader_endpoint,
        'cluster_mode': cluster_mode,
        'shards': len(node_groups) or 1,
        'tls': bool(g.get('TransitEncryptionEnabled')),
        'at_rest_encryption': bool(g.get('AtRestEncryptionEnabled')),
        'auth_mode': auth_mode,
        'user_group_ids': user_groups,
        'node_type': g.get('CacheNodeType'),
        'engine': g.get('Engine'),
        'engine_version': g.get('EngineVersion'),
        # Deliberately absent, BOTH of them: describe_replication_groups reports
        # neither the subnet group nor the security groups. Verified against the
        # live API -- the response has no security-group field of any kind. Both
        # are reported on a member cache cluster, so they take a two-hop lookup; see
        # describe_cache_network, which returns them together.
        #
        # This previously read g['SecurityGroups'], which is always absent, so
        # every target resolved with an EMPTY security group list. The problem went
        # unnoticed until a step needed the value: the CDC stack takes the target's security
        # group so it can add ingress from the pipeline tasks, and with no group
        # the deployment could not be planned at all.
        'status': g.get('Status'),
        'member_clusters': g.get('MemberClusters') or [],
    }


def describe_cache_network(clients, target):
    """VPC, subnets AND security groups for a target cache, via a member cluster.

    Two hops, because `describe_replication_groups` does not include
    `CacheSubnetGroupName` in its response at all -- only
    `describe_cache_clusters` on one of the `MemberClusters` has it. Returns
    `problem` when the lookup could not complete, so a caller can say the VPC
    check was skipped instead of quietly not doing it.
    """
    empty = {'vpc_id': None, 'subnet_ids': [], 'subnet_group_name': None,
             'security_group_ids': [], 'problem': None}

    members = target.get('member_clusters') or []
    if not members:
        return dict(empty, problem='the replication group reports no member '
                                   'clusters, so its subnets could not be read')

    try:
        page = clients.elasticache.describe_cache_clusters(
            CacheClusterId=members[0])
    except Exception as exc:
        credential = _credential_error(exc)
        if credential:
            raise credential
        return dict(empty, problem='could not describe cache cluster {}: {}'
                                   .format(members[0], exc))
    clusters = page.get('CacheClusters') or []
    if not clusters:
        return dict(empty, problem='cache cluster {} not found'
                                   .format(members[0]))
    # The security groups come from the SAME hop, so asking for them here costs
    # nothing extra and there is no second place for the lookup to be forgotten.
    security_group_ids = [m['SecurityGroupId']
                          for m in clusters[0].get('SecurityGroups') or []
                          if m.get('SecurityGroupId')]

    subnet_group_name = clusters[0].get('CacheSubnetGroupName')
    if not subnet_group_name:
        return dict(empty, security_group_ids=security_group_ids,
                    problem='cache cluster {} reports no subnet group'
                            .format(members[0]))

    try:
        page = clients.elasticache.describe_cache_subnet_groups(
            CacheSubnetGroupName=subnet_group_name)
    except Exception as exc:
        credential = _credential_error(exc)
        if credential:
            raise credential
        return dict(empty, subnet_group_name=subnet_group_name,
                    security_group_ids=security_group_ids,
                    problem='could not describe cache subnet group {}: {}'
                            .format(subnet_group_name, exc))
    items = page.get('CacheSubnetGroups') or []
    if not items:
        return dict(empty, subnet_group_name=subnet_group_name,
                    security_group_ids=security_group_ids,
                    problem='cache subnet group {} not found'
                            .format(subnet_group_name))

    group = items[0]
    return {
        'vpc_id': group.get('VpcId'),
        'subnet_ids': [s['SubnetIdentifier']
                       for s in group.get('Subnets') or []
                       if s.get('SubnetIdentifier')],
        'subnet_group_name': subnet_group_name,
        'security_group_ids': security_group_ids,
        'problem': None,
    }


# Services a Fargate task must reach BEFORE any of our code runs. The task role is
# irrelevant here: the ECS agent fetches the image and resolves `secrets` in the
# task definition during task startup, so if these are unreachable the container
# never executes and the only symptom is a startup timeout.
#
#   secretsmanager  the broker auth token and the source database password
#   ecr.api/ecr.dkr the images
#   logs            the awslogs driver, which fails the task if it cannot connect
# ECR also stores layers in S3, so a private-subnet deployment needs the S3
# gateway endpoint as well -- interface endpoints alone are not enough.
_TASK_STARTUP_SERVICES = ('secretsmanager', 'ecr.api', 'ecr.dkr', 'logs')


def describe_egress(clients, vpc_id, subnet_ids):
    """How -- if at all -- a task in these subnets can reach the AWS APIs.

    This exists because of a real deployment failure. The subnets resolved from
    the target cache route 0.0.0.0/0 to an internet gateway, and the plan asked
    for tasks with no public IP. An internet gateway route does nothing for an
    instance without a public address, so every task waited, unable to fetch its
    secrets and CloudFormation eventually reported only "Exceeded attempts to
    wait" -- after twenty-five minutes, with no information about the cause.

    Returns one of:
      nat        a NAT gateway route exists; private tasks work
      endpoints  interface endpoints cover startup; private tasks work
      igw        internet gateway only, so a task MUST have a public IP
      none       no viable path; deploying would hang and roll back
      unknown    the lookup could not be performed (no ec2 client or denied)
    """
    result = {'kind': 'unknown', 'detail': None, 'endpoints': [],
              'missing_endpoints': []}
    if clients is None or clients.ec2 is None or not subnet_ids:
        result['detail'] = 'no ec2 client available, so routing was not checked'
        return result

    # Interface endpoints are checked first: when they cover startup, a private
    # deployment is correct regardless of what the default route happens to be.
    try:
        page = clients.ec2.describe_vpc_endpoints(
            Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
        present = set()
        for endpoint in page.get('VpcEndpoints') or []:
            if endpoint.get('State') not in ('available', 'Available'):
                continue
            name = str(endpoint.get('ServiceName') or '')
            # com.amazonaws.us-east-2.secretsmanager -> secretsmanager
            suffix = name.split('.', 3)[-1] if name.count('.') >= 3 else name
            present.add(suffix)
        result['endpoints'] = sorted(present)
        missing = [s for s in _TASK_STARTUP_SERVICES if s not in present]
        # ECR pulls layers from S3, so the gateway endpoint counts too.
        if not missing and 's3' not in present:
            missing = ['s3']
        result['missing_endpoints'] = missing
        if not missing:
            result['kind'] = 'endpoints'
            result['detail'] = ('interface endpoints cover task startup: '
                                + ', '.join(sorted(present)))
            return result
    except Exception as exc:
        result['detail'] = 'could not list VPC endpoints: {}'.format(exc)

    # Otherwise the default route decides it.
    try:
        page = clients.ec2.describe_route_tables(
            Filters=[{'Name': 'association.subnet-id', 'Values': list(subnet_ids)}])
        tables = page.get('RouteTables') or []
        if not tables:
            # No explicit association means the subnets use the VPC main table.
            page = clients.ec2.describe_route_tables(
                Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]},
                         {'Name': 'association.main', 'Values': ['true']}])
            tables = page.get('RouteTables') or []
    except Exception as exc:
        result['detail'] = 'could not read route tables: {}'.format(exc)
        return result

    has_nat = has_igw = False
    for table in tables:
        for route in table.get('Routes') or []:
            if route.get('DestinationCidrBlock') != '0.0.0.0/0':
                continue
            if route.get('NatGatewayId'):
                has_nat = True
            elif str(route.get('GatewayId') or '').startswith('igw-'):
                has_igw = True

    if has_nat:
        result['kind'] = 'nat'
        result['detail'] = 'a NAT gateway route exists, so tasks need no public IP'
    elif has_igw:
        result['kind'] = 'igw'
        result['detail'] = ('the only egress is an internet gateway, so a task '
                            'without a public IP cannot reach Secrets Manager '
                            'or ECR')
    else:
        result['kind'] = 'none'
        result['detail'] = ('these subnets have no NAT gateway, no internet '
                            'gateway route, and no interface endpoints for '
                            + ', '.join(_TASK_STARTUP_SERVICES))
    return result


def confirm_secret(clients, arn, what):
    """Check a secret exists and is readable, without reading its value.

    `DescribeSecret` is deliberate: the control plane has no reason to hold a
    credential, and one it never fetches is one it cannot leak. The pipeline task
    reads the value itself at runtime with its own role.
    """
    try:
        meta = clients.secretsmanager.describe_secret(SecretId=arn)
    except Exception as exc:
        raise _access_error('{} secret'.format(what), arn, exc)
    return {
        'arn': meta.get('ARN') or arn,
        'name': meta.get('Name'),
        'rotation_enabled': bool(meta.get('RotationEnabled')),
        # RDS names its managed secrets rds!... and sets OwningService.
        'managed_by_rds': (str(meta.get('Name', '')).startswith('rds!')
                           or meta.get('OwningService') == 'rds'),
    }


# ── the resolver ───────────────────────────────────────────────────────────

def resolve_link(link, clients=None, region=None, profile=None):
    """Resolve a validated link into concrete deployment values.

    Returns a dict with `source`, `target`, `tables`, `debezium`, `network`,
    `notes` and `warnings`. Raises ResolutionError when the referenced
    infrastructure cannot support the link.
    """
    link = normalize(link)
    if clients is None:
        clients = Clients(region=region, profile=profile)

    notes = []
    warnings = []

    source_cfg = link.get('source') or {}
    target_cfg = link.get('target') or {}
    advanced = link.get('advanced') or {}
    mode = link.get('mode')

    src = resolve_source(clients, source_cfg)
    family = engine_family(src['engine_raw'])
    facts = ENGINE_FACTS[family]

    src['engine'] = family
    src['engine_label'] = facts['label']
    src['connector_class'] = facts['connector_class']
    src['database_property'] = facts['database_property']
    src['database'] = source_cfg['database']

    if not src['port']:
        src['port'] = facts['port']
        notes.append('source port not reported by RDS; using the {} default {}'
                     .format(facts['label'], facts['port']))
    notes.append('source: {} {} at {}:{}'.format(
        facts['label'], src.get('engine_version') or '?',
        src['endpoint'], src['port']))

    if src.get('external'):
        notes.append(
            'external {} source: reachability (Direct Connect / VPN and the '
            'source firewall opening port {} to the pipeline) and the change '
            'capture prerequisites are the operator\'s responsibility, since '
            'there is no ARN to read them from'.format(facts['label'],
                                                       src['port']))
        warnings.append(
            'change-log retention cannot be checked on an external source; '
            'ensure the {} is retained long enough for the pipeline to read it, '
            'or changes can be lost'.format(facts['log_name']))

    username = source_cfg.get('username') or src.get('username')
    if not username:
        if src.get('external'):
            raise ResolutionError(
                'the source username is required for an external source',
                'Set source.username.')
        raise ResolutionError(
            'could not determine the source username',
            'Set source.username; RDS did not report a MasterUsername.')
    src['username'] = username
    if not source_cfg.get('username'):
        notes.append("source username '{}' read from the cluster's "
                     'MasterUsername'.format(username))

    schema = source_cfg.get('schema') or facts['default_schema']
    if schema is None and facts['qualify_with'] == 'schema':
        # Oracle: the schema is the user's own, uppercased.
        schema = username.upper()
        notes.append("Oracle schema defaulted to '{}' from the username"
                     .format(schema))
    elif schema and not source_cfg.get('schema'):
        notes.append("schema defaulted to '{}' for {}".format(
            schema, facts['label']))
    src['schema'] = schema

    if src.get('status') and src['status'] != 'available':
        warnings.append('the source reports status {!r}; deployment may fail '
                        'until it is available'.format(src['status']))

    if src.get('backup_retention_days') == 0:
        warnings.append(
            'source backup retention is 0 days. Change capture reads the {}, '
            'and with retention off those logs may be purged before the '
            'pipeline reads them, which loses changes irrecoverably.'
            .format(facts['log_name']))

    src['secret'] = confirm_secret(clients, source_cfg['secret_arn'], 'source')
    notes.append('source credential: {} (value never read by the control '
                 'plane)'.format(src['secret']['name']))

    tgt = describe_target(clients, target_cfg['arn'])
    notes.append('target: {} shard{} at {}:{}, read from {}'.format(
        tgt['shards'], '' if tgt['shards'] == 1 else 's',
        tgt['endpoint'], tgt['port'], tgt['endpoint_source']))
    notes.append('target cluster mode: {} (detected)'.format(
        'enabled' if tgt['cluster_mode'] else 'disabled'))
    notes.append('target TLS: {} (detected)'.format(
        'enabled' if tgt['tls'] else 'disabled'))
    notes.append('target auth: {} (detected)'.format(tgt['auth_mode']))

    if tgt.get('status') and tgt['status'] != 'available':
        warnings.append('the target reports status {!r}'.format(tgt['status']))

    if not tgt['tls']:
        warnings.append(
            'the target does not have in-transit encryption enabled, so row '
            'data will cross the VPC in plaintext. That is a property of the '
            'cache and cannot be changed after creation.')

    if tgt['auth_mode'] == 'none' and target_cfg.get('secret_arn'):
        warnings.append(
            'target.secret_arn is set but the cache has neither an auth token '
            'nor RBAC enabled, so the credential will not be used.')
    if tgt['auth_mode'] != 'none' and not target_cfg.get('secret_arn'):
        raise ResolutionError(
            'the target cache requires {} authentication but no '
            'target.secret_arn was given'.format(tgt['auth_mode']),
            'Put the credential in Secrets Manager and reference it. For RBAC '
            'the secret needs both a username and a password.')
    if target_cfg.get('secret_arn'):
        tgt['secret'] = confirm_secret(
            clients, target_cfg['secret_arn'], 'target')

    # Both stores must be in one VPC. Failing here is more useful than failing
    # during deployment with a connection timeout.
    cache_net = describe_cache_network(clients, tgt)
    tgt['vpc_id'] = cache_net['vpc_id']
    tgt['subnet_group_name'] = cache_net['subnet_group_name']
    # From the member-cluster hop as well: the replication group does not report
    # security groups either, so this is the only place they can come from.
    tgt['security_group_ids'] = cache_net['security_group_ids']
    source_vpc = src.get('vpc_id')
    target_vpc = cache_net['vpc_id']
    if cache_net['problem']:
        # Say so rather than skipping the check in silence. An unverified VPC
        # match shows up later as a connection timeout with no explanation.
        warnings.append(
            'could not confirm the source and target share a VPC: {}. '
            'Deployment will proceed but will fail if they do not.'
            .format(cache_net['problem']))
    if source_vpc and target_vpc and source_vpc != target_vpc:
        raise ResolutionError(
            'the source is in {} and the target is in {}'.format(
                source_vpc, target_vpc),
            'ECDL runs its tasks in one VPC and connects to both. Cross-VPC '
            'would need peering and routing that this stack does not create.')

    network_cfg = advanced.get('network') or {}
    vpc_id = network_cfg.get('vpc_id') or target_vpc or source_vpc
    subnet_ids = (network_cfg.get('subnet_ids')
                  or cache_net['subnet_ids']
                  or src.get('subnet_ids') or [])
    if not vpc_id:
        raise ResolutionError(
            'could not determine a VPC for the pipeline tasks',
            'Set advanced.network.vpc_id.')
    if not subnet_ids:
        raise ResolutionError(
            'could not determine subnets for the pipeline tasks',
            'Set advanced.network.subnet_ids.')
    if not network_cfg.get('subnet_ids'):
        notes.append('pipeline subnets taken from the {} subnet group: {}'
                     .format('cache' if cache_net['subnet_ids'] else 'database',
                             ', '.join(subnet_ids)))

    tables = resolve_tables(link.get('tables') or [], family,
                            src['database'], src['schema'])
    topic_prefix = link['name']
    key_prefix = advanced.get('key_prefix') or ''
    for entry in tables:
        entry['stream'] = stream_key(topic_prefix, entry['topic_suffix'])
        entry['key_prefix'] = cache_key_prefix(entry['table'], key_prefix)

    if facts['uppercase_identifiers']:
        notes.append('Oracle folds identifiers to upper case, so cache keys are '
                     '{} rather than lower case'.format(
                         ', '.join(sorted(t['key_prefix'] for t in tables))))

    debezium_cfg = advanced.get('debezium') or {}
    snapshot_mode = debezium_cfg.get('snapshot_mode')
    if not snapshot_mode:
        # The parallel loader does the data in both loading modes, so Debezium
        # captures schema only and streams from the recorded position.
        snapshot_mode = facts['no_data_snapshot_mode']
    server_id = None
    if facts['needs_server_id']:
        server_id = debezium_cfg.get('server_id') or mysql_server_id(
            link['name'])
        notes.append('MySQL server id {} derived from the link name; it must be '
                     'unique among everything reading this binlog'.format(
                         server_id))

    # Signalling. Qualified by the engine's own rule, and named per link because
    # Debezium requires the signalling collection to be unique to a connector
    # instance -- two links sharing one would read each other's signals and
    # snapshot each other's tables. See ecdl_link/signals.py.
    from .signals import (SignalError, signal_collection, signal_table_name,
                          topic_pattern)
    try:
        signal_coll = signal_collection(
            family, link['name'],
            database=src.get('database'),
            schema=src.get('schema'),
            override=debezium_cfg.get('signal_collection'))
    except SignalError as exc:
        # Reported like every other resolution problem, not as a traceback.
        raise ResolutionError(str(exc), exc.hint)
    notes.append(
        'day-2 changes use the signalling table {}, which must exist on the '
        'source before deploying'.format(signal_coll))

    debezium = {
        'topic_prefix': topic_prefix,
        'signal_collection': signal_coll,
        'signal_table': signal_table_name(link['name']),
        # Control traffic the connector must NOT emit as data: the heartbeat and
        # the signalling table's own inserts. Both would otherwise accumulate in the
        # broker with no consumer and read as a stalled backlog on the dashboard.
        'signal_topic_pattern': topic_pattern(topic_prefix, signal_coll),
        'connector_class': facts['connector_class'],
        'database_property': facts['database_property'],
        'table_include_list': ','.join(t['qualified'] for t in tables),
        'schema_include_list': (
            ','.join(sorted({t['schema'] for t in tables if t['schema']}))
            if facts['qualify_with'] == 'schema' and family == 'oracle'
            else None),
        'snapshot_mode': snapshot_mode,
        'server_id': server_id,
        'needs_schema_history': facts['needs_schema_history'],
        # initial_load never starts Debezium at all.
        'enabled': mode != 'initial_load',
    }

    resolved = {
        'schema_version': link.get('schema_version', 1),
        'name': link['name'],
        'mode': mode,
        'source': src,
        'target': tgt,
        'tables': tables,
        'debezium': debezium,
        'network': {'vpc_id': vpc_id, 'subnet_ids': subnet_ids,
                    # Recorded at resolve time so the plan can decide whether
                    # tasks need a public IP from the actual routing rather than
                    # from a default that is often incorrect.
                    'egress': describe_egress(clients, vpc_id, subnet_ids)},
        'components': {
            'loader': mode in ('initial_load', 'initial_load_and_cdc'),
            'broker': mode != 'initial_load',
            'debezium': mode != 'initial_load',
            'sink': mode != 'initial_load',
        },
        'notes': notes,
        'warnings': warnings,
        'link': link,
    }
    return resolved


def describe_resolution(resolved):
    """Human-readable summary, for `ecdl` output and the UI review step."""
    lines = ['{} ({})'.format(resolved['name'], resolved['mode'])]
    for note in resolved['notes']:
        lines.append('  - {}'.format(note))
    tables = resolved['tables']
    lines.append('  tables ({}):'.format(len(tables)))
    for entry in tables:
        lines.append('    {:<28} -> {:<44} keys {}*'.format(
            entry['qualified'], entry['stream'], entry['key_prefix']))
    created = [name for name, on in sorted(resolved['components'].items()) if on]
    lines.append('  creates: {}'.format(', '.join(created) or 'nothing'))
    for warning in resolved['warnings']:
        lines.append('  ! {}'.format(warning))
    return '\n'.join(lines)
