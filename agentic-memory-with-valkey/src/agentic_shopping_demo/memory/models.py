"""
Data models for agent memory system.

Defines the core data structures for storing and retrieving memories.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class MemoryType(str, Enum):
    """Type of memory: short-term (session-scoped) or long-term (user-scoped)."""
    
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"


@dataclass
class Memory:
    """
    Represents a stored memory entry.
    
    Attributes:
        id: Unique identifier for the memory
        user_id: User or session identifier (format: "user:id", "anon:id", or "session:id")
        content: The memory content (natural language text)
        memory_type: SHORT_TERM or LONG_TERM
        metadata: Additional structured data (domain, entities, slots, etc.)
        created_at: When the memory was first created
        updated_at: When the memory was last modified
        expires_at: When the memory should be automatically deleted (None = no expiration)
        relevance_score: Similarity score from vector search (set during retrieval)
    """
    
    id: str
    user_id: str
    content: str
    memory_type: MemoryType
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    expires_at: Optional[datetime] = None
    relevance_score: Optional[float] = None
    
    def __post_init__(self):
        """Validate memory entry after initialization."""
        if not self.id:
            raise ValueError("Memory id cannot be empty")
        if not self.user_id:
            raise ValueError("Memory user_id cannot be empty")
        if not self.content:
            raise ValueError("Memory content cannot be empty")
        if not isinstance(self.memory_type, MemoryType):
            raise ValueError(f"Invalid memory_type: {self.memory_type}")
        if not isinstance(self.metadata, dict):
            raise ValueError("Memory metadata must be a dictionary")
        if not isinstance(self.created_at, datetime):
            raise ValueError("Memory created_at must be a datetime")
        if not isinstance(self.updated_at, datetime):
            raise ValueError("Memory updated_at must be a datetime")
        if self.expires_at is not None and not isinstance(self.expires_at, datetime):
            raise ValueError("Memory expires_at must be a datetime or None")
        if self.relevance_score is not None and not (0.0 <= self.relevance_score <= 1.0):
            raise ValueError("Memory relevance_score must be between 0.0 and 1.0")
    
    def is_expired(self) -> bool:
        """Check if memory has expired."""
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at
    
    def to_dict(self) -> dict[str, Any]:
        """Convert memory to dictionary for serialization."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "content": self.content,
            "memory_type": self.memory_type.value,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "relevance_score": self.relevance_score,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Memory":
        """Create memory from dictionary."""
        return cls(
            id=data["id"],
            user_id=data["user_id"],
            content=data["content"],
            memory_type=MemoryType(data["memory_type"]),
            metadata=data["metadata"],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
            relevance_score=data.get("relevance_score"),
        )


@dataclass
class MemoryCandidate:
    """
    Represents a potential memory before storage.
    
    Used by MemoryExtractor to identify memory-worthy information
    from conversations before committing to storage.
    
    Attributes:
        content: The memory content to potentially store
        memory_type: SHORT_TERM or LONG_TERM
        metadata: Additional structured data
        confidence: Confidence score (0.0-1.0) that this should be stored
    """
    
    content: str
    memory_type: MemoryType
    metadata: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    
    def __post_init__(self):
        """Validate memory candidate after initialization."""
        if not self.content:
            raise ValueError("MemoryCandidate content cannot be empty")
        if not isinstance(self.memory_type, MemoryType):
            raise ValueError(f"Invalid memory_type: {self.memory_type}")
        if not isinstance(self.metadata, dict):
            raise ValueError("MemoryCandidate metadata must be a dictionary")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("MemoryCandidate confidence must be between 0.0 and 1.0")
    
    def to_dict(self) -> dict[str, Any]:
        """Convert candidate to dictionary for serialization."""
        return {
            "content": self.content,
            "memory_type": self.memory_type.value,
            "metadata": self.metadata,
            "confidence": self.confidence,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryCandidate":
        """Create candidate from dictionary."""
        return cls(
            content=data["content"],
            memory_type=MemoryType(data["memory_type"]),
            metadata=data.get("metadata", {}),
            confidence=data.get("confidence", 1.0),
        )
