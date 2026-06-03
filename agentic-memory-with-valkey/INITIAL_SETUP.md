# Initial Setup Guide

Two things to set up: AWS infrastructure (automated by script) and Bedrock Knowledge Bases (manual, console only).

---

## Before You Start

### Local tools required

| Tool | Install |
|---|---|
| AWS CLI | https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html |
| Python 3.13 | `mise use python@3.13` or https://python.org |
| hatch | `pipx install hatch` |
| Node.js 18+ | https://nodejs.org |

Run `aws configure` to set up credentials before proceeding.

### Bedrock model access (manual — console only)

This must be done in the AWS Console **before** running the setup script. Go to **Amazon Bedrock → Model access** and enable:

- Anthropic Claude Sonnet 4.5
- Anthropic Claude Haiku 4.5
- Amazon Titan Embed Text v2

Model access is per-region — make sure you enable it in the same region you'll deploy to.

---

## Step 1 — Run the Setup Script

The script provisions all AWS infrastructure automatically:

- VPC, subnets, internet gateway, route tables
- Security group
- ElastiCache subnet group
- ElastiCache Valkey cluster (cache.t3.micro, single node)
- IAM role + instance profile for SSM
- EC2 jump host (t3.micro, Amazon Linux 2023, SSM-only)
- Valkey HNSW vector indexes (4 indexes)
- Outputs a `.env` file with all connection details

```bash
chmod +x scripts/setup_infra.sh
./scripts/setup_infra.sh
```

To use a different region or cluster name:

```bash
AWS_REGION=us-west-2 CLUSTER_ID=my-cache ./scripts/setup_infra.sh
```

The script takes about 10–15 minutes (most of that is waiting for ElastiCache to become available). It's idempotent — safe to re-run if something fails partway through.

> **Already have an ElastiCache cluster?** Skip the script and just create the indexes manually. Open the SSM tunnel to your cluster, then run:
> ```bash
> SHOPNOW_CACHE_ENDPOINT=localhost SHOPNOW_CACHE_PORT=6379 \
>   hatch run python3 scripts/create_cache_indexes.py
> ```
> Then fill in your `.env` manually using the template in Step 3.

---

## Step 2 — Create the Six Bedrock Knowledge Bases

This step is manual — the Bedrock console wizard handles IAM roles and vector store creation automatically, which is much easier than scripting it.

The agent uses six Knowledge Bases. For each one:

1. Go to **Amazon Bedrock → Knowledge Bases → Create knowledge base**
2. Name it (see table below)
3. Choose **Amazon S3** as the data source — upload your documents to an S3 bucket first
4. Select **Titan Embed Text v2** as the embeddings model
5. Complete the wizard — it creates the OpenSearch Serverless collection automatically
6. After creation, click **Sync** to index your documents
7. Copy the **Knowledge Base ID** from the KB detail page

### What to put in each KB

| Env Variable | KB Name | Content |
|---|---|---|
| `KB_PRODUCT` | Product Knowledge | Product descriptions, specs, reviews, sizing guides |
| `KB_STORE_OPS` | Store Operations | Store hours, curbside pickup, in-store return rules |
| `KB_TROUBLESHOOT` | Troubleshooting | Setup guides, known issues, past case resolutions |
| `KB_VENDOR` | Vendor/Seller | Seller return policies, partner handling instructions |
| `KB_POLICY` | Policies | Shipping, returns, refunds, warranty, compliance policies |
| `KB_CS` | Customer Service | Agent SOPs, response macros, escalation paths, incident bulletins |

> For the demo, you can start with just `KB_POLICY` and `KB_PRODUCT` — the agent degrades gracefully when other KBs are empty.

---

## Step 3 — Fill In KB IDs

Open the `.env` file the setup script created and fill in the KB IDs:

```bash
KB_PRODUCT=<paste-kb-id-here>
KB_STORE_OPS=<paste-kb-id-here>
KB_TROUBLESHOOT=<paste-kb-id-here>
KB_VENDOR=<paste-kb-id-here>
KB_POLICY=<paste-kb-id-here>
KB_CS=<paste-kb-id-here>
```

---

## Step 4 — Start the SSM Tunnel

The setup script printed the exact tunnel command at the end. It looks like:

```bash
aws ssm start-session \
  --target <instance-id> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["<elasticache-endpoint>"],"portNumber":["6379"],"localPortNumber":["6379"]}' \
  --region <your-region>
```

Keep this terminal open while the app is running.

> Stop the EC2 instance when not demoing to avoid charges:
> `aws ec2 stop-instances --instance-ids <instance-id> --region <your-region>`

---

## Step 5 — Run the App

```bash
# Source the env file
source .env

# Start
hatch run start
```

Open http://localhost:5173

---

## Cleanup

See the **Cleanup — Avoiding AWS Costs** section in [README.md](./README.md).

The setup script creates resources tagged `accent-apparel-demo` where possible, making them easy to find and delete.
