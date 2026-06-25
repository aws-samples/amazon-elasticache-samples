"""
Unit tests for MemoryClient short-term memory functionality.

Tests Requirements 2.2, 2.3, 2.4, 8.3:
- Short-term memory storage with session scoping
- 24-hour TTL for short-term memories
- Expiration filtering in search queries
"""

import pytest
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

from agentic_shopping_demo.memory import (
    MemoryClient,
    MemoryType,
    MemoryConfig,
)


@pytest.fixture
def mock_config():
    """Create a test configuration."""
    return MemoryConfig(
        endpoint="localhost",
        port=6380,
        tls_enabled=False,
        embedding_model="amazon.titan-embed-text-v2:0",
        embedding_dim=1024,
        short_term_ttl=timedelta(hours=24),
        long_term_ttl=timedelta(days=90),
        max_retention=timedelta(days=365),
        search_limit=5,
        search_threshold=0.7,
        hash_salt="test_salt",
        pii_filtering=True,
        operation_timeout_ms=150,
        async_storage=True,
        enabled=True,
        short_term_enabled=True,
        long_term_enabled=True,
        aws_region="us-east-1",
    )


@pytest.fixture
def mock_mem0_client():
    """Create a mock mem0 client."""
    mock = MagicMock()
    mock.add.return_value = {"id": "test_memory_id"}
    mock.search.return_value = []
    mock.get_all.return_value = []
    return mock


class TestShortTermMemory:
    """Test short-term memory with session scoping and expiration."""
    
    def test_add_short_term_memory_with_session_scoping(
        self, mock_config, mock_mem0_client
    ):
        """Test that short-term memories are scoped to session_id."""
        client = MemoryClient(config=mock_config, test_mode=True)
        client._mem0_client = mock_mem0_client
        client._initialized = True
        
        # Store short-term memory with session scoping
        session_id = "session_123"
        conversation_state = {
            "active_domain": "commerce",
            "active_task": "product_search",
            "entities": {"category": "shoes"},
            "slots": {"size": 10},
        }
        
        memory_id = client.add_short_term_memory(
            messages=[
                {"role": "user", "content": "I need shoes"},
                {"role": "assistant", "content": "Here are some options"}
            ],
            session_id=session_id,
            conversation_state=conversation_state
        )
        
        # Verify mem0 add was called with correct parameters
        assert mock_mem0_client.add.called
        call_args = mock_mem0_client.add.call_args
        
        # Check user_id includes session_id (with test namespace)
        assert session_id in call_args.kwargs["user_id"]
        
        # Check metadata includes conversation state
        metadata = call_args.kwargs["metadata"]
        assert metadata["domain"] == "commerce"
        assert metadata["active_task"] == "product_search"
        assert metadata["entities"] == {"category": "shoes"}
        assert metadata["slots"] == {"size": 10}
        assert metadata["memory_type"] == MemoryType.SHORT_TERM.value
    
    def test_short_term_memory_24_hour_ttl(self, mock_config, mock_mem0_client):
        """Test that short-term memories have 24-hour TTL."""
        client = MemoryClient(config=mock_config, test_mode=True)
        client._mem0_client = mock_mem0_client
        client._initialized = True
        
        # Store short-term memory
        before_add = datetime.now()
        memory_id = client.add(
            messages=[{"role": "user", "content": "test"}],
            user_id="session_123",
            memory_type=MemoryType.SHORT_TERM
        )
        after_add = datetime.now()
        
        # Verify expires_at is set to ~24 hours from now
        call_args = mock_mem0_client.add.call_args
        metadata = call_args.kwargs["metadata"]
        
        expires_at = datetime.fromtimestamp(metadata["expires_at"])
        expected_expiration = before_add + timedelta(hours=24)
        
        # Allow 1 minute tolerance for test execution time
        assert abs((expires_at - expected_expiration).total_seconds()) < 60
    
    def test_search_filters_expired_memories(self, mock_config, mock_mem0_client):
        """Test that search automatically filters out expired memories."""
        client = MemoryClient(config=mock_config, test_mode=True)
        client._mem0_client = mock_mem0_client
        client._initialized = True
        
        # Perform search
        client.search(
            query="test query",
            user_id="session_123",
            limit=5
        )
        
        # Verify search was called with expiration filter
        assert mock_mem0_client.search.called
        call_args = mock_mem0_client.search.call_args
        filters = call_args.kwargs.get("filters", {})
        
        # Check that expires_at filter is present
        assert "expires_at" in filters
        assert "$gt" in filters["expires_at"]
        
        # Verify the timestamp is approximately now
        now = datetime.now().timestamp()
        filter_timestamp = filters["expires_at"]["$gt"]
        assert abs(filter_timestamp - now) < 2  # Within 2 seconds
    
    def test_search_with_filters_still_applies_expiration(
        self, mock_config, mock_mem0_client
    ):
        """Test that expiration filter is applied even when other filters exist."""
        client = MemoryClient(config=mock_config, test_mode=True)
        client._mem0_client = mock_mem0_client
        client._initialized = True
        
        # Perform search with custom filters
        client.search(
            query="test query",
            user_id="session_123",
            filters={
                "memory_type": ["short_term"],
                "domain": "commerce"
            }
        )
        
        # Verify all filters are present including expiration
        call_args = mock_mem0_client.search.call_args
        filters = call_args.kwargs.get("filters", {})
        
        assert "memory_type" in filters
        assert "domain" in filters
        assert "expires_at" in filters
        assert "$gt" in filters["expires_at"]
    
    def test_expire_session_memories(self, mock_config, mock_mem0_client):
        """Test that session memories can be explicitly expired."""
        client = MemoryClient(config=mock_config, test_mode=True)
        client._mem0_client = mock_mem0_client
        client._initialized = True
        
        # Mock get_all to return some short-term memories
        mock_mem0_client.get_all.return_value = [
            {
                "id": "mem1",
                "metadata": {"memory_type": "short_term"}
            },
            {
                "id": "mem2",
                "metadata": {"memory_type": "short_term"}
            },
            {
                "id": "mem3",
                "metadata": {"memory_type": "long_term"}
            }
        ]
        
        # Mock update method
        client.update = Mock(return_value=True)
        
        # Expire session memories
        count = client.expire_session_memories("session_123")
        
        # Verify only short-term memories were expired
        assert count == 2
        assert client.update.call_count == 2
        
        # Verify update was called with expires_at set to now
        for call in client.update.call_args_list:
            memory_id, data = call[0]
            assert "expires_at" in data
            # Verify timestamp is approximately now
            now = datetime.now().timestamp()
            assert abs(data["expires_at"] - now) < 2
    
    def test_session_scoping_isolation(self, mock_config, mock_mem0_client):
        """Test that memories from different sessions are isolated."""
        client = MemoryClient(config=mock_config, test_mode=True)
        client._mem0_client = mock_mem0_client
        client._initialized = True
        
        # Store memory for session 1
        client.add_short_term_memory(
            messages=[{"role": "user", "content": "session 1 message"}],
            session_id="session_1"
        )
        
        # Store memory for session 2
        client.add_short_term_memory(
            messages=[{"role": "user", "content": "session 2 message"}],
            session_id="session_2"
        )
        
        # Verify different user_ids were used
        calls = mock_mem0_client.add.call_args_list
        assert len(calls) == 2
        
        user_id_1 = calls[0].kwargs["user_id"]
        user_id_2 = calls[1].kwargs["user_id"]
        
        assert "session_1" in user_id_1
        assert "session_2" in user_id_2
        assert user_id_1 != user_id_2


