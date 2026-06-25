"""
Privacy and data handling components for memory system.
"""

import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PIIFilter:
    """
    Detects and redacts Personally Identifiable Information (PII).
    
    Prevents storage of sensitive personal information in memories.
    """
    
    # PII detection patterns
    EMAIL_PATTERN = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    PHONE_PATTERN = r'\b(?:\+?1[-.]?)?\(?([0-9]{3})\)?[-.]?([0-9]{3})[-.]?([0-9]{4})\b'
    SSN_PATTERN = r'\b\d{3}-\d{2}-\d{4}\b'
    CREDIT_CARD_PATTERN = r'\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b'
    ADDRESS_PATTERN = r'\b\d+\s+[A-Za-z\s]+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Circle|Cir)\b'
    
    def __init__(self):
        """Initialize PII filter."""
        logger.info("[MEMORY] PIIFilter initialized")
    
    def contains_pii(self, text: str) -> bool:
        """
        Check if text contains PII.
        
        Args:
            text: Text to check
            
        Returns:
            True if PII detected
        """
        patterns = [
            self.EMAIL_PATTERN,
            self.PHONE_PATTERN,
            self.SSN_PATTERN,
            self.CREDIT_CARD_PATTERN,
            self.ADDRESS_PATTERN,
        ]
        
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        
        return False
    
    def redact_pii(self, text: str) -> str:
        """
        Redact PII from text.
        
        Args:
            text: Text to redact
            
        Returns:
            Text with PII replaced by placeholders
        """
        # Redact email
        text = re.sub(
            self.EMAIL_PATTERN,
            '[REDACTED_EMAIL]',
            text,
            flags=re.IGNORECASE
        )
        
        # Redact phone
        text = re.sub(
            self.PHONE_PATTERN,
            '[REDACTED_PHONE]',
            text
        )
        
        # Redact SSN
        text = re.sub(
            self.SSN_PATTERN,
            '[REDACTED_SSN]',
            text
        )
        
        # Redact credit card
        text = re.sub(
            self.CREDIT_CARD_PATTERN,
            '[REDACTED_CREDIT_CARD]',
            text
        )
        
        # Redact address
        text = re.sub(
            self.ADDRESS_PATTERN,
            '[REDACTED_ADDRESS]',
            text,
            flags=re.IGNORECASE
        )
        
        return text
    
    def should_store(self, text: str) -> bool:
        """
        Determine if text should be stored (no PII).
        
        Args:
            text: Text to evaluate
            
        Returns:
            True if safe to store, False if contains PII
        """
        return not self.contains_pii(text)


class SensitiveDataHasher:
    """
    Hashes sensitive identifiers for privacy-safe storage.
    
    Uses SHA-256 with salt for one-way hashing.
    """
    
    def __init__(self, salt: Optional[str] = None):
        """
        Initialize hasher.
        
        Args:
            salt: Salt for hashing (generated if None)
        """
        self.salt = salt or "shopnow_memory_salt_2024"
        logger.info("[MEMORY] SensitiveDataHasher initialized")
    
    def hash_value(self, value: str) -> str:
        """
        Hash a sensitive value.
        
        Args:
            value: Value to hash
            
        Returns:
            Hashed value (hex string)
        """
        salted = f"{self.salt}:{value}"
        hash_obj = hashlib.sha256(salted.encode())
        return hash_obj.hexdigest()
    
    def hash_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """
        Hash sensitive fields in metadata.
        
        Hashes: order_id, tracking_number
        
        Args:
            metadata: Metadata dict
            
        Returns:
            Metadata with sensitive fields hashed
        """
        hashed_metadata = metadata.copy()
        hashed_fields = []
        
        # Hash order_id
        if "order_id" in hashed_metadata:
            original = hashed_metadata["order_id"]
            hashed_metadata["order_id"] = self.hash_value(str(original))
            hashed_fields.append("order_id")
        
        # Hash tracking_number
        if "tracking_number" in hashed_metadata:
            original = hashed_metadata["tracking_number"]
            hashed_metadata["tracking_number"] = self.hash_value(str(original))
            hashed_fields.append("tracking_number")
        
        # Track which fields were hashed
        if hashed_fields:
            hashed_metadata["_hashed_fields"] = hashed_fields
        
        return hashed_metadata


class DataRetentionManager:
    """
    Manages data retention policies for compliance.
    
    Enforces TTL and maximum retention periods.
    """
    
    def __init__(
        self,
        short_term_ttl_days: int = 1,
        long_term_ttl_days: int = 90,
        max_retention_days: int = 365
    ):
        """
        Initialize retention manager.
        
        Args:
            short_term_ttl_days: TTL for short-term memories
            long_term_ttl_days: TTL for long-term memories
            max_retention_days: Maximum retention period
        """
        self.short_term_ttl = timedelta(days=short_term_ttl_days)
        self.long_term_ttl = timedelta(days=long_term_ttl_days)
        self.max_retention = timedelta(days=max_retention_days)
        
        logger.info(
            f"[MEMORY] DataRetentionManager initialized: "
            f"short_term={short_term_ttl_days}d, "
            f"long_term={long_term_ttl_days}d, "
            f"max={max_retention_days}d"
        )
    
    def set_expiration(
        self,
        memory_type: str,
        created_at: Optional[datetime] = None
    ) -> datetime:
        """
        Calculate expiration timestamp for memory.
        
        Args:
            memory_type: "short_term" or "long_term"
            created_at: Creation timestamp (uses now if None)
            
        Returns:
            Expiration timestamp
        """
        if created_at is None:
            created_at = datetime.now()
        
        if memory_type == "short_term":
            expires_at = created_at + self.short_term_ttl
        else:
            expires_at = created_at + self.long_term_ttl
        
        # Enforce maximum retention
        max_expires_at = created_at + self.max_retention
        if expires_at > max_expires_at:
            expires_at = max_expires_at
        
        return expires_at
    
    def enforce_max_retention(
        self,
        created_at: datetime
    ) -> bool:
        """
        Check if memory exceeds maximum retention period.
        
        Args:
            created_at: Memory creation timestamp
            
        Returns:
            True if should be deleted
        """
        age = datetime.now() - created_at
        return age > self.max_retention


