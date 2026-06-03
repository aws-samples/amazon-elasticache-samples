# Agent Memory System

The ShopNow AI agent memory system provides short-term and long-term memory capabilities using the mem0 framework with ElastiCache/Valkey as the backend.

## Overview

The memory system enables the agent to:
- Remember user preferences and patterns across sessions
- Maintain context within a conversation session
- Provide personalized recommendations based on history
- Learn from user interactions over time

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Agent Workflow                          │
├─────────────────────────────────────────────────────────────┤
│  Pre-Turn: Retrieve Memories → Augment Context              │
│  Agent Processing → Generate Response                        │
│  Post-Turn: Extract & Store Memories (async)                │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Memory Components                         │
├─────────────────────────────────────────────────────────────┤
│  MemoryClient    │ Core CRUD operations                     │
│  MemoryExtractor │ Content extraction from conversations    │
│  UserIdentifier  │ User identification & tracking           │
│  PIIFilter       │ Privacy & data protection                │
│  ErrorHandler    │ Fault tolerance & graceful degradation   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                  mem0 Framework                              │
├─────────────────────────────────────────────────────────────┤
│  Vector Embeddings: Bedrock Titan v2 (1024-dim)            │
│  Vector Search: HNSW indexes                                 │
│  Storage: ElastiCache/Valkey cluster                        │
└─────────────────────────────────────────────────────────────┘
```

## Memory Types

### Short-Term Memory
- **Scope**: Session-scoped
- **TTL**: 24 hours
- **Use Cases**: 
  - Current conversation context
  - Active task state
  - Session-specific entities
- **Example**: "User is looking for black running shoes, size 10"

### Long-Term Memory
- **Scope**: User-scoped
- **TTL**: 90 days
- **Use Cases**:
  - User preferences
  - Behavioral patterns
  - Communication style
- **Example**: "User prefers minimalist running shoes under $150"

## Key Features

### 1. Automatic Memory Extraction
The system automatically extracts memory-worthy content from conversations:
- User preferences ("I prefer...", "I like...")
- Behavioral patterns (communication style, question patterns)
- Product recommendations and interactions
- Domain-specific context (commerce, orders, stores)

### 2. Privacy & Compliance
- **PII Detection**: Automatically detects and filters email, phone, SSN, credit cards, addresses
- **Sensitive Data Hashing**: One-way hashing of order IDs and tracking numbers
- **GDPR Compliance**: Data export and deletion capabilities
- **Audit Logging**: Complete audit trail for all operations

### 3. User Identification
Priority hierarchy for user identification:
1. Authenticated user ID (from auth system)
2. Anonymous user ID (persistent cookie/header)
3. Session ID (fallback)

Anonymous memories are automatically migrated when users authenticate.

### 4. Fault Tolerance
- **Circuit Breaker**: Prevents cascading failures
- **Retry Logic**: Exponential backoff for transient errors
- **Graceful Degradation**: Falls back to conversation state if memory system fails
- **Non-Blocking**: Memory operations never block agent responses

### 5. Performance
- **Target Latency**: <150ms for memory retrieval
- **Vector Search**: HNSW indexes for fast similarity search
- **Connection Pooling**: Efficient cluster access
- **Caching**: In-memory cache for hot memories

## Usage

### Basic Operations

```python
from agentic_shopping_demo.memory import get_memory_client, MemoryType

# Get client
client = get_memory_client()

# Store a memory
memory_id = client.add(
    messages=[
        {"role": "user", "content": "I prefer running shoes"},
        {"role": "assistant", "content": "I'll remember that preference"}
    ],
    user_id="user:123",
    metadata={"domain": "commerce", "preference_type": "product"},
    memory_type=MemoryType.LONG_TERM
)

# Search for memories
memories = client.search(
    query="What shoes does the user like?",
    user_id="user:123",
    limit=5,
    filters={"domain": "commerce"}
)

# Update a memory
client.update(
    memory_id=memory_id,
    data={"metadata": {"updated": True}}
)

# Delete memories
client.delete(user_id="user:123")
```

### Integration with Agent

The memory system is automatically integrated into the agent workflow:

```python
# Pre-turn: Memories are retrieved and added to context
# (happens automatically in api.py)

# Post-turn: Memories are extracted and stored asynchronously
# (happens automatically in api.py)
```

### User Identification

```python
from agentic_shopping_demo.memory import UserIdentifier

