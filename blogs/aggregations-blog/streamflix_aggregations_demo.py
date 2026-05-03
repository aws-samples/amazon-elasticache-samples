#!/usr/bin/env python3
"""
StreamFlix Aggregations Demo
=============================
Companion script for the AWS blog post:
"Announcing aggregations on Amazon ElastiCache"

This script demonstrates three aggregation patterns on ElastiCache for Valkey 9.0:
  1. Faceted filtering  — drill-down counts for catalog browsing
  2. Live trending       — top content per genre by real-time views
  3. Studio reporting    — engagement metrics grouped by production studio

Aggregations run directly in-memory on the ElastiCache cluster, returning results
with microsecond latency across shards without a separate analytics layer.


Prerequisites
-------------
1. Python 3.8 or later
     macOS:   brew install python3
     Amazon Linux 2023:  sudo dnf install python3
     Ubuntu:  sudo apt install python3 python3-pip

2. redis-py <= 5.1.1
     pip install "redis<=5.1.1"

   Why this version?
   - redis-py >= 5.2.0 adds a SCORER TFIDF argument to every FT.AGGREGATE
     command. Valkey 9.0 does not support SCORER on aggregations, so all
     aggregation queries fail with "Unexpected argument `SCORER`".
   - redis-py >= 5.2.0 also routes FT.CREATE to a single shard instead of
     broadcasting it to all shards. This means the index is only created on
     one shard and aggregation results are incomplete.
   - redis-py <= 5.1.1 does not send SCORER and correctly broadcasts
     FT.CREATE to every shard in cluster mode.

   Why redis-py and not valkey-py?
   - valkey-py (as of 6.1.1) does not include a search module. Importing
     from valkey.search raises ModuleNotFoundError. redis-py includes full
     search and aggregation support out of the box.

3. AWS CLI (for cluster creation/cleanup)
     https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html

4. An ElastiCache for Valkey 9.0 cluster (cluster mode enabled)
   See the "Cluster setup" section below for the CLI command.

5. Network access to the cluster
   This script must run from a host that can reach the cluster endpoint on
   port 6379 — typically an EC2 instance in the same VPC. If transit
   encryption is enabled (recommended), the script connects with TLS
   automatically (ssl=True).


Cluster setup (CLI)
-------------------
Uncomment and run the command below to create a cluster. It provisions two
shards with one replica each, transit encryption, Multi-AZ, and automatic
failover. Estimated creation time: 8–12 minutes.

# aws elasticache create-replication-group \
#     --replication-group-id streamflix-cache \
#     --replication-group-description "StreamFlix Valkey cluster" \
#     --engine valkey \
#     --engine-version 9.0 \
#     --transit-encryption-enabled \
#     --cache-node-type cache.r7g.large \
#     --num-node-groups 2 \
#     --replicas-per-node-group 1 \
#     --multi-az-enabled \
#     --automatic-failover-enabled

Wait for the cluster status to become "available":

# aws elasticache describe-replication-groups \
#     --replication-group-id streamflix-cache \
#     --query "ReplicationGroups[0].{Status:Status,Endpoint:ConfigurationEndpoint}"

Then set VALKEY_CLUSTER_ENDPOINT below to the ConfigurationEndpoint Address.


Cluster cleanup (CLI)
---------------------
Delete the cluster when you are done to avoid ongoing charges:

# aws elasticache delete-replication-group \
#     --replication-group-id streamflix-cache


Usage
-----
  python3 streamflix_aggregations_demo.py
"""

import csv
import io
import time
import urllib.request
import pprint

import redis
from redis.commands.search.field import TextField, TagField, NumericField
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from redis.commands.search.aggregation import AggregateRequest, Desc
from redis.commands.search import reducers


def rows_to_dicts(rows):
    """Convert aggregate result rows to a list of dicts.

    redis-py returns each aggregate row as a flat list of alternating
    field names and values, e.g. ['genre', 'drama', 'count', '6'].
    This helper turns each row into a dict: {'genre': 'drama', 'count': '6'}.
    """
    result = []
    for row in rows:
        if isinstance(row, dict):
            result.append(row)
        elif isinstance(row, (list, tuple)):
            result.append(dict(zip(row[::2], row[1::2])))
        else:
            result.append(dict(row))
    return result


