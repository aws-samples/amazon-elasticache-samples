# Memory System UI Guide

## Overview

The React UI now includes controls for the agent memory system, allowing users to enable/disable short-term and long-term memory, and provide a user ID for persistent memory across sessions.

## UI Components

### Memory System Section

Located in the left sidebar, below the conversation list and above the cache controls.

#### User ID Input
- **Purpose**: Identifies the user for long-term memory storage
- **Format**: Free-text input (e.g., email, username, UUID)
- **Required for**: Long-term memory only
- **Optional for**: Short-term memory (uses session ID if no user ID)

#### Short-term Memory Toggle
- **Label**: "Short-term (Session)"
- **TTL**: 24 hours
- **Scope**: Session-scoped (per conversation)
- **Default**: Enabled
- **Use case**: Remember context within a single conversation session

#### Long-term Memory Toggle
- **Label**: "Long-term (User)"
- **TTL**: 90 days
- **Scope**: User-scoped (across all sessions)
- **Default**: Disabled
- **Requires**: User ID must be entered
- **Use case**: Remember user preferences across multiple sessions

## How It Works

### Short-term Memory (Session)
1. User enables "Short-term (Session)" toggle (enabled by default)
2. Agent stores conversation context in Valkey with session ID
3. Memories expire after 24 hours
4. Memories are scoped to the current session only

### Long-term Memory (User)
1. User enters a User ID in the input field
2. User enables "Long-term (User)" toggle
3. Agent stores user preferences in Valkey with user ID
4. Memories persist for 90 days
5. Memories are available across all sessions for that user

### Combined Memory
When both are enabled:
- Agent retrieves both short-term (session) and long-term (user) memories
- Memories are ranked by relevance score
- Top 5 memories are injected into the agent prompt

## Backend Integration

### Request Parameters

The UI sends these additional parameters to `/api/invocations`:

```typescript
{
  chatId: string,              // Session ID
  userId?: string,             // User ID (optional, for long-term memory)
  shortTermMemoryEnabled: boolean,
  longTermMemoryEnabled: boolean,
  prompt: string,
  cacheMode: string,
  // ... other cache parameters
}
```

### Memory Injection

When memories are retrieved:
1. Backend retrieves relevant memories from Valkey
2. Memories are formatted and prepended to the user message:
   ```
   ## Relevant Context from Memory
   
   1. [Session] User is looking for trail running shoes
   2. [User] User prefers Nike brand
   3. [User] User's shoe size is 10
   
   <original user message>
   ```
3. Agent sees the memory context and uses it in the response

## User Experience

### First-time User (Anonymous)
- Short-term memory enabled by default
- No user ID required
- Agent remembers context within the session
- Memories lost after 24 hours or when session ends

### Authenticated User
1. User enters their User ID (e.g., email or username)
2. User enables "Long-term (User)" toggle
3. Agent remembers preferences across sessions
4. User can return days later and agent remembers their preferences

### Example Flow

**Session 1:**
```
User: I prefer Nike running shoes in size 10
Agent: Got it! I'll remember that you prefer Nike running shoes in size 10.
```

**Session 2 (days later, same user ID):**
```
User: What shoes should I get for trail running?
Agent: Based on your preference for Nike shoes in size 10, I'd recommend...
```

## UI States

### Long-term Memory Disabled (No User ID)
- Long-term toggle is disabled (grayed out)
- Warning message: "⚠️ Enter User ID to enable long-term memory"

### Long-term Memory Enabled (With User ID)
- Long-term toggle is enabled
- User ID is sent with every request
- Agent stores and retrieves user-scoped memories

### Both Memories Disabled
- Agent operates without memory context
- Each message is treated independently
- No conversation history beyond the current session

## Testing

### Test Short-term Memory
1. Enable "Short-term (Session)" toggle
2. Send: "I'm looking for trail running shoes"
3. Send: "What was I looking for?"
4. Agent should remember "trail running shoes"

### Test Long-term Memory
1. Enter User ID: "test-user-123"
2. Enable "Long-term (User)" toggle
3. Send: "I prefer Nike shoes in size 10"
4. Start a new conversation (different session)
5. Send: "What brand of shoes do I like?"
6. Agent should remember "Nike"

### Test Memory Isolation
1. User A (ID: "alice") stores preference: "I love Adidas"
2. User B (ID: "bob") asks: "What brand do I like?"
3. Agent should NOT mention Adidas (different user)

## Troubleshooting

### Agent doesn't remember preferences
- Check that memory toggle is enabled
- For long-term memory, verify User ID is entered
- Check application logs for `[MEMORY]` messages
- Verify both SSM tunnels are running (ports 6379 and 6380)

### Long-term memory toggle is disabled
- Enter a User ID in the input field
- Toggle will become enabled automatically

### Memories not persisting across sessions
- Verify long-term memory toggle is enabled
- Ensure the same User ID is used in both sessions
- Check that MEMORY_ENABLED=true in .env file

## Architecture

```
┌─────────────────┐
│   React UI      │
│  - User ID      │
│  - Memory       │
│    Toggles      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   FastAPI       │
│  - Extract      │
│    userId       │
│  - Retrieve     │
│    memories     │
│  - Inject into  │
│    prompt       │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Valkey        │
│  - Short-term   │
│    (session_id) │
│  - Long-term    │
│    (user_id)    │
└─────────────────┘
```

## Privacy Considerations

- User IDs are not validated or authenticated
- Users can enter any identifier they choose
- No PII is required (can use anonymous IDs)
- Memories are stored in Valkey with TTL
- Short-term: 24 hours
- Long-term: 90 days

## Future Enhancements

Potential improvements:
- OAuth/SSO integration for automatic user ID
- Memory management UI (view/delete memories)
- Memory search and filtering
- Export memories to JSON
- Memory analytics dashboard
