# Memory System Deployment

Quick guide to deploy the memory system with your manually created ElastiCache cluster.

## Prerequisites

Your ElastiCache cluster is ready:
- Endpoint: `master.shopnow-memory-cluster.b8bui8.use1.cache.amazonaws.com:6379`
- TLS enabled
- In your VPC

## Step 1: Set Up SSM Tunnel (if cluster is in private subnet)

If your cluster is in a private subnet, create an SSM tunnel:

```bash
aws ssm start-session \
  --target i-0594295b8314b037f \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["master.shopnow-memory-cluster.b8bui8.use1.cache.amazonaws.com"],"portNumber":["6379"],"localPortNumber":["6380"]}'
```

Keep this terminal open. If using tunnel, update `.env`:
```bash
MEMORY_CACHE_ENDPOINT=localhost
MEMORY_CACHE_PORT=6380
```

## Step 2: Install Dependencies

```bash
pip install redis mem0ai
```

## Step 3: Create Vector Indexes

```bash
python scripts/create_memory_indexes.py
```

Expected output: `✅ All memory indexes created successfully`

## Step 4: Test Connection

```bash
python -c "
from agentic_shopping_demo.memory import get_memory_client
client = get_memory_client()
print(client.health_check())
"
```

Expected: `{"status": "healthy", "connected": true, ...}`

## Done!

Start your application - memory system is integrated and ready.

---

## Troubleshooting

**Connection fails**: 
- Check SSM tunnel is running (if using private subnet)
- Verify security group allows port 6379 from your source

**Index creation fails**: 
- Ensure Redis Stack/Valkey with vector search support
- Check cluster is accessible

**Disable memory**: Set `MEMORY_ENABLED=false` in .env