# ═══════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════

# <Input required>: Replace with your cluster's configuration endpoint.
VALKEY_CLUSTER_ENDPOINT = "mycluster.cnxa6h.clustercfg.use1.cache.amazonaws.com"
VALKEY_PORT = 6379

# URL for the sample catalog data (50 movie titles with metadata).
CATALOG_CSV_URL = (
    "https://github.com/aws-samples/amazon-elasticache-samples/blogs/aggregations-blog/catalog_data.csv"
)


# ═══════════════════════════════════════════════════════════════════════
# Connect
# ═══════════════════════════════════════════════════════════════════════

# If --transit-encryption-enabled was set during cluster creation, the
# client must connect over TLS. Set ssl=True to enable this.
client = redis.RedisCluster(
    host=VALKEY_CLUSTER_ENDPOINT,
    port=VALKEY_PORT,
    decode_responses=True,
    ssl=True,
)

print("Connected to", VALKEY_CLUSTER_ENDPOINT)


# ═══════════════════════════════════════════════════════════════════════
# Step 1 — Create an index on the data
# ═══════════════════════════════════════════════════════════════════════
# Genre, language, and studio are indexed as exact-match tags for faceted
# filtering. Release_year, rating, and views_24h are indexed as numeric
# fields for range filters and sorting. Title is indexed as a full-text
# searchable field that supports keyword, prefix, and fuzzy matching.
#
# In cluster mode, FT.CREATE is broadcast to every shard so the index
# covers all hash keys regardless of which shard owns them.
# You can create the index before or after loading data. If keys matching
# the prefix already exist, Valkey Search backfills them automatically.

client.ft("catalog_index").create_index(
    fields=[
        TextField("title"),
        TagField("genre"),
        TagField("language"),
        TagField("studio"),
        NumericField("release_year"),
        NumericField("rating"),
        NumericField("views_24h"),
    ],
    definition=IndexDefinition(
        prefix=["title:"],
        index_type=IndexType.HASH,
    ),
)

print("Index 'catalog_index' created")


# ═══════════════════════════════════════════════════════════════════════
# Step 2 — Populate the store with catalog data
# ═══════════════════════════════════════════════════════════════════════
# Each row becomes a hash key (e.g. "title:1") with fields for title,
# genre, language, studio, release_year, rating, and views_24h.

response = urllib.request.urlopen(CATALOG_CSV_URL)
reader = csv.DictReader(io.TextIOWrapper(response))

count = 0
for row in reader:
    key = row.pop("id")
    client.hset(key, mapping=row)
    count += 1

print(f"Loaded {count} records")

# Allow time for background indexing to complete.
time.sleep(2)


# ═══════════════════════════════════════════════════════════════════════
# Step 3 — Faceted filters
# ═══════════════════════════════════════════════════════════════════════
# Faceted search lets a UI show how many products match each filter value
# as the user browses. This function takes the user's active filters,
# groups matching results by genre, language, and rating, and returns the
# count per group so the UI can display accurate facet numbers.
#
# The user's filters are passed in the query string so only matching
# documents enter the GROUPBY stage — this is a best practice to reduce
# the number of documents flowing through the pipeline.

def get_facet_counts(filters):
    # Build query string from user-selected filters
    clauses = []
    if "genre" in filters:
        clauses.append(f"@genre:{{{filters['genre']}}}")
    if "language" in filters:
        clauses.append(f"@language:{{{filters['language']}}}")
    if "min_rating" in filters:
        clauses.append(f"@rating:[{filters['min_rating']} +inf]")
    query = " ".join(clauses) if clauses else "@rating:[-inf +inf]"

    # Run an aggregation for each facet dimension
    dimensions = ["genre", "language", "rating"]
    facets = {}
    for dim in dimensions:
        req = AggregateRequest(query) \
            .load(f"@{dim}") \
            .group_by(f"@{dim}", reducers.count().alias("count"))
        facets[dim] = client.ft("catalog_index").aggregate(req).rows
    return facets


