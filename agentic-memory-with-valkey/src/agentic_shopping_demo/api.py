import json
import logging
import re
import uuid
import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from strands import Agent
from strands.models.bedrock import BedrockModel

logger = logging.getLogger(__name__)

# ── KB IDs (local-only, not committed) ────────────────────────────────────────
# Must be set BEFORE importing agent/tools, since kb_tools reads env at module level.
# Reads from .env file in repo root. See .env.example for template.
from pathlib import Path as _Path
_env_file = _Path(__file__).resolve().parents[2] / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Import our existing agent builder
from agentic_shopping_demo.agent import build_agent
from agentic_shopping_demo.state_extractor import pre_turn_update, post_turn_update
from agentic_shopping_demo.cache.client import get_cache, CacheMode


def default_conversation_state() -> Dict[str, Any]:
    return {
        "active_domain": None,   # kb | commerce | order_tracking | store_locator | escalation
        "active_task": None,     # product_recommendation | track_order | find_store | ...
        "turn_stage": "new",     # new | collecting_info | executing | follow_up | recommendation_presented | confirming
        "entities": {
            "order_id": None,
            "cart_id": None,
            "sku": None,
            "selected_item_id": None,
            "selected_store_id": None,
        },
        "slots": {
            "category": None,
            "color": None,
            "size": None,
            "budget_usd": None,
            "style": None,
            "zip": None,
            "radius_miles": None,
            "delivery_mode": None,
        },
        "references": {
            "last_presented_options": [],
            "last_action": None,
            "last_tools_used": [],
        },
        "relevant_summary": "",
        "versions": {
            "router_prompt_version": "shopnow_router_v1",
            "toolset_version": "commerce_v1",
            "kb_version": "kb_2026_02_23",
        },
    }

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    # Initialize semantic cache indexes on startup
    try:
        from agentic_shopping_demo.cache.client import get_cache
        cache = get_cache()
        if cache:
            ok = cache.initialize()
            if ok:
                print("[CACHE] Indexes initialized successfully")
    except Exception as e:
        print(f"[CACHE] Startup initialization failed: {e}")

    # Warm up Bedrock connections so the first user request isn't slow.
    # Fire-and-forget: don't block startup if warmup fails.
    async def _warmup():
        try:
            from agentic_shopping_demo.tools.knowledge_agent import _get_knowledge_agent
            from strands.models.bedrock import BedrockModel
            import asyncio

            print("[WARMUP] Warming up Bedrock connections...")

            # Warm main agent model
            main_model = BedrockModel(
                model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
                additional_request_fields={
                    "thinking": {
                        "type": "enabled",
                        "budget_tokens": 1024
                    },
                    "anthropic_beta": ["interleaved-thinking-2025-05-14"]
                },
            )

            # Warm knowledge agent (also initializes the singleton)
            kb_agent = _get_knowledge_agent()

            # Send minimal ping to both in parallel
            async def _ping_main():
                async for _ in Agent(model=main_model, system_prompt="ping").stream_async("hi"):
                    break  # just need the connection established

            async def _ping_kb():
                async for _ in kb_agent.stream_async("hi"):
                    break

            await asyncio.gather(_ping_main(), _ping_kb(), return_exceptions=True)
            print("[WARMUP] Bedrock connections warmed up")
        except Exception as e:
            print(f"[WARMUP] Warmup failed (non-fatal): {e}")

    asyncio.create_task(_warmup())
    yield

app = FastAPI(lifespan=lifespan)

# In-memory session storage for agents
# Key: session_id (string), Value: Agent instance
sessions: Dict[str, Agent] = {}

# In-memory conversation metadata storage
# Key: session_id (string), Value: dict with "name", "created_at", etc.
conversation_metadata: Dict[str, Dict[str, Any]] = {}

# Memory storage results — written by background task, polled by frontend
# Key: session_id, Value: {"short_term": int, "long_term": int}
memory_storage_results: Dict[str, Dict[str, int]] = {}

# Title generation agent (lightweight, uses Haiku 4.5)
title_agent = Agent(
    model=BedrockModel(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    system_prompt="Generate concise 3-5 word titles for conversations. Respond with only the title, no explanation."
)

async def generate_title(first_message: str) -> str:
    """Generate a conversation title from the first user message."""
    try:
        # Use stream_async and collect all text
        title_parts = []
        async for event in title_agent.stream_async(
            f"Generate a concise 3-5 word title: {first_message[:200]}"
        ):
            if "data" in event:
                title_parts.append(event["data"])

        title = "".join(title_parts).strip()
        return title
    except Exception as e:
        print(f"Failed to generate title: {e}")
        return ""

async def _store_title(title_task: asyncio.Task, session_id: str) -> None:
    """Store title in metadata when ready — fire and forget."""
    try:
        title = await asyncio.wait_for(asyncio.shield(title_task), timeout=5.0)
        if title and session_id:
            if session_id not in conversation_metadata:
                conversation_metadata[session_id] = {}
            conversation_metadata[session_id]["name"] = title
            # Also persist to Valkey so it survives restarts
            try:
                client = _get_shared_valkey()
                client.set(f"conv:title:{session_id}", title, ex=_USER_SESSIONS_TTL)
            except Exception:
                pass
    except Exception:
        pass


async def check_and_send_title(
    title_task: Optional[asyncio.Task],
    session_id: str
) -> Optional[Dict[str, Any]]:
    """
    Check if title generation is complete and return the SSE event data.
    Returns None if title is not ready or already processed.
    """
    if not title_task or not title_task.done():
        return None

    try:
        title = title_task.result()
        if title and session_id:
            # Store title in metadata
            if session_id not in conversation_metadata:
                conversation_metadata[session_id] = {}
            conversation_metadata[session_id]["name"] = title
            print(f"[API] Title generated and stored: '{title}'")

            return {
                "type": "data-conversation-title",
                "data": {"title": title}
            }
    except Exception as e:
        print(f"[API] Error retrieving title: {e}")

    return None

def _sse(obj: Dict[str, Any]) -> bytes:
    """Format one Server-Sent-Event line."""
    return f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n".encode("utf-8")

def _try_parse_json(text: Optional[str]) -> Any:
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except Exception:
        return text

def _extract_latest_prompt(body: Dict[str, Any]) -> str:
    """Extract the latest user message text from the request."""
    if "prompt" in body and isinstance(body["prompt"], str):
        return body["prompt"]

    ui_messages = body.get("messages") or []

    for msg in reversed(ui_messages):
        if msg.get("role") == "user":
            parts = msg.get("parts") or []
            text_parts = []
            for part in parts:
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))

            text = "".join(text_parts) or msg.get("content", "")
            if text:
                return text

    return ""

