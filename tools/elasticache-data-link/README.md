# ElastiCache Data Link (ECDL)

**For** teams whose apps read the same database rows far more often than they
change them, and want those reads served from Amazon ElastiCache for Valkey.

**The problem:** keeping a cache in sync with the database is hard to get right.
Writing to both, or expiring entries on a timer, still serves stale data, and
building a reliable sync is work each team does alone.

**ECDL** reads every committed change from the database log and applies it to
the cache, usually within a second, with no change to the app's write code.
More in the [one-pager](docs/one-pager.md).

How: a parallel bulk load fills the cache once; change data capture (CDC) then
applies every committed insert, update and delete, with optional transforms (row
filters, computed fields, column selection, renames, denormalization) on the
way. The application reads from the cache; writes still go to the database.

![ECDL architecture](docs/ecdl-architecture.png)

| Start here | |
|---|---|
| **[Getting started](docs/getting-started.md)** | from an empty account to a running link: publish images, deploy the control plane, create a link, verify, tear down |
| [Prerequisites](docs/prerequisites.md) | what the source, target and network need, per engine |
| [Operations](docs/operations.md) | alarms, troubleshooting, dead-letter replay, changing a running link |
| [One-pager](docs/one-pager.md) | what ECDL is for, in one page |
| [Benchmark results](docs/benchmark-results.md) | measured throughput and freshness, and the setup behind each number |
| [Value formats](docs/value-formats.md) | how each column type is stored in the cache |
| [When something goes wrong](#when-something-goes-wrong) | where each step's error is, and the usual causes |
| [TESTING.md](TESTING.md) | the test suites and how to run them |

Design documents: [high-level design (HLD)](docs/ECDL-HLD.docx), the
[AWS deployment diagram](docs/ecdl-aws-deployment.png) and the
[AWS architecture diagram](docs/ecdl-aws-architecture.svg) (editable `.drawio`
sources alongside).

---

## Deploy in five steps

You need an AWS account, **your own source database and ElastiCache for Valkey
cache** (ECDL creates neither), and the source set up as in
[Prerequisites](docs/prerequisites.md): change capture on, a database user in
Secrets Manager, and the signalling table. Tools: AWS CLI v2 with the Session
Manager plugin, Docker with `buildx`, Python 3.9+ with `pyyaml` and `boto3`.

1. **Publish the images into your account** (10–30 min). Nothing is published
   centrally, so this comes first:
   ```bash
   export AWS_REGION=us-east-1
   ./release.sh push
   ```
2. **Deploy the control plane** (about 5 min): one stack, in the VPC of the cache.
   ```bash
   aws cloudformation create-stack --stack-name ecdl-control-plane \
     --template-body file://infra/ecdl-control-plane.yaml \
     --capabilities CAPABILITY_NAMED_IAM \
     --parameters ParameterKey=VpcId,ParameterValue=<vpc-id> \
                  ParameterKey=SubnetId,ParameterValue=<subnet-id> \
                  ParameterKey=DeployVpcId,ParameterValue=<vpc-id>
   ```
3. **Open the UI**: `ecdl-cli/ecdl-ui ecdl-control-plane --region us-east-1`,
   then http://localhost:3000. There are no inbound ports; the script opens an
   SSM port forward, so IAM decides who can use it.
4. **Create a link** (10–15 min): in the UI, **Define → Build Pipeline**, or
   write the file below and run `ecdl validate`, `ecdl plan`, `ecdl apply`. ECDL
   creates the broker and the pipeline tasks, bulk-loads every table, then
   applies every change.
5. **Check it**: the Pipeline Monitor shows every hop green, and
   `HGETALL <table>:<pk>` on the cache returns the row. Add
   `advanced.alarms.email` to the link to be emailed alarms.

The application then reads from the cache and keeps writing to the database.
To remove a link: **Delete pipeline** in the UI or `ecdl destroy --link <name>`,
before deleting the control-plane stack. Every step in detail, including
sign-in through your own load balancer, is in
[Getting started](docs/getting-started.md). If a step fails, see
[When something goes wrong](#when-something-goes-wrong).

---

## How it works

```
Source DB ──(1) bulk load, once, parallel──────────────────────────▶ ElastiCache (target)
    │                                                                      ▲
    └──(2) change log ──▶ Debezium ──▶ broker (Valkey Streams) ──▶ sink ───┘
```

- **Bulk load:** a run-once Fargate task reads each table in parallel
  primary-key ranges and writes straight to the target.
- **CDC:** Debezium Server (one Fargate task) reads the source log; a broker
  (ElastiCache for Valkey, cluster mode disabled) buffers the events; the Go sink
  (Fargate, autoscaled) applies transforms and writes to the target.
- **Control plane:** a web UI and API (`infra/ecdl-control-plane.yaml`) that plan,
  deploy and monitor links. The `ecdl` CLI does the same from a terminal.
  By default it has no inbound ports and is opened through an SSM port forward,
  so IAM decides who can use it. Teams that want browser sign-in put their own
  HTTPS load balancer with SSO in front (`AccessMode`; see
  [Getting started](docs/getting-started.md#2-deploy-the-control-plane)).

ECDL creates the broker, the pipeline tasks and their alarms. **The source
database and the target cache are yours**; a link references them by ARN (Amazon Resource Name).

| Source | Status | How changes are read |
|---|---|---|
| MySQL / Aurora MySQL | supported | row-based binlog |
| PostgreSQL / Aurora PostgreSQL | supported | logical decoding (`pgoutput`) |
| SQL Server | supported | CDC change tables, polled |
| Oracle | preview | LogMiner |

The engine is detected from `source.arn`.

---

## A link

A **link** is one file: which tables to cache, from which source, into which
target. Endpoints, engine, cluster mode, TLS, subnets and key names are read from
the ARNs, and setting them is rejected.

```yaml
schema_version: 1
name: checkout-cache
mode: initial_load_and_cdc      # initial_load | cdc | initial_load_and_cdc

source:
  arn: arn:aws:rds:us-east-2:123456789012:cluster:my-cluster
  database: Inventory
  secret_arn: arn:aws:secretsmanager:us-east-2:123456789012:secret:shop-db-a1b2c3

target:
  arn: arn:aws:elasticache:us-east-2:123456789012:replicationgroup:my-cache

tables:
  - products
  - customers
  - orders

transforms:
  tables:
    products:
      filter:                   # ops: = != > >= < <=
        field: status
        op: '='
        value: active
      computed:
        - name: price_cents
          expr: price * 100
      columns: [id, name, price, status, price_cents]
    orders:
      denormalize:              # embed the parent's fields onto each order
        - parent_table: customers
          join_field: customer_id
          fields: [name, tier]
          prefix: cust_
          on_parent_delete: "null"
```

Each table is cached as hashes keyed `<table>:<primary key>`. Per row the order
is filter → computed → column select → rename → denormalize. The full schema is
`ecdl_link/link.schema.json`; `ecdl-cli/examples/checkout-cache/link.yaml` is a
complete example.

| `mode` | Deploys |
|---|---|
| `initial_load_and_cdc` | broker, Debezium, sink, loader: loads once, then streams |
| `cdc` | broker, Debezium, sink: streams only |
| `initial_load` | loader only: a one-time copy that goes stale afterwards |

---

## CLI

| Task | Command |
|---|---|
| Check a link (no AWS calls) | `ecdl validate my-link.yaml` |
| See what it reads from the ARNs | `ecdl resolve my-link.yaml` |
| See what would be deployed | `ecdl plan my-link.yaml` |
| Deploy, or update a deployed link in place | `ecdl apply my-link.yaml` (`--reload` runs the bulk load again) |
| What a change would break | `ecdl diff deployed.yaml desired.yaml` |
| Signalling-table DDL (CREATE TABLE statement) / backfill a table | `ecdl signal ddl my-link.yaml` / `ecdl signal snapshot --tables orders my-link.yaml` |
| Stack outputs | `ecdl status --stack ecdl-<link>-cdc` |
| Tear a link down (keeps source and target) | `ecdl destroy --link <link>` |

The CLI is `ecdl-cli/ecdl`; see [ecdl-cli/README.md](ecdl-cli/README.md).

---

## Images

Five images are published into your own account's ECR by `./release.sh push`
(run it once per account and region before the first deploy):

| Image | Built from |
|---|---|
| `ecdl-debezium-server` | upstream Debezium Server 3.0.6 plus `data-plane/debezium-server/` |
| `ecdl-sink` | `tools/go-sink` |
| `ecdl-loader` | `data-plane/bulk-loader` |
| `ecdl-control-plane-api` | `backend/`, with `ecdl_link/` and `infra/` |
| `ecdl-control-plane-ui` | `frontend/` |

Tags are immutable; the templates pull `ecdl/<image>:<ImageTag>` from the
deploying account, and `./release.sh pin` sets `ImageTag` to `VERSION`.

### Build host and CPU architecture

A multi-architecture build needs a host with 4 GB or more; for a faster
single-architecture build use `PLATFORMS=linux/arm64 ./release.sh push` and
deploy with `advanced.cpu_architecture: arm64`.

---

## When something goes wrong

Find the step that failed; each row says where its error is and what it
usually means. For a link that is already running (alarms, lag, dead letters),
go to [Operations](docs/operations.md).

**1. `./release.sh push` fails.** The error is printed in the terminal.

| Message | Cause and fix |
|---|---|
| `could not determine an AWS account for the registry` | No working credentials. Check `aws sts get-caller-identity` and `AWS_PROFILE`. |
| `docker buildx is required` | Install Docker Desktop, or `docker-buildx-plugin` on Linux. |
| The build is killed, or stops with no error | The host has less than 4 GB of memory for a multi-architecture build. Build one architecture: see [Build host and CPU architecture](#build-host-and-cpu-architecture). |

**2. The control-plane stack fails** (`CREATE_FAILED` or `ROLLBACK_COMPLETE`).
Read the stack events first:
`aws cloudformation describe-stack-events --stack-name ecdl-control-plane`.
If the `Instance` resource failed, the instance's own log names the step:
`aws ssm start-session --target <InstanceId>`, then
`sudo tail -100 /var/log/cloud-init-output.log`.

| Message | Cause and fix |
|---|---|
| `Requires capabilities : [CAPABILITY_NAMED_IAM]` | Add `--capabilities CAPABILITY_NAMED_IAM` to `create-stack`. |
| `control-plane user data failed at line …`, with `manifest unknown` or `not found` at `docker pull` in the instance log | The images are not in this account and region at `ImageTag`. Run step 1 in the same region. |
| The same, with a timeout at `aws ecr` or `docker pull`; or `Failed to receive 1 resource signal(s)` | The subnet cannot reach ECR or the AWS APIs. Use a subnet with a NAT gateway or VPC endpoints, or set `AssignPublicIp=true` in a subnet routed to an internet gateway. |
| The instance log ends with `control-plane API did not answer on :5000` | The API container started and stopped; the lines after it are its own log. |
| `is not authorized to perform` | Your identity lacks a permission from [Getting started, step 0](docs/getting-started.md#0-before-you-start). |

**3. The UI does not open.** `ecdl-cli/ecdl-ui` prints the reason.

| Message | Cause and fix |
|---|---|
| `'session-manager-plugin' is not installed` | Install the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html). |
| `local port 3000 (or 5000) is already in use` | Stop whatever uses it, or the previous tunnel. |
| `uses AccessMode=…, not ssm` | Open the URL in the stack's `OpenUi` output instead. |
| `TargetNotConnected` | The instance cannot reach Systems Manager; it needs the same route to the AWS APIs as in step 2. |
| `AccessDeniedException` on `StartSession` | Your identity needs `ssm:StartSession`; [Getting started, step 2](docs/getting-started.md#2-deploy-the-control-plane) has a policy. |
| The page loads but shows no data | The tunnel dropped (they close after a period with no use). Press Ctrl-C and run `ecdl-ui` again. |

**4. Deploying a link fails.** The UI's **Deployments** page shows the failed
step, the error and a hint saying what to do; `ecdl apply` prints the same.
For a stack error, `aws cloudformation describe-stack-events --stack-name
ecdl-<link>-<broker|cdc|loader>`. For a task error, the log groups below.

| Message | Cause and fix |
|---|---|
| `Deploy would be refused`: a table or column does not exist | The link names a table or column the source does not have. Fix the link, or create the table first. |
| `already deployed` | The link exists. Confirm to update it in place; nothing is reloaded unless you ask for a reload. |
| `CannotPullContainerError` | The pipeline images are not in this account and region at the stack's `ImageTag` (step 1). |
| Debezium log: `Access denied`, `binlog`, `replication slot`, `wal_level` | The source is not set up for change capture, or the user lacks grants: [Prerequisites, sections 3–5](docs/prerequisites.md#4-change-capture-must-be-enabled-on-the-source). |
| `ResourceInitializationError ... secretsmanager` | The task cannot read the source or target secret. Add it to the control plane's `PipelineSecretArns`, or check the subnet's route to Secrets Manager. |
| Loader log: `FATAL: ... will not fit in the target cache` | The target is too small for the tables. Use a larger node type or more shards. |
| Loader log: `FATAL: ... no primary key` or `composite primary key` | Name the key column with `advanced.loader.primary_keys`. |
| Timeouts connecting to the source or the cache | Security groups or routing between the pipeline subnets and the source or cache: [Prerequisites](docs/prerequisites.md). |

**5. The link is running but the cache is wrong or late.** Open
**Observe → Pipeline Monitor**: it shows which hop is unhealthy and whether any
change was lost. Then read that component's log:

| Component | CloudWatch log group | What to look for |
|---|---|---|
| Debezium (capture) | `/ecs/ecdl-<link>-cdc/debezium` | `ERROR` lines; a healthy task logs that it is streaming |
| Sink (writes to the cache) | `/ecs/ecdl-<link>-cdc/sink` | `ERROR <kind>: …` (a change not yet written, retried later); `WARN target-write-retry` (a failed write that succeeded on a re-send) |
| Bulk loader | `/ecs/ecdl-<link>-loader` | `ERROR chunk …` and `FATAL` lines |
| Broker | `/ecs/ecdl-<link>-broker/broker` | memory and slow commands |

A change that cannot be written is never dropped: it is retried, and after
5 attempts moved to a dead-letter stream and alarmed. Recovering from that,
from a paused capture and from a failed load is in
[Operations](docs/operations.md). How each column type is stored (dates,
booleans, binary) is in [Value formats](docs/value-formats.md).

---

## Repository

| Path | What |
|---|---|
| `ecdl_link/` | the link engine shared by the CLI and the control plane: schema, validation, ARN resolution, deployment planner, deployer, post-deployment (day-2) diff/repair |
| `ecdl-cli/` | the `ecdl` CLI and the example link |
| `backend/`, `frontend/` | the control-plane API (Flask) and UI (React), including the Pipeline Monitor |
| `infra/` | CloudFormation: broker, CDC and loader stacks per link, and the control plane |
| `data-plane/` | Debezium Server configuration and the parallel bulk loader |
| `tools/` | the Go sink |
| `docs/` | the guides, diagrams, HLD and benchmark results linked above |
| `release.sh`, `Makefile` | build and publish the images; run every test suite (`make test`) |