# Example: user filters for dramas in english, get counts for each dimension
facets = get_facet_counts({"genre": "drama", "language": "english"})

print("\n--- Faceted filter counts ---")
pprint.pprint({dim: rows_to_dicts(rows) for dim, rows in facets.items()})

# Expected output:
# {'genre': [{'genre': 'drama', 'count': '6'}],
#  'language': [{'language': 'english', 'count': '6'}],
#  'rating': [{'rating': '4', 'count': '4'},
#             {'rating': '5', 'count': '2'}]}


# ═══════════════════════════════════════════════════════════════════════
# Step 4 — Live trending items
# ═══════════════════════════════════════════════════════════════════════
# Retrieves the top trending title per genre based on the views_24h field
# that updates in real time as users watch content. Because indexes update
# synchronously on writes, results reflect the latest data with no
# polling, cache invalidation, or scheduled recomputation.
#
# MAX is passed to SORTBY so the engine tracks only the top results
# rather than sorting the entire working set — another best practice.

def get_trending_by_genre(limit=10):
    # Get the highest view count per genre
    # sorted by most popular genre first
    req = AggregateRequest("@rating:[-inf +inf]") \
        .load("@genre", "@views_24h") \
        .group_by("@genre", reducers.max("@views_24h").alias("max_views")) \
        .sort_by(Desc("@max_views"), max=limit)
    return client.ft("catalog_index").aggregate(req).rows


trending_by_genre = get_trending_by_genre()

print("\n--- Trending by genre ---")
pprint.pprint(rows_to_dicts(trending_by_genre))

# Expected output:
# [{'genre': 'action', 'max_views': '4500'},
#  {'genre': 'comedy', 'max_views': '3800'},
#  {'genre': 'thriller', 'max_views': '3600'},
#  {'genre': 'sci-fi', 'max_views': '3400'},
#  {'genre': 'drama', 'max_views': '3200'},
#  {'genre': 'animation', 'max_views': '3100'},
#  {'genre': 'romance', 'max_views': '2800'},
#  {'genre': 'horror', 'max_views': '2600'},
#  {'genre': 'documentary', 'max_views': '1900'}]


# ═══════════════════════════════════════════════════════════════════════
# Step 5 — Offline engagement reporting
# ═══════════════════════════════════════════════════════════════════════
# Computes studio-level metrics: title count, average rating, and total
# 24-hour engagement. This runs against the same in-memory index used for
# the user-facing queries above — no separate analytics cluster or ETL
# pipeline required.
#
# Note: The AVG reducer returns full-precision decimals (e.g.
# '4.3333333333' rather than '4.33'). You can add an APPLY stage with
# format() to round if needed.

def get_studio_report():
    # Studio performance: title count, average rating, total 24h views
    req = AggregateRequest("@rating:[-inf +inf]") \
        .load("@studio", "@rating", "@views_24h") \
        .group_by("@studio", reducers.count().alias("title_count"),
                             reducers.avg("@rating").alias("avg_rating"),
                             reducers.sum("@views_24h").alias("total_views")) \
        .sort_by(Desc("@total_views"))
    return client.ft("catalog_index").aggregate(req).rows


studio_report = get_studio_report()

print("\n--- Studio engagement report ---")
pprint.pprint(rows_to_dicts(studio_report))

# Expected output:
# [{'studio': 'StreamFlix Originals', 'title_count': '18',
#   'avg_rating': '4.3333333333', 'total_views': '46200'},
#  {'studio': 'Summit Pictures', 'title_count': '13',
#   'avg_rating': '3.8461538462', 'total_views': '30000'},
#  {'studio': 'Crimson Studios', 'title_count': '11',
#   'avg_rating': '4.4545454545', 'total_views': '23100'},
#  {'studio': 'Emerald Films', 'title_count': '8',
#   'avg_rating': '4', 'total_views': '13600'}]
