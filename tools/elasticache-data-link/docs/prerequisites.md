# Prerequisites

What has to be true before a link will deploy and stay correct. Each item is here
because it has actually broken something, and each says what the failure looks like
— a prerequisite you can only recognise after it has caused a failure does not help you prevent it.

**How confident each section is.** MySQL/Aurora MySQL has been deployed and
verified end to end: initial load, live change data capture (CDC), adding a table, and backfill by signal.
PostgreSQL, SQL Server and Oracle requirements come from the connector
configuration in `data-plane/debezium-server/` and from the benchmark runs (see
[benchmark results](benchmark-results.md)), **not** from a full link deployment. Where that distinction
matters it is marked.

---

## 1. The target cache must already exist

A link references its target by ARN (Amazon Resource Name):

```yaml
target:
  arn: arn:aws:elasticache:us-east-2:123456789012:replicationgroup:my-cache
```

ECDL does **not** create the target. In this model the source and the target are
your resources — ECDL creates the broker and the pipeline, and writes into a cache
you own.

> Earlier documentation offered `target.endpoint: auto` to create a cache. That is
> gone. `ecdl validate` rejects it and names the replacement.

Everything else about the target is **detected from the ARN, not configured**:

| Detected | Why it is not a setting |
|---|---|
| endpoint | Configuration endpoint when cluster mode is on, primary endpoint when off. Choosing wrongly is a silent failure — see below. |
| cluster mode | A property of the replication group. A plain client aimed at a cluster-mode endpoint connects, then returns `MOVED` (a redirect to the shard that owns the key) for most keys, and a plain client does not follow `MOVED`. With 3 shards about two thirds of writes fail and dead-letter while the sink reports itself healthy. |
| TLS | From `TransitEncryptionEnabled`. |
| auth mode | none / auth token / RBAC (role-based access control), from `AuthTokenEnabled` and `UserGroupIds`. |
| shards | Counted from the node groups. |

If the target has an auth token or an RBAC user, put its secret in
`target.secret_arn`. Without it the sink exits at startup saying the target
requires authentication and naming the field to set — it does not start and
dead-letter everything.

---

## 2. The pipeline tasks must be able to reach the AWS APIs

**This requirement caused the longest delays in testing, so it is first among
the network requirements.** Debezium, the sink and the loader run as Fargate tasks. Before any
ECDL code runs, the ECS agent fetches the container images and resolves the
`secrets` in the task definition. If it cannot reach Secrets Manager and ECR, the
task never starts, and the only symptom is CloudFormation reporting
`Exceeded attempts to wait` after about twenty minutes.

One of these must be true for the subnets the pipeline runs in:

- a **NAT gateway** route, or
- **interface VPC endpoints** for `secretsmanager`, `ecr.api`, `ecr.dkr` and
  `logs`, **plus the S3 gateway endpoint** (ECR stores layers in S3, so the
  interface endpoints alone are not enough), or
- the subnets route to an **internet gateway** *and* the tasks get a public IP.

`ecdl resolve` reports which of these it found, as `network.egress`:

```
task egress: igw
the only egress is an internet gateway, so a task without a public IP cannot
reach Secrets Manager or ECR
```

`ecdl plan` then chooses `AssignPublicIp` from that, prefers no public IP whenever
NAT or endpoints make it possible, and **refuses to plan at all** when there is no
route of any kind rather than deploying something guaranteed to hang.

Also required, and normally already true because the subnets come from the cache's
own subnet group:

- the tasks' security group can reach the **source** on its port,
- the tasks' security group can reach the **target** on 6379,
- source, target and broker are in the **same VPC**.

The CDC stack adds the ingress rules it needs to the target and broker security
groups itself.

---

## 3. The source must be readable, and its credential must exist

```yaml
source:
  arn: arn:aws:rds:us-east-2:123456789012:cluster:my-cluster
  database: Inventory
  secret_arn: arn:aws:secretsmanager:us-east-2:123456789012:secret:shop-db-a1b2c3
```

`database` is required and cannot be derived: RDS reports `DatabaseName` as null
on most Aurora clusters.

