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

The target must use `maxmemory-policy noeviction`. The default ElastiCache
parameter groups use `volatile-lru`, so create a custom parameter group with
`maxmemory-policy` set to `noeviction` and apply it to the cache. With
`noeviction` a full cache refuses writes, which the pipeline holds and retries.
Under `allkeys-*` a full cache deletes cached rows without any error, so a deploy
is refused; under `volatile-*` it deletes the delete markers first, so a deploy
warns. The control plane reads the policy with
`elasticache:DescribeCacheParameters`.

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

**The pipeline tasks may read only secrets marked for ECDL.** Tag the source
and target secrets `ecdl:link` (any value, for example the link name):
`aws secretsmanager tag-resource --secret-id <arn> --tags Key=ecdl:link,Value=<link>`.
Or, if the control plane was deployed with `PipelineSecretArns`, list them
there instead (then the tag is not used). Use the list for a secret you cannot
tag. Without either, the tasks fail to start with `ResourceInitializationError`;
the deploy preflight warns first.

A secret that RDS does not manage may be plaintext (only the password) or
JSON. For a JSON one, name the key that holds the password:

```yaml
source:
  secret_arn: arn:aws:secretsmanager:us-east-2:123456789012:secret:shop-db-a1b2c3
  secret_json_key: password
```

Without it the whole JSON document is sent as the password and every login
fails; Debezium refuses to start and says so. An RDS-managed secret needs
nothing: its `password` key is used automatically.

**A secret encrypted with your own KMS key** (not `aws/secretsmanager`) works
if the key is in the same account and its key policy lets IAM policies grant
access (the default key policy does). The task roles may decrypt with it only
through Secrets Manager and only for the link's own secrets. A key in another
account must name the task execution role in its key policy.

The **secret must already exist and hold the password**. The control plane only
calls `DescribeSecret` — it confirms the credential is reachable and never reads
it. The pipeline tasks read the value at runtime with their own roles. So a secret
that exists but holds the wrong password passes validation and fails later, in the
task log, with an authentication error from the database.

The database user needs read access to the tables in the link, plus the
replication privileges in the next section.

**The initial load connects to the source over verified TLS** (MySQL and
PostgreSQL). The source's certificate must chain to the Amazon RDS CA bundle
or a public CA, and name the endpoint the loader connects to; the loader image
carries both (`/app/source-ca.pem`). An RDS or Aurora endpoint, or RDS Proxy,
needs nothing more. An external source with a certificate from a private CA
fails with a certificate error: run a loader image with that CA added and point
`SOURCE_TLS_CA_FILE` at it. An external source without TLS needs `tls: false`
on the link, which turns TLS off for the load (`SOURCE_TLS=false`).

