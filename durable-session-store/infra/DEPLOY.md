# Deploy runbook — Durable Valkey 9.1 store + storefront (end to end)

This runbook deploys **one stack** (`DurableSessionStoreStack`, defined in
`infra/lib/session-store-stack.ts`) that stands up both the durable cache and a
clickable storefront in front of it:

- A **Valkey 9.1 cluster-mode-enabled** replication group: 1 shard, 1 primary +
  2 replicas, synchronous durability.
- A **cluster-aware Flask storefront** on ECS Fargate behind an internet-facing
  Application Load Balancer, so you can use the app from your own browser and
  click the "fail over the primary" button to watch the cart survive.

## What this stack provisions

| Area | Detail |
|---|---|
| Engine | Valkey `9.1`, **cluster mode enabled** |
| Topology | 1 shard, **1 primary + 2 replicas** (`cache.r7g.large`) |
| Durability | **`sync`** — writes persisted to the Multi-AZ transactional log *before* the client is acknowledged |
| Failover | Multi-AZ, automatic failover enabled |
| Encryption | At-rest (customer-managed KMS, rotation on) + in-transit TLS `required` |
| Secrets | Valkey AUTH token **and** Flask `SECRET_KEY`, both generated into Secrets Manager |
| Cache network | Private **isolated** subnets, SG-to-SG ingress only (from the app) |
| App | ECS Fargate (private subnets, egress via 1 NAT), 2 tasks, cluster-aware client |
| Ingress | Internet-facing **ALB, HTTPS only (443, TLS 1.3)**; no HTTP:80 listener. Requires an ACM certificate ARN |
| Demo | Failover button enabled; task role granted `elasticache:TestFailover` |
| Logs | slow-log + engine-log + app logs to CloudWatch, VPC flow logs |

> **Status: verified to synth, NOT yet deployed in this form.** `cdk synth`,
> `cdk-nag` (`AwsSolutionsChecks`), and 17 Jest assertion tests pass locally.
> Treat the first `cdk deploy` as unproven and watch the CloudFormation events.

## ⚠️ Before you start

1. **Cost.** Billable resources run until you tear down: 3 × `cache.r7g.large`
   nodes, **1 NAT gateway**, an **ALB**, 2 Fargate tasks, a KMS key, and
   CloudWatch logs. The KMS key uses `RETAIN`.

2. **The ALB is HTTPS only, needs a certificate, and a scoped ingress CIDR.**
   You must pass an allowed ingress CIDR at synth (`-c allowedCidr=<cidr>` or
   `ALB_ALLOWED_CIDR`); synth fails without one and rejects `0.0.0.0/0`. You
   must also pass an ACM certificate ARN at deploy as the `CertificateArn`
   CloudFormation parameter (`--parameters CertificateArn=<arn>`); the deploy
   fails without it. The ALB serves 443 (TLS 1.3) from that CIDR only
   and opens no port 80. Request/import a certificate in ACM in this region
   first, and scope the CIDR to your office/VPN range.

3. **Docker is required at deploy time.** `cdk deploy` builds the app container
   image from the repo root (`ecs.ContainerImage.fromAsset`). The machine
   running the deploy needs a working Docker daemon and network access to pull
   the `python:3.12-slim` base image.

## Prerequisites

```bash
cd infra
npm install

export CDK_DEFAULT_ACCOUNT=<account-id>
export CDK_DEFAULT_REGION=us-east-1
# One-time per account+region (already done if you deployed before):
npx cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION
```

Use least-privilege / non-production credentials. The deploying principal needs
permission to create VPC, ElastiCache, ECS, ELBv2, ECR, IAM, KMS, Secrets
Manager, and CloudWatch Logs resources.

## Step 1 — Verify locally

```bash
# App (from repo root):
pytest -q && ruff check app tests && bandit -r app

# Infra (from infra/):
npm run build && npm test && npx cdk synth   # synth must exit 0, nag clean
```

## Step 2 — Preview and deploy

