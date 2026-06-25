"""
User identification system for memory scoping.
"""

import hashlib
import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


class UserIdentifier:
    """
    Manages user identification for memory scoping.
    
    Implements priority hierarchy:
    1. Authenticated user ID (from auth system)
    2. Anonymous user ID (from cookie/header)
    3. Session ID (fallback)
    """
    
    def __init__(
        self,
        authenticated_user_id: Optional[str] = None,
        anonymous_user_id: Optional[str] = None,
        session_id: Optional[str] = None
    ):
        """
        Initialize user identifier.
        
        Args:
            authenticated_user_id: ID from authentication system
            anonymous_user_id: Persistent anonymous ID from cookie/header
            session_id: Current session ID (fallback)
        """
        self.authenticated_user_id = authenticated_user_id
        self.anonymous_user_id = anonymous_user_id
        self.session_id = session_id or str(uuid.uuid4())
    
    def get_user_id(self) -> str:
        """
        Get user ID following priority hierarchy.
        
        Priority:
        1. Authenticated user ID
        2. Anonymous user ID
        3. Session ID
        
        Returns:
            User identifier string
        """
        if self.authenticated_user_id:
            return f"user:{self.authenticated_user_id}"
        elif self.anonymous_user_id:
            return f"anon:{self.anonymous_user_id}"
        else:
            return f"session:{self.session_id}"
    
    def is_authenticated(self) -> bool:
        """Check if user is authenticated."""
        return self.authenticated_user_id is not None
    
    def is_anonymous(self) -> bool:
        """Check if user is anonymous (has persistent anonymous ID)."""
        return (
            self.authenticated_user_id is None
            and self.anonymous_user_id is not None
        )
    
    def is_session_only(self) -> bool:
        """Check if user is session-only (no persistent ID)."""
        return (
            self.authenticated_user_id is None
            and self.anonymous_user_id is None
        )
    
    @staticmethod
    def generate_anonymous_id(seed: Optional[str] = None) -> str:
        """
        Generate a persistent anonymous user ID.
        
        Args:
            seed: Optional seed for deterministic generation
            
        Returns:
            Anonymous user ID
        """
        if seed:
            # Deterministic generation from seed
            hash_obj = hashlib.sha256(seed.encode())
            return hash_obj.hexdigest()[:16]
        else:
            # Random generation
            return uuid.uuid4().hex[:16]
    
    def get_memory_scope(self) -> str:
        """
        Get memory scope type for logging/debugging.
        
        Returns:
            Scope type: "authenticated", "anonymous", or "session"
        """
        if self.is_authenticated():
            return "authenticated"
        elif self.is_anonymous():
            return "anonymous"
        else:
            return "session"


def get_or_create_anonymous_id(
    request_cookies: Optional[dict] = None,
    request_headers: Optional[dict] = None
) -> str:
    """
    Get or create anonymous user ID from request.
    
    Checks in order:
    1. Cookie: anonymous_user_id
    2. Header: X-Anonymous-User-Id
    3. Generate new ID
    
    Args:
        request_cookies: Request cookies dict
        request_headers: Request headers dict
        
    Returns:
        Anonymous user ID
    """
    # Check cookie
    if request_cookies and "anonymous_user_id" in request_cookies:
        anon_id = request_cookies["anonymous_user_id"]
        logger.debug(f"[MEMORY] Using anonymous ID from cookie: {anon_id}")
        return anon_id
    
    # Check header
    if request_headers:
        # Try different header formats
        for header_name in ["X-Anonymous-User-Id", "x-anonymous-user-id"]:
            if header_name in request_headers:
                anon_id = request_headers[header_name]
                logger.debug(f"[MEMORY] Using anonymous ID from header: {anon_id}")
                return anon_id
    
    # Generate new ID
    anon_id = UserIdentifier.generate_anonymous_id()
    logger.info(f"[MEMORY] Generated new anonymous ID: {anon_id}")
    return anon_id


def create_user_identifier_from_request(
    authenticated_user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    request_cookies: Optional[dict] = None,
    request_headers: Optional[dict] = None
) -> UserIdentifier:
    """
    Create UserIdentifier from request context.
    
    Args:
        authenticated_user_id: ID from auth system
        session_id: Current session ID
        request_cookies: Request cookies
        request_headers: Request headers
        
    Returns:
        UserIdentifier instance
    """
    # Get or create anonymous ID if not authenticated
    anonymous_user_id = None
    if not authenticated_user_id:
        anonymous_user_id = get_or_create_anonymous_id(
            request_cookies=request_cookies,
            request_headers=request_headers
        )
    
    return UserIdentifier(
        authenticated_user_id=authenticated_user_id,
        anonymous_user_id=anonymous_user_id,
        session_id=session_id
    )


