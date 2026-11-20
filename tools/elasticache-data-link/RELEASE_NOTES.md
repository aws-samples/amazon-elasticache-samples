# Release notes
## 0.2.31
Fixes the known vulnerabilities in the images and their dependencies.
(0.2.30 was published with the same changes except the Alpine Python
images, and is superseded; do not deploy it.)
### Check before you upgrade
- **Update the control plane before any link**, as for 0.2.29.
- **Debezium runs on a different base image.** The image keeps upstream's
  Debezium Server 3.0.6 files unchanged but runs them on the current Eclipse
  Temurin 21 JRE (Ubuntu) instead of upstream's Red Hat UBI 8 with Java 21.0.5.
  It runs as the same uid (185). Nothing in a link changes.
- **The UI is built with Vite.** Create React App is unmaintained and pinned
  build tools with known vulnerabilities. The served files and their paths
  are the same. Developers: `npm ci`, then `npx vitest run` and
  `npx vite build` in `frontend/` (Node 22).
### Fixed
- `npm audit`: 0 vulnerabilities (was 54: 3 critical, 31 high). axios 1.21.1.
- Image OS packages: every image applies the OS fixes published since its base
  image at build time, and `release.sh push` builds without a layer cache so
  each push takes that day's fixes. The sink builds with Go 1.27 on Alpine
  3.24.
- The API and the loader run on Python's Alpine 3.24 images. Debian 13's glibc
  had findings Debian does not plan to fix.
- cfn-guard: the control-plane instance has detailed (one-minute) monitoring,
  about $2 a month.
- checkov (Dockerfiles): every image has a `HEALTHCHECK`.

## 0.2.29
Fixes the Medium and Low findings of a code review (Jira stories F009-F198; code comments name the story each change belongs to).
### Check before you upgrade
- **Update the control plane before any link.** A link update now creates a
  small Lambda function that restarts the tasks when a secret changes; the
  control plane's deploy role is what allows that. In load-balancer mode,
  `UiHostNames` is still required (0.2.26).
- **The loader verifies TLS to the source.** It connects with certificate and
  host-name checks (PostgreSQL `verify-full`) against the Amazon RDS CA
  bundle and the system CAs. A source with a private or self-signed
  certificate fails the load; set `source.tls: false` for an external source
  that cannot present a verifiable certificate.
- **Data Sources need a secret ARN.** The older Data Sources page no longer
  stores passwords. Each source must name a Secrets Manager secret
  (`password_secret_arn`); a stored password is blanked on start and a source
  without a secret cannot connect.
- **JSON secrets that are not RDS-managed need `source.secret_json_key`.**
  Without it such a secret is sent whole as the password and the login fails.
- **Settings that never took effect are now refused.** Every setting the
  link schema accepts now reaches a stack, or validation refuses it and says
  why (for example `advanced.sink.cpu`, `debezium.max_queue_size`,
  `broker.engine_version`). Remove them from link files before the next apply.
- **Names with `$` are refused**, along with any character outside letters,
  digits, `_` and `-` in table, schema and database names. Debezium renamed
  their streams, so they never worked.
- **`snapshot_mode: initial` with `mode: initial_load_and_cdc` is refused.**
  Both the loader and Debezium loaded every table. Use `mode: cdc`.
- **Expressions use one grammar for both engines.** A filter or computed
  column that one engine used to accept and the other refused is now refused
  on both.
### Behaviour changes
- **New links get a larger broker:** `cache.m7g.large` with three nodes (about
  $345 a month in us-east-2, was about $46). Running brokers keep their size;
  set `advanced.broker.node_type` and `replicas` to choose.
- **Tables with no key are blocked** at deploy for `cdc` and
  `initial_load_and_cdc` links. Every change of such a table was a dead
  letter.
- **Deleting a link refuses while the broker holds changes** not yet in the
  cache, or dead letters. The UI shows what is at risk and asks for a
  checkbox; the CLI needs `--delete-backlog` with `--yes`; the API needs
  `"delete_backlog": true`. A delete also refuses while a stack is updating,
  and stops running loader tasks first. The teardown keeps the last error
  lines of each log group in its result.
- **A key clash with another deployed link is refused** even with
  `ignore_preflight`. Set `advanced.key_prefix` or use another cache.
- **A new secret value restarts the tasks that use it**: a new source secret
  restarts Debezium, a new target secret restarts the sink. This includes a
  value changed by hand. For a target credential, add the new one beside the
  old, update the secret, wait for the sink, then remove the old one
  ([operations](docs/operations.md)).
