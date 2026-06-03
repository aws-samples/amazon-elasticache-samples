"""
Memory system error classes and error handling.
"""

import logging
import time
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# Exception classes
class MemoryError(Exception):
    """Base exception for memory system errors."""
    pass


class MemoryConnectionError(MemoryError):
    """Error connecting to memory cluster."""
    pass


class MemoryTimeoutError(MemoryError):
    """Memory operation timed out."""
    pass


class MemoryValidationError(MemoryError):
    """Memory data validation failed."""
    pass


class MemoryNotFoundError(MemoryError):
    """Memory not found."""
    pass


class MemoryStorageError(MemoryError):
    """Error storing memory."""
    pass


# Circuit breaker states
class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreaker:
    """
    Circuit breaker for fault tolerance.
    
    Prevents cascading failures by opening circuit after
    threshold failures and allowing recovery attempts.
    """
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 3
    ):
        """
        Initialize circuit breaker.
        
        Args:
            failure_threshold: Number of failures before opening
            recovery_timeout: Seconds before attempting recovery
            half_open_max_calls: Max calls in half-open state
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.state = CircuitState.CLOSED
        self.half_open_calls = 0
        
        logger.info(
            f"[MEMORY] CircuitBreaker initialized: "
            f"threshold={failure_threshold}, timeout={recovery_timeout}s"
        )
    
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection.
        
        Args:
            func: Function to execute
            *args: Function arguments
            **kwargs: Function keyword arguments
            
        Returns:
            Function result
            
        Raises:
            MemoryError: If circuit is open
        """
        # Check circuit state
        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if self.last_failure_time:
                elapsed = time.time() - self.last_failure_time
                if elapsed >= self.recovery_timeout:
                    logger.info("[MEMORY] Circuit breaker entering half-open state")
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_calls = 0
                else:
                    raise MemoryError("Circuit breaker is open")
            else:
                raise MemoryError("Circuit breaker is open")
        
        # Half-open state: limit calls
        if self.state == CircuitState.HALF_OPEN:
            if self.half_open_calls >= self.half_open_max_calls:
                raise MemoryError("Circuit breaker half-open limit reached")
            self.half_open_calls += 1
        
        # Execute function
        try:
            result = func(*args, **kwargs)
            
            # Success: reset or close circuit
            if self.state == CircuitState.HALF_OPEN:
                logger.info("[MEMORY] Circuit breaker closing after successful recovery")
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.half_open_calls = 0
            elif self.state == CircuitState.CLOSED:
                # Reset failure count on success
                self.failure_count = 0
            
            return result
            
        except Exception as e:
            # Failure: increment count and potentially open circuit
            self.failure_count += 1
            self.last_failure_time = time.time()
            
            if self.state == CircuitState.HALF_OPEN:
                # Failed during recovery: reopen circuit
                logger.warning("[MEMORY] Circuit breaker reopening after failed recovery")
                self.state = CircuitState.OPEN
            elif self.failure_count >= self.failure_threshold:
                # Threshold reached: open circuit
                logger.error(
                    f"[MEMORY] Circuit breaker opening after {self.failure_count} failures"
                )
                self.state = CircuitState.OPEN
            
            raise
    
    def reset(self):
        """Reset circuit breaker to closed state."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.half_open_calls = 0
        self.last_failure_time = None
        logger.info("[MEMORY] Circuit breaker reset")


class RetryHandler:
    """
    Handles retries with exponential backoff.
    
    Retries transient failures with increasing delays.
    """
    
    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 0.1,
        max_delay: float = 5.0,
        exponential_base: float = 2.0
    ):
        """
        Initialize retry handler.
        
        Args:
            max_retries: Maximum number of retry attempts
            base_delay: Initial delay in seconds
            max_delay: Maximum delay in seconds
            exponential_base: Base for exponential backoff
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        
        logger.info(
            f"[MEMORY] RetryHandler initialized: "
            f"max_retries={max_retries}, base_delay={base_delay}s"
        )
    
    def retry_with_backoff(
        self,
        func: Callable,
        *args,
        **kwargs
    ) -> Any:
        """
        Execute function with exponential backoff retry.
        
        Args:
            func: Function to execute
            *args: Function arguments
            **kwargs: Function keyword arguments
            
        Returns:
            Function result
            
        Raises:
            Exception: If all retries exhausted
        """
        last_exception = None
        
        for attempt in range(self.max_retries + 1):
            try:
                return func(*args, **kwargs)
                
            except (MemoryConnectionError, MemoryTimeoutError) as e:
                # Transient errors: retry
                last_exception = e
                
                if attempt < self.max_retries:
                    # Calculate delay with exponential backoff
                    delay = min(
                        self.base_delay * (self.exponential_base ** attempt),
                        self.max_delay
                    )
                    
                    logger.warning(
                        f"[MEMORY] Retry attempt {attempt + 1}/{self.max_retries} "
                        f"after {delay:.2f}s: {e}"
                    )
                    
                    time.sleep(delay)
                else:
                    logger.error(
                        f"[MEMORY] All retries exhausted after {attempt + 1} attempts"
                    )
                    raise
                    
            except Exception as e:
                # Non-transient errors: don't retry
                logger.error(f"[MEMORY] Non-retryable error: {e}")
                raise
        
        # Should not reach here, but raise last exception if we do
        if last_exception:
            raise last_exception