from agentic_shopping_demo.intent_classifier import CacheScope, classify_intent

# ── Valkey Session Manager (replaces custom ConversationStore for persistence) ──
import time as _time
import valkey as _valkey
from strands_valkey_session_manager import ValkeySessionManager

def _get_session_valkey_client() -> _valkey.Valkey:
    """Shared Valkey client for session managers (memory cluster, port 6380)."""
    from agentic_shopping_demo.memory.config import get_config
    config = get_config()
    return _valkey.Valkey(
        host=config.endpoint,
        port=config.port,
        ssl=config.tls_enabled,
        ssl_cert_reqs="none",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )

_session_valkey: Optional[_valkey.Valkey] = None

def _get_shared_valkey() -> _valkey.Valkey:
    global _session_valkey
    if _session_valkey is None:
        _session_valkey = _get_session_valkey_client()
    return _session_valkey

# Map session_id → ValkeySessionManager for reuse within the process
session_managers: Dict[str, ValkeySessionManager] = {}

def get_session_manager(session_id: str) -> ValkeySessionManager:
    """Get or create a ValkeySessionManager for a given session."""
    if session_id not in session_managers:
        session_managers[session_id] = ValkeySessionManager(
            session_id=session_id,
            client=_get_shared_valkey(),
        )
    return session_managers[session_id]

# User → sessions index key prefix (sorted set, same as before)
_USER_SESSIONS_PREFIX = "conv:user:"
_USER_SESSIONS_SUFFIX = ":sessions"
_USER_SESSIONS_TTL = 90 * 24 * 60 * 60  # 90 days

def _update_user_session_index(user_id: str, session_id: str):
    """Add/update session in the user's sorted set index."""
    if not user_id:
        return
    try:
        client = _get_shared_valkey()
        idx_key = f"{_USER_SESSIONS_PREFIX}{user_id}{_USER_SESSIONS_SUFFIX}"
        client.zadd(idx_key, {session_id: _time.time()})
        client.expire(idx_key, _USER_SESSIONS_TTL)
    except Exception as e:
        print(f"[SESSION] Failed to update user session index: {e}")

