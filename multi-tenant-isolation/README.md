# Multi-Tenant Isolation with Amazon ElastiCache for Valkey

A self-contained, runnable code sample that shows how to isolate tenants on a
single Amazon ElastiCache for Valkey cluster using **numbered databases**
(Valkey 9.0) and **database-level access control** (Valkey 9.1).

## Use case

High-volume B2C / large-scale SaaS with many small tenants. The cost rationale,
scaling limits (up to 10,000 databases per cluster), and fleet-sharding guidance
are in [`docs/01-concepts.md`](./docs/01-concepts.md).

The sample comes in two parts you can run independently:

- **CLI demo** — scripts that deploy the clusters, create per-tenant RBAC users, and prove isolation from `redis-cli` with pass/fail assertions.
- **Browser demo (optional)** — a multi-tenant SaaS storefront on ECS Fargate behind an HTTPS load balancer. You sign in as different tenants and watch the engine block cross-tenant access, which makes the use case tangible.

End to end:

1. Deploy the infrastructure (VPC, EC2 client, two Valkey clusters, app-tier networking).
2. Demonstrate numbered-database isolation.
3. Create N per-tenant RBAC users, each locked to its own numbered database.
4. Prove that one tenant cannot reach another tenant's data (CLI and/or browser).
5. Tear everything down.

For a one-page overview with a request-flow diagram, see
[`ONEPAGER.md`](./ONEPAGER.md). For the concepts and a step-by-step walkthrough
of what the scripts do, see [`docs/`](./docs):

- [`docs/01-concepts.md`](./docs/01-concepts.md) — numbered databases, RBAC, and the `db=` ACL rule.
- [`docs/02-numbered-databases.md`](./docs/02-numbered-databases.md) — the numbered-database walkthrough.
- [`docs/03-database-level-acls.md`](./docs/03-database-level-acls.md) — per-tenant RBAC and the isolation proof.

## Project layout

```
.
├── README.md
├── ONEPAGER.md
├── LICENSE
├── Dockerfile                                 # browser-demo app image
├── requirements.txt / requirements-dev.txt
├── cloudformation/
│   ├── elasticache-valkey-multi-tenant.yaml   # base stack (clusters + networking)
│   └── app-stack.yaml                         # browser demo (ALB + Fargate)
├── app/                                       # Flask multi-tenant storefront
│   └── rotation/                              # Secrets Manager rotation Lambda
├── tests/                                     # app route/isolation-wiring tests
├── scripts/                                   # deploy / demo / verify / app / cleanup
└── docs/                                      # concepts + walkthrough
```

## What gets deployed

A single **hardened** CloudFormation stack ([`cloudformation/elasticache-valkey-multi-tenant.yaml`](./cloudformation/elasticache-valkey-multi-tenant.yaml)) creates:

- A dedicated VPC with **no internet path** — no internet gateway, no NAT, no public IPs.
- A private subnet for the client and two isolated subnets for the cache.
- **Interface VPC endpoints** (SSM, `ssmmessages`, `ec2messages`, Secrets Manager, KMS, ElastiCache) so the private client reaches AWS APIs and Session Manager without any route to the internet.
- A **customer-managed KMS key** (rotation enabled) used for at-rest encryption of both clusters, the AUTH secret, the client's root volume, and the log groups.
- A **Secrets Manager secret** holding the generated Valkey **AUTH token** (never a plaintext parameter).
- Least-privilege security groups: cache ingress on `6379` from the client SG only, and the cache SG denies all egress.
- An ElastiCache subnet group and a custom parameter group that enables `cluster-databases` for the Cluster Mode Enabled cluster.
- An **ElastiCache for Valkey Cluster Mode Disabled (CMD)** cluster — numbered databases (up to 10,000; default 16 here), TLS `required`, at-rest encryption, daily snapshots (7-day retention), slow-/engine-log delivery.
- An **ElastiCache for Valkey Cluster Mode Enabled (CME)** cluster with numbered databases in cluster mode and the same encryption/logging.
- An **EC2 client** (Amazon Linux 2023, IMDSv2 required, encrypted root volume) with the Valkey CLI, reachable through AWS Systems Manager Session Manager (no SSH keys, no open inbound ports, no public IP).
- **VPC flow logs** to CloudWatch.
- **App-tier networking** for the optional browser demo: private app subnets for Fargate and the internal ALB, plus ECR/CloudWatch Logs/S3 VPC endpoints so tasks pull their image and log **without a NAT**. Nothing in the VPC has an internet path.

