"""
Memory system metrics tracking and monitoring.
"""

import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


class MemoryMetrics:
    """
    Tracks memory system metrics for monitoring and observability.
    
    Metrics tracked:
    - Memory storage size per user
    - Memory retrieval hit rate
    - Operation latencies (p50, p95, p99)
    """
    
    def __init__(self):
        """Initialize metrics tracker."""
        self.storage_size: dict[str, int] = defaultdict(int)
        self.retrieval_hits: int = 0
        self.retrieval_misses: int = 0
        self.operation_latencies: dict[str, list[float]] = defaultdict(list)
        self.operation_counts: dict[str, int] = defaultdict(int)
        
        logger.info("[MEMORY] MemoryMetrics initialized")
    
    def record_storage(self, user_id: str, size_bytes: int):
        """
        Record memory storage size for user.
        
        Args:
            user_id: User identifier
            size_bytes: Size in bytes
        """
        self.storage_size[user_id] += size_bytes
    
    def record_retrieval_hit(self):
        """Record a cache hit."""
        self.retrieval_hits += 1
    
    def record_retrieval_miss(self):
        """Record a cache miss."""
        self.retrieval_misses += 1
    
    def get_hit_rate(self) -> float:
        """
        Calculate retrieval hit rate.
        
        Returns:
            Hit rate (0.0 to 1.0)
        """
        total = self.retrieval_hits + self.retrieval_misses
        if total == 0:
            return 0.0
        return self.retrieval_hits / total
    
    def record_operation_latency(self, operation: str, latency_ms: float):
        """
        Record operation latency.
        
        Args:
            operation: Operation name (add, search, update, delete)
            latency_ms: Latency in milliseconds
        """
        self.operation_latencies[operation].append(latency_ms)
        self.operation_counts[operation] += 1
        
        # Keep only last 1000 samples per operation
        if len(self.operation_latencies[operation]) > 1000:
            self.operation_latencies[operation] = self.operation_latencies[operation][-1000:]
    
    def get_latency_percentiles(
        self,
        operation: str
    ) -> dict[str, float]:
        """
        Calculate latency percentiles for operation.
        
        Args:
            operation: Operation name
            
        Returns:
            Dict with p50, p95, p99 latencies
        """
        latencies = self.operation_latencies.get(operation, [])
        if not latencies:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        
        sorted_latencies = sorted(latencies)
        n = len(sorted_latencies)
        
        return {
            "p50": sorted_latencies[int(n * 0.50)],
            "p95": sorted_latencies[int(n * 0.95)],
            "p99": sorted_latencies[int(n * 0.99)],
        }
    
    def get_storage_size(self, user_id: Optional[str] = None) -> int:
        """
        Get storage size for user or total.
        
        Args:
            user_id: User identifier (None for total)
            
        Returns:
            Size in bytes
        """
        if user_id:
            return self.storage_size.get(user_id, 0)
        return sum(self.storage_size.values())
    
    def get_metrics_summary(self) -> dict[str, Any]:
        """
        Get summary of all metrics.
        
        Returns:
            Metrics summary dict
        """
        return {
            "timestamp": datetime.now().isoformat(),
            "storage": {
                "total_bytes": self.get_storage_size(),
                "user_count": len(self.storage_size),
            },
            "retrieval": {
                "hits": self.retrieval_hits,
                "misses": self.retrieval_misses,
                "hit_rate": self.get_hit_rate(),
            },
            "operations": {
                op: {
                    "count": self.operation_counts[op],
                    "latency": self.get_latency_percentiles(op),
                }
                for op in self.operation_counts.keys()
            },
        }
    
    def check_storage_threshold(
        self,
        user_id: str,
        threshold_bytes: int = 10 * 1024 * 1024  # 10 MB default
    ) -> bool:
        """
        Check if user storage exceeds threshold.
        
        Args:
            user_id: User identifier
            threshold_bytes: Threshold in bytes
            
        Returns:
            True if threshold exceeded
        """
        size = self.storage_size.get(user_id, 0)
        if size > threshold_bytes:
            logger.warning(
                f"[MEMORY] Storage threshold exceeded: "
                f"user={user_id}, size={size}, threshold={threshold_bytes}"
            )
            return True
        return False


# Global metrics instance
_metrics: Optional[MemoryMetrics] = None


def get_metrics() -> MemoryMetrics:
    """
    Get global metrics instance.
    
    Returns:
        MemoryMetrics instance
    """
    global _metrics
    if _metrics is None:
        _metrics = MemoryMetrics()
    return _metrics


def reset_metrics():
    """Reset global metrics instance (for testing)."""
    global _metrics
    _metrics = None
