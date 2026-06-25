"""
Memory system integration helpers for agent workflow.
"""

import logging
from typing import Any, Optional

from agentic_shopping_demo.memory.client import get_memory_client
from agentic_shopping_demo.memory.errors import get_memories_with_fallback
from agentic_shopping_demo.memory.models import Memory
from agentic_shopping_demo.memory.user_identifier import UserIdentifier

logger = logging.getLogger(__name__)


def format_memories_for_prompt(memories: list[Memory]) -> str:
    """
    Format memories as contextual information for agent prompt.
    
    Args:
        memories: List of Memory objects
        
    Returns:
        Formatted string for prompt injection
    """
    if not memories:
        return ""
    
    lines = ["## Relevant Context from Memory\n"]
    
    for i, memory in enumerate(memories, 1):
        # Format memory with type indicator
        type_label = "Session" if memory.memory_type.value == "short_term" else "User"
        lines.append(f"{i}. [{type_label}] {memory.content}")
    
    lines.append("")  # Empty line after memories
    return "\n".join(lines)


def inject_memory_context(
    system_prompt: str,
    memories: list[Memory]
) -> str:
    """
    Augment system prompt with memory context.
    
    Args:
        system_prompt: Original system prompt
        memories: Retrieved memories
        
    Returns:
        Augmented system prompt
    """
    if not memories:
        return system_prompt
    
    memory_context = format_memories_for_prompt(memories)
    
    # Insert memory context after the main instructions but before examples
    # Look for common section markers
    insertion_markers = [
        "\n## Examples",
        "\n# Examples",
        "\n## Tools",
        "\n# Tools",
    ]
    
    for marker in insertion_markers:
        if marker in system_prompt:
            parts = system_prompt.split(marker, 1)
            return parts[0] + "\n" + memory_context + marker + parts[1]
    
    # If no marker found, append to end
    return system_prompt + "\n\n" + memory_context


