# Comprehensive Test Plan: Memory + Cache Systems

## Overview

This document provides detailed test scenarios for validating the AgenticShoppingDemo memory system (short-term and long-term) in combination with cache systems (Full App Cache and KB Cache).

## Test Environment Setup

### Prerequisites
- Two SSM tunnels running:
  - Semantic cache: `localhost:6379` → `master.shopnow-cache.b8bui8.use1.cache.amazonaws.com:6379`
  - Memory cluster: `localhost:6380` → `master.shopnow-memory-cluster.b8bui8.use1.cache.amazonaws.com:6380`
- Backend running: `hatch run start`
- Frontend built: `npm run build` (in react-ui folder)
- Access UI at: `http://localhost:5173`

### Configuration Files
- `.env` - Environment variables for memory and cache settings
- UI toggles - Memory and cache controls in the UI

---

## Scenario 1: Full System Test (Recommended)

### Settings
- **Short-Term Memory**: ON
- **Long-Term Memory**: ON
- **Full App Cache**: HOT
- **KB Cache**: ON

### Purpose
Validate all systems working together for optimal performance and user experience.

### Test Sequence

#### Session 1: Initial User - Build Profile
**User ID**: `user-test-001`
**Session ID**: Auto-generated (e.g., `session-abc123`)

**Test Steps**:
1. Open UI, ensure all toggles are ON (ST, LT, Full Cache HOT, KB Cache ON)
2. Start new conversation with user ID: `user-test-001`
3. Send message: "I'm looking for running shoes"
4. Send message: "I wear size 10.5"
5. Send message: "My budget is $150"
6. Send message: "I prefer lightweight shoes"
7. Send message: "I like neutral colors"

**Expected Results**:
- ✅ Agent responds with relevant product suggestions
- ✅ No memory badge on responses (first session, no memories to retrieve yet)
- ✅ Backend logs show:
  - `[MEMORY STORAGE] Stored long-term memory` (5 times for 5 preferences)
  - `[MEMORY STORAGE] Stored short-term memory` (for session context)
  - Cache MISS on first queries, then cache SET
- ✅ UI shows smooth responses

**Validation Queries**:
```bash
# Check memories stored
grep "Stored long-term memory" backend.log | wc -l  # Should be 5
grep "Stored short-term memory" backend.log | wc -l  # Should be 3+
```

---

#### Session 2: Same User - New Session (Cross-Session Persistence)
**User ID**: `user-test-001` (same user)
**Session ID**: Auto-generated NEW session (e.g., `session-xyz789`)

**Test Steps**:
1. Click "New Chat" button in the UI (this starts a new session with a new session ID)
2. Enter SAME user ID: `user-test-001`
3. Send message: "Show me running shoes"

**Note**: You don't need to explicitly "close" the old session. Clicking "New Chat" automatically creates a new session ID, which isolates short-term memories. The old session's short-term memories remain in Valkey but won't be retrieved in the new session.

**Expected Results**:
- ✅ Memory badge appears: "🧠 5 memories (5 user)" or similar
- ✅ Agent response references user's preferences (size 10.5, $150 budget, lightweight, neutral colors)
- ✅ Backend logs show:
  - `[MEMORY] Retrieved 5 long-term memories`
  - `[MEMORY] Retrieved 0 short-term memories` (new session)
  - Cache HIT for product queries
- ✅ Fast response time (<2 seconds)

**Validation Queries**:
```bash
# Check memory retrieval
grep "Retrieved.*long-term memories" backend.log | tail -1
# Should show: Retrieved 5 long-term memories

# Check cache hits
grep "cache_hit=True" backend.log | wc -l
# Should show multiple hits
```

---

#### Session 3: Different User (Cache Sharing)
**User ID**: `user-test-002` (different user)
**Session ID**: Auto-generated NEW session

**Test Steps**:
1. Close previous conversation
2. Start NEW conversation with NEW user ID: `user-test-002`
3. Send message: "Show me running shoes"