async def strands_to_ai_sdk_stream(agent: Agent, prompt: str, title_task: Optional[asyncio.Task] = None, session_id: Optional[str] = None, cache_mode_str: str = "off", full_temp: str = "hot", turn_count: int = 0, intent=None, user_id: Optional[str] = None, short_term_enabled: bool = True, long_term_enabled: bool = False) -> AsyncIterator[bytes]:
    """
    Translate Strands streaming events into AI SDK Data Stream Protocol v1 parts.
    """
    yield _sse({"type": "start", "messageId": f"msg_{uuid.uuid4().hex}"})

    # Phase A: pre-turn state update from user message
    if session_id and session_id in conversation_metadata:
        state = conversation_metadata[session_id].setdefault("conversation_state", default_conversation_state())
        pre_turn_update(state, prompt, turn_count)
        
        # Memory retrieval: Get relevant memories after state update
        # Only retrieve if memory is enabled
        print(f"[MEMORY] Retrieval check: short_term={short_term_enabled}, long_term={long_term_enabled}, userId={user_id}, sessionId={session_id}")
        logger.info(f"[MEMORY] Retrieval check: short_term={short_term_enabled}, long_term={long_term_enabled}, userId={user_id}, sessionId={session_id}")
        
        if short_term_enabled or long_term_enabled:
            try:
                from agentic_shopping_demo.memory.integration import retrieve_memories, format_memories_for_prompt
                from agentic_shopping_demo.memory.user_identifier import UserIdentifier
                
                # Create user identifier (supports both anonymous and authenticated users)
                # If userId is provided in request, use it for long-term memory
                user_identifier = UserIdentifier(
                    authenticated_user_id=user_id if long_term_enabled else None,
                    anonymous_user_id=None,
                    session_id=session_id if short_term_enabled else None
                )
                
                # Retrieve memories
                memories = retrieve_memories(
                    user_message=prompt,
                    user_identifier=user_identifier,
                    conversation_state=state,
                    limit=5
                )
                
                # Store memories in state for potential use
                if memories:
                    state["_memories"] = [
                        {
                            "content": m.content,
                            "type": m.memory_type.value,
                            "relevance": m.relevance_score
                        }
                        for m in memories
                    ]
                    
                    # Send memory metadata to UI
                    memory_counts = {"short_term": 0, "long_term": 0}
                    for m in memories:
                        if m.memory_type.value == "short_term":
                            memory_counts["short_term"] += 1
                        else:
                            memory_counts["long_term"] += 1
                    
                    print(f"[MEMORY] Sending data-memory-retrieved event: total={len(memories)}, short_term={memory_counts['short_term']}, long_term={memory_counts['long_term']}")
                    
                    yield _sse({
                        "type": "data-memory-retrieved",
                        "data": {
                            "total": len(memories),
                            "short_term": memory_counts["short_term"],
                            "long_term": memory_counts["long_term"],
                            "memories": [
                                {
                                    "type": m.memory_type.value,
                                    "relevance": m.relevance_score
                                }
                                for m in memories
                            ]
                        }
                    })
                    
                    # Inject memory context into the prompt
                    memory_context = format_memories_for_prompt(memories)
                    if memory_context:
                        # Prepend memory context to the user message
                        prompt = memory_context + "\n" + prompt
                        logger.info(f"[MEMORY] Injected {len(memories)} memories into prompt (short_term={memory_counts['short_term']}, long_term={memory_counts['long_term']})")
            except Exception as e:
                print(f"[MEMORY] Memory retrieval failed (non-blocking): {e}")
                logger.error(f"[MEMORY] Memory retrieval failed (non-blocking): {e}")
        else:
            print("[MEMORY] Memory retrieval skipped (both toggles disabled)")
            logger.info("[MEMORY] Memory retrieval skipped (both toggles disabled)")

    # Title is handled fully async via background task + /conversation-title endpoint.
    # Do NOT await or poll it here — it must not affect response timing.
    if title_task and session_id:
        asyncio.create_task(_store_title(title_task, session_id))

    text_block_id: Optional[str] = None
    reasoning_block_id: Optional[str] = None  # track open reasoning block
    tool_inputs: Dict[str, Dict[str, Any]] = {}
    _full_response_text = ""
    _llm_cacheable: Optional[bool] = None

    async for ev in agent.stream_async(prompt):

        # Tools that are sub-agents — they make their own internal LLM call
        # but it's invisible to the main stream, so we synthesize a marker for the UI.
        SUBAGENT_TOOLS = {"lookup_knowledge", "lookup_order_operations"}

        # --- reasoning ---
        # Strands fires ReasoningTextStreamEvent once per chunk with ev["reasoning"]=True
        # and ev["reasoningText"] = the incremental delta string.
        # IMPORTANT: process reasoning FIRST so we can close the reasoning block
        # BEFORE opening a text or tool block. The AI SDK requires reasoning-end
        # to arrive before text-start, otherwise text leaks into the reasoning part.
        _new_tool_started = False
        _new_tool_name = None

        if ev.get("reasoning"):
            reasoning_text = ev.get("reasoningText")
            if isinstance(reasoning_text, dict):
                reasoning_text = reasoning_text.get("text", "")
            if reasoning_text:
                if reasoning_block_id is None:
                    reasoning_block_id = f"rsn_{uuid.uuid4().hex}"
                    yield _sse({"type": "reasoning-start", "id": reasoning_block_id})
                yield _sse({"type": "reasoning-delta", "id": reasoning_block_id, "delta": reasoning_text})
        else:
            # Close reasoning block on phase-change events BEFORE emitting text/tool.
            # current_tool_use fires repeatedly for the same tool — only close on NEW tools.
            is_text_output = "data" in ev
            is_new_tool = False
            if "current_tool_use" in ev:
                tu_peek = ev["current_tool_use"] or {}
                tcid_peek = tu_peek.get("toolUseId") or tu_peek.get("id") or ""
                is_new_tool = tcid_peek not in tool_inputs
            if reasoning_block_id is not None and (is_text_output or is_new_tool):
                yield _sse({"type": "reasoning-end", "id": reasoning_block_id})
                reasoning_block_id = None

        # --- text ---
        if "data" in ev:
            delta = ev["data"]
            _full_response_text += delta

            # Stream every token immediately — no buffering
            if text_block_id is None:
                text_block_id = f"blk_{uuid.uuid4().hex}"
                yield _sse({"type": "text-start", "id": text_block_id})
            yield _sse({"type": "text-delta", "id": text_block_id, "delta": delta})

        # --- tool lifecycle / input ---
        if "current_tool_use" in ev:
            tu = ev["current_tool_use"] or {}
            tool_call_id = tu.get("toolUseId") or tu.get("id") or f"tool_{uuid.uuid4().hex}"
            tool_name = tu.get("name", "tool")

            # If this is a new tool call, start it and close any open text block
            if tool_call_id not in tool_inputs:
                _new_tool_started = True
                _new_tool_name = tool_name
                if text_block_id:
                    yield _sse({"type": "text-end", "id": text_block_id})
                    text_block_id = None
                # Sub-agent tools: emit synthetic reasoning AFTER tool-input-start
                # so the sequence is: reasoning(main) → tool → reasoning(synthetic)
                # This prevents the merge pass from collapsing them together.
                if tool_name in SUBAGENT_TOOLS:
                    _sub_rsn_id = f"rsn_sub_{uuid.uuid4().hex}"
                    yield _sse({"type": "reasoning-start", "id": _sub_rsn_id})
                    yield _sse({"type": "reasoning-end", "id": _sub_rsn_id})
                yield _sse({
                    "type": "tool-input-start",
                    "toolCallId": tool_call_id,
                    "toolName": tool_name,
                })
                tool_inputs[tool_call_id] = {"name": tool_name, "buf": ""}

            # Strands accumulates input as it streams; capture whatever is present
            if "input" in tu and tu["input"] is not None:
                tool_inputs[tool_call_id]["buf"] = (
                    tu["input"] if isinstance(tu["input"], str)
                    else json.dumps(tu["input"], default=str)
                )

        # --- tool streaming output (progress or partials) ---
        if "tool_stream_event" in ev:
            tse = ev["tool_stream_event"] or {}
            tool_use = tse.get("tool_use") or {}
            tool_call_id = tool_use.get("toolUseId") or tool_use.get("id")
            yield _sse({
                "type": "data-tool-stream",
                "toolCallId": tool_call_id,
                "data": tse.get("data"),
            })

        # --- end of turn ---
        if "result" in ev:
            # Close any open reasoning block
            if reasoning_block_id is not None:
                yield _sse({"type": "reasoning-end", "id": reasoning_block_id})
                reasoning_block_id = None
            # Flush any remaining line buffer (last line may not end with \n)
            # Extract cache signal from the full response text (last line)
            # The LLM appends {"_shopnow_cache": ...} as the final line
            _cache_signal_line = None
            for _line in reversed(_full_response_text.split("\n")):
                _stripped = _line.strip()
                if _stripped.startswith('{"_shopnow_cache":'):
                    try:
                        import json as _json
                        _cache_data = _json.loads(_stripped)
                        _llm_cacheable = _cache_data["_shopnow_cache"].get("cacheable", True)
                        _cache_signal_line = _line
                    except Exception:
                        pass
                    break
            # Remove cache signal from the accumulated response text
            if _cache_signal_line is not None:
                _full_response_text = _full_response_text.replace(_cache_signal_line, "").rstrip()

            # Flush any pending tool inputs as available
            for tcid, meta in list(tool_inputs.items()):
                yield _sse({
                    "type": "tool-input-available",
                    "toolCallId": tcid,
                    "toolName": meta["name"],
                    "input": _try_parse_json(meta["buf"]),
                })

            # ── Cacheability decision ─────────────────────────────────────────
            # Three-tier tool classification:
            #   NEVER_CACHE  — user-specific / transactional / live-human
            #   AMBIGUOUS    — depends on whether inputs are public or user-specific
            #   ALWAYS_CACHE — generic/public outputs (lookup_knowledge, catalog, pricing, etc.)
            #   LOADER_STUBS — ignored (load_*_tools don't affect cacheability)
            #
            # Decision order:
            #   1. Any NEVER_CACHE tool → not cacheable
            #   2. No AMBIGUOUS tools → cacheable
            #   3. AMBIGUOUS tools present → deterministic input check, then LLM signal

            never_cache_tools = {
                "lookup_order_operations",
                "cart_read_update", "checkout_readiness_check",
                "build_escalation_packet", "write_case_note",
                "connect_to_it_support_human", "connect_to_support_human",
            }
            ambiguous_tools = {
                "find_nearby_shopnow_locations",
                "lookup_shopnow_store",
                "get_place_coordinates",
                "generate_qr_code",
            }
            loader_stubs = {
                "load_locations_tools", "load_commerce_tools", "load_agent_assist_tools",
            }

            tools_called = {meta["name"] for meta in tool_inputs.values()}
            tools_called_ordered = list(dict.fromkeys(meta["name"] for meta in tool_inputs.values()))
            effective_tools = tools_called - loader_stubs

            def _ambiguous_cacheable(user_prompt: str, tools_used: set) -> Optional[bool]:
                """Deterministic check for ambiguous tools. Returns True/False/None (→ use LLM)."""
                p = user_prompt.lower()
                if tools_used & {"find_nearby_shopnow_locations", "get_place_coordinates"}:
                    if any(x in p for x in ["near me", "my location", "current location", "where i am"]):
                        return False
                    if re.search(r'\b\d{5}\b', user_prompt):  # zip code typed by user
                        return True
                    if any(x in p for x in ["near ", "in ", "around ", "close to "]):
                        return True
                if "lookup_shopnow_store" in tools_used:
                    return True  # public store info
                if "generate_qr_code" in tools_used:
                    if any(x in p for x in ["my order", "my cart", "my account", "my ticket"]):
                        return False
                    return True  # static store/promo payload
                return None

            cache_reason: str
            if intent is not None and intent.cache_scope == CacheScope.SKIP:
                cacheable = False
                cache_reason = "intent_skip"
            elif effective_tools & never_cache_tools:
                cacheable = False
                cache_reason = "never_cache_tool"
            elif not (effective_tools & ambiguous_tools):
                cacheable = True
                cache_reason = "always_cacheable_tools"
            else:
                det = _ambiguous_cacheable(prompt, effective_tools)
                if det is not None:
                    cacheable = det
                    cache_reason = "deterministic_ambiguous_check"
                else:
                    cacheable = _llm_cacheable is not False
                    cache_reason = "llm_signal_ambiguous"

            print(f"[API] Cacheability: tools={effective_tools} cacheable={cacheable} reason={cache_reason} llm={_llm_cacheable}")

            # Check if KB cache was hit during this turn
            kb_cache_hit_similarity = os.environ.pop("SHOPNOW_KB_CACHE_HIT", None)

            # Phase B: post-turn state update from tool trace + assistant output
            if session_id and session_id in conversation_metadata:
                state = conversation_metadata[session_id].setdefault("conversation_state", default_conversation_state())
                post_turn_update(state, tools_called, _full_response_text, tools_called_ordered)
                
                # Memory storage: Extract and store memories asynchronously
                # Only store if at least one memory type is enabled
                if short_term_enabled or long_term_enabled:
                    try:
                        from agentic_shopping_demo.memory.integration import store_memories_async
                        from agentic_shopping_demo.memory.user_identifier import UserIdentifier
                        
                        # Create user identifier based on enabled memory types
                        # Only pass IDs for enabled memory types
                        user_identifier = UserIdentifier(
                            authenticated_user_id=user_id if long_term_enabled else None,
                            anonymous_user_id=None,
                            session_id=session_id if short_term_enabled else None
                        )
                        
                        # Store memories in background (fire-and-forget), stash result for polling
                        async def _store_memories_task():
                            try:
                                result = await store_memories_async(
                                    user_message=prompt,
                                    agent_response=_full_response_text,
                                    user_identifier=user_identifier,
                                    conversation_state=state
                                )
                                if session_id:
                                    memory_storage_results[session_id] = result
                            except Exception as e:
                                logger.error(f"[MEMORY] Background storage failed: {e}")
                        
                        asyncio.create_task(_store_memories_task())
                        print(f"[MEMORY] Storage initiated: short_term={short_term_enabled}, long_term={long_term_enabled}")
                        logger.info(f"[MEMORY] Storage initiated: short_term={short_term_enabled}, long_term={long_term_enabled}")
                    except Exception as e:
                        logger.error(f"[MEMORY] Memory storage setup failed (non-blocking): {e}")
                else:
                    print("[MEMORY] Memory storage skipped (both toggles disabled)")
                    logger.info("[MEMORY] Memory storage skipped (both toggles disabled)")

            # Update user→sessions index for session resumption UI
            if session_id and user_id:
                _update_user_session_index(user_id, session_id)

            yield _sse({
                "type": "data-cacheable",
                "data": {
                    "cacheable": cacheable and cache_mode_str != "off",
                    "tools_called": list(tools_called),
                    "reason": cache_reason,
                    "cache_hit": False,
                    "kb_cache_hit": kb_cache_hit_similarity is not None,
                    "kb_cache_similarity": float(kb_cache_hit_similarity) if kb_cache_hit_similarity else None,
                }
            })
            yield _sse({"type": "data-response-complete"})  # timer stops here

            tool_inputs.clear()

            if text_block_id:
                yield _sse({"type": "text-end", "id": text_block_id})
                text_block_id = None

            yield _sse({"type": "finish"})
            yield b"data: [DONE]\n\n"  # required terminator for AI SDK streams.

            # Full Cache WRITE — background, after stream closes (never blocks user)
            # Only write when full cache mode is active AND intent is not SKIP
            _skip_write = (intent is not None and intent.cache_scope == CacheScope.SKIP)
            if cacheable and prompt and _full_response_text and cache_mode_str == "full" and not _skip_write:
                # Capture intent scope and prior user message for context-enriched writes
                _write_scope = intent.cache_scope.value if intent is not None else "global"

                # Extract prior user message (second-to-last user turn) for SESSION writes.
                # At the time "result" fires, Strands has already appended the current turn,
                # so _user_msgs[-1] is the current prompt and _user_msgs[-2] is the prior.
                _write_prior: Optional[str] = None
                if _write_scope == "session" and session_id and session_id in sessions:
                    _agent_msgs = sessions[session_id].messages
                    _user_msgs = [m for m in _agent_msgs if m.get("role") == "user"]
                    print(f"[API] Prior extraction: total_user_msgs={len(_user_msgs)}")
                    if len(_user_msgs) >= 2:
                        _prior_content = _user_msgs[-2].get("content", [])
                        # content can be a list of dicts OR a plain string
                        if isinstance(_prior_content, str):
                            _write_prior = _prior_content
                        else:
                            for _blk in _prior_content:
                                if isinstance(_blk, dict) and "text" in _blk:
                                    _write_prior = _blk["text"]
                                    break
                        print(f"[API] Prior msg extracted: {repr(_write_prior[:80]) if _write_prior else None}")

                async def _write_full_cache(p=prompt, r=_full_response_text, t=full_temp,
                                            sid=session_id, scope=_write_scope,
                                            prior=_write_prior,
                                            st=conversation_metadata.get(session_id, {}).get("conversation_state") if session_id else None):
                    try:
                        from agentic_shopping_demo.cache.client import get_cache, CacheMode
                        _c = get_cache()
                        if _c is not None:
                            print(f"[API] Writing to cache: temp={t} scope={scope} prior={'yes' if prior else 'no'}")
                            _c.put(p, r, CacheMode.FULL, temp=t, state=st, session_id=sid,
                                   prior_user_msg=prior, cache_scope_override=scope)
                            print(f"[API] Full cache write complete ({t})")
                    except Exception as _e:
                        print(f"[API] Full cache write failed: {_e}")
                asyncio.create_task(_write_full_cache())