```bash
# Scope ALB ingress to your network (0.0.0.0/0 is rejected at synth).
CIDR=203.0.113.0/24
# Your ACM certificate ARN (HTTPS is required), passed as a stack parameter.
CERT_ARN=arn:aws:acm:us-east-1:<account>:certificate/<id>
npx cdk diff   -c allowedCidr=$CIDR                                   # review
npx cdk deploy -c allowedCidr=$CIDR --parameters CertificateArn=$CERT_ARN   # ~15-20 min
```

On success, CDK prints the outputs. The one you want:

| Output | Meaning |
|---|---|
| `StorefrontUrl` | `https://<alb-dns>` — open this in your browser |
| `ConfigurationEndpoint` / `ConfigurationPort` | Valkey cluster config endpoint |
| `AuthTokenSecretArn` | Secrets Manager ARN for the Valkey AUTH token |
| `ClientSecurityGroupId` | SG attached to the app tasks |

Fetch outputs later:

```bash
aws cloudformation describe-stacks --stack-name DurableSessionStoreStack \
  --query "Stacks[0].Outputs" --output table
```

## Step 3 — Validate the cluster (read-only)

```bash
RG_ID=$(aws elasticache describe-replication-groups \
  --query "ReplicationGroups[?contains(Description, 'session store')].ReplicationGroupId | [0]" \
  --output text)

aws elasticache describe-replication-groups --replication-group-id "$RG_ID" \
  --query "ReplicationGroups[0].{Status:Status, ClusterEnabled:ClusterEnabled, \
NodeGroups:length(NodeGroups), MembersInShard:length(NodeGroups[0].NodeGroupMembers), \
MultiAZ:MultiAZ, AutoFailover:AutomaticFailover, Durability:EffectiveDurability, \
AtRest:AtRestEncryptionEnabled, Transit:TransitEncryptionEnabled}" --output table
```

Expect: `available`, `ClusterEnabled True`, `NodeGroups 1`, `MembersInShard 3`,
`MultiAZ enabled`, `AutoFailover enabled`, `Durability sync`, encryption `True`.

## Step 4 — End-to-end test from your browser

1. Open **`StorefrontUrl`**. You should see the catalog of 32 products.
2. Add a few items to your cart; log in with any username.
3. Open the **cart** page and confirm the items + total.
4. Click **Fail over the primary** (confirm the prompt). It calls
   `TestFailover` on the shard.
5. Wait ~30-60s for ElastiCache to promote a replica, then reload the cart.
   **It is unchanged** — the session lived in the cluster, not on the node that
   stepped down. That is the durability demonstration.

You can watch the failover from the CLI in parallel:

```bash
aws elasticache describe-replication-groups --replication-group-id "$RG_ID" \
  --query "ReplicationGroups[0].Status" --output text   # 'modifying' then 'available'
```

## Teardown (stop the charges)

```bash
cd infra
npx cdk destroy
```

- Removes the cluster, Fargate service, ALB, NAT gateway, VPC, secrets, logs.
- The **KMS key is retained** (`RemovalPolicy.RETAIN`); CDK schedules it for
  deletion on destroy in most cases — confirm and, if needed, schedule it:
  ```bash
  aws kms schedule-key-deletion --key-id <key-id> --pending-window-in-days 7
  ```
- Verify nothing bills on:
  ```bash
  aws elasticache describe-replication-groups --query "ReplicationGroups[].ReplicationGroupId" --output table
  aws elbv2 describe-load-balancers --query "LoadBalancers[].LoadBalancerName" --output table
  ```

## Notes / gotchas

- **Durability is fixed at create time.** You can switch `sync`↔`async` via
  `ModifyReplicationGroup --durability`, but cannot enable/disable it later.
- **Changing `NumNodeGroups`** (resharding) or adding an AuthToken triggers
  replacement of the replication group — plan for a new endpoint / data loss.
- **First deploy builds a container image**; if the base-image pull fails,
  retry (public registries occasionally return transient errors).
- The failover button is enabled here via `ENABLE_FAILOVER_DEMO=true` on the
  task. It is POST-only and the task role scopes `elasticache:TestFailover` to
  this replication group.
