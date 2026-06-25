"""
Memory system for agent short-term and long-term memory using mem0 framework.

This module provides memory capabilities for the ShopNow AI agent:
- Short-term memory: Session-scoped context (24h TTL)
- Long-term memory: User-scoped preferences and patterns (90d TTL)
"""

from agentic_shopping_demo.memory.models import (
    Memory,
    MemoryCandidate,
    MemoryType,
)
from agentic_shopping_demo.memory.config import (
    MemoryConfig,
    get_config,
    reset_config,
)
from agentic_shopping_demo.memory.client import (
    MemoryClient,
    get_memory_client,
    reset_memory_client,
)
from agentic_shopping_demo.memory.extractor import MemoryExtractor
from agentic_shopping_demo.memory.lifecycle import (
    MemoryExpirationManager,
    cleanup_expired_memories,
)
from agentic_shopping_demo.memory.privacy import (
    PIIFilter,
    SensitiveDataHasher,
    DataRetentionManager,
    MemoryContentSanitizer,
    MemoryAuditLogger,
    export_user_data,
)
from agentic_shopping_demo.memory.user_identifier import (
    UserIdentifier,
    create_user_identifier_from_request,
    get_or_create_anonymous_id,
    migrate_anonymous_memories,
    handle_user_id_collision,
)
from agentic_shopping_demo.memory.errors import (
    MemoryError,
    MemoryConnectionError,
    MemoryTimeoutError,
    MemoryValidationError,
    MemoryNotFoundError,
    MemoryStorageError,
    CircuitBreaker,
    RetryHandler,
    MemoryErrorHandler,
    get_memories_with_fallback,
)
from agentic_shopping_demo.memory.integration import (
    format_memories_for_prompt,
    inject_memory_context,
    retrieve_memories,
    store_memories_async,
)
from agentic_shopping_demo.memory.metrics import (
    MemoryMetrics,
    get_metrics,
    reset_metrics,
)

__all__ = [
    # Models
    "Memory",
    "MemoryCandidate",
    "MemoryType",
    # Config
    "MemoryConfig",
    "get_config",
    "reset_config",
    # Client
    "MemoryClient",
    "get_memory_client",
    "reset_memory_client",
    # Extractor
    "MemoryExtractor",
    # Lifecycle
    "MemoryExpirationManager",
    "cleanup_expired_memories",
    # Privacy
    "PIIFilter",
    "SensitiveDataHasher",
    "DataRetentionManager",
    "MemoryContentSanitizer",
    "MemoryAuditLogger",
    "export_user_data",
    # User identification
    "UserIdentifier",
    "create_user_identifier_from_request",
    "get_or_create_anonymous_id",
    "migrate_anonymous_memories",
    "handle_user_id_collision",
    # Error handling
    "MemoryError",
    "MemoryConnectionError",
    "MemoryTimeoutError",
    "MemoryValidationError",
    "MemoryNotFoundError",
    "MemoryStorageError",
    "CircuitBreaker",
    "RetryHandler",
    "MemoryErrorHandler",
    "get_memories_with_fallback",
    # Integration
    "format_memories_for_prompt",
    "inject_memory_context",
    "retrieve_memories",
    "store_memories_async",
    # Metrics
    "MemoryMetrics",
    "get_metrics",
    "reset_metrics",
]