@app.get("/conversation-title/{session_id}")
async def get_conversation_title(session_id: str):
    """Poll endpoint for async title generation. Returns title when ready, empty string if still pending."""
    meta = conversation_metadata.get(session_id, {})
    return {"title": meta.get("name", "")}


@app.get("/memory-storage-status/{session_id}")
async def get_memory_storage_status(session_id: str):
    """Poll endpoint for async memory storage results. Returns counts when ready, null if still pending."""
    result = memory_storage_results.pop(session_id, None)
    if result is None:
        return {"ready": False}
    return {"ready": True, "short_term": result.get("short_term", 0), "long_term": result.get("long_term", 0)}


@app.get("/debug/sessions")
async def debug_sessions():
    def summarize_message(m):
        role = m["role"]
        content = m.get("content", [])
        parts = []
        for block in content:
            if "text" in block:
                parts.append({"type": "text", "preview": block["text"][:120]})
            elif "toolUse" in block:
                parts.append({"type": "toolUse", "name": block["toolUse"].get("name"), "id": block["toolUse"].get("toolUseId")})
            elif "toolResult" in block:
                parts.append({"type": "toolResult", "id": block["toolResult"].get("toolUseId")})
            else:
                parts.append({"type": list(block.keys())})
        return {"role": role, "parts": parts}

    return {
        "session_count": len(sessions),
        "sessions": {
            sid: {
                "message_count": len(agent.messages),
                "messages": [summarize_message(m) for m in agent.messages],
            }
            for sid, agent in sessions.items()
        },
        "metadata": conversation_metadata,
    }


