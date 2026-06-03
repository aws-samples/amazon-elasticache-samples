# Long-Term Memory - Quick Start

## TL;DR

Pass `userId` in your API request to enable long-term memory:

```json
{
  "chatId": "session-123",
  "userId": "user-456",  // ← Add this for long-term memory
  "prompt": "I prefer Nike running shoes"
}
```

## Setup (One-Time)

```bash
# 1. Run index fix
python fix_index_for_elasticache.py

# 2. Start SSM tunnels (both required)
# Terminal 1: Semantic cache
aws ssm start-session --target i-0594295b8314b037f \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["master.shopnow-cache.b8bui8.use1.cache.amazonaws.com"],"portNumber":["6379"],"localPortNumber":["6379"]}'

# Terminal 2: Memory cluster
aws ssm start-session --target i-0594295b8314b037f \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["master.shopnow-memory-cluster.b8bui8.use1.cache.amazonaws.com"],"portNumber":["6379"],"localPortNumber":["6380"]}'
```

## How It Works

### Without userId (Anonymous)
- Only session memories stored
- Memories expire after 24 hours
- No cross-session memory

### With userId (Authenticated)
- Session memories + user memories stored
- User memories persist for 90 days
- Cross-session memory retrieval

## What Gets Remembered

### Long-Term (User Preferences)
- Brand preferences: "I prefer Nike"
- Size/fit: "I wear size 10"
- Style: "I like minimalist designs"
- Budget: "My budget is $100-150"
- Patterns: "I'm a marathon runner"

### Short-Term (Session Context)
- Current search: "Looking for shoes today"
- Temporary needs: "Need something for this weekend"
- Session-specific: "Show me more options"

## Testing

```bash
# Test storage
curl -X POST http://localhost:8000/invocations \
  -H "Content-Type: application/json" \
  -d '{"chatId":"test-1","userId":"user-123","prompt":"I prefer Nike shoes"}'

# Test retrieval (new session, same user)
curl -X POST http://localhost:8000/invocations \
  -H "Content-Type: application/json" \
  -d '{"chatId":"test-2","userId":"user-123","prompt":"Show me running shoes"}'
```

## Monitoring

```bash
# Check if memories are being stored
redis-cli -h localhost -p 6380 --tls --insecure KEYS "mem0:shopnow_memory:*" | wc -l

# Check index status
redis-cli -h localhost -p 6380 --tls --insecure FT.INFO shopnow_memory | grep num_docs

# View application logs
grep "\[MEMORY\]" application.log
```

## Troubleshooting

### No memories stored
- Check: `MEMORY_ENABLED=true` in `.env`
- Check: SSM tunnel running on port 6380
- Check: Application logs for errors

### No memories retrieved
- Check: `userId` is being passed in request
- Check: Memories exist in Valkey
- Check: Index has documents (`num_docs > 0`)

### Index shows num_docs: 0
- Run: `python fix_index_for_elasticache.py`
- This fixes the ElastiCache Valkey 8.2 bug

## Full Documentation

See `docs/LONG_TERM_MEMORY_GUIDE.md` for complete details.
