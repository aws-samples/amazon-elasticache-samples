# Memory System Configuration

This document describes all configuration options for the agent memory system using mem0.

## Environment Variables

### Cluster Configuration

```bash
# Memory cluster endpoint (required)
MEMORY_CLUSTER_ENDPOINT=your-cluster.cache.amazonaws.com

# Memory cluster port (default: 6379)
MEMORY_CLUSTER_PORT=6379

# Enable TLS for cluster connection (default: true)
MEMORY_TLS_ENABLED=true

# AWS region for Bedrock embeddings (default: us-east-1)
MEMORY_AWS_REGION=us-east-1
```

### Embedding Configuration

```bash
# Bedrock embedding model (default: amazon.titan-embed-text-v2:0)
MEMORY_EMBEDDING_MODEL=amazon.titan-embed-text-v2:0

# Embedding dimension (default: 1024 for Titan v2)
MEMORY_EMBEDDING_DIM=1024
```

### Retention Policies

```bash
# Short-term memory TTL in hours (default: 24)
MEMORY_SHORT_TERM_TTL_HOURS=24

# Long-term memory TTL in days (default: 90)
MEMORY_LONG_TERM_TTL_DAYS=90

# Maximum retention period in days (default: 365)
MEMORY_MAX_RETENTION_DAYS=365
```

### Search Configuration

```bash
# Maximum search results (default: 5)
MEMORY_SEARCH_LIMIT=5

# Minimum relevance score threshold (default: 0.7)
MEMORY_SEARCH_THRESHOLD=0.7

# Search timeout in seconds (default: 5.0)
MEMORY_SEARCH_TIMEOUT=5.0
```

### Privacy Settings

```bash
# Enable PII filtering (default: true)
MEMORY_PII_FILTER_ENABLED=true

# Enable sensitive data hashing (default: true)
MEMORY_HASH_SENSITIVE_DATA=true

# Salt for hashing (default: auto-generated)
MEMORY_HASH_SALT=your-secret-salt
```

### Performance Tuning

```bash
# Connection pool size (default: 10)
MEMORY_POOL_SIZE=10

# Operation timeout in seconds (default: 10.0)
MEMORY_OPERATION_TIMEOUT=10.0

# Enable caching (default: true)
MEMORY_CACHE_ENABLED=true

# Cache TTL in seconds (default: 300)
MEMORY_CACHE_TTL=300
```

### Feature Flags

```bash
# Enable memory system (default: true)
MEMORY_ENABLED=true

# Enable async operations (default: true)
MEMORY_ASYNC_ENABLED=true

# Enable audit logging (default: true)
MEMORY_AUDIT_ENABLED=true
```

## Example .env Configuration

```bash
# Minimal configuration
MEMORY_CLUSTER_ENDPOINT=shopnow-memory.cache.amazonaws.com
MEMORY_CLUSTER_PORT=6379
MEMORY_TLS_ENABLED=true
MEMORY_AWS_REGION=us-east-1

# Retention policies
MEMORY_SHORT_TERM_TTL_HOURS=24
MEMORY_LONG_TERM_TTL_DAYS=90

# Search settings
MEMORY_SEARCH_LIMIT=5
MEMORY_SEARCH_THRESHOLD=0.7

# Privacy
MEMORY_PII_FILTER_ENABLED=true
MEMORY_HASH_SENSITIVE_DATA=true
```

## Configuration Validation

The memory system validates configuration on startup. Invalid settings will log warnings and use defaults.

To validate configuration manually:

```python
from agentic_shopping_demo.memory import MemoryConfig

config = MemoryConfig.from_env()
config.validate()  # Raises ValueError if invalid
```

## Default Values

| Setting | Default | Valid Range |
|---------|---------|-------------|
| MEMORY_CLUSTER_PORT | 6379 | 1-65535 |
| MEMORY_TLS_ENABLED | true | true/false |
| MEMORY_SHORT_TERM_TTL_HOURS | 24 | 1-168 (1 week) |
| MEMORY_LONG_TERM_TTL_DAYS | 90 | 1-365 |
| MEMORY_MAX_RETENTION_DAYS | 365 | 1-730 (2 years) |
| MEMORY_SEARCH_LIMIT | 5 | 1-50 |
| MEMORY_SEARCH_THRESHOLD | 0.7 | 0.0-1.0 |
| MEMORY_SEARCH_TIMEOUT | 5.0 | 0.1-30.0 |
| MEMORY_POOL_SIZE | 10 | 1-100 |
| MEMORY_OPERATION_TIMEOUT | 10.0 | 0.1-60.0 |
| MEMORY_CACHE_TTL | 300 | 60-3600 |

## Troubleshooting

### Connection Issues

If you see connection errors:

1. Verify cluster endpoint and port
2. Check TLS settings match cluster configuration
3. Verify security group allows connections
4. Check AWS credentials have access to cluster

### Performance Issues

If memory operations are slow:

1. Increase connection pool size
2. Reduce search limit
3. Increase search threshold
4. Enable caching
5. Check cluster performance metrics

### Storage Issues

If storage is growing too fast:

1. Reduce TTL values
2. Enable PII filtering
3. Increase search threshold (store less)
4. Review memory extraction logic

## Security Best Practices

1. **Use TLS**: Always enable TLS for production
2. **Rotate salts**: Change MEMORY_HASH_SALT periodically
3. **Enable PII filtering**: Prevent storing sensitive data
4. **Audit logging**: Enable for compliance
5. **Access control**: Restrict cluster access via security groups
6. **Encryption at rest**: Enable on ElastiCache cluster