class MemoryErrorHandler:
    """
    Handles memory operation errors with circuit breaker and retries.
    
    Provides safe wrappers for memory operations that handle
    failures gracefully and prevent cascading failures.
    """
    
    def __init__(
        self,
        circuit_breaker: Optional[CircuitBreaker] = None,
        retry_handler: Optional[RetryHandler] = None
    ):
        """
        Initialize error handler.
        
        Args:
            circuit_breaker: CircuitBreaker instance
            retry_handler: RetryHandler instance
        """
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.retry_handler = retry_handler or RetryHandler()
        
        logger.info("[MEMORY] MemoryErrorHandler initialized")
    
    def safe_search(
        self,
        client: Any,
        query: str,
        user_id: str,
        **kwargs
    ) -> list:
        """
        Safe memory search with error handling.
        
        Args:
            client: MemoryClient instance
            query: Search query
            user_id: User identifier
            **kwargs: Additional search parameters
            
        Returns:
            List of memories (empty on error)
        """
        try:
            # Execute with circuit breaker and retry
            def search_func():
                return client.search(query=query, user_id=user_id, **kwargs)
            
            result = self.circuit_breaker.call(
                self.retry_handler.retry_with_backoff,
                search_func
            )
            
            return result
            
        except Exception as e:
            logger.error(
                f"[MEMORY] Safe search failed: query='{query[:50]}...', "
                f"user={user_id}, error={e}"
            )
            # Return empty list on error (non-blocking)
            return []
    
    def safe_add(
        self,
        client: Any,
        messages: list,
        user_id: str,
        **kwargs
    ) -> Optional[str]:
        """
        Safe memory add with error handling.
        
        Args:
            client: MemoryClient instance
            messages: Messages to store
            user_id: User identifier
            **kwargs: Additional add parameters
            
        Returns:
            Memory ID (None on error)
        """
        try:
            # Validate inputs
            if not messages:
                raise MemoryValidationError("Messages cannot be empty")
            if not user_id:
                raise MemoryValidationError("User ID cannot be empty")
            
            # Execute with circuit breaker and retry
            def add_func():
                return client.add(messages=messages, user_id=user_id, **kwargs)
            
            result = self.circuit_breaker.call(
                self.retry_handler.retry_with_backoff,
                add_func
            )
            
            return result
            
        except MemoryValidationError as e:
            logger.error(f"[MEMORY] Validation error: {e}")
            return None
            
        except Exception as e:
            logger.error(
                f"[MEMORY] Safe add failed: user={user_id}, error={e}"
            )
            # Return None on error (non-blocking)
            return None


def get_memories_with_fallback(
    client: Any,
    query: str,
    user_id: str,
    conversation_state: Optional[dict] = None,
    **kwargs
) -> list:
    """
    Get memories with fallback to conversation state.
    
    If memory system fails, falls back to using conversation state
    as pseudo-memory.
    
    Args:
        client: MemoryClient instance
        query: Search query
        user_id: User identifier
        conversation_state: Current conversation state
        **kwargs: Additional search parameters
        
    Returns:
        List of memories or pseudo-memories from state
    """
    try:
        # Try to get memories
        memories = client.search(query=query, user_id=user_id, **kwargs)
        return memories
        
    except Exception as e:
        logger.warning(
            f"[MEMORY] Memory retrieval failed, using fallback: {e}"
        )
        
        # Fallback to conversation state
        if conversation_state:
            # Create pseudo-memory from relevant_summary
            relevant_summary = conversation_state.get("relevant_summary", "")
            if relevant_summary:
                from agentic_shopping_demo.memory.models import Memory, MemoryType
                from datetime import datetime
                
                pseudo_memory = Memory(
                    id="fallback",
                    user_id=user_id,
                    content=relevant_summary,
                    memory_type=MemoryType.SHORT_TERM,
                    metadata={"source": "conversation_state_fallback"},
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                    expires_at=None,
                    relevance_score=0.5
                )
                
                logger.info("[MEMORY] Using conversation state as fallback memory")
                return [pseudo_memory]
        
        # No fallback available
        return []