@app.get("/session-state/{session_id}")
async def get_session_state(session_id: str):
    if session_id not in conversation_metadata:
        return {"error": "session not found"}
    return conversation_metadata[session_id].get("conversation_state", {})


@app.patch("/session-state/{session_id}")
async def patch_session_state(session_id: str, req: Request):
    """Partially update conversation state fields."""
    if session_id not in conversation_metadata:
        return {"error": "session not found"}
    updates = await req.json()
    state = conversation_metadata[session_id].setdefault("conversation_state", default_conversation_state())
    for key, val in updates.items():
        if key in state and isinstance(state[key], dict) and isinstance(val, dict):
            state[key].update(val)
        else:
            state[key] = val
    return state


@app.post("/flush-cache")
async def flush_cache(req: Request):
    """Flush temp cache indexes (called when Reset is selected in UI)."""
    body = await req.json()
    mode_str = body.get("mode", "all")
    print(f"[API] Flush cache called: mode={mode_str}")
    try:
        from agentic_shopping_demo.cache.client import get_cache, CacheMode
        cache = get_cache()
        if mode_str == "full":
            cache.flush_temp(CacheMode.FULL)
        elif mode_str == "subagent":
            cache.flush_temp(CacheMode.SUBAGENT)
        else:
            cache.flush_temp()
        print(f"[API] Flush cache complete: mode={mode_str}")
        return {"flushed": True, "mode": mode_str}
    except Exception as e:
        print(f"[API] Flush cache error: {e}")
        return {"flushed": False, "error": str(e)}


