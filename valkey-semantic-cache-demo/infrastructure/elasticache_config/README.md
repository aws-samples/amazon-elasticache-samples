# ElastiCache Vector Index Configuration

This directory chiefly consists of scripts for creating and managing the HNSW vector index. This index is instrumental for semantic caching.

## Prerequisites

1. ElastiCache (Valkey) cluster must be running
2. Python dependencies installed via `agents/` environment

## Running Scripts

Scripts use the `agents` virtual environment (which includes `valkey-glide-sync`):

```bash
# From repository root
cd agents && uv run python ../infrastructure/elasticache_config/create_vector_index.py
```

## Environment Variables

| Variable               | Default     | Description               |
| ---------------------- | ----------- | ------------------------- |
| `ELASTICACHE_ENDPOINT` | `localhost` | Cluster endpoint hostname |
| `ELASTICACHE_PORT`     | `6379`      | Cluster port              |

Example with ElastiCache endpoint:

```bash
ELASTICACHE_ENDPOINT=semantic-cache-valkey.abc123.cache.amazonaws.com \
ELASTICACHE_PORT=6379 \
cd agents && uv run python ../infrastructure/elasticache_config/create_vector_index.py
```

## Vector Index Schema

### Index Name: `idx:requests`

| Field        | Type          | Description                        |
| ------------ | ------------- | ---------------------------------- |
| `request_id` | TAG           | Unique identifier for cache lookup |
| `embedding`  | VECTOR (HNSW) | 1536-dimensional Titan embedding   |
| `timestamp`  | NUMERIC       | Unix timestamp for ordering/TTL    |

### HNSW Parameters

| Parameter         | Value  | Rationale                                     |
| ----------------- | ------ | --------------------------------------------- |
| `DIM`             | 1536   | Titan Embeddings output dimensions            |
| `DISTANCE_METRIC` | COSINE | Best for normalized semantic embeddings       |
| `M`               | 16     | Connections per node (balanced recall/memory) |
| `EF_CONSTRUCTION` | 200    | Build-time quality (higher = better index)    |

## How Keys and Indexes Relate

Valkey vector search separates **data storage** from **search indexing**:

**Data lives in Redis keys** (HASH type):

```
request:vector:550e8400-e29b-41d4-a716-446655440000  ← actual key
```

**The index watches a key prefix** and makes matching keys searchable:

```python
"PREFIX", "1", "request:vector:",  # Index all keys starting with this
```

So when we run `FT.SEARCH idx:requests ...`, Valkey searches across all HASH keys that match the `request:vector:*` pattern. The index (`idx:requests`) is a search layer on top of the underlying keys — it doesn't store data itself.

### Example: Storing a Cached Request

```bash
# Store data in a HASH key (automatically indexed by idx:requests)
HSET request:vector:550e8400-e29b-41d4-a716-446655440000
  request_id "550e8400-e29b-41d4-a716-446655440000"
  embedding <binary float32 vector>
  timestamp 1700000000
```

The index picks this up because the key starts with `request:vector:`.

## Related Data Structures

### Request-Response Store (separate from index)

Stores the actual request/response content, linked by `request_id`:

```
Key: rr:{request_id}
Hash:
  - request_text (string)
  - response_text (string)
  - tokens_input (numeric)
  - tokens_output (numeric)
  - cost_dollars (numeric)
  - created_at (timestamp)
```

## Similarity Search

Query the index with a vector to find semantically similar cached requests:

```
FT.SEARCH idx:requests
  "*=>[KNN 5 @embedding $query_vec AS score]"
  PARAMS 2 query_vec <binary_vector>
  RETURN 2 request_id score
  SORTBY score
  DIALECT 2
```

### Similarity Threshold (Application-Side)

`FT.SEARCH` with KNN returns the K nearest neighbors **regardless of how similar they are**. The 0.85 similarity threshold is enforced in application code (the @entrypoint), not in the query itself:

```python
# Pseudocode in @entrypoint
results = ft_search(index, query_vector, k=1)
if results and results[0].score >= 0.85:
    return cached_response  # Cache hit
else:
    response = invoke_agent()  # Cache miss
    cache_response(response)
```

This gives us flexibility to adjust the threshold without recreating the index.

## Troubleshooting

### "Unknown index name" error

The index doesn't exist yet. Run `create_vector_index.py`.

### "unknown command 'FT.CREATE'" error

Vector search module not loaded. Ensure you're using Valkey 8.2+ with the search module enabled.

### Connection refused

- Verify cluster is running: Check AWS Console → ElastiCache
- Verify security group allows your IP/VPC
- For local testing, ensure you're in the same VPC or using SSH tunnel