When the control plane can connect to the source, the deploy preflight checks
that every table and every column the transforms name exists. A table
qualified with another schema (`sales.orders`) or MySQL database
(`Archive.orders`) is checked in that schema or database. If the preflight
cannot read it (the link's user may not see it), it warns instead of blocking.

---

## 4. Change capture must be enabled on the source

Every table a link streams needs a primary key (or, failing that, a column
named `id`): the sink caches each row under it. The preflight refuses a
streaming link with a table that has neither, since every change to it would
be dead-lettered.

### MySQL / Aurora MySQL — verified end to end

| Setting | Required value | If it is wrong |
|---|---|---|
| `binlog_format` | `ROW` | Debezium cannot decode statement-based entries |
| `binlog_row_image` | `FULL` | `MINIMAL` omits unchanged columns, so the cached row loses fields that did not change in that write |
| binlog retention | set it — see below | **unrecoverable loss** |
| `gtid-mode` and `enforce_gtid_consistency` | `ON` and `ON` | After a change of writer (blue/green switchover, failover, restore) capture resumes at the wrong binlog position and can skip changes. **The deploy preflight blocks** when it reads `OFF` |

Aurora sets these in the **cluster** parameter group, not the instance one.

User privileges, the complete list (tested with a user that had only these):

```sql
GRANT SELECT ON Inventory.* TO 'cdc'@'%';
GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'cdc'@'%';
GRANT SELECT, INSERT ON Inventory.ecdl_signal_<link> TO 'cdc'@'%';
```

(`REPLICATION SLAVE` is the literal MySQL privilege name and cannot be changed.)
`INSERT` on the [signalling table](#5-the-signalling-table-must-exist-before-the-link-is-deployed)
is needed because an incremental snapshot writes its markers into it;
`ecdl signal ddl` prints that grant with the table's statement.

Debezium takes **no lock** on the writer. On a start without a stored
position it reads only the table definitions, for a few seconds, with
`snapshot.locking.mode=none`; Debezium's default would take
`FLUSH TABLES WITH READ LOCK` (or `LOCK TABLES`), which, queued behind a long
query, stops every write. So `RELOAD` and `LOCK TABLES` are not needed. Avoid
DDL on the captured tables during a first deploy's start: a change made in
those seconds can be recorded twice, which Debezium tolerates. The exception is
a `cdc` link overridden to have Debezium read the data itself (an `initial`
snapshot): then Debezium keeps its default lock, and the user also needs
`LOCK TABLES` on the database (and `RELOAD`, where the global lock is allowed).

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
For an `initial_load_and_cdc` deploy the preflight also estimates the pause:
the catalogue's row counts at a conservative 20,000 rows/s, plus 5 minutes per
table and 15 minutes for pausing and resuming. Over half the retention is a
warning; over all of it blocks the deploy (`ignore_preflight` overrides) and
names the setting to raise. An unknown row count or retention is a warning.

**GTIDs are required** because the position Debezium stores is otherwise a
binlog file name and offset, which only mean something on the server that wrote
them. A new writer has its own binlog, so the stored position points at the
wrong place or at nothing. With GTIDs on, Debezium finds its place on any
server. Turning them on is a parameter group change and a reboot (Aurora: the
cluster parameter group). An RDS instance that already has read replicas needs
the stepped procedure in the RDS guide,
[Enabling GTID-based replication for existing read replicas](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/mysql-replication-gtid.configuring-existing-read-replicas.html).
The preflight cannot check MariaDB, which has no `gtid_mode`. See
[Switchovers, blue/green and major upgrades](operations.md#switchovers-bluegreen-and-major-upgrades).

`server_id` is derived from the link name, so two links against one MySQL source
do not collide. Nothing to configure — but if something *else* replicating from
that source uses a colliding id, MySQL disconnects one of them.

### PostgreSQL / Aurora PostgreSQL — from configuration, not a live deployment

- `rds.logical_replication=1` (Aurora) or `wal_level=logical`.
- A replica identity on each captured table: `REPLICA IDENTITY FULL`, or
  `DEFAULT` with a primary key. The preflight reads it and refuses `NOTHING` and
  `DEFAULT` without a primary key: once such a table is in the publication,
  PostgreSQL rejects every UPDATE and DELETE on it. It prints the
  `ALTER TABLE ... REPLICA IDENTITY FULL;` to run.
- Large values (TOAST). With `DEFAULT`, an UPDATE that leaves a large text,
  jsonb or bytea value unchanged does not carry it, and Debezium sends
  `__debezium_unavailable_value` instead. The sink skips that field, so the
  cached value stays. A filter, computed field or denormalize rule that reads
  such a column sees no value for that change, so the preflight warns on tables
  with these columns and prints the `REPLICA IDENTITY FULL` statement.
- A publication per link, `ecdl_<link>_pub`, holding every captured table **and
  the link's signalling table**. PostgreSQL sends Debezium only the tables in
  it: a table missing from it is never captured, and a signal written to a
  signalling table missing from it is never seen, so a backfill is silently
  ignored. Debezium does not create it. `ecdl signal ddl` prints the
  `CREATE PUBLICATION` (or `ALTER PUBLICATION ... ADD TABLE`). When the
  control plane can read the source, the preflight refuses a deploy whose
  publication is missing a table, and prints the statement to run. Links
  deployed before per-link publications keep reading `dbz_pub`; check and
  alter that one for them. The publication must publish `truncate`, which it
  does unless it was created with a `publish` list that leaves it out;
  otherwise a `TRUNCATE` never reaches the cache.
- User privileges: `SELECT` on the captured tables, `INSERT` on the signalling
  table (the grant `ecdl signal ddl` prints), and replication: on RDS and Aurora
  `GRANT rds_replication TO <user>;`, elsewhere `ALTER ROLE <user> REPLICATION;`.
  Without replication the connector cannot create or read its slot and stops
  at start.
- The slot moves on while the captured tables are idle. PostgreSQL sends
  Debezium only changes to the publication's tables, and Debezium confirms a
  WAL position only for what it receives, so with idle captured tables and busy
  other tables (or RDS's own housekeeping) the slot used to stay put and WAL
  piled up until the disk filled. Every heartbeat (10 seconds) now also writes
  a logical decoding message (`pg_logical_emit_message`), which reaches
  Debezium through the slot and moves it on. It is part of no table, needs no
  grant, changes no data, and never reaches the broker. It needs PostgreSQL 14
  or later; older versions send Debezium empty transactions instead, which move
  the slot too.
- Each link creates its own replication slot, `ecdl_<link>` (links deployed
  before per-link slots keep `ecdl_slot`). Once a link has stored a position,
  Debezium refuses to start if its slot is gone: a new slot would start at the
  current WAL position and skip every change since. Slots are not carried over
  by a blue/green switchover or a major version upgrade; see
  [Switchovers, blue/green and major upgrades](operations.md#switchovers-bluegreen-and-major-upgrades).
- **`max_slot_wal_keep_size`** (PostgreSQL 13+): `-1` (unlimited) or larger than
  the WAL (write-ahead log) your longest pause or outage produces. If capture falls behind by more
  than a finite limit, PostgreSQL INVALIDATES the slot and those changes are
  lost; the preflight warns on it, with the estimated load pause next to it.
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
- A user with Debezium's documented grants (Debezium 3.0 Oracle connector,
  "Creating the connector's LogMiner user"): `CREATE SESSION`, `SET CONTAINER`
  (CDB only), `FLASHBACK ANY TABLE`, `SELECT ANY TABLE`, `SELECT_CATALOG_ROLE`,
  `EXECUTE_CATALOG_ROLE`, `SELECT ANY TRANSACTION`, `LOGMINING`,
  `CREATE TABLE` and a tablespace quota (the connector keeps a LogMiner flush
  table of its own), `LOCK ANY TABLE`, `CREATE SEQUENCE`, `EXECUTE` on
  `DBMS_LOGMNR` and `DBMS_LOGMNR_D`, and `SELECT` on `V_$DATABASE`, `V_$LOG`,
  `V_$LOG_HISTORY`, `V_$LOGMNR_LOGS`, `V_$LOGMNR_CONTENTS`,
  `V_$LOGMNR_PARAMETERS`, `V_$LOGFILE`, `V_$ARCHIVED_LOG`,
  `V_$ARCHIVE_DEST_STATUS`, `V_$TRANSACTION`, `V_$MYSTAT` and `V_$STATNAME`.
  On RDS, grant the `V_$` views with `rdsadmin.rdsadmin_util.grant_sys_object`.
  A missing grant stops Debezium at start with the Oracle error.
- Identifiers fold to uppercase, so cache keys are `ORDERS:123`, not `orders:123`.
- LOB columns (CLOB, BLOB) are captured (`lob.enabled=true`). An UPDATE that
  does not change a LOB carries a placeholder for it, which the sink skips, so
  the cached value stays. Large LOBs need more Debezium memory.

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

The table is qualified (`Inventory.ecdl_signal_<link>` on MySQL), so it lands in
the source database whichever database your session is using. On MySQL the
output also has `GRANT SELECT, INSERT` on it for the CDC user; check the host
part (`'%'`) matches how that user was created.

On PostgreSQL the output also has the publication statement (the signalling
table must be in the link's publication) and `GRANT INSERT` on the signalling
table, because Debezium writes its snapshot markers into it. On PostgreSQL,
when the source can be read, the preflight refuses a deploy without the
signalling table.

On the other engines, skipping this does not stop the link deploying. It stops you **changing** it
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

The repositories are created with immutable tags and scan-on-push, so a version
cannot be moved once pushed. `push` stops if the working tree has uncommitted
changes or an existing repository has mutable tags (it prints the command that
fixes it), and turns scan-on-push on where it is off. It writes the digests of
all five images to `infra/images.lock`, or stops if it cannot read one; commit
that file, for anyone who wants to pin one deployment by digest through the
`*Image` parameters.

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

Every ARN in the link (source, target and both secrets) must be in the region
and account you resolve with (`--region` / `--profile`, or the control plane's
own). `resolve` refuses any other, because each resource is looked up by name in
that region and account, and a resource with the same name there would be used
instead. If the account cannot be read from STS, only the region is checked, and
a warning says so.
