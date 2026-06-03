# Long-Term Memory Implementation Guide

## Overview

The ShopNow agent now supports **long-term memory** that persists user preferences across sessions. This enables personalized experiences where the agent remembers user preferences, shopping history, and patterns.

## Architecture

### Memory Types

1. **Short-Term Memory** (Session-scoped)
   - Scoped to: `session_id`
   - TTL: 24 hours
   - Use case: Current conversation context, temporary preferences
   - Example: "User is looking for running shoes in this session"

2. **Long-Term Memory** (User-scoped)
   - Scoped to: `authenticated_user_id`
   - TTL: 90 days
   - Use case: User preferences, shopping patterns, persistent profile
   - Example: "User prefers Nike brand, size 10, neutral running shoes"

### Storage Backend

- **ElastiCache Valkey 8.2** with vector search
- **mem0 framework** for memory orchestration
- **7-field index** (workaround for ElastiCache Valkey 8.2 bug)

## Setup

### 1. Prerequisites

- ElastiCache Valkey 8.2 cluster with vector search enabled
- SSM tunnel to the cluster (port 6380)
- Environment variables configured in `.env`:

```bash
MEMORY_CACHE_ENDPOINT=localhost
MEMORY_CACHE_PORT=6380
MEMORY_ENABLED=true
```

### 2. Index Setup (One-Time)

Run the index fix script to create the correct 7-field index:

```bash
source venv/bin/activate
python fix_index_for_elasticache.py
```

This creates an index with only the fields mem0 actually writes, working around the ElastiCache Valkey 8.2 bug where documents with missing indexed fields fail to index silently.

### 3. Deployment

Ensure both SSM tunnels are running:

```bash
# Terminal 1: Semantic cache tunnel
aws ssm start-session \
  --target i-0594295b8314b037f \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["master.shopnow-cache.b8bui8.use1.cache.amazonaws.com"],"portNumber":["6379"],"localPortNumber":["6379"]}'

# Terminal 2: Memory cluster tunnel
aws ssm start-session \
  --target i-0594295b8314b037f \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["master.shopnow-memory-cluster.b8bui8.use1.cache.amazonaws.com"],"portNumber":["6379"],"localPortNumber":["6380"]}'
```

## Usage

### API Integration

The memory system is automatically integrated into the `/invocations` endpoint.

#### Request Format

To enable long-term memory, include the `userId` field in your request:

```json
{
  "chatId": "session-123",
  "userId": "user-456",  // Optional: enables long-term memory
  "prompt": "I'm looking for running shoes",
  "messages": [...]
}
```

**Without `userId`**: Only short-term (session) memories are stored/retrieved
**With `userId`**: Both short-term and long-term memories are stored/retrieved

### Memory Flow

#### 1. Pre-Turn (Memory Retrieval)

Before the agent processes the user's message:

```python
# Retrieves both short-term and long-term memories
memories = retrieve_memories(
    user_message=prompt,
    user_identifier=user_identifier,  # Contains userId if authenticated
    conversation_state=state,
    limit=5
)
```

Retrieved memories are stored in `state["_memories"]` for potential use.

#### 2. Post-Turn (Memory Storage)

After the agent responds:

```python
# Extracts and stores memories asynchronously
store_memories_async(
    user_message=prompt,
    agent_response=response,
    user_identifier=user_identifier,
    conversation_state=state
)
```

The system automatically:
- Extracts memory candidates from the conversation
- Classifies them as short-term or long-term
- Stores short-term memories with `session_id`
- Stores long-term memories with `authenticated_user_id` (if provided)

### Memory Extraction

The `MemoryExtractor` automatically identifies long-term preferences:

**Long-term indicators:**
- Brand preferences: "I prefer Nike", "I always buy Adidas"
- Size/fit preferences: "I wear size 10", "I need wide width"
- Style preferences: "I like minimalist designs", "I prefer bright colors"
- Shopping patterns: "I usually shop for trail running", "I'm a marathon runner"
- Budget preferences: "My budget is usually $100-150"

**Short-term indicators:**
- Current search: "Looking for shoes today"
- Temporary needs: "Need something for this weekend"
- Session-specific: "Can you show me more options?"

## Testing

### Test Long-Term Memory

