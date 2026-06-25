# Changes Over Siva Version

Tracks all modifications and new files added on top of the original codebase.

---

## Changes Log

### 1. `scripts/tunnels.sh` (new)
Dual auto-reconnecting SSM tunnel script. Launches both the cache tunnel (localhost:6379) and memory tunnel (localhost:6380) in parallel with shared jump host management. Replaces the need to run two separate terminal tabs with `tunnel.sh`.

### 2. `.env` (modified)
Added `MEMORY_CACHE_REMOTE_ENDPOINT` variable for the memory cluster's remote ElastiCache endpoint, used by `tunnels.sh`.

### 3. `pyproject.toml` (modified)
Bumped `mem0ai` dependency from `>=0.1.0` to `>=1.0.0`. The original floor allowed pip to resolve v0.1.115 which does not support the `valkey` vector store provider — that was only added in v1.0.0. This caused the memory system to fail at startup with `Unsupported vector store provider: valkey`. Also capped `valkey-glide` to `>=2.0.0,<2.2.0` (was `[all]>=2.2.1`) because valkey-glide 2.2.x requires `protobuf>=6.20` which conflicts with mem0ai's `protobuf>=5.29,<6.0`. Added `valkey>=6.0.0` as a direct dependency — this is the Python client that mem0's valkey vector store backend imports internally.

### 4. `src/agentic_shopping_demo/memory/integration.py` (modified)
Fixed long-term memory retrieval category filter. When a product category was detected from the user message (e.g. "footwear"), the filter was excluding all memories without that exact category tag — including uncategorized general preferences like trip plans. Now includes uncategorized long-term memories alongside category-matched ones, so cross-cutting context (e.g. "planning a trip to Dublin") is surfaced when asking about shoes for a trip.

### 5. `src/agentic_shopping_demo/memory/integration.py` (modified)
Replaced custom regex-based `MemoryExtractor` with mem0's native LLM-based memory extraction. Previously, `store_memories_async()` used `MemoryExtractor` which relied on regex patterns to identify preferences (e.g. "I prefer...", "I like...") and behavioral patterns — this missed most natural language and produced low-quality extractions like "User preference: to plan my trip to dublin". Now the raw conversation messages are passed directly to `client.add()`, and mem0 uses the configured LLM (Claude Sonnet) to intelligently decide what's worth remembering. This produces higher-quality, more natural memory entries and handles edge cases the regex couldn't (implicit preferences, multi-turn context, etc.). Removed unused imports of `MemoryExtractor` and `PIIFilter` from the storage path.
### 6. `src/agentic_shopping_demo/memory/conversation_store.py` (new)
Conversation persistence layer using Valkey. Stores conversation messages (`agent.messages`) and metadata (title, conversation state) to the memory Valkey cluster (port 6380) with a 24-hour TTL. Enables session resumption after backend restarts, crashes, or across multi-instance deployments. Uses `conv:{session_id}:messages` and `conv:{session_id}:metadata` keys, plus a `conv:user:{user_id}:sessions` sorted set for listing a user's recent sessions. Provides `save_session()`, `load_session()`, `delete_session()`, and `list_user_sessions()` methods.

### 7. `src/agentic_shopping_demo/api.py` (modified)
Wired in session resumption via `ConversationStore`. Three changes: (1) On session lookup miss in the in-memory `sessions` dict, the `/invocations` endpoint now tries to load the session from Valkey before creating a fresh agent — if found, it rebuilds the agent with `build_agent(messages=...)` and restores conversation metadata. (2) After each turn completes, a fire-and-forget task persists the current messages and metadata to Valkey. (3) The `/clear-session` endpoint now also deletes the persisted session from Valkey. Added `/user-sessions/{user_id}` GET endpoint for listing a user's recent sessions.

### 8. `react-ui/src/utils/messageConverter.ts` (new)
Utility to convert Strands agent message format to ai-sdk UIMessage format for session resumption. Strands stores messages as `{role, content: [{text}, {toolUse}, {toolResult}]}` while ai-sdk expects `{id, role, parts: [{type: "text"}, {type: "tool-invocation"}, ...]}`. Handles text blocks, tool invocations, tool results, and strips the `_shopnow_cache` signal from response text.

