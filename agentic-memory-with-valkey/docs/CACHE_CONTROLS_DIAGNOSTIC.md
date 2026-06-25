# Cache Controls Diagnostic Guide

## How Cache Controls Work

### Full App Cache
- **Location**: Sidebar → "Full App Cache" section
- **Options**: Off | 🔥 Hot | ❄️ New
- **What it does**: Caches entire agent responses (full conversation turns)
- **Backend**: Sets `cacheMode='full'` → triggers full cache lookup in `api.py`

### Knowledge Base Cache
- **Location**: Sidebar → "Knowledge Base Cache" section
- **Options**: Off | 🔥 Hot | ❄️ New
- **What it does**: Caches knowledge agent (lookup_knowledge tool) responses
- **Backend**: Sets `cacheMode='subagent'` → sets `SHOPNOW_KB_CACHE_ENABLED=true` → knowledge agent checks cache

### Hot vs New (Cold)
- **Hot**: Uses pre-loaded cache index (never flushed, persistent)
- **New**: Uses temporary cache index (flushed when you click "New", starts fresh)

## Testing Cache Controls

### Test 1: Full App Cache
1. **Setup**: Set Full App Cache to "Off", KB Cache to "Off"
2. **Action**: Ask "What are your return policies?"
3. **Expected**: Agent responds normally, no cache hit badge
4. **Verify logs**: Should see `[API] Cache: mode=off`

5. **Setup**: Set Full App Cache to "🔥 Hot"
6. **Action**: Ask the SAME question again
7. **Expected**: Agent responds normally (first time, cache miss)
8. **Verify logs**: Should see `[API] Searching cache: temp=hot` and `[CACHE MISS]`

9. **Action**: Ask the SAME question a third time
10. **Expected**: Fast response with "⚡ full cache hit" badge
11. **Verify logs**: Should see `[CACHE HIT] mode=full temp=hot similarity=1.0000`

### Test 2: Knowledge Base Cache
1. **Setup**: Set Full App Cache to "Off", KB Cache to "🔥 Hot"
2. **Action**: Ask "What are your return policies?"
3. **Expected**: Agent responds normally (first time, cache miss)
4. **Verify logs**: Should see `[KNOWLEDGE_AGENT] KB CACHE HIT` or miss

5. **Action**: Ask the SAME question again
6. **Expected**: Response with "🟡 kb cache hit" badge
7. **Verify logs**: Should see `[KNOWLEDGE_AGENT] KB CACHE HIT rid=... temp=hot similarity=...`

### Test 3: New (Cold) Mode
1. **Setup**: Set Full App Cache to "🔥 Hot"
2. **Action**: Ask "What are your return policies?" twice
3. **Expected**: Second time shows cache hit

4. **Setup**: Click "❄️ New" button (this flushes the temp cache)
5. **Action**: Ask the same question again
6. **Expected**: Cache miss (temp index was flushed)
7. **Note**: "New" mode uses the temp index, which starts empty

## Common Issues

### Issue 1: Cache not working at all
**Symptoms**: No cache hit badges, logs show cache misses
**Possible causes**:
- ElastiCache tunnel not running
- Cache indexes not initialized
- Similarity threshold too high

**Debug**:
```bash
# Check if tunnel is running
lsof -i :6379  # Should show SSM tunnel for semantic cache

# Check backend logs for:
[CACHE] Connected to localhost:6379
[CACHE] Indexes initialized successfully
```

### Issue 2: Cache controls not responding
**Symptoms**: Changing toggles doesn't affect behavior
**Possible causes**:
- Frontend not rebuilt after changes
- Browser cache serving old JavaScript

**Fix**:
```bash
cd react-ui
npm run build
# Then restart backend
hatch run start
```

### Issue 3: Wrong cache mode being used
**Symptoms**: Full cache hits when KB cache is selected, or vice versa
**Debug**: Check backend logs for:
```
[API] Cache: mode=full full_temp=hot kb_temp=hot kb_enabled=False
```

This line shows:
- `mode`: Which cache mode is active (off/subagent/full)
- `full_temp`: Full cache temperature (hot/cold)
- `kb_temp`: KB cache temperature (hot/cold)
- `kb_enabled`: Whether KB cache is enabled

## Backend Log Patterns

### Full Cache Hit
```
[API] Cache: mode=full full_temp=hot ...
[API] Searching cache: temp=hot threshold=0.65 scope=global prompt=What are your return policies?
[API] Cache result: HIT similarity=0.9876
[CACHE HIT] mode=full temp=hot similarity=0.9876
```

### KB Cache Hit
```
[API] Cache: mode=subagent ... kb_enabled=True
[KNOWLEDGE_AGENT] rid=abc123 kb_cache_enabled=True
[KNOWLEDGE_AGENT] KB CACHE HIT rid=abc123 temp=hot similarity=0.9234
```

### Cache Miss
```
[API] Searching cache: temp=hot threshold=0.65 scope=global prompt=...
[API] Cache result: MISS
[CACHE MISS] mode=full temp=hot similarity=0.5432
```

## UI Badge Reference

| Badge | Meaning |
|-------|---------|
| ⚡ full cache hit | Full app cache hit (entire response cached) |
| 🟡 kb cache hit | Knowledge base cache hit (lookup_knowledge cached) |
| 🟢 Response Cached | Response was cacheable and stored for future use |
| ⬜ Response not Cacheable | Response contains user-specific data, not cached |

## Troubleshooting Steps

1. **Check tunnels are running**:
   ```bash
   # Semantic cache tunnel (port 6379)
   lsof -i :6379
   
   # Memory cluster tunnel (port 6380)
   lsof -i :6380
   ```

2. **Check backend logs** when you send a message:
   - Look for `[API] Cache: mode=...` line
   - Look for `[CACHE HIT]` or `[CACHE MISS]` lines
   - Look for `[KNOWLEDGE_AGENT] KB CACHE HIT` lines

3. **Verify frontend is sending correct values**:
   - Open browser DevTools → Network tab
   - Send a message
   - Find the `/api/invocations` request
   - Check the request payload for:
     - `cacheMode`: should be 'off', 'subagent', or 'full'
     - `fullCacheTemp`: should be 'hot' or 'cold'
     - `kbCacheTemp`: should be 'hot' or 'cold'
     - `kbCacheEnabled`: should be true or false

4. **Test with simple queries**:
   - Use exact same question twice
   - Should see cache hit on second attempt
   - If not, check similarity scores in logs (might be below threshold)

## Expected Behavior Summary

| Full Cache | KB Cache | Result |
|------------|----------|--------|
| Off | Off | No caching, agent runs normally |
| Hot/New | Off | Full response cached, fast repeat queries |
| Off | Hot/New | Knowledge lookups cached, faster KB queries |
| Hot/New | Hot/New | Both caches active (Full takes precedence) |

**Important**: Full App Cache takes precedence. If Full Cache hits, the agent never runs, so KB Cache is never checked.
