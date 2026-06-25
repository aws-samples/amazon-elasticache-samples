"""
Memory lifecycle management - expiration and cleanup.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from agentic_shopping_demo.memory.client import MemoryClient, get_memory_client
from agentic_shopping_demo.memory.models import MemoryType

logger = logging.getLogger(__name__)


class MemoryExpirationManager:
    """
    Manages memory expiration and automatic cleanup.
    
    Handles:
    - Cleanup of expired memories
    - Extension of expiration for valuable memories
    - Scheduled cleanup tasks
    """
    
    def __init__(self, client: Optional[MemoryClient] = None):
        """
        Initialize expiration manager.
        
        Args:
            client: MemoryClient instance (uses global if None)
        """
        self.client = client or get_memory_client()
        logger.info("[MEMORY] MemoryExpirationManager initialized")
    
    def cleanup_expired(self, batch_size: int = 100) -> int:
        """
        Remove expired memories from the system.
        
        This method should be run periodically (e.g., daily) to clean up
        expired memories and free storage space.
        
        Args:
            batch_size: Number of memories to process per batch
            
        Returns:
            Number of memories deleted
        """
        try:
            logger.info("[MEMORY] Starting expired memory cleanup")
            
            # Note: mem0 doesn't provide a direct way to query all users
            # In production, you'd maintain a user index or use Redis SCAN
            # For now, we'll document that cleanup happens via TTL
            
            # mem0 with Redis backend uses TTL for automatic expiration
            # The expires_at field is used for filtering in searches
            # Actual deletion happens via Redis TTL mechanism
            
            logger.info(
                "[MEMORY] Expired memory cleanup relies on Redis TTL. "
                "Memories are automatically removed when TTL expires."
            )
            
            # Return 0 since cleanup is handled by Redis TTL
            return 0
            
        except Exception as e:
            logger.error(f"[MEMORY] Failed to cleanup expired memories: {e}")
            return 0
    
    def extend_expiration(
        self,
        memory_id: str,
        extension_days: int = 30
    ) -> bool:
        """
        Extend expiration time for a valuable memory.
        
        Use this to keep important memories longer than their default TTL.
        
        Args:
            memory_id: Memory identifier
            extension_days: Number of days to extend expiration
            
        Returns:
            Success status
        """
        try:
            # Calculate new expiration
            new_expires_at = datetime.now() + timedelta(days=extension_days)
            
            # Update memory
            success = self.client.update(
                memory_id=memory_id,
                data={"expires_at": new_expires_at.timestamp()}
            )
            
            if success:
                logger.info(
                    f"[MEMORY] Extended expiration: id={memory_id}, "
                    f"new_expires_at={new_expires_at.isoformat()}"
                )
            else:
                logger.warning(
                    f"[MEMORY] Failed to extend expiration: id={memory_id}"
                )
            
            return success
            
        except Exception as e:
            logger.error(
                f"[MEMORY] Failed to extend expiration: id={memory_id}, error={e}"
            )
            return False
    
    def schedule_cleanup_task(self) -> dict:
        """
        Get configuration for scheduled cleanup task.
        
        Returns a configuration dict that can be used with task schedulers
        like APScheduler, Celery, or AWS EventBridge.
        
        Returns:
            Task configuration
        """
        return {
            "task_name": "memory_cleanup",
            "function": "agentic_shopping_demo.memory.lifecycle.MemoryExpirationManager.cleanup_expired",
            "schedule": "cron",
            "cron_expression": "0 2 * * *",  # Daily at 2 AM
            "description": "Clean up expired memories",
            "enabled": True,
        }


def cleanup_expired_memories(batch_size: int = 100) -> int:
    """
    Convenience function for scheduled cleanup tasks.
    
    Args:
        batch_size: Number of memories to process per batch
        
    Returns:
        Number of memories deleted
    """
    manager = MemoryExpirationManager()
    return manager.cleanup_expired(batch_size=batch_size)