def retrieve_memories(
    user_message: str,
    user_identifier: UserIdentifier,
    conversation_state: Optional[dict[str, Any]] = None,
    limit: int = 5
) -> list[Memory]:
    """
    Retrieve relevant memories for pre-turn context.
    
    Retrieves both short-term (session) and long-term (user) memories:
    - Short-term: Recent conversation context from current session
    - Long-term: User preferences and patterns from previous sessions
    
    Args:
        user_message: Current user message
        user_identifier: User identification
        conversation_state: Current conversation state
        limit: Maximum memories to retrieve (split between short/long term)
        
    Returns:
        List of relevant memories (combined short-term and long-term)
    """
    try:
        print(f"[MEMORY] retrieve_memories called: session_id={user_identifier.session_id}, user_id={user_identifier.authenticated_user_id}")
        client = get_memory_client()
        all_memories = []
        
        # Build filters from conversation state and user message
        filters = {}
        if conversation_state:
            active_domain = conversation_state.get("active_domain")
            if active_domain:
                filters["domain"] = active_domain
        
        # Detect product category from user message for filtering
        import re
        product_category = None
        if re.search(r"\b(?:shoe|shoes|footwear|sneaker|boot)\b", user_message, re.IGNORECASE):
            product_category = "footwear"
            filters["category"] = product_category
        elif re.search(r"\b(?:jacket|coat|hoodie|shirt|tee|clothing|apparel)\b", user_message, re.IGNORECASE):
            product_category = "apparel"
            filters["category"] = product_category
        elif re.search(r"\b(?:watch|tracker|device)\b", user_message, re.IGNORECASE):
            product_category = "accessories"
            filters["category"] = product_category
        
        # 1. Retrieve short-term memories (session context)
        if user_identifier.session_id:
            try:
                print(f"[MEMORY] Attempting short-term retrieval for session: {user_identifier.session_id}")
                short_term_memories = get_memories_with_fallback(
                    client=client,
                    query=user_message,
                    user_id=user_identifier.session_id,
                    conversation_state=conversation_state,
                    limit=limit // 2,  # Half the limit for short-term
                    filters=filters if filters else None
                )
                all_memories.extend(short_term_memories)
                print(f"[MEMORY] Retrieved {len(short_term_memories)} short-term memories")
                logger.debug(
                    f"[MEMORY] Retrieved {len(short_term_memories)} short-term memories"
                )
            except Exception as e:
                logger.error(f"[MEMORY] Short-term retrieval failed: {e}")
                print(f"[MEMORY] Short-term retrieval failed: {e}")
        
        # 2. Retrieve long-term memories (user preferences)
        if user_identifier.authenticated_user_id:
            try:
                # For long-term memories, get ALL memories for the user in this category
                # instead of using semantic search, since preferences should always be available
                print(f"[MEMORY] Attempting long-term retrieval for user: {user_identifier.authenticated_user_id}")
                
                # Get all long-term memories for this user
                client = get_memory_client()
                all_user_memories = client.get_all(user_identifier.authenticated_user_id)
                print(f"[MEMORY] Total memories for user: {len(all_user_memories)}")
                
                # Filter by category if detected
                if product_category:
                    # Include both category-matched AND uncategorized long-term memories
                    # Uncategorized memories (like trip plans, general preferences) provide
                    # cross-cutting context that's valuable regardless of product category
                    long_term_memories = [
                        m for m in all_user_memories 
                        if m.memory_type.value == "long_term"
                        and (m.metadata.get("category") == product_category
                             or not m.metadata.get("category"))
                    ]
                    print(f"[MEMORY] After category filter ({product_category}, incl. uncategorized): {len(long_term_memories)} memories")
                else:
                    long_term_memories = [
                        m for m in all_user_memories
                        if m.memory_type.value == "long_term"
                    ]
                    print(f"[MEMORY] After long-term filter (no category): {len(long_term_memories)} memories")
                
                # Limit to configured amount
                # Use dynamic allocation: if no short-term memories, use full limit for long-term
                short_term_count = len([m for m in all_memories if m.memory_type.value == "short_term"])
                if short_term_count == 0:
                    # No short-term memories, so use full limit for long-term
                    long_term_limit = limit
                    print(f"[MEMORY] No short-term memories, using full limit for long-term: {long_term_limit}")
                else:
                    # Have short-term memories, split the limit
                    long_term_limit = limit // 2
                    print(f"[MEMORY] Have short-term memories, splitting limit: {long_term_limit}")
                
                long_term_memories = long_term_memories[:long_term_limit]
                
                all_memories.extend(long_term_memories)
                print(f"[MEMORY] Retrieved {len(long_term_memories)} long-term memories")
                logger.debug(
                    f"[MEMORY] Retrieved {len(long_term_memories)} long-term memories"
                )
            except Exception as e:
                logger.error(f"[MEMORY] Long-term retrieval failed: {e}")
                print(f"[MEMORY] Long-term retrieval failed: {e}")
        
        # Sort by relevance score (highest first)
        all_memories.sort(
            key=lambda m: m.relevance_score if m.relevance_score else 0,
            reverse=True
        )
        
        # Limit total results
        # Note: We already limited short-term and long-term separately above,
        # so this final limit ensures we don't exceed the total
        all_memories = all_memories[:limit]
        
        logger.info(
            f"[MEMORY] Retrieved {len(all_memories)} total memories "
            f"(session={user_identifier.session_id}, "
            f"user={user_identifier.authenticated_user_id})"
        )
        print(f"[MEMORY] Retrieved {len(all_memories)} total memories")
        
        return all_memories
        
    except Exception as e:
        logger.error(f"[MEMORY] Failed to retrieve memories: {e}")
        print(f"[MEMORY] Failed to retrieve memories: {e}")
        import traceback
        traceback.print_exc()
        return []


