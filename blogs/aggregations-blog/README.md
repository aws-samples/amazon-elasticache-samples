# ElastiCache Aggregations Demo — StreamFlix

Companion code for the AWS blog post **"Announcing aggregations on Amazon ElastiCache"**.

This demo builds a faceted browsing and analytics engine for **StreamFlix**, a media streaming platform. It stores a 50-title content catalog in ElastiCache as hash keys and runs three aggregation query patterns directly against the in-memory data:

| Pattern | What it does | Use case |
|---|---|---|
| **Faceted filtering** | Groups matching catalog entries by genre, language, and rating; returns counts per group | E-commerce filter panels, catalog browsing |
| **Live trending** | Finds the highest-viewed title per genre, sorted by popularity | Trending feeds, recommendation rankings |
| **Studio reporting** | Computes title count, average rating, and total views per studio | Operational analytics, daily reports |

All three patterns run as single `FT.AGGREGATE` commands on the ElastiCache cluster with microsecond latency — no separate analytics layer, ETL pipeline, or scheduled recomputation required.

---

## Repository contents

```
├── README.md                          # This file
├── streamflix_aggregations_demo.py    # Complete runnable demo script
└── catalog_data.csv                   # Sample catalog (50 movie titles)
```

---

## Prerequisites

| Requirement | Details |
|---|---|
| **AWS account** | With permissions to create ElastiCache clusters and EC2 instances |
| **AWS CLI** | [Install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| **Python 3.9+** | `brew install python3` (macOS) · `sudo dnf install python3` (Amazon Linux 2023) · `sudo apt install python3 python3-pip` (Ubuntu) |
| **valkey-py ≥ 6.1.1** | `pip install valkey` |

### Why valkey-py?

[valkey-py](https://github.com/valkey-io/valkey-py) is the official Python client for Valkey. Starting with version 6.1.1, it includes a full search and aggregation module at `valkey.commands.search` with high-level classes for index creation (`TextField`, `TagField`, `NumericField`, `IndexDefinition`), aggregation (`AggregateRequest`, `Desc`), and reducers (`count`, `avg`, `sum`, `max`, `min`). The API is wire-compatible with Valkey Search and does not inject unsupported arguments like `SCORER`.

---

## Step 1 — Create an ElastiCache cluster

Create a Valkey 9.0 cluster with two shards, transit encryption, Multi-AZ, and automatic failover. Estimated creation time: 8–12 minutes.

```bash
aws elasticache create-replication-group \
    --replication-group-id streamflix-cache \
    --replication-group-description "StreamFlix Valkey cluster" \
    --engine valkey \
    --engine-version 9.0 \
    --transit-encryption-enabled \
    --cache-node-type cache.r7g.large \
    --num-node-groups 2 \
    --replicas-per-node-group 1 \
    --multi-az-enabled \
    --automatic-failover-enabled
```

Wait for the cluster to become available:

```bash
aws elasticache describe-replication-groups \
    --replication-group-id streamflix-cache \
    --query "ReplicationGroups[0].{Status:Status,Endpoint:ConfigurationEndpoint}"
```

Note the `ConfigurationEndpoint.Address` — you will need it in Step 3.

---

## Step 2 — Set up network access

ElastiCache clusters run inside a VPC and are not directly accessible from the public internet. You have two options for running the demo script.

### Option A — Run directly from an EC2 instance (recommended)

Launch an EC2 instance in the **same VPC** as your ElastiCache cluster.

```bash
aws ec2 run-instances \
    --image-id resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
    --instance-type t3.small \
    --key-name <your-key-pair> \
    --subnet-id <subnet-in-same-vpc> \
    --security-group-ids <sg-id> \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=streamflix-jumphost}]'
```

**Security group rules required:**

| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| EC2 → ElastiCache (outbound) | TCP | 6379 | ElastiCache security group | Allow the client to reach the cluster |
| ElastiCache (inbound) | TCP | 6379 | EC2 security group | Allow the cluster to accept connections |
| EC2 (inbound) | TCP | 22 | Your IP | SSH access to the instance |

You can configure this by adding the EC2 instance's security group as an inbound source on the ElastiCache cluster's security group:

```bash
# Find the ElastiCache cluster's security group
aws elasticache describe-cache-clusters \
    --cache-cluster-id streamflix-cache-0001-001 \
    --query "CacheClusters[0].SecurityGroups[0].SecurityGroupId" \
    --output text

# Allow inbound from the EC2 security group on port 6379
aws ec2 authorize-security-group-ingress \
    --group-id <elasticache-sg-id> \
    --protocol tcp \
    --port 6379 \
    --source-group <ec2-sg-id>
```

Then SSH into the instance and install dependencies:

```bash
ssh -i <your-key.pem> ec2-user@<ec2-public-ip>

sudo dnf install python3 python3-pip -y
pip3 install valkey
```

### Option B — SSH tunnel from your local machine

If you already have an EC2 instance in the same VPC, you can create an SSH tunnel to forward local traffic to the cluster:

```bash
ssh -i <your-key.pem> -N -L 6379:<cluster-config-endpoint>:6379 ec2-user@<ec2-public-ip>
```

This forwards `localhost:6379` to the ElastiCache cluster. In the demo script, set:

```python
VALKEY_CLUSTER_ENDPOINT = "localhost"
```

> **Note:** SSH tunneling works for single-node testing but may not route correctly to all shards in cluster mode. For full multi-shard aggregation testing, Option A (running directly from EC2) is recommended.

---

## Step 3 — Run the demo

Clone this repository onto the EC2 instance (or copy the files):

```bash
git clone https://github.com/aws-samples/amazon-elasticache-samples.git
cd amazon-elasticache-samples/blogs/aggregations-blog
```

Edit the script to set your cluster endpoint:

```bash
# Replace the placeholder endpoint with your actual cluster endpoint
sed -i 's|mycluster.cnxa6h.clustercfg.use1.cache.amazonaws.com|<your-cluster-endpoint>|' \
    streamflix_aggregations_demo.py
```

Run the script:

```bash
python3 streamflix_aggregations_demo.py
```

### Expected output

```
Connected to <your-cluster-endpoint>
Index 'catalog_index' created
Loaded 50 records

--- Faceted filter counts ---
{'genre': [{'genre': 'drama', 'count': '6'}],
 'language': [{'language': 'english', 'count': '6'}],
 'rating': [{'rating': '4', 'count': '4'}, {'rating': '5', 'count': '2'}]}

--- Trending by genre ---
[{'genre': 'action', 'max_views': '4500'},
 {'genre': 'comedy', 'max_views': '3800'},
 {'genre': 'thriller', 'max_views': '3600'},
 {'genre': 'sci-fi', 'max_views': '3400'},
 {'genre': 'drama', 'max_views': '3200'},
 {'genre': 'animation', 'max_views': '3100'},
 {'genre': 'romance', 'max_views': '2800'},
 {'genre': 'horror', 'max_views': '2600'},
 {'genre': 'documentary', 'max_views': '1900'}]

--- Studio engagement report ---
[{'studio': 'StreamFlix Originals', 'title_count': '18',
  'avg_rating': '4.3333333333', 'total_views': '46200'},
 {'studio': 'Summit Pictures', 'title_count': '13',
  'avg_rating': '3.8461538462', 'total_views': '30000'},
 {'studio': 'Crimson Studios', 'title_count': '11',
  'avg_rating': '4.4545454545', 'total_views': '23100'},
 {'studio': 'Emerald Films', 'title_count': '8',
  'avg_rating': '4', 'total_views': '13600'}]
```

---

## How it works

### Data model

Each movie title is stored as a hash key with the prefix `title:`:

```
title:1  →  { title: "The Last Horizon", genre: "action", language: "english",
              studio: "StreamFlix Originals", release_year: "2025",
              rating: "5", views_24h: "4500" }
```

### Index

A single Valkey Search index (`catalog_index`) covers all `title:*` keys. The index defines:

- **TAG fields** (`genre`, `language`, `studio`) — exact-match filtering for faceted search
- **NUMERIC fields** (`release_year`, `rating`, `views_24h`) — range filters and sorting
- **TEXT field** (`title`) — full-text keyword, prefix, and fuzzy search

In cluster mode, the index is automatically created on all shards so aggregations query the full dataset.

### Aggregation pipeline

Each query is built as a pipeline of stages:

```
FT.AGGREGATE catalog_index <query>
    LOAD <fields>        ← pull fields into the pipeline
    GROUPBY <field>      ← group rows by a dimension
    REDUCE <func>        ← compute COUNT, MAX, AVG, SUM per group
    SORTBY <field> DESC  ← order results
    MAX <n>              ← return only top N (optimization)
```

The output of each stage feeds into the next. Aggregations provide read-after-write consistency on primaries and scale across shards without client code changes.

---

## Clean up

Delete the ElastiCache cluster to stop incurring charges:

```bash
aws elasticache delete-replication-group --replication-group-id streamflix-cache
```

If you created an EC2 instance for this demo, terminate it as well:

```bash
aws ec2 terminate-instances --instance-ids <instance-id>
```

---

## Additional resources

- [ElastiCache aggregations documentation](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/search-aggregations.html)
- [Valkey Search documentation](https://valkey.io/docs/topics/search/)
- [ElastiCache for Valkey 9.0 release blog](https://aws.amazon.com/blogs/database/)
- [AWS re:Post for ElastiCache](https://repost.aws/tags/TARDe-88acTLSr8i9rSAfn_Q/amazon-elasticache)

---

## Security

See [CONTRIBUTING](https://github.com/aws-samples/amazon-elasticache-samples/blob/main/CONTRIBUTING.md) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](https://github.com/aws-samples/amazon-elasticache-samples/blob/main/LICENSE) file.