@app.post("/clear-session")
async def clear_session(req: Request):
    body = await req.json()
    session_id = body.get("sessionId")
    if session_id and session_id in sessions:
        del sessions[session_id]
    if session_id and session_id in conversation_metadata:
        del conversation_metadata[session_id]
    # Also clean up persisted session in Valkey via session manager
    if session_id:
        try:
            sm = get_session_manager(session_id)
            sm.delete_session(session_id)
            session_managers.pop(session_id, None)
        except Exception:
            pass
    return {"cleared": True}


@app.get("/user-sessions/{user_id}")
async def list_user_sessions(user_id: str, limit: int = 20):
    """List recent persisted sessions for a user (for session resumption UI)."""
    try:
        client = _get_shared_valkey()
        idx_key = f"{_USER_SESSIONS_PREFIX}{user_id}{_USER_SESSIONS_SUFFIX}"
        entries = client.zrevrange(idx_key, 0, limit - 1, withscores=True)
        if not entries:
            return {"sessions": []}

        results = []
        for sid, score in entries:
            # Fetch title: in-memory first, then Valkey persisted title
            name = ""
            if sid in conversation_metadata:
                name = conversation_metadata[sid].get("name", "")
            if not name:
                try:
                    name = client.get(f"conv:title:{sid}") or ""
                except Exception:
                    pass
            results.append({
                "session_id": sid,
                "last_active": score,
                "name": name,
            })
        return {"sessions": results}
    except Exception as e:
        return {"sessions": [], "error": str(e)}


@app.get("/session-messages/{session_id}")
async def get_session_messages(session_id: str):
    """Load persisted session messages for replaying in the chat UI."""
    try:
        sm = get_session_manager(session_id)
        # The agent_id is needed — read it from the session's agent key
        # ValkeySessionManager stores agent under session:<sid>:agent:<agent_id>
        # We need to discover the agent_id. Check in-memory first.
        agent_id = None
        if session_id in sessions:
            agent_id = sessions[session_id].agent_id

        if not agent_id:
            # Scan for agent keys under this session
            client = _get_shared_valkey()
            prefix = f"session:{session_id}:agent:"
            cursor = 0
            while True:
                cursor, keys = client.scan(cursor, match=f"{prefix}*", count=100)
                for key in keys:
                    # Filter out message sub-keys
                    if ":message:" not in key:
                        # Extract agent_id from session:<sid>:agent:<agent_id>
                        parts = key.split(":")
                        if len(parts) >= 4:
                            agent_id = parts[3]
                            break
                if agent_id or cursor == 0:
                    break

        if not agent_id:
            return {"messages": [], "metadata": {}}

        msg_objects = sm.list_messages(session_id, agent_id)
        # Convert to Strands message format for the frontend converter
        messages = [msg.message for msg in msg_objects] if msg_objects else []
        metadata = conversation_metadata.get(session_id, {})
        return {"messages": messages, "metadata": metadata}
    except Exception as e:
        print(f"[SESSION] Failed to load session messages: {e}")
        return {"messages": [], "metadata": {}, "error": str(e)}