**Expected Results**:
- ✅ NO memory badge (new user, no memories)
- ✅ Agent responds with general product suggestions
- ✅ Backend logs show:
  - `[MEMORY] Retrieved 0 long-term memories`
  - `[MEMORY] Retrieved 0 short-term memories`
  - Cache HIT for product queries (shared cache from user-test-001)
- ✅ Fast response time (<2 seconds due to cache)

**Validation Queries**:
```bash
# Check no memories retrieved
grep "Retrieved 0.*memories" backend.log | tail -2

# Check cache hits
grep "cache_hit=True" backend.log | tail -5
```

---

## Scenario 2: Memory Only (No Cache)

### Settings
- **Short-Term Memory**: ON
- **Long-Term Memory**: ON
- **Full App Cache**: OFF
- **KB Cache**: OFF

### Purpose
Isolate memory system performance without cache interference.

### Test Sequence

#### Session 1: Build Profile
**User ID**: `user-test-003`

**Test Steps**:
1. Disable both cache toggles in UI
2. Start conversation with user ID: `user-test-003`
3. Send messages:
   - "I'm looking for running shoes"
   - "I wear size 9"
   - "My budget is $200"

**Expected Results**:
- ✅ Memories stored (3 LT, 2+ ST)
- ✅ No cache operations in logs
- ✅ Slower response times (no cache, full KB queries)

**Validation**:
```bash
# Check no cache operations
grep "cache_hit" backend.log | tail -10
# Should show cache_hit=False or no cache logs

# Check memory storage
grep "Stored.*memory" backend.log | tail -5
```

---

#### Session 2: New Session - Memory Retrieval
**User ID**: `user-test-003` (same user, new session)

**Test Steps**:
1. Start NEW conversation with user ID: `user-test-003`
2. Send message: "Show me running shoes"

**Expected Results**:
- ✅ Memory badge: "🧠 3 memories (3 user)"
- ✅ Agent uses preferences from memory
- ✅ No cache hits
- ✅ Response time 3-5 seconds (no cache)

**Validation**:
```bash
# Check memory retrieval
grep "Retrieved.*long-term memories" backend.log | tail -1

# Verify no cache
grep "cache_hit=True" backend.log | tail -10
# Should be empty or old entries
```

---

## Scenario 3: Cache Only (No Memory)

### Settings
- **Short-Term Memory**: OFF
- **Long-Term Memory**: OFF
- **Full App Cache**: HOT
- **KB Cache**: ON

### Purpose
Isolate cache system performance without memory.

### Test Sequence

#### Session 1: Initial Query
**User ID**: `user-test-004`

**Test Steps**:
1. Disable both memory toggles in UI
2. Enable both cache toggles
3. Start conversation with user ID: `user-test-004`
4. Send message: "Show me running shoes under $100"

**Expected Results**:
- ✅ No memory badge
- ✅ Cache MISS on first query
- ✅ Cache SET after response
- ✅ No memory storage logs

**Validation**:
```bash
# Check no memory operations
grep "\[MEMORY STORAGE\]" backend.log | tail -10
# Should be empty

# Check cache operations
grep "cache_hit=False" backend.log | tail -1
grep "Cache SET" backend.log | tail -1
```

---

#### Session 2: Same Query - Cache Hit
**User ID**: `user-test-005` (different user)

**Test Steps**:
1. Start NEW conversation with user ID: `user-test-005`
2. Send SAME message: "Show me running shoes under $100"

**Expected Results**:
- ✅ No memory badge
- ✅ Cache HIT
- ✅ Very fast response (<1 second)
- ✅ No KB queries in logs

**Validation**:
```bash
# Check cache hit
grep "cache_hit=True" backend.log | tail -1

# Check no KB queries
grep "Querying knowledge base" backend.log | tail -10
# Should be empty or old entries
```

---

## Scenario 4: Short-Term Memory Only

### Settings
- **Short-Term Memory**: ON
- **Long-Term Memory**: OFF
- **Full App Cache**: OFF
- **KB Cache**: OFF

### Purpose
Test within-session memory without cross-session persistence.

### Test Sequence

#### Session 1: Within-Session Context
**User ID**: `user-test-006`