### 9. `react-ui/src/components/ConversationSidebar.tsx` (modified)
Replaced the free-form User ID text input with a combo dropdown. On focus or clicking the dropdown toggle, fetches `/api/known-users` to populate a list of previously seen users. Typing still works for entering new user IDs. Selecting a known user (or pressing Enter on a typed ID) triggers `onUserSelected`, which tells `App.tsx` to fetch their past sessions and add them directly into the normal conversations list.

### 10. `react-ui/src/components/Chat.tsx` (modified)
Converted from a default function export to a `forwardRef` component that exposes a `ChatHandle` with `setMessages()`. This allows `App.tsx` to inject pre-loaded messages (from restored sessions) into the Chat component's `useChat` hook after mount. Destructures `setMessages` (renamed `setChatMessages`) from the `useChat` return value.

### 11. `react-ui/src/App.tsx` (modified)
Added session resumption orchestration. New refs: `chatRefs` (Map of ChatHandle refs), `pendingRestoreRef` (Map of pre-fetched messages awaiting injection). New handler: `handleUserSelected` fetches `/api/user-sessions/{userId}` when a known user is picked from the dropdown, fetches messages for all sessions in parallel via `/api/session-messages/{sessionId}`, converts them to UIMessage format via `strandsToUIMessages()`, stashes them in `pendingRestoreRef`, and prepends the session IDs directly into the normal `conversationIds` list. When the user clicks a restored session, `handleSelectConversation` checks `pendingRestoreRef` and injects the messages into the Chat component via its `ChatHandle.setMessages()` ref after a short mount delay. No separate "Past Sessions" UI — restored sessions appear as normal conversations in the sidebar.

### 12. `react-ui/src/utils/messageConverter.ts` (rewritten)
Rewrote the Strands-to-ai-sdk message converter to fix three issues with restored session rendering: (1) Tool parts now use `type: 'tool-<toolName>'` (e.g. `tool-lookup_knowledge`) instead of `type: 'tool-invocation'` — matching the ai-sdk `ToolUIPart` format so `MessagePart` renders the actual tool name badge instead of bare "invocation"/"result" text. (2) Consecutive assistant messages that form a single agent turn (separated only by tool-result-carrying user messages) are merged into one UIMessage — Strands splits a turn into multiple assistant→toolResult→assistant exchanges, but the UI expects one assistant message per turn. (3) The `## Relevant Context from Memory` prefix that the backend prepends to user messages during memory retrieval is stripped, so restored sessions show only the user's actual text.

### 13. `react-ui/src/components/ConversationSidebar.css` (modified)
Removed unused `.past-session-*` CSS classes (`.past-session-loading`, `.conversation-item.past-session`, `.past-session-date`) that were left over from the earlier separate "Past Sessions" UI approach.

### 14. `pyproject.toml` (modified)
Added `strands-valkey-session-manager` dependency. This is a community Strands plugin that provides native Valkey-backed session persistence, replacing our custom `ConversationStore`.

### 15. `src/agentic_shopping_demo/agent.py` (modified)
Added `session_manager` parameter to `build_agent()` and passes it through to `Agent(session_manager=...)`. When a `ValkeySessionManager` is provided, Strands automatically persists every message to Valkey as the agent runs — no manual save calls needed.

### 16. `src/agentic_shopping_demo/api.py` (modified)
Replaced custom `ConversationStore` with `strands-valkey-session-manager`. Key changes: (1) Removed `get_conversation_store` import and all manual `save_session`/`load_session` calls. (2) Added `ValkeySessionManager` factory (`get_session_manager`) that creates a session manager per session using a shared Valkey client on port 6380. (3) Removed the `_persist_conversation` fire-and-forget background task — the session manager handles persistence transparently inside the Strands agent lifecycle. (4) Session resumption now works by passing the session manager to `build_agent()` — if the session exists in Valkey, the agent loads its messages automatically. (5) Rewrote `/session-messages/{session_id}` to use `session_manager.list_messages()` and discover the agent_id via key scanning. (6) Kept the `conv:user:{user_id}:sessions` sorted set index for the user dropdown since the plugin doesn't provide user-level session listing. (7) `/known-users` and `/user-sessions/{user_id}` now use the shared Valkey client directly instead of going through ConversationStore.