```python
from agentic_shopping_demo.memory import get_memory_client
from agentic_shopping_demo.memory.user_identifier import UserIdentifier

client = get_memory_client()

# Store long-term memory
user_id = "test_user_123"
memory_id = client.add_long_term_memory(
    messages=[
        {"role": "user", "content": "I prefer Nike running shoes in size 10"},
        {"role": "assistant", "content": "Got it, you prefer Nike running shoes in size 10."}
    ],
    user_id=user_id,
    metadata={"domain": "preferences", "category": "footwear"}
)

print(f"Stored memory: {memory_id}")

# Retrieve memories
memories = client.search(
    query="what brand do I like",
    user_id=user_id,
    limit=5
)

for memory in memories:
    print(f"- {memory.content} (score: {memory.relevance_score})")
```

### Test End-to-End

```bash
# Start the application
uvicorn src.agentic_shopping_demo.api:app --reload

# Send request with userId
curl -X POST http://localhost:8000/invocations \
  -H "Content-Type: application/json" \
  -d '{
    "chatId": "test-session",
    "userId": "user-123",
    "prompt": "I prefer Nike running shoes"
  }'

# Later session - memories should be retrieved
curl -X POST http://localhost:8000/invocations \
  -H "Content-Type: application/json" \
  -d '{
    "chatId": "new-session",
    "userId": "user-123",
    "prompt": "Show me some running shoes"
  }'
```

## Monitoring

### Check Memory Storage

```bash
# Connect to Valkey
redis-cli -h localhost -p 6380 --tls --insecure

# Check index status
FT.INFO shopnow_memory

# Count memories
KEYS "mem0:shopnow_memory:*" | wc -l

# View a specific memory
HGETALL "mem0:shopnow_memory:<memory-id>"
```

### Application Logs

Look for these log messages:

```
[MEMORY] Retrieved 3 short-term memories
[MEMORY] Retrieved 2 long-term memories
[MEMORY] Retrieved 5 total memories (session=sess-123, user=user-456)
[MEMORY] Stored memories: short_term=1, long_term=2, total=3
```

## Troubleshooting

### No memories being stored

1. Check if `MEMORY_ENABLED=true` in `.env`
2. Verify SSM tunnel is running on port 6380
3. Check application logs for memory errors
4. Verify index exists: `redis-cli -h localhost -p 6380 --tls --insecure FT._LIST`

### No memories being retrieved

1. Check if `userId` is being passed in the request
2. Verify memories exist: `redis-cli -h localhost -p 6380 --tls --insecure KEYS "mem0:shopnow_memory:*"`
3. Check index has documents: `redis-cli -h localhost -p 6380 --tls --insecure FT.INFO shopnow_memory | grep num_docs`

### Index shows num_docs: 0

This is the ElastiCache Valkey 8.2 bug. Run the fix script:

```bash
python fix_index_for_elasticache.py
```

The script creates a 7-field index that matches what mem0 actually writes.

## Best Practices

### 1. User Authentication

Always pass `userId` for authenticated users to enable long-term memory:

```javascript
// Frontend example
const response = await fetch('/invocations', {
  method: 'POST',
  body: JSON.stringify({
    chatId: sessionId,
    userId: currentUser?.id,  // Pass authenticated user ID
    prompt: userMessage
  })
});
```

### 2. Privacy

- Only store long-term memories for authenticated users
- PII filtering is automatically applied
- Users can request memory deletion via `client.delete(user_id=user_id)`

### 3. Memory Limits

- Default: 5 memories per query (2-3 short-term, 2-3 long-term)
- Adjust via `limit` parameter in `retrieve_memories()`
- Higher limits = more context but slower retrieval

### 4. Memory Expiration

- Short-term: 24 hours (automatic)
- Long-term: 90 days (automatic)
- Manual expiration: `client.expire_session_memories(session_id)`

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     User Request                             │
│  { chatId, userId, prompt }                                  │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                  Memory Retrieval                            │
│  - Short-term: session_id → recent context                  │
│  - Long-term: user_id → preferences                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                  Agent Processing                            │
│  - Memories available in state["_memories"]                 │
│  - Agent generates response                                  │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                  Memory Storage                              │
│  - Extract candidates from conversation                      │
│  - Classify as short-term or long-term                       │
│  - Store with appropriate user_id/session_id                 │
└─────────────────────────────────────────────────────────────┘
```

## Future Enhancements

1. **Memory Injection**: Automatically inject memories into agent prompt
2. **Memory Management UI**: Allow users to view/edit/delete their memories
3. **Memory Analytics**: Track memory usage and effectiveness
4. **Cross-Session Patterns**: Detect patterns across multiple sessions
5. **Memory Consolidation**: Merge similar memories to reduce redundancy

## Support

For issues or questions:
- Check logs: `[MEMORY]` prefix
- Review ElastiCache metrics
- Verify index status with `FT.INFO shopnow_memory`
- Contact the team with memory IDs and user IDs for debugging
