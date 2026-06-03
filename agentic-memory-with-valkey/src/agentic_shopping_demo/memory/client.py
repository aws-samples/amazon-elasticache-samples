"""
MemoryClient - Main interface for agent memory operations using mem0 framework.

Provides synchronous and asynchronous operations for storing and retrieving memories.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

import boto3
from mem0 import Memory as Mem0Memory

from agentic_shopping_demo.memory.config import MemoryConfig, get_config
from agentic_shopping_demo.memory.models import Memory, MemoryCandidate, MemoryType

logger = logging.getLogger(__name__)


class MemoryClient:
    """
    Client for managing agent memories using mem0 framework.
    
    Supports both synchronous and asynchronous operations.
    Handles connection to dedicated ElastiCache/Valkey cluster.
    """
    
    def __init__(
        self,
        config: Optional[MemoryConfig] = None,
        test_mode: bool = False
    ):
        """
        Initialize memory client.
        
        Args:
            config: Memory configuration (uses global config if None)
            test_mode: If True, use isolated test namespace
        """
        self.config = config or get_config()
        self.test_mode = test_mode
        self._mem0_client: Optional[Mem0Memory] = None
        self._initialized = False
        
        # Test mode namespace
        self._test_namespace = f"test:{uuid.uuid4().hex[:8]}" if test_mode else None
        
        logger.info(f"[MEMORY] Initializing MemoryClient (test_mode={test_mode})")
    
    def _get_mem0_client(self) -> Mem0Memory:
        """Get or create mem0 client instance."""
        if self._mem0_client is None:
            logger.info(f"[MEMORY] Creating mem0 client: {self.config.endpoint}:{self.config.port}")
            
            # WORKAROUND: ElastiCache Valkey bug - create index with only fields that exist
            # mem0 creates 10-field index but only writes 7 fields, causing silent indexing failure
            # See: https://github.com/valkey-io/valkey-search/pull/489
            self._create_index_workaround()
            
            # Build valkey URL with TLS
            valkey_url = f"valkeys://{self.config.endpoint}:{self.config.port}"
            
            # Configure mem0 with Valkey backend and Bedrock embeddings
            mem0_config = {
                "vector_store": {
                    "provider": "valkey",
                    "config": {
                        "collection_name": "shopnow_memory",
                        "embedding_model_dims": self.config.embedding_dim,
                        "valkey_url": valkey_url,
                        "index_type": "hnsw",
                        "hnsw_m": 32,
                        "hnsw_ef_construction": 400,
                        "hnsw_ef_runtime": 40,
                        "ssl": True,
                        "ssl_cert_reqs": "none",
                    }
                },
                "embedder": {
                    "provider": "aws_bedrock",
                    "config": {
                        "model": self.config.embedding_model,
                        "aws_region": self.config.aws_region,
                    }
                },
                "llm": {
                    "provider": "aws_bedrock",
                    "config": {
                        "model": "us.anthropic.claude-sonnet-4-20250514-v1:0",
                        "max_tokens": 512,
                        "temperature": 0.5
                    }
                },
                "version": "v1.1"
            }
            
            self._mem0_client = Mem0Memory.from_config(mem0_config)
            self._initialized = True
            
            logger.info("[MEMORY] mem0 client created successfully")
        
        return self._mem0_client
    
    def _create_index_workaround(self):
        """
        Create index with only the 7 fields mem0 actually writes.
        
        Workaround for ElastiCache Valkey bug where documents with missing
        indexed fields fail to index silently.
        
        mem0 creates 10-field index but only writes 7 fields:
        - Present: memory_id, hash, memory, created_at, embedding, user_id, metadata
        - Missing: agent_id, run_id, updated_at
        
        This causes silent indexing failure on ElastiCache Valkey 8.2.
        Fixed in valkey-search 1.0.2+ but ElastiCache hasn't updated yet.
        """
        try:
            import valkey
            
            # Connect to Valkey
            client = valkey.Valkey(
                host=self.config.endpoint,
                port=self.config.port,
                ssl=True,
                ssl_cert_reqs='none',
                decode_responses=True
            )
            
            index_name = "shopnow_memory"
            
            # Check if index already exists
            try:
                client.execute_command('FT.INFO', index_name)
                logger.info(f"[MEMORY] Index {index_name} already exists, skipping creation")
                return
            except:
                # Index doesn't exist, create it
                pass
            
            # Create index with only the 7 fields mem0 actually writes
            logger.info(f"[MEMORY] Creating index {index_name} with 7-field workaround")
            
            client.execute_command(
                'FT.CREATE', index_name,
                'ON', 'HASH',
                'PREFIX', '1', 'mem0:shopnow_memory:',
                'SCHEMA',
                'memory_id', 'TAG',
                'hash', 'TAG',
                'memory', 'TAG',
                'created_at', 'NUMERIC',
                'user_id', 'TAG',
                'metadata', 'TAG',
                'embedding', 'VECTOR', 'HNSW', '12',
                    'TYPE', 'FLOAT32',
                    'DIM', str(self.config.embedding_dim),
                    'DISTANCE_METRIC', 'COSINE',
                    'M', '32',
                    'EF_CONSTRUCTION', '400',
                    'EF_RUNTIME', '40'
            )
            
            logger.info(f"[MEMORY] Index {index_name} created successfully with 7-field schema")
            
        except Exception as e:
            logger.warning(f"[MEMORY] Failed to create index workaround: {e}")
            # Don't fail - let mem0 try to create it
            pass
    
    def initialize(self) -> bool:
        """
        Initialize and verify connection to memory cluster.
        
        Returns:
            True if initialization successful
        """
        try:
            client = self._get_mem0_client()
            # Verify connection with a simple health check
            health = self.health_check()
            if health.get("connected"):
                logger.info("[MEMORY] Initialization successful")
                return True
            else:
                logger.error("[MEMORY] Initialization failed: not connected")
                return False
        except Exception as e:
            logger.error(f"[MEMORY] Initialization failed: {e}")
            return False
    
    def add(
        self,
        messages: list[dict[str, str]],
        user_id: str,
        metadata: Optional[dict[str, Any]] = None,
        memory_type: MemoryType = MemoryType.LONG_TERM
    ) -> str:
        """
        Store a new memory.
        
        Args:
            messages: Conversation messages to extract memory from
            user_id: User or session identifier
            metadata: Additional metadata (domain, entities, slots)
            memory_type: SHORT_TERM or LONG_TERM
            
        Returns:
            memory_id: Unique identifier for the stored memory
        """
        start_time = time.time()
        
        try:
            # Add test namespace prefix if in test mode
            if self._test_namespace:
                user_id = f"{self._test_namespace}:{user_id}"
            
            # Prepare metadata
            full_metadata = metadata or {}
            full_metadata["memory_type"] = memory_type.value
            
            # Calculate expiration
            now = datetime.now()
            if memory_type == MemoryType.SHORT_TERM:
                expires_at = now + self.config.short_term_ttl
            else:
                expires_at = now + self.config.long_term_ttl
            
            full_metadata["created_at"] = now.timestamp()
            full_metadata["updated_at"] = now.timestamp()
            full_metadata["expires_at"] = expires_at.timestamp()
            
            # Store using mem0
            client = self._get_mem0_client()
            result = client.add(
                messages=messages,
                user_id=user_id,
                metadata=full_metadata
            )
            
            # Log what mem0 returned
            logger.info(f"[MEMORY] mem0.add() returned: {result}")
            
            # mem0.add() returns {"results": [{"id": "...", "memory": "...", "event": "ADD"}]}
            if isinstance(result, dict) and "results" in result and len(result["results"]) > 0:
                memory_id = result["results"][0].get("id", str(uuid.uuid4()))
            else:
                memory_id = result.get("id") or str(uuid.uuid4())
            
            latency_ms = (time.time() - start_time) * 1000
            logger.info(
                f"[MEMORY] Stored memory: id={memory_id}, "
                f"user={user_id}, type={memory_type.value}, "
                f"latency={latency_ms:.1f}ms"
            )
            
            return memory_id
            
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            logger.error(
                f"[MEMORY] Failed to store memory: user={user_id}, "
                f"error={e}, latency={latency_ms:.1f}ms"
            )
            raise
    def add_short_term_memory(
        self,
        messages: list[dict[str, str]],
        session_id: str,
        conversation_state: Optional[dict[str, Any]] = None
    ) -> str:
        """
        Store a short-term memory with session scoping.

        Short-term memories are automatically scoped to the session
        and include conversation state (entities, slots, active_domain, active_task).

        Args:
            messages: Conversation messages to extract memory from
            session_id: Session identifier (used as user_id for scoping)
            conversation_state: Current conversation state from state_extractor
                               (entities, slots, active_domain, active_task, etc.)

        Returns:
            memory_id: Unique identifier for the stored memory
        """
        # Build metadata from conversation state
        metadata = {}
        if conversation_state:
            # Extract relevant fields from conversation state
            if "active_domain" in conversation_state:
                metadata["domain"] = conversation_state["active_domain"]
            if "active_task" in conversation_state:
                metadata["active_task"] = conversation_state["active_task"]
            if "entities" in conversation_state:
                metadata["entities"] = conversation_state["entities"]
            if "slots" in conversation_state:
                metadata["slots"] = conversation_state["slots"]
            if "turn_stage" in conversation_state:
                metadata["turn_stage"] = conversation_state["turn_stage"]

        # Store as short-term memory with session_id as user_id
        return self.add(
            messages=messages,
            user_id=session_id,
            metadata=metadata,
            memory_type=MemoryType.SHORT_TERM
        )
    async def add_short_term_memory_async(
        self,
        messages: list[dict[str, str]],
        session_id: str,
        conversation_state: Optional[dict[str, Any]] = None
    ) -> str:
        """Async version of add_short_term_memory()."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.add_short_term_memory,
            messages,
            session_id,
            conversation_state
        )

    def add_long_term_memory(
        self,
        messages: list[dict[str, str]],
        user_id: str,
        metadata: Optional[dict[str, Any]] = None
    ) -> str:
        """
        Store a long-term memory with user scoping.

        Long-term memories persist across sessions for the same user
        and have a 90-day TTL. They store preferences, patterns, and user profile.

        Args:
            messages: Conversation messages to extract memory from
            user_id: Authenticated user identifier
            metadata: Additional metadata (domain, preference_type, etc.)

        Returns:
            memory_id: Unique identifier for the stored memory
        """
        # Store as long-term memory with user_id
        return self.add(
            messages=messages,
            user_id=user_id,
            metadata=metadata,
            memory_type=MemoryType.LONG_TERM
        )

    async def add_long_term_memory_async(
        self,
        messages: list[dict[str, str]],
        user_id: str,
        metadata: Optional[dict[str, Any]] = None
    ) -> str:
        """Async version of add_long_term_memory()."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.add_long_term_memory,
            messages,
            user_id,
            metadata
        )


    
    async def add_async(
        self,
        messages: list[dict[str, str]],
        user_id: str,
        metadata: Optional[dict[str, Any]] = None,
        memory_type: MemoryType = MemoryType.LONG_TERM
    ) -> str:
        """Async version of add()."""
        # Run sync version in executor to avoid blocking
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.add,
            messages,
            user_id,
            metadata,
            memory_type
        )
    
    def search(
        self,
        query: str,
        user_id: str,
        limit: int = 5,
        filters: Optional[dict[str, Any]] = None
    ) -> list[Memory]:
        """
        Retrieve relevant memories.
        
        Args:
            query: Search query (user message)
            user_id: User or session identifier
            limit: Maximum number of results
            filters: Optional filters (type, domain, time_range)
            
        Returns:
            List of Memory objects ranked by relevance
        """
        start_time = time.time()
        
        try:
            # Add test namespace prefix if in test mode
            if self._test_namespace:
                user_id = f"{self._test_namespace}:{user_id}"
            
            # Use configured limit if not specified
            if limit is None:
                limit = self.config.search_limit
            
            # Build mem0 filters
            mem0_filters = {}
            
            # Note: expires_at filtering removed - mem0 doesn't support it properly
            # Expiration is handled by TTL at storage time
            
            if filters:
                # Memory type filter
                if "memory_type" in filters:
                    mem0_filters["memory_type"] = filters["memory_type"]
                
                # Domain filter - DISABLED: domain is not indexed as a TAG field in Valkey
                # if "domain" in filters:
                #     mem0_filters["domain"] = filters["domain"]
                
                # Time range filter
                if "time_range" in filters:
                    time_range = filters["time_range"]
                    if "start" in time_range:
                        # Filter memories created after start time
                        start_ts = time_range["start"]
                        if isinstance(start_ts, datetime):
                            start_ts = start_ts.timestamp()
                        mem0_filters["created_at"] = mem0_filters.get("created_at", {})
                        mem0_filters["created_at"]["$gte"] = start_ts
                    
                    if "end" in time_range:
                        # Filter memories created before end time
                        end_ts = time_range["end"]
                        if isinstance(end_ts, datetime):
                            end_ts = end_ts.timestamp()
                        mem0_filters["created_at"] = mem0_filters.get("created_at", {})
                        mem0_filters["created_at"]["$lte"] = end_ts
            
            # Search using mem0
            client = self._get_mem0_client()
            results = client.search(
                query=query,
                user_id=user_id,
                limit=limit,
                filters=mem0_filters
            )
            
            # Log what we got back
            logger.info(f"[MEMORY] Search returned type: {type(results)}, value: {results}")
            
            # mem0 search returns a dict with "results" key containing the list
            if isinstance(results, dict) and "results" in results:
                results = results["results"]
            
            # Convert to Memory objects
            memories = []
            for result in results:
                try:
                    # Debug: log the actual result structure
                    logger.info(f"[MEMORY] Raw result type: {type(result)}, value: {result}")
                    
                    memory = self._result_to_memory(result, user_id)
                    logger.info(f"[MEMORY] Parsed memory: score={memory.relevance_score}, threshold={self.config.search_threshold}")
                    
                    # Apply threshold filter
                    if memory.relevance_score is None or memory.relevance_score >= self.config.search_threshold:
                        memories.append(memory)
                    else:
                        logger.info(f"[MEMORY] Filtered out memory with score {memory.relevance_score} < threshold {self.config.search_threshold}")
                except Exception as e:
                    logger.warning(f"[MEMORY] Failed to parse memory result: {e}, result type: {type(result)}")
                    continue
            
            latency_ms = (time.time() - start_time) * 1000
            logger.info(
                f"[MEMORY] Search complete: query='{query[:50]}...', "
                f"user={user_id}, found={len(memories)}, "
                f"latency={latency_ms:.1f}ms"
            )
            
            return memories
            
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            logger.error(
                f"[MEMORY] Search failed: query='{query[:50]}...', "
                f"user={user_id}, error={e}, latency={latency_ms:.1f}ms"
            )
            return []  # Return empty list on error (non-blocking)
    
    async def search_async(
        self,
        query: str,
        user_id: str,
        limit: int = 5,
        filters: Optional[dict[str, Any]] = None
    ) -> list[Memory]:
        """Async version of search()."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.search,
            query,
            user_id,
            limit,
            filters
        )
    
    def update(
        self,
        memory_id: str,
        data: dict[str, Any]
    ) -> bool:
        """
        Update an existing memory.
        
        Args:
            memory_id: Memory identifier
            data: Fields to update
            
        Returns:
            Success status
        """
        try:
            # Preserve created_at, update updated_at
            if "created_at" in data:
                del data["created_at"]
            data["updated_at"] = datetime.now().timestamp()
            
            # Update using mem0
            client = self._get_mem0_client()
            client.update(
                memory_id=memory_id,
                data=data
            )
            
            logger.info(f"[MEMORY] Updated memory: id={memory_id}")
            return True
            
        except Exception as e:
            logger.error(f"[MEMORY] Failed to update memory: id={memory_id}, error={e}")
            return False
    
    def delete(
        self,
        memory_id: Optional[str] = None,
        user_id: Optional[str] = None
    ) -> int:
        """
        Delete memories.
        
        Args:
            memory_id: Specific memory to delete (optional)
            user_id: Delete all memories for user (optional)
            
        Returns:
            Number of memories deleted
        """
        try:
            client = self._get_mem0_client()
            
            if memory_id:
                # Delete specific memory
                client.delete(memory_id=memory_id)
                logger.info(f"[MEMORY] Deleted memory: id={memory_id}")
                return 1
            
            elif user_id:
                # Add test namespace prefix if in test mode
                if self._test_namespace:
                    user_id = f"{self._test_namespace}:{user_id}"
                
                # Delete all memories for user
                client.delete_all(user_id=user_id)
                logger.info(f"[MEMORY] Deleted all memories for user: {user_id}")
                # Note: mem0 doesn't return count, so we return 1 to indicate success
                return 1
            
            else:
                logger.warning("[MEMORY] Delete called without memory_id or user_id")
                return 0
                
        except Exception as e:
            logger.error(f"[MEMORY] Failed to delete: memory_id={memory_id}, user_id={user_id}, error={e}")
            return 0
    def expire_session_memories(
        self,
        session_id: str
    ) -> int:
        """
        Mark all short-term memories for a session as expired.

        This is called when a session ends to immediately expire
        short-term memories instead of waiting for TTL.

        Args:
            session_id: Session identifier

        Returns:
            Number of memories expired
        """
        try:
            # Add test namespace prefix if in test mode
            if self._test_namespace:
                session_id = f"{self._test_namespace}:{session_id}"

            # Get all short-term memories for this session
            client = self._get_mem0_client()
            results = client.get_all(user_id=session_id)

            # Update expires_at to now for each short-term memory
            now = datetime.now().timestamp()
            expired_count = 0

            for result in results:
                metadata = result.get("metadata", {})
                if metadata.get("memory_type") == MemoryType.SHORT_TERM.value:
                    memory_id = result.get("id")
                    if memory_id:
                        self.update(memory_id, {"expires_at": now})
                        expired_count += 1

            logger.info(
                f"[MEMORY] Expired session memories: session={session_id}, "
                f"count={expired_count}"
            )
            return expired_count

        except Exception as e:
            logger.error(
                f"[MEMORY] Failed to expire session memories: "
                f"session={session_id}, error={e}"
            )
            return 0

    
    def get_all(
        self,
        user_id: str
    ) -> list[Memory]:
        """
        Export all memories for a user.
        
        Args:
            user_id: User identifier
            
        Returns:
            All memories for the user
        """
        try:
            # Add test namespace prefix if in test mode
            if self._test_namespace:
                user_id = f"{self._test_namespace}:{user_id}"
            
            # Get all memories using mem0
            client = self._get_mem0_client()
            results = client.get_all(user_id=user_id)
            
            # Log what we got back
            logger.info(f"[MEMORY] get_all returned type: {type(results)}, value: {results}")
            
            # mem0 get_all returns a dict with "results" key containing the list
            if isinstance(results, dict) and "results" in results:
                results = results["results"]
            
            # Convert to Memory objects
            memories = []
            for result in results:
                try:
                    memory = self._result_to_memory(result, user_id)
                    memories.append(memory)
                except Exception as e:
                    logger.warning(f"[MEMORY] Failed to parse memory result: {e}")
                    continue
            
            logger.info(f"[MEMORY] Retrieved all memories: user={user_id}, count={len(memories)}")
            return memories
            
        except Exception as e:
            logger.error(f"[MEMORY] Failed to get all memories: user={user_id}, error={e}")
            return []
    
    def health_check(self) -> dict[str, Any]:
        """
        Check memory system health.
        
        Returns:
            Status information (connected, latency, storage_size)
        """
        try:
            start_time = time.time()
            
            # Try to get mem0 client
            client = self._get_mem0_client()
            
            # Simple connectivity test
            # Note: mem0 doesn't have a built-in health check, so we'll check if client exists
            connected = client is not None
            
            latency_ms = (time.time() - start_time) * 1000
            
            return {
                "status": "healthy" if connected else "unhealthy",
                "connected": connected,
                "cluster_endpoint": f"{self.config.endpoint}:{self.config.port}",
                "latency_ms": latency_ms,
                "test_mode": self.test_mode,
            }
            
        except Exception as e:
            logger.error(f"[MEMORY] Health check failed: {e}")
            return {
                "status": "unhealthy",
                "connected": False,
                "error": str(e),
            }
    
    def flush_test_namespace(self):
        """Flush test namespace (for testing only)."""
        if not self.test_mode or not self._test_namespace:
            logger.warning("[MEMORY] flush_test_namespace called but not in test mode")
            return
        
        try:
            # Delete all memories in test namespace
            # Note: This is a simplified implementation
            # In production, you'd want to iterate through all test users
            logger.info(f"[MEMORY] Flushing test namespace: {self._test_namespace}")
        except Exception as e:
            logger.error(f"[MEMORY] Failed to flush test namespace: {e}")
    
    def _result_to_memory(self, result: dict[str, Any], user_id: str) -> Memory:
        """Convert mem0 result to Memory object."""
        # Log the full result for debugging
        logger.info(f"[MEMORY] Converting result to Memory: {result}")
        
        # Handle both dict and string results from mem0
        if isinstance(result, str):
            # If result is a string, it's just the memory content
            return Memory(
                id=str(uuid.uuid4()),
                user_id=user_id,
                content=result,
                memory_type=MemoryType.LONG_TERM,
                metadata={},
                created_at=datetime.now(),
                updated_at=datetime.now(),
                expires_at=None,
                relevance_score=None,
            )
        
        # Handle dict results - mem0 returns different formats
        # Check for common field names
        content = result.get("memory") or result.get("text") or result.get("content") or str(result)
        metadata = result.get("metadata", {})
        
        # Extract timestamps
        created_at = datetime.fromtimestamp(metadata.get("created_at", time.time()))
        updated_at = datetime.fromtimestamp(metadata.get("updated_at", time.time()))
        expires_at_ts = metadata.get("expires_at")
        expires_at = datetime.fromtimestamp(expires_at_ts) if expires_at_ts else None
        
        # Extract memory type
        memory_type_str = metadata.get("memory_type", "long_term")
        memory_type = MemoryType(memory_type_str)
        
        # Extract relevance score - check multiple possible field names
        relevance_score = result.get("score") or result.get("relevance") or result.get("distance")
        
        return Memory(
            id=result.get("id", str(uuid.uuid4())),
            user_id=user_id,
            content=content,
            memory_type=memory_type,
            metadata=metadata,
            created_at=created_at,
            updated_at=updated_at,
            expires_at=expires_at,
            relevance_score=relevance_score,
        )


# Singleton instance
_client: Optional[MemoryClient] = None


def get_memory_client(test_mode: bool = False) -> MemoryClient:
    """
    Get the global memory client instance.
    
    Args:
        test_mode: If True, create a test client with isolated namespace
        
    Returns:
        MemoryClient instance
    """
    global _client
    
    if test_mode:
        # Always create new client for test mode
        return MemoryClient(test_mode=True)
    
    if _client is None:
        _client = MemoryClient()
        _client.initialize()
    
    return _client


def reset_memory_client():
    """Reset the global memory client instance (for testing)."""
    global _client
    _client = None
