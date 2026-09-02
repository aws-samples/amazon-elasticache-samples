"""Shared pytest fixtures for all tests."""
import os
import tempfile
import shutil
from pathlib import Path
from typing import Dict, Any, Generator
from unittest.mock import Mock, MagicMock

import pytest


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Provide a temporary directory for tests."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir)


@pytest.fixture
def sample_data_dir(temp_dir: Path) -> Path:
    """Create a temporary directory with sample test data files."""
    data_dir = temp_dir / "sample_data"
    data_dir.mkdir()
    
    # Create sample files
    (data_dir / "test.txt").write_text("sample content")
    (data_dir / "config.json").write_text('{"key": "value"}')
    
    return data_dir


@pytest.fixture
def mock_env_vars() -> Generator[Dict[str, str], None, None]:
    """Provide mock environment variables and restore original ones after test."""
    original_env = os.environ.copy()
    test_env = {
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "test-key",
        "AWS_SECRET_ACCESS_KEY": "test-secret",
        "REDIS_HOST": "localhost",
        "REDIS_PORT": "6379",
    }
    os.environ.update(test_env)
    yield test_env
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture
def mock_redis_client():
    """Mock Redis client for testing."""
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.get.return_value = None
    mock_client.set.return_value = True
    mock_client.delete.return_value = 1
    mock_client.exists.return_value = False
    mock_client.keys.return_value = []
    return mock_client


@pytest.fixture
def mock_boto3_client():
    """Mock boto3 client for AWS services."""
    mock_client = MagicMock()
    mock_client.describe_cache_clusters.return_value = {
        'CacheClusters': [
            {
                'CacheClusterId': 'test-cluster',
                'CacheClusterStatus': 'available',
                'Engine': 'redis',
                'CacheNodeType': 'cache.t3.micro'
            }
        ]
    }
    return mock_client


@pytest.fixture
def sample_config() -> Dict[str, Any]:
    """Provide sample configuration data for tests."""
    return {
        "database": {
            "host": "localhost",
            "port": 5432,
            "name": "testdb",
            "user": "testuser",
            "password": "testpass"
        },
        "cache": {
            "host": "localhost",
            "port": 6379,
            "db": 0,
            "timeout": 30
        },
        "app": {
            "debug": True,
            "log_level": "DEBUG"
        }
    }


@pytest.fixture
def mock_logger():
    """Mock logger for testing."""
    return MagicMock()


@pytest.fixture(autouse=True)
def reset_modules():
    """Reset imported modules state between tests."""
    yield
    # Clean up any module-level state if needed


@pytest.fixture
def mock_aws_credentials():
    """Mock AWS credentials for testing."""
    credentials = {
        'aws_access_key_id': 'test-access-key',
        'aws_secret_access_key': 'test-secret-key',
        'region_name': 'us-east-1'
    }
    return credentials


@pytest.fixture
def mock_valkey_client():
    """Mock Valkey/Glide client for testing."""
    mock_client = MagicMock()
    mock_client.ping.return_value = b"PONG"
    mock_client.get.return_value = None
    mock_client.set.return_value = b"OK"
    mock_client.delete.return_value = 1
    mock_client.exists.return_value = 0
    return mock_client


@pytest.fixture
def sample_cache_data() -> Dict[str, Any]:
    """Provide sample cache data for tests."""
    return {
        "user:123": {"name": "John Doe", "email": "john@example.com"},
        "session:abc": {"user_id": 123, "expires": "2024-12-31T23:59:59"},
        "counter:views": "1000",
        "config:app": {"version": "1.0.0", "debug": False}
    }


@pytest.fixture
def mock_database_connection():
    """Mock database connection for testing."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = None
    mock_cursor.fetchall.return_value = []
    mock_cursor.execute.return_value = None
    return mock_conn


@pytest.fixture(scope="session")
def docker_compose_file():
    """Path to docker-compose file for integration tests."""
    return Path(__file__).parent.parent / "docker-compose.test.yml"


# Markers for test categorization
pytestmark = [
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
    pytest.mark.filterwarnings("ignore::UserWarning"),
]