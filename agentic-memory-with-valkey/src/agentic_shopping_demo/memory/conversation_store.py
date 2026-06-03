"""
Conversation persistence in Valkey for session resumption.

Stores conversation messages and metadata so sessions survive backend
restarts, crashes, and multi-instance deployments. Uses the same Valkey
cluster as the memory system (port 6380) but with its own key namespace.

Keys:
  conv:{session_id}:messages  — JSON-encoded list of Strands message dicts
  conv:{session_id}:metadata  — JSON-encoded conversation metadata dict
  conv:user:{user_id}:sessions — sorted set of session_ids by last-active timestamp
"""

import json
import logging
import time
from typing import Any, Optional

import valkey

from agentic_shopping_demo.memory.config import get_config

logger = logging.getLogger(__name__)

# TTL for persisted conversations (90 days, matches long-term memory)
CONVERSATION_TTL_SECONDS = 90 * 24 * 60 * 60


class ConversationStore:
    """Persists conversation state to Valkey for session resumption."""

    def __init__(self):
        self._client: Optional[valkey.Valkey] = None

    def _get_client(self) -> valkey.Valkey:
        if self._client is None:
            config = get_config()
            self._client = valkey.Valkey(
                host=config.endpoint,
                port=config.port,
                ssl=config.tls_enabled,
                ssl_cert_reqs="none",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
        return self._client

    def save_session(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any],
        user_id: Optional[str] = None,
    ) -> bool:
        """
        Persist conversation messages and metadata to Valkey.

        Called after each turn so the latest state is always available.

        Args:
            session_id: Session identifier
            messages: Strands agent.messages list
            metadata: conversation_metadata[session_id] dict
            user_id: Optional authenticated user id (for user→sessions index)

        Returns:
            True on success
        """
        try:
            client = self._get_client()
            msg_key = f"conv:{session_id}:messages"
            meta_key = f"conv:{session_id}:metadata"

            pipe = client.pipeline()
            pipe.set(msg_key, json.dumps(messages, default=str))
            pipe.expire(msg_key, CONVERSATION_TTL_SECONDS)
            pipe.set(meta_key, json.dumps(metadata, default=str))
            pipe.expire(meta_key, CONVERSATION_TTL_SECONDS)

            # Maintain user → sessions index if user_id provided
            if user_id:
                idx_key = f"conv:user:{user_id}:sessions"
                pipe.zadd(idx_key, {session_id: time.time()})
                pipe.expire(idx_key, CONVERSATION_TTL_SECONDS)

            pipe.execute()
            print(f"[CONV STORE] Saved session {session_id} ({len(messages)} messages)")
            return True

        except Exception as e:
            logger.error(f"[CONV STORE] Failed to save session {session_id}: {e}")
            print(f"[CONV STORE] Failed to save session {session_id}: {e}")
            return False

    def load_session(
        self,
        session_id: str,
    ) -> Optional[dict[str, Any]]:
        """
        Load persisted conversation from Valkey.

        Returns:
            Dict with "messages" and "metadata" keys, or None if not found.
        """
        try:
            client = self._get_client()
            msg_key = f"conv:{session_id}:messages"
            meta_key = f"conv:{session_id}:metadata"

            raw_messages = client.get(msg_key)
            raw_metadata = client.get(meta_key)

            if not raw_messages:
                return None

            messages = json.loads(raw_messages)
            metadata = json.loads(raw_metadata) if raw_metadata else {}

            print(f"[CONV STORE] Loaded session {session_id} ({len(messages)} messages)")
            return {"messages": messages, "metadata": metadata}

        except Exception as e:
            logger.error(f"[CONV STORE] Failed to load session {session_id}: {e}")
            print(f"[CONV STORE] Failed to load session {session_id}: {e}")
            return None

    def delete_session(self, session_id: str) -> bool:
        """Delete a persisted session."""
        try:
            client = self._get_client()
            client.delete(
                f"conv:{session_id}:messages",
                f"conv:{session_id}:metadata",
            )
            print(f"[CONV STORE] Deleted session {session_id}")
            return True
        except Exception as e:
            logger.error(f"[CONV STORE] Failed to delete session {session_id}: {e}")
            return False

    def list_user_sessions(
        self,
        user_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        List recent sessions for a user (most recent first).

        Returns list of {"session_id": str, "last_active": float, "name": str}.
        """
        try:
            client = self._get_client()
            idx_key = f"conv:user:{user_id}:sessions"

            # Get session IDs sorted by score (timestamp) descending
            entries = client.zrevrange(idx_key, 0, limit - 1, withscores=True)
            if not entries:
                return []

            results = []
            for sid, score in entries:
                # Fetch just the title from metadata
                raw_meta = client.get(f"conv:{sid}:metadata")
                name = ""
                if raw_meta:
                    try:
                        meta = json.loads(raw_meta)
                        name = meta.get("name", "")
                    except Exception:
                        pass
                results.append({
                    "session_id": sid,
                    "last_active": score,
                    "name": name,
                })

            return results

        except Exception as e:
            logger.error(f"[CONV STORE] Failed to list sessions for {user_id}: {e}")
            return []


# Singleton
_store: Optional[ConversationStore] = None


def get_conversation_store() -> ConversationStore:
    """Get the global ConversationStore instance."""
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store