# Create identifier
user_id = UserIdentifier(
    authenticated_user_id="user123",  # From auth system
    anonymous_user_id=None,
    session_id="session456"
)

# Get effective user ID
effective_id = user_id.get_user_id()  # Returns "user:user123"

# Check user type
user_id.is_authenticated()  # True
user_id.is_anonymous()      # False
user_id.is_session_only()   # False
```

### Memory Migration

```python
from agentic_shopping_demo.memory import migrate_anonymous_memories

# When user authenticates, migrate their anonymous memories
count = migrate_anonymous_memories(
    from_anonymous_id="anon123",
    to_user_id="user456"
)
print(f"Migrated {count} memories")
```

## Configuration

See [MEMORY_CONFIGURATION.md](./MEMORY_CONFIGURATION.md) for detailed configuration options.

Minimal configuration:
```bash
MEMORY_CLUSTER_ENDPOINT=your-cluster.cache.amazonaws.com
MEMORY_CLUSTER_PORT=6379
MEMORY_TLS_ENABLED=true
MEMORY_AWS_REGION=us-east-1
```

## Deployment

See [MEMORY_DEPLOYMENT.md](./MEMORY_DEPLOYMENT.md) for deployment instructions.

Quick start:
1. Run infrastructure setup script
2. Create vector indexes
3. Set environment variables
4. Test connectivity
5. Deploy application

## Monitoring

### Key Metrics
- Memory operation latency (p50, p95, p99)
- Memory storage size per user
- Retrieval hit rate
- Error rate

### Health Check
```bash
curl http://localhost:8000/memory/health
```

### Logs
```bash
# Memory operations
tail -f /var/log/shopnow/memory.log

# Audit trail
tail -f /var/log/shopnow/memory-audit.log
```

## API Endpoints

Optional REST API for memory operations:

```bash
# Add memory
POST /memory/add
{
  "messages": [...],
  "user_id": "user:123",
  "memory_type": "long_term"
}

# Search memories
POST /memory/search
{
  "query": "running shoes",
  "user_id": "user:123",
  "limit": 5
}

# Update memory
PATCH /memory/{memory_id}
{
  "data": {"metadata": {...}}
}

# Delete memory
DELETE /memory/{memory_id}

# Export user data (GDPR)
GET /memory/export/{user_id}

# Health check
GET /memory/health
```

## Testing

### Test Mode
```python
from agentic_shopping_demo.memory import get_memory_client

# Create test client with isolated namespace
client = get_memory_client(test_mode=True)

# Use normally...
client.add(...)

# Cleanup
client.flush_test_namespace()
```

### Unit Tests
```bash
# Run memory system tests
pytest tests/memory/
```

## Troubleshooting

### Common Issues

**Memory not retrieved**
- Check search threshold (may be too high)
- Verify user_id matches
- Check memory hasn't expired

**High latency**
- Increase connection pool size
- Reduce search limit
- Enable caching

**Storage growing too fast**
- Reduce TTL values
- Enable PII filtering
- Review extraction logic

See [MEMORY_DEPLOYMENT.md](./MEMORY_DEPLOYMENT.md) for detailed troubleshooting.

## Security

- **TLS**: Always enabled in production
- **Encryption at Rest**: Enabled on ElastiCache cluster
- **PII Filtering**: Prevents storing sensitive data
- **Access Control**: Cluster restricted to application security group
- **Audit Logging**: Complete audit trail for compliance

## Performance Characteristics

- **Retrieval Latency**: <150ms (p99)
- **Storage Latency**: <100ms (p99)
- **Throughput**: 1000+ ops/sec
- **Storage**: ~1KB per memory entry
- **Scalability**: Horizontal scaling via cluster nodes

## Limitations

- Maximum 50 memories per search
- Maximum 10MB storage per user (configurable)
- 90-day maximum retention for long-term memories
- 365-day absolute maximum retention

## Future Enhancements

- Multi-modal memory (images, audio)
- Memory consolidation and summarization
- Federated memory across multiple agents
- Advanced privacy controls (user-configurable retention)
- Memory quality scoring and pruning

## Support

For issues or questions:
1. Check logs and metrics
2. Review documentation
3. Contact platform team

## References

- [mem0 Documentation](https://docs.mem0.ai/)
- [ElastiCache Documentation](https://docs.aws.amazon.com/elasticache/)
- [Bedrock Embeddings](https://docs.aws.amazon.com/bedrock/latest/userguide/embeddings.html)
