# Memory System Deployment Guide

This guide provides step-by-step instructions for deploying the agent memory system.

## Prerequisites

- AWS CLI configured with appropriate credentials
- Access to AWS account 786237623131
- Python 3.9+ installed
- Required Python packages: boto3, redis

## Deployment Checklist

### Phase 1: Infrastructure Setup

- [ ] 1.1 Review infrastructure requirements
- [ ] 1.2 Verify AWS credentials and permissions
- [ ] 1.3 Run infrastructure setup script
- [ ] 1.4 Verify cluster creation
- [ ] 1.5 Create HNSW vector indexes
- [ ] 1.6 Test cluster connectivity

### Phase 2: Application Configuration

- [ ] 2.1 Set environment variables
- [ ] 2.2 Validate configuration
- [ ] 2.3 Test memory client initialization
- [ ] 2.4 Verify embedding model access

### Phase 3: Integration Testing

- [ ] 3.1 Test memory storage operations
- [ ] 3.2 Test memory retrieval operations
- [ ] 3.3 Test agent workflow integration
- [ ] 3.4 Verify backward compatibility

### Phase 4: Monitoring Setup

- [ ] 4.1 Configure CloudWatch metrics
- [ ] 4.2 Set up alerting thresholds
- [ ] 4.3 Enable audit logging
- [ ] 4.4 Test health check endpoint

### Phase 5: Production Deployment

- [ ] 5.1 Deploy to staging environment
- [ ] 5.2 Run smoke tests
- [ ] 5.3 Deploy to production
- [ ] 5.4 Monitor initial traffic
- [ ] 5.5 Verify metrics collection

## Detailed Steps

### 1. Infrastructure Setup

#### 1.1 Run Infrastructure Script

**IMPORTANT**: This script creates resources in AWS account 786237623131. Review the script before running.

```bash
# Make script executable
chmod +x scripts/setup_memory_cluster.sh

# Run with appropriate AWS profile
AWS_PROFILE=your-profile ./scripts/setup_memory_cluster.sh
```

The script will:
- Create a dedicated VPC (10.1.0.0/16)
- Create ElastiCache/Valkey cluster (2 nodes, cache.r7g.xlarge)
- Configure TLS and encryption at rest
- Set up security groups

#### 1.2 Create Vector Indexes

```bash
# Run index creation script
python scripts/create_memory_indexes.py
```

This creates HNSW indexes for 1024-dimensional embeddings.

#### 1.3 Verify Cluster

```bash
# Test connectivity
redis-cli -h <cluster-endpoint> -p 6379 --tls ping
```

Expected output: `PONG`

### 2. Application Configuration

#### 2.1 Set Environment Variables

Create or update `.env` file:

```bash
# Copy example configuration
cp .env.example .env

# Edit with your cluster details
MEMORY_CLUSTER_ENDPOINT=your-cluster.cache.amazonaws.com
MEMORY_CLUSTER_PORT=6379
MEMORY_TLS_ENABLED=true
MEMORY_AWS_REGION=us-east-1
```

See [MEMORY_CONFIGURATION.md](./MEMORY_CONFIGURATION.md) for all options.

#### 2.2 Validate Configuration

```python
from agentic_shopping_demo.memory import MemoryConfig

config = MemoryConfig.from_env()
config.validate()
print("Configuration valid!")
```

#### 2.3 Test Memory Client

```python
from agentic_shopping_demo.memory import get_memory_client

client = get_memory_client()
health = client.health_check()
print(f"Health: {health}")
```

### 3. Integration Testing

#### 3.1 Test Memory Operations

```python
from agentic_shopping_demo.memory import get_memory_client, MemoryType

client = get_memory_client()

# Test add
memory_id = client.add(
    messages=[{"role": "user", "content": "I prefer running shoes"}],
    user_id="test:user123",
    memory_type=MemoryType.LONG_TERM
)
print(f"Stored: {memory_id}")

# Test search
memories = client.search(
    query="shoes",
    user_id="test:user123",
    limit=5
)
print(f"Found: {len(memories)} memories")

# Cleanup
client.delete(user_id="test:user123")
```

#### 3.2 Test Agent Integration

Run a test conversation and verify:
- Memories are retrieved before agent response
- Memories are stored after agent response
- No errors in logs