**If the cluster uses an RDS-managed admin password, use that secret** (the
cluster's `MasterUserSecret`, named `rds!cluster-...`). RDS rotates it, and a
copy of the password kept in another secret goes stale at the first rotation:
the link then deploys and fails to log in. Find it with
`aws rds describe-db-clusters --db-cluster-identifier <id> --query 'DBClusters[0].MasterUserSecret.SecretArn'`.

The **secret must already exist and hold the password**. The control plane only
calls `DescribeSecret` — it confirms the credential is reachable and never reads
it. The pipeline tasks read the value at runtime with their own roles. So a secret
that exists but holds the wrong password passes validation and fails later, in the
task log, with an authentication error from the database.

The database user needs read access to the tables in the link, plus the
replication privileges in the next section.

---

## 4. Change capture must be enabled on the source

### MySQL / Aurora MySQL — verified end to end

| Setting | Required value | If it is wrong |
|---|---|---|
| `binlog_format` | `ROW` | Debezium cannot decode statement-based entries |
| `binlog_row_image` | `FULL` | `MINIMAL` omits unchanged columns, so the cached row loses fields that did not change in that write |
| binlog retention | set it — see below | **unrecoverable loss** |

Aurora sets these in the **cluster** parameter group, not the instance one.

User privileges: `SELECT`, `REPLICATION SLAVE`, `REPLICATION CLIENT`.
(`REPLICATION SLAVE` is the literal MySQL privilege name and cannot be changed.)

**Binlog retention is the one prerequisite with no recovery.** If a binlog file is
purged before Debezium reads it, those changes are gone; no retry, no backfill of
that window, and nothing in the pipeline reports it as loss because the events
never arrived. On RDS it is **not configured by default** — verified on this
cluster, where `mysql.rds_show_configuration` returned NULL:

```sql
CALL mysql.rds_set_configuration('binlog retention hours', 72);
```

The deploy preflight reads this back and warns when it is NULL or under 24 hours,
and the dashboard says so when it is unset. Pick a window longer than the longest
outage you need to recover from, **and longer than your bulk load**: capture is
paused for the whole load, and changes made meanwhile wait in the binlog.

`server_id` is derived from the link name, so two links against one MySQL source
do not collide. Nothing to configure — but if something *else* replicating from
that source uses a colliding id, MySQL disconnects one of them.

### PostgreSQL / Aurora PostgreSQL — from configuration, not a live deployment

- `rds.logical_replication=1` (Aurora) or `wal_level=logical`.
- `REPLICA IDENTITY FULL` on each captured table. Without it an UPDATE's old
  values are absent, which matters for row filters that depend on the previous
  value.
- A publication covering exactly the captured tables (default name `dbz_pub`;
  several links may share it, since each connector filters by its own tables).
- Each link creates its own replication slot, `ecdl_<link>` (links deployed
  before per-link slots keep `ecdl_slot`).
- **`max_slot_wal_keep_size`** (PostgreSQL 13+): `-1` (unlimited) or larger than
  the WAL (write-ahead log) your longest pause or outage produces. If capture falls behind by more
  than a finite limit, PostgreSQL INVALIDATES the slot and those changes are
  lost; the preflight warns on it.
- **Drop replication slots you stop using.** An inactive slot retains WAL
  indefinitely and will fill the volume; `ecdl destroy` prints the
  `pg_drop_replication_slot` statement, and the preflight lists inactive slots.
  This has caused outages in this project before.

### SQL Server — from configuration and benchmark 08

- **SQL Server Agent must be running.** CDC captures nothing without it, silently.
- `sys.sp_cdc_enable_db`, then `sys.sp_cdc_enable_table` per table.
- A user that can read the `cdc` schema.
- **Set `@pollinginterval = 0` on the capture job.** The shipped default is 5
  seconds, which measured **4,859 ms** p50 (median) freshness; `0` measured **18.3 ms** —
  a 265x difference from one setting. See
  [benchmark results](benchmark-results.md).
- `encrypt=false` or `trustServerCertificate=true` in the connection: JDBC 10+
  defaults to `encrypt=true`, and a self-signed certificate fails PKIX certificate-path validation before the
  connector reads anything.
- Monitor `COUNT(source) − COUNT(cdc.*_CT)` yourself. When the capture job is
  stopped, `sys.dm_cdc_errors` stays **empty** — the engine does not tell you.
- **Change-table retention.** The CDC cleanup job deletes change-table rows
  older than its `retention` (default 4320 minutes, 3 days). Changes Debezium
  has not read by then are lost. Keep it longer than your longest pause or
  outage: `EXEC sys.sp_cdc_change_job @job_type = 'cleanup', @retention = <minutes>;`
  (check with `EXEC sys.sp_cdc_help_jobs;`). The preflight cannot check this one
  yet, because it does not connect to SQL Server sources.

### Oracle — preview

Works as a source (see the [benchmark results](benchmark-results.md)),
and the bulk loader has an Oracle adapter (python-oracledb in thin mode, no
Oracle client needed), but it has not yet been deployed through a link.

- ARCHIVELOG mode, `archivelog retention hours` set (24 or more, for the same
  reason as MySQL binlog retention).
- `ALTER TABLE <t> ADD SUPPLEMENTAL LOG DATA (ALL) COLUMNS` per captured table —
  database-level minimal logging is not enough.
- A user with `CREATE SESSION`, `LOGMINING`, `SELECT ANY DICTIONARY`,
  `SELECT ANY TRANSACTION`.
- Identifiers fold to uppercase, so cache keys are `ORDERS:123`, not `orders:123`.

---

## 5. The signalling table must exist before the link is deployed

Day-2 changes (changes made after the first deploy) — adding a table, changing a transform — are applied by asking
Debezium for an incremental snapshot, and it takes that request from a table in
**your** database. One per link, because Debezium requires the signalling
collection to be unique per connector instance.

Get the statement and run it yourself:

```bash
ecdl signal ddl my-link.yaml
```

ECDL emits the DDL (the CREATE TABLE statement) rather than executing it: it changes your database, and the
control plane holds no database credential by design.

Skipping this does not stop the link deploying. It stops you **changing** it
later: without a signalling collection, an added table would only ever receive
changes made after the update, and every row that already existed would never
reach the cache. `ecdl diff` says so explicitly rather than letting the snapshot
step fail.

---

## 6. Images must be published

The templates pull every image from **your account's own ECR** at the version in
`ImageTag` (`<account>.dkr.ecr.<region>.amazonaws.com/ecdl/<image>:<version>`).
Nothing is published centrally, so publish them once per account and region
before the first deploy; otherwise the tasks fail with `CannotPullContainerError`:

```bash
./release.sh push     # builds multi-arch (amd64 + arm64), pushes, writes images.lock
./release.sh pin      # sets each template's ImageTag default to VERSION
```

The repositories are created with immutable tags, so a version cannot be moved
once pushed. `infra/images.lock` records the digests, for anyone who wants to pin
one deployment by digest through the `*Image` parameters.

---

## 7. Decide who can reach the control plane

Anyone who can use the control-plane UI can deploy and delete pipelines with its
IAM role, which can create CloudFormation stacks, ECS services and IAM roles
(scoped to `ecdl-*` names). Choose `AccessMode` before you create the stack:
`ssm` (the default, IAM-controlled), `load-balancer` (your own HTTPS load
balancer with SSO) or `cidr` (a network range, no login). The options, and what
you need for each, are in
[Getting started, section 2](getting-started.md#2-deploy-the-control-plane).

The control plane reads **no database password** unless you list the secret in
`MonitorSourceSecretArns`, which turns on the monitor's source-side panels for
that source.

---

## Quick check

```bash
ecdl validate my-link.yaml   # shape and semantics, no AWS calls
ecdl resolve  my-link.yaml   # reads the ARNs: engine, endpoints, topology,
                             # auth mode, egress, per-table stream and key names
ecdl plan     my-link.yaml   # the ordered deployment, changing nothing
```

`resolve` is the command that detects most of the problems on this page. It reports what it found
rather than what you asked for, so the endpoint, cluster mode, TLS, auth mode and
egress on screen are the ones the pipeline will actually use.