def migrate_anonymous_memories(
    from_anonymous_id: str,
    to_user_id: str,
    client: Optional["MemoryClient"] = None
) -> int:
    """
    Migrate memories from anonymous user to authenticated user.
    
    Called when a user authenticates to transfer their anonymous
    memories to their authenticated account.
    
    Args:
        from_anonymous_id: Anonymous user ID
        to_user_id: Authenticated user ID
        client: MemoryClient instance (uses global if None)
        
    Returns:
        Number of memories migrated
    """
    from agentic_shopping_demo.memory.client import get_memory_client
    
    if client is None:
        client = get_memory_client()
    
    try:
        # Get all memories for anonymous user
        from_user_id = f"anon:{from_anonymous_id}"
        to_user_id_full = f"user:{to_user_id}"
        
        memories = client.get_all(user_id=from_user_id)
        
        if not memories:
            logger.info(
                f"[MEMORY] No memories to migrate: "
                f"from={from_user_id} to={to_user_id_full}"
            )
            return 0
        
        # Migrate each memory
        migrated_count = 0
        for memory in memories:
            try:
                # Update user_id in metadata
                memory.metadata["migrated_from"] = from_user_id
                memory.metadata["migrated_at"] = memory.updated_at.timestamp()
                
                # Note: mem0 doesn't support changing user_id directly
                # We need to delete and recreate with new user_id
                
                # Store with new user_id
                client.add(
                    messages=[{"role": "assistant", "content": memory.content}],
                    user_id=to_user_id_full,
                    metadata=memory.metadata,
                    memory_type=memory.memory_type
                )
                
                # Delete old memory
                client.delete(memory_id=memory.id)
                
                migrated_count += 1
                
            except Exception as e:
                logger.error(
                    f"[MEMORY] Failed to migrate memory: "
                    f"id={memory.id}, error={e}"
                )
                continue
        
        logger.info(
            f"[MEMORY] Migrated memories: from={from_user_id} "
            f"to={to_user_id_full}, count={migrated_count}"
        )
        
        return migrated_count
        
    except Exception as e:
        logger.error(
            f"[MEMORY] Failed to migrate memories: "
            f"from={from_anonymous_id} to={to_user_id}, error={e}"
        )
        return 0



def handle_user_id_collision(
    memory_content: str,
    user_id: str,
    metadata: dict,
    client: Optional["MemoryClient"] = None
) -> Optional[str]:
    """
    Handle duplicate memory detection and merging.
    
    When storing a memory, check if similar content already exists
    for this user. If so, update the existing memory instead of
    creating a duplicate.
    
    Args:
        memory_content: Content to store
        user_id: User identifier
        metadata: Memory metadata
        client: MemoryClient instance
        
    Returns:
        Existing memory ID if collision detected, None otherwise
    """
    from agentic_shopping_demo.memory.client import get_memory_client
    
    if client is None:
        client = get_memory_client()
    
    try:
        # Search for similar memories
        existing_memories = client.search(
            query=memory_content,
            user_id=user_id,
            limit=1
        )
        
        if not existing_memories:
            return None
        
        # Check if top result is very similar (high relevance score)
        top_memory = existing_memories[0]
        if top_memory.relevance_score and top_memory.relevance_score > 0.95:
            # Very similar memory exists - update instead of create
            logger.info(
                f"[MEMORY] Collision detected: id={top_memory.id}, "
                f"score={top_memory.relevance_score:.3f}"
            )
            
            # Increment observation count
            observation_count = top_memory.metadata.get("observation_count", 1)
            top_memory.metadata["observation_count"] = observation_count + 1
            
            # Update confidence score
            confidence = top_memory.metadata.get("confidence", 0.5)
            # Increase confidence with each observation (max 1.0)
            new_confidence = min(1.0, confidence + 0.1)
            top_memory.metadata["confidence"] = new_confidence
            
            # Merge metadata
            for key, value in metadata.items():
                if key not in top_memory.metadata:
                    top_memory.metadata[key] = value
            
            # Update the memory
            client.update(
                memory_id=top_memory.id,
                data=top_memory.metadata
            )
            
            return top_memory.id
        
        return None
        
    except Exception as e:
        logger.error(
            f"[MEMORY] Failed to check collision: user={user_id}, error={e}"
        )
        return None