**Test Steps**:
1. Enable ST memory, disable LT memory and caches
2. Start conversation with user ID: `user-test-006`
3. Send messages:
   - "I'm looking for running shoes"
   - "What sizes do you have?"
   - "Tell me more about the first one"

**Expected Results**:
- ✅ Short-term memories stored (session context)
- ✅ Memory badge shows session memories: "🧠 2 memories (2 session)"
- ✅ Agent maintains context within session
- ✅ No long-term memories stored

**Validation**:
```bash
# Check only short-term storage
grep "Stored short-term memory" backend.log | tail -5
grep "Stored long-term memory" backend.log | tail -5
# Should only see short-term
```

---

#### Session 2: New Session - No Persistence
**User ID**: `user-test-006` (same user, new session)

**Test Steps**:
1. Start NEW conversation with user ID: `user-test-006`
2. Send message: "What were we talking about?"

**Expected Results**:
- ✅ No memory badge (ST memories don't persist)
- ✅ Agent doesn't remember previous session
- ✅ Logs show 0 memories retrieved

**Validation**:
```bash
# Check no memories retrieved
grep "Retrieved 0.*memories" backend.log | tail -2
```

---

## Scenario 5: Long-Term Memory Only

### Settings
- **Short-Term Memory**: OFF
- **Long-Term Memory**: ON
- **Full App Cache**: OFF
- **KB Cache**: OFF

### Purpose
Test cross-session persistence without session context.

### Test Sequence

#### Session 1: Build Preferences
**User ID**: `user-test-007`

**Test Steps**:
1. Disable ST memory, enable LT memory, disable caches
2. Start conversation with user ID: `user-test-007`
3. Send messages:
   - "I prefer lightweight running shoes"
   - "I like blue colors"
   - "My budget is $120"

**Expected Results**:
- ✅ Long-term memories stored (3 preferences)
- ✅ No short-term memories stored
- ✅ No memory badge (no retrieval in first session)

**Validation**:
```bash
# Check only long-term storage
grep "Stored long-term memory" backend.log | tail -5
grep "Stored short-term memory" backend.log | tail -5
# Should only see long-term
```

---

#### Session 2: Preferences Persist
**User ID**: `user-test-007` (same user, new session)

**Test Steps**:
1. Start NEW conversation with user ID: `user-test-007`
2. Send message: "Show me running shoes"

**Expected Results**:
- ✅ Memory badge: "🧠 3 memories (3 user)"
- ✅ Agent uses preferences (lightweight, blue, $120)
- ✅ No session context (no ST memories)

**Validation**:
```bash
# Check long-term retrieval
grep "Retrieved.*long-term memories" backend.log | tail -1
# Should show 3 memories

# Check no short-term
grep "Retrieved.*short-term memories" backend.log | tail -1
# Should show 0
```

---

## Scenario 6: Baseline (All Systems OFF)

### Settings
- **Short-Term Memory**: OFF
- **Long-Term Memory**: OFF
- **Full App Cache**: OFF
- **KB Cache**: OFF

### Purpose
Establish baseline performance without any enhancements.

### Test Sequence

#### Session 1: Basic Query
**User ID**: `user-test-008`

**Test Steps**:
1. Disable all toggles (ST, LT, Full Cache, KB Cache)
2. Start conversation with user ID: `user-test-008`
3. Send message: "Show me running shoes"

**Expected Results**:
- ✅ No memory badge
- ✅ No memory operations in logs
- ✅ No cache operations in logs
- ✅ Full KB queries every time
- ✅ Slowest response times (3-5 seconds)

**Validation**:
```bash
# Check no memory operations
grep "\[MEMORY" backend.log | tail -20
# Should be empty or initialization only

# Check no cache operations
grep "cache_hit" backend.log | tail -10
# Should be empty

# Check KB queries
grep "Querying knowledge base" backend.log | tail -5
# Should show full KB queries
```

---

#### Session 2: Repeat Query - No Optimization
**User ID**: `user-test-008` (same user, new session)

**Test Steps**:
1. Start NEW conversation with user ID: `user-test-008`
2. Send SAME message: "Show me running shoes"

**Expected Results**:
- ✅ No memory badge
- ✅ No memory of previous session
- ✅ No cache hit
- ✅ Full KB queries again
- ✅ Same slow response time

**Validation**:
```bash
# Verify no optimization
grep "cache_hit=True" backend.log | tail -10
# Should be empty

grep "Retrieved.*memories" backend.log | tail -10
# Should be empty
```

---

## Performance Comparison Matrix

| Scenario | First Query | Repeat Query (Same User) | Repeat Query (Diff User) | Memory Badge |
|----------|-------------|--------------------------|--------------------------|--------------|
| 1. Full System | 3-5s (cache MISS) | <2s (cache HIT + memory) | <2s (cache HIT) | ✅ Session 2+ |
| 2. Memory Only | 3-5s (no cache) | 3-5s (no cache, has memory) | 3-5s (no cache) | ✅ Session 2+ |
| 3. Cache Only | 3-5s (cache MISS) | <1s (cache HIT) | <1s (cache HIT) | ❌ Never |
| 4. ST Memory Only | 3-5s | 3-5s (no persistence) | 3-5s | ✅ Within session |
| 5. LT Memory Only | 3-5s | 3-5s (has memory, no cache) | 3-5s | ✅ Session 2+ |
| 6. Baseline | 3-5s | 3-5s | 3-5s | ❌ Never |

---

## Success Criteria

### Memory System
- ✅ Short-term memories stored and retrieved within session
- ✅ Long-term memories persist across sessions
- ✅ Category filtering works (footwear, apparel, accessories)
- ✅ Memory badge displays correct counts
- ✅ No PII stored in memories
- ✅ Retrieval latency <150ms

### Cache System
- ✅ Cache MISS on first query
- ✅ Cache HIT on repeat queries
- ✅ Cache shared across users
- ✅ Response time <1s with cache hit

### Integration
- ✅ Memory + Cache work together without conflicts
- ✅ Graceful degradation when systems disabled
- ✅ No errors in backend logs
- ✅ UI toggles control systems correctly

---

## Troubleshooting

### Memory Not Storing
```bash
# Check memory client initialization
grep "MemoryClient" backend.log | head -5

# Check memory toggle
grep "memory_enabled" backend.log | tail -1

# Check extraction
grep "MEMORY EXTRACTOR" backend.log | tail -20
```

### Memory Not Retrieving
```bash
# Check user ID
grep "retrieve_memories called" backend.log | tail -1

# Check category detection
grep "Detected category" backend.log | tail -5

# Check retrieval results
grep "Retrieved.*memories" backend.log | tail -5
```

### Cache Not Working
```bash
# Check cache mode
grep "cache.*mode" backend.log | tail -1

# Check cache operations
grep "cache_hit" backend.log | tail -10

# Check cache toggle
grep "full_cache_enabled" backend.log | tail -1
```

### Performance Issues
```bash
# Check latencies
grep "latency=" backend.log | tail -20

# Check KB queries
grep "Querying knowledge base" backend.log | tail -10

# Check memory retrieval time
grep "Memory retrieval.*ms" backend.log | tail -5
```

---

## Test Execution Checklist

- [ ] SSM tunnels running (both ports 6379 and 6380)
- [ ] Backend running (`hatch run start`)
- [ ] Frontend built (`npm run build`)
- [ ] UI accessible at `http://localhost:5173`
- [ ] `.env` configured with correct endpoints
- [ ] Scenario 1 completed successfully
- [ ] Scenario 2 completed successfully
- [ ] Scenario 3 completed successfully
- [ ] Scenario 4 completed successfully
- [ ] Scenario 5 completed successfully
- [ ] Scenario 6 completed successfully
- [ ] Performance comparison documented
- [ ] All success criteria met

---

## Notes

- Use unique user IDs for each scenario to avoid cross-contamination
- Clear browser cache between scenarios if needed
- Monitor backend logs in real-time: `tail -f backend.log`
- Take screenshots of memory badges for documentation
- Record response times for performance comparison
- Document any unexpected behavior or errors