The optional app stack ([`cloudformation/app-stack.yaml`](./cloudformation/app-stack.yaml)) adds an **ECS Fargate** service behind an **internal, HTTPS-only ALB** (TLS 1.3, ACM certificate required, no internet path). You reach it from your laptop via SSM port-forwarding through the EC2 client. The app connects to the cluster as each tenant's RBAC user and reads tenant passwords from Secrets Manager at runtime. It also deploys a Secrets Manager **rotation Lambda** and a rotation schedule for the Flask key; `deploy-app.sh` attaches 30-day rotation schedules to the AUTH token and tenant password secrets too.

```
   ┌──────────────────────────── VPC 10.0.0.0/16 (no internet path) ────────────────────────────┐
   │                                                                                             │
   │   ┌───────────────┐   443    ┌─────────────────────────┐                                    │
   │   │  EC2 client   │─────────▶│  Interface VPC endpoints │  SSM · Secrets Manager · KMS ...   │
   │   │  (valkey-cli) │          └─────────────────────────┘                                    │
   │   │  private, SSM │   6379    ┌──────────────────────────────┐                               │
   │   └───────────────┘─────────▶│  ElastiCache for Valkey        │  (isolated subnets)          │
   │        ▲                      │   • CMD  (numbered dbs, RBAC)│  TLS required · KMS at-rest   │
   │        │ reads AUTH token     │   • CME  (cluster-databases)  │                               │
   │        ▼                      └──────────────────────────────┘                               │
   │   ┌───────────────┐                                                                           │
   │   │ Secrets Mgr   │  AUTH token (generated, KMS-encrypted)                                    │
   │   └───────────────┘                                                                           │
   └─────────────────────────────────────────────────────────────────────────────────────────────┘

   tenant1  ──AUTH tenant1──▶  db=1 only   (NOPERM on any other db)
   tenant2  ──AUTH tenant2──▶  db=2 only   (NOPERM on any other db)
```

## Prerequisites

- An AWS account you can create VPC/EC2/IAM/ElastiCache resources in (use a
  personal or sandbox account, not production). This sample creates billable resources.
- A Region where **ElastiCache for Valkey 9.0 or later** is available (default: `us-east-1`).
- Local tools:
  - [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), configured with credentials (`aws configure`).
  - The [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) for the AWS CLI (the scripts use `aws ssm start-session`).
  - `bash` (the scripts are POSIX-ish bash; macOS/Linux/CloudShell all work).

Everything in `scripts/` can also be run from **AWS CloudShell**, which already
has the AWS CLI and Session Manager plugin installed.

## Quick start (CLI demo)

```bash
# 1. Deploy the base stack (≈5–10 min for the clusters to come up)
./scripts/deploy.sh

# 2. Run the full isolation demo end to end
#    (creates N RBAC users, migrates the CMD cluster to RBAC, and proves isolation)
./scripts/run-demo.sh

# 3. Clean everything up when you are done
./scripts/cleanup.sh
```

## Quick start (browser demo)

The browser demo needs an ACM certificate (HTTPS is required on the ALB) and a
running Docker daemon (the image is built and pushed at deploy time). The ALB is
**internal**, so you reach it over an SSM tunnel — there is no public URL and no
ingress CIDR to set. Run it **after** `deploy.sh` and `setup-rbac.sh`:

```bash
./scripts/deploy.sh         # base stack
./scripts/setup-rbac.sh     # create the N tenant RBAC users + password secrets

CERT_ARN=arn:aws:acm:us-east-1:<account>:certificate/<id> \
  ./scripts/deploy-app.sh   # build image, deploy internal ALB + Fargate + rotation

./scripts/port-forward.sh   # open an SSM tunnel -> https://localhost:8443/

./scripts/cleanup.sh        # removes the app stack first, then everything else
```