class MemoryContentSanitizer:
    """
    Sanitizes memory content for privacy-safe storage.
    
    Combines PII filtering and sensitive data hashing.
    """
    
    def __init__(self):
        """Initialize sanitizer."""
        self.pii_filter = PIIFilter()
        self.hasher = SensitiveDataHasher()
        logger.info("[MEMORY] MemoryContentSanitizer initialized")
    
    def sanitize(
        self,
        content: str,
        metadata: Optional[dict[str, Any]] = None
    ) -> tuple[str, dict[str, Any]]:
        """
        Sanitize content and metadata.
        
        Args:
            content: Memory content
            metadata: Memory metadata
            
        Returns:
            Tuple of (sanitized_content, sanitized_metadata)
        """
        # Redact PII from content
        sanitized_content = self.pii_filter.redact_pii(content)
        
        # Generalize content
        sanitized_content = self._generalize_content(sanitized_content)
        
        # Hash sensitive metadata
        sanitized_metadata = metadata or {}
        sanitized_metadata = self.hasher.hash_metadata(sanitized_metadata)
        
        return sanitized_content, sanitized_metadata
    
    def _generalize_content(self, content: str) -> str:
        """
        Generalize overly specific information.
        
        Args:
            content: Content to generalize
            
        Returns:
            Generalized content
        """
        # Replace specific dates with relative time
        content = re.sub(
            r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b',
            '[DATE]',
            content,
            flags=re.IGNORECASE
        )
        
        # Replace specific prices with price range
        content = re.sub(
            r'\$\d+\.\d{2}',
            '[PRICE]',
            content
        )
        
        # Replace specific times
        content = re.sub(
            r'\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\b',
            '[TIME]',
            content
        )
        
        return content


class MemoryAuditLogger:
    """
    Logs memory operations for audit trail.
    
    Maintains compliance logs for GDPR and data governance.
    """
    
    def __init__(self):
        """Initialize audit logger."""
        self.logger = logging.getLogger("memory.audit")
        logger.info("[MEMORY] MemoryAuditLogger initialized")
    
    def log_operation(
        self,
        operation: str,
        memory_id: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None
    ):
        """
        Log a memory operation.
        
        Args:
            operation: Operation type (create, read, update, delete)
            memory_id: Memory identifier
            user_id: User identifier
            metadata: Additional metadata
        """
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "operation": operation,
            "memory_id": memory_id,
            "user_id": user_id,
            "metadata": metadata or {}
        }
        
        self.logger.info(f"[AUDIT] {log_entry}")
    
    def log_deletion(
        self,
        memory_id: str,
        user_id: str,
        reason: str = "user_request"
    ):
        """
        Log memory deletion for compliance.
        
        Args:
            memory_id: Memory identifier
            user_id: User identifier
            reason: Deletion reason
        """
        self.log_operation(
            operation="delete",
            memory_id=memory_id,
            user_id=user_id,
            metadata={"reason": reason}
        )


def export_user_data(
    user_id: str,
    client: Optional[Any] = None
) -> dict[str, Any]:
    """
    Export all user data for GDPR compliance.
    
    Args:
        user_id: User identifier
        client: MemoryClient instance
        
    Returns:
        User data export in structured format
    """
    from agentic_shopping_demo.memory.client import get_memory_client
    
    if client is None:
        client = get_memory_client()
    
    try:
        # Get all memories
        memories = client.get_all(user_id=user_id)
        
        # Format for export
        export_data = {
            "user_id": user_id,
            "export_timestamp": datetime.now().isoformat(),
            "memory_count": len(memories),
            "memories": [
                {
                    "id": m.id,
                    "content": m.content,
                    "type": m.memory_type.value,
                    "created_at": m.created_at.isoformat(),
                    "updated_at": m.updated_at.isoformat(),
                    "expires_at": m.expires_at.isoformat() if m.expires_at else None,
                    "metadata": m.metadata
                }
                for m in memories
            ]
        }
        
        # Log export request
        audit_logger = MemoryAuditLogger()
        audit_logger.log_operation(
            operation="export",
            user_id=user_id,
            metadata={"memory_count": len(memories)}
        )
        
        logger.info(
            f"[MEMORY] Exported user data: user={user_id}, "
            f"count={len(memories)}"
        )
        
        return export_data
        
    except Exception as e:
        logger.error(
            f"[MEMORY] Failed to export user data: user={user_id}, error={e}"
        )
        return {
            "user_id": user_id,
            "export_timestamp": datetime.now().isoformat(),
            "error": str(e)
        }