@app.get("/known-users")
async def known_users():
    """List user IDs that have persisted sessions (for the user dropdown)."""
    try:
        client = _get_shared_valkey()
        # Scan for conv:user:*:sessions keys (same index format as before)
        users = []
        cursor = 0
        while True:
            cursor, keys = client.scan(cursor, match=f"{_USER_SESSIONS_PREFIX}*{_USER_SESSIONS_SUFFIX}", count=100)
            for key in keys:
                # Extract user_id from conv:user:{user_id}:sessions
                parts = key.split(":")
                if len(parts) == 4:
                    users.append(parts[2])
            if cursor == 0:
                break
        return {"users": sorted(set(users))}
    except Exception as e:
        return {"users": [], "error": str(e)}


@app.post("/invocations")
async def invocations(req: Request):
    body = await req.json()

    # Extract session/chat ID, prompt, user ID, and cache mode from request
    session_id = body.get("chatId") or body.get("id")
    user_id = body.get("userId")  # Optional authenticated user ID for long-term memory
    short_term_enabled = body.get("shortTermMemoryEnabled", True)
    long_term_enabled = body.get("longTermMemoryEnabled", False)
    prompt = _extract_latest_prompt(body)
    cache_mode_str = body.get("cacheMode", "off")
    full_temp = body.get("fullCacheTemp", "hot")
    kb_temp   = body.get("kbCacheTemp", "hot")
    full_threshold = float(body.get("fullCacheThreshold", 0.65))
    kb_threshold   = float(body.get("kbCacheThreshold", 0.70))
    
    # Log memory configuration for debugging (using print for visibility)
    print(f"[MEMORY] Request: userId={user_id}, shortTerm={short_term_enabled}, longTerm={long_term_enabled}")
    logger.info(f"[MEMORY] Request: userId={user_id}, shortTerm={short_term_enabled}, longTerm={long_term_enabled}")
    # Derive whether KB cache is actually enabled from the kbCacheTemp field:
    # if kbCacheTemp is sent as "hot" or "cold", the KB toggle is on;
    # but we need the actual UI toggle state.  The UI sends kbCacheTemp only
    # when KB cache is on; however DefaultChatTransport always sends it.
    # Instead, check if the cacheMode includes subagent-level caching.
    # The UI sets cacheMode='subagent' when only KB is on, 'full' when full is on.
    # We need a separate env var so the knowledge agent knows if KB cache is wanted.
    kb_cache_enabled = body.get("kbCacheEnabled", cache_mode_str in ("subagent", "full"))
    print(f"[API] Cache: mode={cache_mode_str} full_temp={full_temp} kb_temp={kb_temp} kb_enabled={kb_cache_enabled} full_threshold={full_threshold} kb_threshold={kb_threshold}")

    os.environ["SHOPNOW_CACHE_MODE"] = cache_mode_str
    os.environ["SHOPNOW_KB_CACHE_ENABLED"] = "true" if kb_cache_enabled else "false"
    os.environ["SHOPNOW_KB_CACHE_TEMP"] = kb_temp
    os.environ["SHOPNOW_KB_THRESHOLD"] = str(kb_threshold)

    # Full Cache READ — only if full cache is enabled
    # Pre-turn state is built here so the lookup uses slots/entities/turn_stage
    # extracted from the current user message before the agent runs.
    full_cache_hit = None
    intent = None  # set below when cache_mode_str == "full"
    if cache_mode_str == "full" and prompt:
        # Pre-flight intent classification — determines cache scope before hitting the index
        has_prior_context = bool(session_id and session_id in sessions and len(sessions[session_id].messages) > 0)
        intent = classify_intent(prompt, has_prior_context=has_prior_context)
        print(f"[API] Intent: scope={intent.cache_scope} fired={intent.fired_rules}")

        if intent.cache_scope != CacheScope.SKIP:
            try:
                _cache = get_cache()

                # Build pre-turn state for the lookup
                if session_id and session_id in conversation_metadata:
                    # Existing session — copy current state so we don't mutate it yet
                    import copy as _copy
                    _lookup_state = _copy.deepcopy(conversation_metadata[session_id].get("conversation_state"))
                    _lookup_turn_count = len(sessions[session_id].messages) // 2 if session_id in sessions else 0
                else:
                    # New session — create a temporary state just for the lookup
                    _lookup_state = default_conversation_state()
                    _lookup_turn_count = 0

                if _lookup_state is not None:
                    pre_turn_update(_lookup_state, prompt, _lookup_turn_count)

                # Extract prior user message for SESSION-scoped context enrichment
                _prior_user_msg: Optional[str] = None
                if intent.cache_scope == CacheScope.SESSION and session_id and session_id in sessions:
                    _agent_msgs = sessions[session_id].messages
                    for _m in reversed(_agent_msgs):
                        if _m.get("role") == "user":
                            _content = _m.get("content", [])
                            if isinstance(_content, str):
                                _prior_user_msg = _content
                            else:
                                for _block in _content:
                                    if isinstance(_block, dict) and "text" in _block:
                                        _prior_user_msg = _block["text"]
                                        break
                            if _prior_user_msg:
                                break

                print(f"[API] Searching cache: temp={full_temp} threshold={full_threshold} scope={intent.cache_scope} prompt={prompt[:50]}")
                full_cache_hit = _cache.get(
                    prompt, CacheMode.FULL,
                    temp=full_temp, threshold=full_threshold,
                    state=_lookup_state, session_id=session_id,
                    cache_scope=intent.cache_scope.value,
                    prior_user_msg=_prior_user_msg,
                )
                print(f"[API] Cache result: {'HIT similarity=' + str(round(full_cache_hit['similarity'],4)) if full_cache_hit else 'MISS'}")
            except Exception as e:
                print(f"[API] Full cache read failed: {e}")

    if full_cache_hit:
        # Serve from full response cache — skip agent entirely
        # Still generate a title for new conversations
        title_task = None
        is_new_conversation = not (session_id and session_id in sessions)
        if is_new_conversation and prompt:
            if not session_id:
                session_id = str(uuid.uuid4())
            title_task = asyncio.create_task(generate_title(prompt))

        # Inject the cached exchange into the agent's conversation history
        # so subsequent turns have the right context
        if session_id and session_id in sessions:
            agent = sessions[session_id]
        else:
            if not session_id:
                session_id = str(uuid.uuid4())
            sm = get_session_manager(session_id)
            agent = build_agent(thinking_budget=1024, session_manager=sm)
            sessions[session_id] = agent
            if session_id not in conversation_metadata:
                conversation_metadata[session_id] = {"name": "", "conversation_state": default_conversation_state()}

        agent.messages.append({"role": "user", "content": [{"text": prompt}]})
        agent.messages.append({"role": "assistant", "content": [{"text": full_cache_hit["response"]}]})

        # Apply pre-turn update to the real session state on cache hit
        # (strands_to_ai_sdk_stream won't run, so we do it here)
        _hit_state = conversation_metadata[session_id].setdefault("conversation_state", default_conversation_state())
        _hit_turn_count = (len(agent.messages) - 2) // 2  # before we appended the two messages above
        pre_turn_update(_hit_state, prompt, _hit_turn_count)

        async def _cached_stream():
            yield _sse({"type": "start", "messageId": f"msg_{uuid.uuid4().hex}"})

            # Stream response immediately — don't wait for title
            text_id = f"blk_{uuid.uuid4().hex}"
            yield _sse({"type": "text-start", "id": text_id})
            yield _sse({"type": "text-delta", "id": text_id, "delta": full_cache_hit["response"]})
            yield _sse({"type": "text-end", "id": text_id})
            yield _sse({
                "type": "data-cacheable",
                "data": {"cacheable": True, "tools_called": [], "reason": "full_cache_hit",
                         "similarity": round(full_cache_hit["similarity"], 4), "cache_hit": True}
            })
            yield _sse({"type": "data-response-complete"})  # timer stops here

            # Title is handled fully async — store via background task, frontend polls /conversation-title
            if title_task and session_id:
                asyncio.create_task(_store_title(title_task, session_id))

            yield _sse({"type": "finish"})
            yield b"data: [DONE]\n\n"

        # Update user→sessions index for cache hit path too
        if session_id and user_id:
            _update_user_session_index(user_id, session_id)

        return StreamingResponse(
            _cached_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "x-vercel-ai-ui-message-stream": "v1",
                "x-session-id": session_id or "",
                "x-cache-hit": "true",
            },
        )

    # Get or create agent (with session resumption via ValkeySessionManager)
    is_new_conversation = False
    if session_id and session_id in sessions:
        agent = sessions[session_id]
    else:
        if not session_id:
            session_id = str(uuid.uuid4())

        # Create a ValkeySessionManager for this session — it handles persistence
        sm = get_session_manager(session_id)
        agent = build_agent(thinking_budget=1024, session_manager=sm)
        sessions[session_id] = agent

        # If the session manager loaded existing messages, this is a resumed session
        if agent.messages and len(agent.messages) > 0:
            print(f"[SESSION] Resumed session {session_id} via ValkeySessionManager ({len(agent.messages)} messages)")
            # Restore metadata from in-memory cache or create fresh
            if session_id not in conversation_metadata:
                conversation_metadata[session_id] = {"name": "", "conversation_state": default_conversation_state()}
            # Ensure dynamic tools are loaded for resumed sessions
            from agentic_shopping_demo.agent import ensure_locations_tools_loaded, ensure_commerce_tools_loaded, ensure_agent_assist_tools_loaded
            ensure_locations_tools_loaded(agent)
            ensure_commerce_tools_loaded(agent)
            ensure_agent_assist_tools_loaded(agent)
        else:
            is_new_conversation = True
            conversation_metadata[session_id] = {"name": "", "conversation_state": default_conversation_state()}

    # Start title generation task if this is the first message
    title_task = None
    if is_new_conversation and prompt:
        title_task = asyncio.create_task(generate_title(prompt))

    # SSE response in AI SDK Data Stream Protocol v1
    turn_count = len(agent.messages) // 2
    return StreamingResponse(
        strands_to_ai_sdk_stream(agent, prompt, title_task=title_task, session_id=session_id, cache_mode_str=cache_mode_str, full_temp=full_temp, turn_count=turn_count, intent=intent, user_id=user_id, short_term_enabled=short_term_enabled, long_term_enabled=long_term_enabled),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "x-vercel-ai-ui-message-stream": "v1",  # required by ai-sdk-ui v6
            "x-session-id": session_id,  # return session ID to frontend
        },
    )