### 17. `src/agentic_shopping_demo/memory/config.py` (modified)
Changed short-term memory TTL default from 24 hours to 30 days (720 hours). The env var `MEMORY_SHORT_TERM_TTL_HOURS` still overrides.

### 18. `src/agentic_shopping_demo/memory/integration.py` (modified)
`store_memories_async()` now returns `{"short_term": int, "long_term": int}` counts instead of None, so the caller can report what was stored.

### 19. `src/agentic_shopping_demo/api.py` (modified)
Added memory storage status polling. New `memory_storage_results` dict stashes counts from the fire-and-forget storage task. New `GET /memory-storage-status/{session_id}` endpoint pops and returns the result when ready. No impact on response latency — storage still runs in the background.

### 20. `react-ui/src/components/Chat.tsx` (modified)
Added `memoryStorageMetadata` state and `pollForMemoryStorage()` function. After streaming finishes, polls `/api/memory-storage-status/{session_id}` every 1s (up to 15 attempts). Renders a green `💾 N saved (ST: X, LT: Y)` badge next to the existing retrieval badge when storage completes.

### 21. `react-ui/src/components/Chat.css` (modified)
Added `.memory-badge.stored` variant with green styling to distinguish storage badges from retrieval badges.

### 22. `react-ui/src/components/ConversationSidebar.tsx` (modified)
Updated short-term memory TTL label from "24h TTL" to "30d TTL".

### 23. `react-ui/src/App.tsx` (modified)
Added `restoredSessionIds` ref to track which conversations were loaded from session resumption. When a different user is selected (or a new user ID typed), previously restored sessions are cleared from the sidebar before fetching the new user's sessions. New chats and user-created conversations are preserved.

### 24. `src/agentic_shopping_demo/api.py` (modified — cache key bug fix)
Fixed a bug where the semantic cache never hit on repeated queries when memory was enabled. The memory system prepends `## Relevant Context from Memory` to the `prompt` variable before the agent runs, but the cache write was using this modified prompt as the key. The cache read (in `/invocations`) uses the original clean prompt, so the embeddings never matched. Fix: save the original prompt as `_original_prompt` before memory injection and use it for the cache write key. State-based tag filtering (color, size, domain, etc.) is unaffected.

### 25. `src/agentic_shopping_demo/api.py` (modified — title persistence)
Session titles are now persisted to Valkey as `conv:title:{session_id}` keys (90-day TTL) in addition to the in-memory `conversation_metadata` dict. The `/user-sessions/{user_id}` endpoint reads titles from Valkey when not found in memory, so titles survive backend restarts.

### 26. `react-ui/src/components/ConversationSidebar.tsx` (modified)
Selecting a user from the dropdown or pressing Enter on a typed user ID now auto-enables the long-term memory toggle. Previously users had to manually check it after entering a user ID.

### 27. `react-ui/src/components/Chat.tsx` (modified)
Cache badge is now hidden when cache mode is `'off'`. Previously showed "⬜ Response not Cacheable" even with cache disabled.

### 28. `src/agentic_shopping_demo/intent_rules_config.py` (modified)
Added "suggest" to the `INTENT_VERB` token pattern list alongside "recommend", "find", "show", "search", "browse", "compare". Without it, queries like "suggest red shoes for me" weren't recognized as standalone intents and got routed to `SESSION` cache scope instead of `GLOBAL`.

### 29. `src/agentic_shopping_demo/intent_classifier.py` (modified)
Fixed `is_context_dependent` derivation so that standalone intent queries (those with a recognized verb like find/suggest/show) are not marked context-dependent even when they contain constraint fragments (e.g. color, category) and have prior conversation context. Previously "find red shoes for me" in an existing session was classified as `SESSION` scope because the "red shoes" constraint fragment + `has_prior_context=True` triggered context dependency, overriding the standalone intent signal. Now standalone intents stay `GLOBAL`, while bare fragments like "red shoes" (no verb) correctly remain `SESSION` scoped. This fixes cache misses where the write used `GLOBAL` scope (first message, no prior context) but subsequent reads in the same session used `SESSION` scope filters.