With the tunnel running, open **https://localhost:8443/**, sign in as any
tenant, add items, then use **Attempt to peek** to watch the engine deny
cross-tenant reads. (Accept the self-signed certificate warning.)

Each script is safe to re-run. Configuration is driven by environment variables
with sensible defaults — see [Configuration](#configuration).

## The scripts

| Script | What it does |
|---|---|
| [`scripts/config.sh`](./scripts/config.sh) | Shared settings and helpers sourced by the other scripts. Override defaults with environment variables. |
| [`scripts/deploy.sh`](./scripts/deploy.sh) | Deploys the CloudFormation stack and waits for `CREATE_COMPLETE`, then prints the outputs. |
| [`scripts/demo-numbered-databases.sh`](./scripts/demo-numbered-databases.sh) | Runs `redis-cli` on the EC2 client to show numbered-database isolation and the `FLUSHDB` vs `FLUSHALL` difference (CMD, and optionally CME). |
| [`scripts/setup-rbac.sh`](./scripts/setup-rbac.sh) | **Control plane.** Generates each tenant's password into Secrets Manager, creates the locked-down `default` user and `tenant1..N` users (each `db=N`), groups them, and migrates the CMD cluster from AUTH token to RBAC. |
| [`scripts/verify-isolation.sh`](./scripts/verify-isolation.sh) | **Data plane.** Connects on the EC2 client as each tenant and asserts that each can use only its own database and is denied (`NOPERM`) on the others. |
| [`scripts/run-demo.sh`](./scripts/run-demo.sh) | Orchestrates the numbered-database demo → RBAC setup → isolation verification in one command. |
| [`scripts/deploy-app.sh`](./scripts/deploy-app.sh) | **Browser demo.** Builds/pushes the app image, packages the rotation Lambda, creates the Flask secret, and deploys the internal ALB + Fargate app stack. Requires `CERT_ARN`. |
| [`scripts/setup-rotation.sh`](./scripts/setup-rotation.sh) | Attaches 30-day Secrets Manager rotation schedules to the AUTH token and tenant password secrets (called automatically by `deploy-app.sh`). |
| [`scripts/port-forward.sh`](./scripts/port-forward.sh) | Opens an SSM tunnel to the internal ALB so you can open the storefront at `https://localhost:8443/`. |
| [`scripts/cleanup.sh`](./scripts/cleanup.sh) | Deletes the app stack first, then the base stack, then the RBAC user group, users, secrets, and the Lambda code bucket, in the correct order. |

### How commands reach the cluster

The clusters are private to the VPC, so the data-plane steps (`redis-cli`) must
run **on the EC2 client**. The scripts handle this for you: they send the
`redis-cli` commands to the instance with SSM (`aws ssm send-command`), so you
never have to open an interactive SSM shell yourself. The control-plane steps
(`aws elasticache ...`) run locally with your own credentials.

## Configuration

All scripts source `scripts/config.sh`. Override any of these before running:

| Variable | Default | Meaning |
|---|---|---|
| `STACK_NAME` | `valkey-multi-tenant` | CloudFormation stack name. |
| `AWS_REGION` | your CLI default, else `us-east-1` | Region to deploy into. |
| `ENGINE_VERSION` | `9.1` | Valkey engine version (`9.0` or `9.1`). `9.1` is required for the `db=` ACL rule. |
| `CACHE_NODE_TYPE` | `cache.t3.micro` | Node type for both clusters. |
| `TENANT_COUNT` | `5` | Number of tenants to provision (demo caps at 100; the engine supports up to ~9,999). Each tenant N is pinned to database N. |
| `RUN_CME_DEMO` | `false` | When `true`, the numbered-database demo also runs against the CME cluster. |

The browser demo (`deploy-app.sh`) also reads:

| Variable | Required | Meaning |
|---|---|---|
| `CERT_ARN` | yes | ACM certificate ARN for the internal ALB's HTTPS listener. |
| `APP_STACK_NAME` | no | App stack name (default `${STACK_NAME}-app`). |

> **No passwords in config or source.** The Valkey AUTH token and each tenant's
> RBAC password are **generated into AWS Secrets Manager** — the AUTH token by
> the base template, the tenant passwords by `setup-rbac.sh`. Nothing sensitive
> is set as an environment variable, a CloudFormation parameter, or committed to
> source. At runtime the EC2 client and the Fargate app fetch what they need
> from Secrets Manager using their own IAM roles, so secret values never travel
> through SSM command text or your shell history.

Example:

```bash
export AWS_REGION=us-west-2
export TENANT_COUNT=8
./scripts/deploy.sh
./scripts/run-demo.sh
```

## Security

The sample follows AWS security best practices for ElastiCache so you can see
how a hardened, multi-tenant cache is set up:

| Control | How it is implemented |
|---|---|
| **At-rest encryption** | Customer-managed **KMS key** with automatic rotation, used by both clusters, the AUTH secret, the client's EBS root volume, and the CloudWatch log groups. The key is `Retain` on delete so encrypted snapshots stay recoverable. |
| **In-transit encryption** | `TransitEncryptionEnabled: true` with `TransitEncryptionMode: required` on both clusters; every `redis-cli` connection uses `--tls`. |
| **Secret handling** | The Valkey AUTH token is **generated into Secrets Manager** (never a CFN parameter, never in source). Clusters reference it with a `{{resolve:secretsmanager:...}}` dynamic reference; the client reads it at runtime via its IAM role. |
| **Network isolation** | Cache in **private isolated** subnets; client in a **private** subnet. No internet gateway, no NAT, no public IPs. Session Manager and AWS API calls flow through **interface VPC endpoints** only. |
| **Least-privilege SGs** | Cache SG accepts `6379` from the client SG only and has **no egress**; endpoint SG accepts `443` from the client SG only; client SG egress limited to `443`/`6379` within the VPC. |
| **Least-privilege IAM** | The EC2 role is `AmazonSSMManagedInstanceCore` plus permission to read **only this stack's** AUTH secret and decrypt with **this stack's** key. |
| **Instance hardening** | **IMDSv2 required** (`HttpTokens: required`), encrypted root volume. |
| **Observability** | ElastiCache **slow-log + engine-log** to CloudWatch and **VPC flow logs**. |
| **Backups** | Daily automatic snapshots on the CMD cluster, 7-day retention (configurable). |

The optional browser demo keeps the same fully-private posture: the ALB is
**internal and HTTPS-only (TLS 1.3)** with **no internet path** (reached via SSM
port-forwarding through the EC2 client), Fargate tasks run in **private subnets**
with **no public IP**, pull their image through **ECR/S3 VPC endpoints** (no
NAT), run as a **non-root** container, use a **deployment circuit breaker**, send
**Secure / HttpOnly / SameSite** cookies, and read every secret from Secrets
Manager via a **least-privilege task role**.

**Secret rotation** is enabled: a Secrets Manager rotation Lambda rotates the
Flask signing key and the per-tenant RBAC passwords (via `elasticache:ModifyUser`,
staging both old and new passwords so connections are not dropped) on a 30-day
schedule. See [`app/rotation/handler.py`](./app/rotation/handler.py).

One deliberate exception remains, appropriate for a demo:

- **ALB access logging to S3 is off** (cfn-nag `W52`), to keep the demo
  self-contained — enable it with a log bucket before production use.

Note on IAM: each role attaches a dedicated, **least-privilege customer-managed
policy** scoped to a single secret/key, rather than an inline policy — so the
permissions are auditable on their own and the roles satisfy policy scanners
that disallow inline policies.

### Security scanning

The sample has been checked with the following tools. Run them from this
directory before submitting changes:

- **cfn-lint** — validates both CloudFormation templates.
- **cfn-nag** — scans both templates for insecure IaC patterns (zero failures; the remaining warnings are the documented ALB access-logging exception and Lambda IAM wildcards that AWS does not allow to be scoped).
- **ruff + pytest + bandit + pip-audit** — lints, tests, and security-scans the Flask app and its dependencies.
- **ShellCheck** — lints every script in `scripts/`.
- **Trivy** — filesystem scan for vulnerabilities, leaked secrets, and IaC misconfigurations (CRITICAL/HIGH). Documented exceptions go in [`.trivyignore`](./.trivyignore).

```bash
cfn-lint cloudformation/*.yaml
cfn_nag_scan --input-path cloudformation/
ruff check app tests && pytest -q && bandit -r app -q && pip-audit -r requirements.txt
shellcheck -x scripts/*.sh
trivy fs --scanners vuln,secret,misconfig --severity CRITICAL,HIGH .
```

## What the sample proves

After `run-demo.sh` completes you will have seen:

- **Numbered databases** keep tenant keyspaces separate — a key written in
  database `0` is invisible from database `1`.
- **`FLUSHDB` vs `FLUSHALL`** — `FLUSHDB` clears only the current database;
  `FLUSHALL` clears every database (and requires `alldbs`, which restricted
  tenants do not have).
- **Database-level ACLs** — each `tenantN` (locked to `db=N`) gets a `NOPERM`
  error when it tries to `SELECT` any database that is not its own. Isolation is
  enforced by the engine, not by application code.

### The browser demo

The storefront makes the same guarantees clickable. Each browser session acts as
one tenant; the app connects to the shared cluster **as that tenant's RBAC
user**. On the store page you can:

- add items to your own database and see them persist,
- **Attempt to peek** at another tenant's database — the engine returns
  `NOPERM`, surfaced in the UI, proving the app cannot cross the boundary even
  when it tries,
- **Attempt FLUSHALL** — denied, because a single-database tenant lacks
  `alldbs`,
- **Clear my database (FLUSHDB)** — allowed, and scoped to that tenant only.

All tenants share one cluster, and the engine keeps each tenant's data
inaccessible to the others.

> **Demo only — not a production authorization model.** To make the whole
> Valkey environment easy to explore, the browser demo lets anyone who can
> reach it sign in as **any** tenant with a single selection; there is no user
> authentication. The app's task role can read **every** tenant's password
> secret, and each tenant user has a broad access string (`on ~* +@all db=N`).
> In a real deployment, always add proper user authorization and tighter ACLs:
>
> - Authenticate end users (for example with Amazon Cognito or your identity
>   provider) and map each user to their tenant on the server side. Never let
>   the client choose the tenant.
> - Fetch only the authenticated tenant's credential, and scope IAM so a
>   compromised app instance can't read every tenant's secret.
> - Restrict each tenant's ACL to the commands and key patterns it actually
>   needs (for example `+@read +@write -@dangerous` and a `~tenant:*` key
>   pattern) instead of `+@all`.

## Cost

The hardened stack costs a bit more than a bare setup because of the security
infrastructure. Rough on-demand estimate in a US Region:

**Base stack (CLI demo):**

- Two `cache.t3.micro` nodes + a `t3.micro` client: ~**$0.045/hour**.
- Interface VPC endpoints (SSM/Secrets Manager/KMS/ElastiCache, plus ECR/Logs for the app): ~**$0.01/hour each**, plus per-GB data processing.
- KMS key: ~**$1/month** (and it is **retained** after cleanup — see below).
- CloudWatch logs and snapshots: a few cents for a short session.

**Browser demo (app stack), when deployed:**

- An **Application Load Balancer**: ~**$0.0225/hour** + LCU usage.
- **Two Fargate tasks** (0.5 vCPU / 1 GB): ~**$0.05/hour** combined.
- ECR storage: negligible.

Budget roughly **$0.10–0.15/hour** for the base stack, plus ~**$0.08/hour** when
the browser demo is running. Confirm with the
[AWS Pricing Calculator](https://calculator.aws/). Costs accrue for as long as
the resources exist, so run `cleanup.sh` when done.

## Cleanup

```bash
./scripts/cleanup.sh
```

Order matters and the script handles it:

1. The **app stack is deleted first** (it depends on the cluster and RBAC users).
2. Then the **base stack** — the CMD replication group stays associated with the user group until the stack is gone.
3. Then the **user group, tenant users, and per-tenant password secrets** are removed in the correct order.
4. The script then reminds you about the **retained KMS key** (kept on purpose so encrypted snapshots remain recoverable) and prints the `aws kms schedule-key-deletion` command to remove it once you are sure it is no longer needed.

The base stack's AUTH-token secret is deleted with the stack but enters a
recovery window; force-delete it if you need the name back sooner.

## License

This sample is licensed under the MIT-0 License. See [LICENSE](./LICENSE).