class TestExpirationFiltering:
    """Test expiration filtering behavior."""
    
    def test_expired_memories_not_returned(self, mock_config, mock_mem0_client):
        """Test that expired memories are filtered from search results."""
        client = MemoryClient(config=mock_config, test_mode=True)
        client._mem0_client = mock_mem0_client
        client._initialized = True
        
        # Mock search to return memories with different expiration times
        now = datetime.now()
        mock_mem0_client.search.return_value = [
            {
                "id": "mem1",
                "memory": "Valid memory",
                "score": 0.9,
                "metadata": {
                    "memory_type": "short_term",
                    "created_at": (now - timedelta(hours=1)).timestamp(),
                    "updated_at": (now - timedelta(hours=1)).timestamp(),
                    "expires_at": (now + timedelta(hours=23)).timestamp(),  # Not expired
                }
            },
            {
                "id": "mem2",
                "memory": "Expired memory",
                "score": 0.8,
                "metadata": {
                    "memory_type": "short_term",
                    "created_at": (now - timedelta(hours=25)).timestamp(),
                    "updated_at": (now - timedelta(hours=25)).timestamp(),
                    "expires_at": (now - timedelta(hours=1)).timestamp(),  # Expired
                }
            }
        ]
        
        # Search should filter at the database level, but let's verify
        # the filter is being applied
        results = client.search(
            query="test",
            user_id="session_123"
        )
        
        # Verify expiration filter was applied in the query
        call_args = mock_mem0_client.search.call_args
        filters = call_args.kwargs.get("filters", {})
        assert "expires_at" in filters
        assert "$gt" in filters["expires_at"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