### 4. Monitoring Setup

#### 4.1 CloudWatch Metrics

Key metrics to monitor:
- Memory operation latency (p50, p95, p99)
- Memory storage size per user
- Retrieval hit rate
- Error rate

#### 4.2 Alerting Thresholds

Recommended alerts:
- Latency p99 > 150ms
- Error rate > 1%
- Storage per user > 10MB
- Cluster CPU > 80%

#### 4.3 Health Check

Add health check to monitoring:

```bash
curl http://localhost:8000/memory/health
```

Expected response:
```json
{
  "status": "healthy",
  "connected": true,
  "cluster_endpoint": "...",
  "latency_ms": 5.2
}
```

### 5. Production Deployment

#### 5.1 Staging Deployment

1. Deploy to staging environment
2. Run full test suite
3. Monitor for 24 hours
4. Review metrics and logs

#### 5.2 Production Deployment

1. Schedule deployment during low-traffic window
2. Deploy application with memory system enabled
3. Monitor initial traffic closely
4. Be prepared to rollback if issues occur

#### 5.3 Rollback Plan

If issues occur:

1. Set `MEMORY_ENABLED=false` in environment
2. Restart application
3. Memory system will be disabled, agent continues normally
4. Investigate issues in staging

## Troubleshooting

### Connection Errors

**Symptom**: `MemoryConnectionError` in logs

**Solutions**:
1. Verify cluster endpoint and port
2. Check security group allows connections
3. Verify TLS settings
4. Check AWS credentials

### High Latency

**Symptom**: Memory operations taking >150ms

**Solutions**:
1. Check cluster performance metrics
2. Increase connection pool size
3. Reduce search limit
4. Enable caching

### Storage Growth

**Symptom**: Storage growing faster than expected

**Solutions**:
1. Review memory extraction logic
2. Reduce TTL values
3. Enable PII filtering
4. Increase search threshold

### Memory Not Retrieved

**Symptom**: Memories stored but not retrieved

**Solutions**:
1. Check search threshold (may be too high)
2. Verify user_id matches between store and retrieve
3. Check memory hasn't expired
4. Review search filters

## Rollback Procedure

### Emergency Rollback

If critical issues occur:

```bash
# Disable memory system immediately
export MEMORY_ENABLED=false

# Restart application
systemctl restart shopnow-agent
```

### Graceful Rollback

For planned rollback:

1. Set `MEMORY_ENABLED=false` in configuration
2. Deploy updated configuration
3. Monitor for 1 hour
4. If stable, proceed with investigation

## Post-Deployment Validation

After deployment, verify:

- [ ] Health check returns healthy status
- [ ] Memory operations complete in <150ms
- [ ] No errors in application logs
- [ ] Metrics are being collected
- [ ] Alerts are configured
- [ ] Agent responses include memory context
- [ ] Memories are being stored correctly

## Maintenance

### Daily Tasks

- Review error logs
- Check storage growth
- Monitor latency metrics

### Weekly Tasks

- Review memory quality
- Analyze retrieval hit rate
- Check for PII leaks

### Monthly Tasks

- Review retention policies
- Analyze storage costs
- Update documentation

## Security Considerations

1. **Cluster Access**: Restrict to application security group only
2. **TLS**: Always enabled in production
3. **Encryption**: Enable encryption at rest
4. **PII**: Enable PII filtering
5. **Audit Logs**: Enable and review regularly
6. **Credentials**: Rotate regularly

## Support

For issues or questions:
1. Check logs: `/var/log/shopnow/memory.log`
2. Review metrics in CloudWatch
3. Consult [MEMORY_CONFIGURATION.md](./MEMORY_CONFIGURATION.md)
4. Contact platform team

## Appendix: AWS Resources Created

The infrastructure script creates:

- VPC: `shopnow-memory-vpc` (10.1.0.0/16)
- Subnets: 2 private subnets across AZs
- Security Group: `shopnow-memory-sg`
- ElastiCache Cluster: `shopnow-memory-cluster`
  - Engine: Valkey
  - Node Type: cache.r7g.xlarge
  - Nodes: 2 (with automatic failover)
  - TLS: Enabled
  - Encryption at rest: Enabled
- Parameter Group: `shopnow-memory-params`

**Cost Estimate**: ~$200-300/month for the cluster
