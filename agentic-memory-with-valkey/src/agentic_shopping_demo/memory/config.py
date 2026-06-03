"""
Configuration management for memory system.

Loads configuration from environment variables and validates settings.
"""

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional


@dataclass
class MemoryConfig:
    """
    Memory system configuration.
    
    All settings can be overridden via environment variables.
    """
    
    # Cluster connection (dedicated memory cluster)
    endpoint: str
    port: int
    tls_enabled: bool
    
    # Embedding configuration
    embedding_model: str
    embedding_dim: int
    
    # Retention policies
    short_term_ttl: timedelta
    long_term_ttl: timedelta
    max_retention: timedelta
    
    # Search configuration
    search_limit: int
    search_threshold: float
    
    # Privacy configuration
    hash_salt: str
    pii_filtering: bool
    
    # Performance configuration
    operation_timeout_ms: int
    async_storage: bool
    
    # Feature flags
    enabled: bool
    short_term_enabled: bool
    long_term_enabled: bool
    
    # AWS configuration
    aws_region: str
    
    @classmethod
    def from_env(cls) -> "MemoryConfig":
        """
        Load configuration from environment variables.
        
        Returns:
            MemoryConfig instance with settings from environment
        
        Raises:
            ValueError: If required environment variables are missing
        """
        return cls(
            # Cluster connection
            endpoint=os.getenv("MEMORY_CACHE_ENDPOINT", "localhost"),
            port=int(os.getenv("MEMORY_CACHE_PORT", "6380")),
            tls_enabled=os.getenv("MEMORY_CACHE_TLS", "true").lower() == "true",
            
            # Embedding
            embedding_model=os.getenv(
                "MEMORY_EMBEDDING_MODEL",
                "amazon.titan-embed-text-v2:0"
            ),
            embedding_dim=int(os.getenv("MEMORY_EMBEDDING_DIM", "1024")),
            
            # Retention
            short_term_ttl=timedelta(
                hours=int(os.getenv("MEMORY_SHORT_TERM_TTL_HOURS", "720"))
            ),
            long_term_ttl=timedelta(
                days=int(os.getenv("MEMORY_LONG_TERM_TTL_DAYS", "90"))
            ),
            max_retention=timedelta(
                days=int(os.getenv("MEMORY_MAX_RETENTION_DAYS", "365"))
            ),
            
            # Search
            search_limit=int(os.getenv("MEMORY_SEARCH_LIMIT", "5")),
            search_threshold=float(os.getenv("MEMORY_SEARCH_THRESHOLD", "0.7")),
            
            # Privacy
            hash_salt=os.getenv("MEMORY_HASH_SALT", "default_salt"),
            pii_filtering=os.getenv("MEMORY_PII_FILTERING", "true").lower() == "true",
            
            # Performance
            operation_timeout_ms=int(os.getenv("MEMORY_OPERATION_TIMEOUT_MS", "150")),
            async_storage=os.getenv("MEMORY_ASYNC_STORAGE", "true").lower() == "true",
            
            # Feature flags
            enabled=os.getenv("MEMORY_ENABLED", "true").lower() == "true",
            short_term_enabled=os.getenv("MEMORY_SHORT_TERM_ENABLED", "true").lower() == "true",
            long_term_enabled=os.getenv("MEMORY_LONG_TERM_ENABLED", "true").lower() == "true",
            
            # AWS
            aws_region=os.getenv("AWS_REGION", "us-east-1"),
        )
    
    def validate(self) -> list[str]:
        """
        Validate configuration and return list of errors.
        
        Returns:
            List of error messages (empty if valid)
        """
        errors = []
        
        # Endpoint validation
        if not self.endpoint:
            errors.append("MEMORY_CACHE_ENDPOINT is required")
        
        # Port validation
        if self.port < 1 or self.port > 65535:
            errors.append("MEMORY_CACHE_PORT must be between 1 and 65535")
        
        # Embedding dimension validation
        if self.embedding_dim not in [256, 512, 1024]:
            errors.append("MEMORY_EMBEDDING_DIM must be 256, 512, or 1024")
        
        # Search threshold validation
        if self.search_threshold < 0 or self.search_threshold > 1:
            errors.append("MEMORY_SEARCH_THRESHOLD must be between 0 and 1")
        
        # Search limit validation
        if self.search_limit < 1:
            errors.append("MEMORY_SEARCH_LIMIT must be at least 1")
        
        # Timeout validation
        if self.operation_timeout_ms < 1:
            errors.append("MEMORY_OPERATION_TIMEOUT_MS must be at least 1")
        
        # Hash salt warning (not an error, but important)
        if self.hash_salt == "default_salt":
            errors.append("MEMORY_HASH_SALT should be set to a secure random value (security warning)")
        
        # TTL validation
        if self.short_term_ttl.total_seconds() < 1:
            errors.append("MEMORY_SHORT_TERM_TTL_HOURS must be at least 1 hour")
        
        if self.long_term_ttl.total_seconds() < 1:
            errors.append("MEMORY_LONG_TERM_TTL_DAYS must be at least 1 day")
        
        if self.max_retention.total_seconds() < self.long_term_ttl.total_seconds():
            errors.append("MEMORY_MAX_RETENTION_DAYS must be >= MEMORY_LONG_TERM_TTL_DAYS")
        
        return errors
    
    def is_valid(self) -> bool:
        """Check if configuration is valid."""
        errors = self.validate()
        # Filter out warnings (hash_salt warning)
        critical_errors = [e for e in errors if "security warning" not in e]
        return len(critical_errors) == 0
    
    def __str__(self) -> str:
        """String representation of configuration."""
        return (
            f"MemoryConfig(\n"
            f"  endpoint={self.endpoint}:{self.port} (TLS={self.tls_enabled})\n"
            f"  embedding={self.embedding_model} (dim={self.embedding_dim})\n"
            f"  retention: short={self.short_term_ttl}, long={self.long_term_ttl}, max={self.max_retention}\n"
            f"  search: limit={self.search_limit}, threshold={self.search_threshold}\n"
            f"  privacy: pii_filtering={self.pii_filtering}\n"
            f"  performance: timeout={self.operation_timeout_ms}ms, async={self.async_storage}\n"
            f"  enabled={self.enabled}, short_term={self.short_term_enabled}, long_term={self.long_term_enabled}\n"
            f")"
        )


# Singleton instance
_config: Optional[MemoryConfig] = None


def get_config() -> MemoryConfig:
    """
    Get the global memory configuration instance.
    
    Loads from environment on first call, then returns cached instance.
    
    Returns:
        MemoryConfig instance
    
    Raises:
        ValueError: If configuration is invalid
    """
    global _config
    
    if _config is None:
        _config = MemoryConfig.from_env()
        
        # Validate configuration
        errors = _config.validate()
        if errors:
            # Log errors but don't fail - use defaults
            import logging
            logger = logging.getLogger(__name__)
            for error in errors:
                if "security warning" in error:
                    logger.warning(f"[MEMORY CONFIG] {error}")
                else:
                    logger.error(f"[MEMORY CONFIG] {error}")
            
            # Only raise if there are critical errors
            critical_errors = [e for e in errors if "security warning" not in e]
            if critical_errors:
                raise ValueError(f"Invalid memory configuration: {', '.join(critical_errors)}")
    
    return _config


def reset_config():
    """Reset the global configuration instance (for testing)."""
    global _config
    _config = None