async def store_memories_async(
    user_message: str,
    agent_response: str,
    user_identifier: UserIdentifier,
    conversation_state: Optional[dict[str, Any]] = None
):
    """
    Store memories asynchronously using mem0's native LLM-based extraction.
    
    mem0 uses the configured LLM (Claude Sonnet) to intelligently decide what's
    worth remembering from the conversation — no regex needed. We just pass the
    raw conversation messages and let mem0 handle extraction + deduplication.
    
    Stores both short-term (session) and long-term (user) memories:
    - Short-term: Scoped to session_id, expires after 24 hours
    - Long-term: Scoped to authenticated user_id, persists for 90 days
    
    Args:
        user_message: User's message
        agent_response: Agent's response
        user_identifier: User identification
        conversation_state: Current conversation state
    """
    try:
        print("[MEMORY] store_memories_async started")
        client = get_memory_client()
        
        # Build the conversation messages for mem0
        messages = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": agent_response}
        ]
        
        # Build metadata from conversation state
        metadata = {}
        if conversation_state:
            if conversation_state.get("active_domain"):
                metadata["domain"] = conversation_state["active_domain"]
            if conversation_state.get("active_task"):
                metadata["active_task"] = conversation_state["active_task"]
        
        short_term_count = 0
        long_term_count = 0
        
        # 1. Short-term memory: session context
        #    mem0's LLM extracts session-relevant facts (current task, products discussed, etc.)
        if user_identifier.session_id:
            try:
                session_id = user_identifier.session_id
                print(f"[MEMORY STORAGE] Storing SHORT_TERM via mem0 native extraction for session: {session_id}")
                memory_id = await client.add_short_term_memory_async(
                    messages=messages,
                    session_id=session_id,
                    conversation_state=conversation_state
                )
                if memory_id:
                    short_term_count += 1
                    print(f"[MEMORY STORAGE] ✓ Stored short-term memory: id={memory_id}")
                else:
                    print("[MEMORY STORAGE] ✗ No short-term memory stored (mem0 decided nothing worth keeping)")
            except Exception as e:
                print(f"[MEMORY STORAGE] ✗ Short-term storage failed: {e}")
                logger.error(f"[MEMORY] Short-term storage failed: {e}")
        
        # 2. Long-term memory: user preferences and patterns
        #    mem0's LLM extracts durable preferences (size, style, trip plans, etc.)
        if user_identifier.authenticated_user_id:
            try:
                print(f"[MEMORY STORAGE] Storing LONG_TERM via mem0 native extraction for user: {user_identifier.authenticated_user_id}")
                memory_id = await client.add_long_term_memory_async(
                    messages=messages,
                    user_id=user_identifier.authenticated_user_id,
                    metadata=metadata
                )
                if memory_id:
                    long_term_count += 1
                    print(f"[MEMORY STORAGE] ✓ Stored long-term memory: id={memory_id}")
                else:
                    print("[MEMORY STORAGE] ✗ No long-term memory stored (mem0 decided nothing worth keeping)")
            except Exception as e:
                print(f"[MEMORY STORAGE] ✗ Long-term storage failed: {e}")
                logger.error(f"[MEMORY] Long-term storage failed: {e}")
        else:
            print("[MEMORY STORAGE] Skipping long-term memory (no authenticated user)")
        
        print(f"[MEMORY] Stored memories: short_term={short_term_count}, long_term={long_term_count}, total={short_term_count + long_term_count}")
        logger.info(
            f"[MEMORY] Stored memories: short_term={short_term_count}, "
            f"long_term={long_term_count}, total={short_term_count + long_term_count}"
        )
        return {"short_term": short_term_count, "long_term": long_term_count}
        
    except Exception as e:
        logger.error(f"[MEMORY] Memory storage failed: {e}")
        print(f"[MEMORY] Memory storage failed: {e}")
        import traceback
        traceback.print_exc()
        return {"short_term": 0, "long_term": 0}