- **Port 5000 is closed in cidr mode.** The UI calls the API through
  `:3000/api`; scripts should use `http://<ip>:3000/api/`. The SSM tunnel is
  unchanged (`ecdl-ui --api-port 5000` still forwards the API).
- **MySQL capture takes no table lock** when it reads no data (the default):
  `snapshot.locking.mode=none`. The documented grants are now complete.
  `SnapshotMode` defaults to `no_data`; `recovery` rebuilds the schema
  history without a reload (not PostgreSQL).
- **PostgreSQL slots move while tables are idle** (heartbeat action query),
  so WAL no longer accumulates on the source.
- **Debezium gets 75% of the task memory for its heap** and exits on
  `OutOfMemoryError`, so ECS restarts it.
- **The Randomizer API is off** unless the API runs with
  `ECDL_ENABLE_RANDOMIZER=true`; the page is hidden.
- **The monitor shows no source row values** without the
  `MonitorSourceSecretArns` opt-in, and sends the `DEMO_*` passwords only when
  no link is watched. Cached secrets expire after 5 minutes.
- **New alarms:** sink silent, new dead letters, dead letters dropped at
  `DLQ_MAX_LEN`, capture paused for longer than planned, the third broker
  node's replication lag and failover. A planned pause no longer counts as
  capture down. The wizard has an alarm email field, and the monitor warns
  when alarms reach nobody.
- **The UI shows a paused pipeline as paused**, and a rolled-back update as
  failed.
- **A failed load names its last error** and its log stream.
- **Images and dependencies are pinned** by digest and hash; the API runs as
  a non-root user. `release.sh push` refuses a dirty tree and repositories
  with MUTABLE tags, and writes `infra/images.lock`.
- **Each control-plane release is gated**: the update waits for the new API
  to report healthy, and rolls back if it does not.

## 0.2.26

### Check before you upgrade

- **MySQL sources must have GTIDs on.** Check `gtid-mode` and
  `enforce_gtid_consistency` in the source's parameter group (Aurora: the
  cluster parameter group); both must be `ON`. Without GTIDs, after a writer
  change (failover, blue/green switchover, restore) Debezium resumes at a binlog
  position that only meant something on the old writer, and can skip changes.
  The deploy preflight now blocks a MySQL source with GTIDs off. Turn them on
  before your next deploy: it is a parameter group change and a reboot. An RDS
  instance with read replicas needs the stepped procedure in
  [Enabling GTID-based replication](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/mysql-replication-gtid.configuring-existing-read-replicas.html).
  See [prerequisites](docs/prerequisites.md). `ignore_preflight` deploys anyway;
  use it only if you accept that risk.
- **Pipeline tasks read only the secrets you allow.** With `PipelineSecretArns`
  empty, tasks could read any secret in the account. They now read ECDL's own
  secrets, the secrets listed in `PipelineSecretArns`, and secrets tagged
  `ecdl:link`. Before you update the control plane, tag each link's source and
  target secret `ecdl:link`, or list them in `PipelineSecretArns`. Otherwise
  Debezium and the sink fail the next time ECS starts them. The deploy
  preflight warns about a secret that is neither.
- **Load-balancer mode needs `UiHostNames`.** The API now refuses requests whose
  Host header is not on its list, in every mode. In load-balancer mode, set
  `UiHostNames` to the host names users open; the stack refuses to update
  without it.
- **The control plane is created from S3.** Its template is larger than the
  51,200 bytes CloudFormation accepts inline. `./release.sh push` uploads it to
  your template bucket; create and update the stack with `--template-url`.

### Behaviour changes

- Deleting a link keeps a final snapshot of its broker. The teardown output
  names it and prints the command to delete it.
- The sink confirms each batch reached a target replica (`WAIT`) before it
  acknowledges it. Measured locally: about 25-30% fewer writes per second on a
  single-shard target with a replica, no measurable change on a cluster. A
  target user needs the `WAIT` command; `TARGET_WAIT_REPLICAS=0` turns it off.
- A `TRUNCATE` on the source now empties the table in the cache. PostgreSQL
  publications must publish `truncate` (they do unless created with a
  `publish` list that leaves it out).
- PostgreSQL links get their own publication, `ecdl_<link>_pub`. Running links
  keep the one they use.
- Debezium refuses to start when the broker has lost a link's stored position,
  instead of silently starting from the current position. See
  [operations](docs/operations.md#debezium-refuses-to-start-no-stored-offset).
- The background probe that wrote to a customer row is off by default.
