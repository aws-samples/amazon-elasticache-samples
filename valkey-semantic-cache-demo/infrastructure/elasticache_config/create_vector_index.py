#!/usr/bin/env python3
"""
Create HNSW vector index for semantic caching in ElastiCache (Valkey).

This script creates the vector search index used for semantic similarity
matching of customer support queries. It's idempotent - safe to run multiple times.

Usage:
    cd agents && uv run python ../infrastructure/elasticache_config/create_vector_index.py

Environment Variables:
    ELASTICACHE_ENDPOINT: Cluster endpoint (default: localhost)
    ELASTICACHE_PORT: Cluster port (default: 6379)
"""

import os
import sys
from typing import cast

# Add agents directory to path for shared constants
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../agents'))

from glide_sync import GlideClient, GlideClientConfiguration, NodeAddress, TEncodable
from cache_constants import INDEX_NAME, KEY_PREFIX_VECTOR as KEY_PREFIX, VECTOR_DIM  # type: ignore[import-not-found]
DISTANCE_METRIC = "COSINE"  # Best for normalized embeddings (semantic similarity)

# HNSW algorithm parameters
HNSW_M = 16  # Number of connections per node (Good recall, moderate memory overhead)
HNSW_EF_CONSTRUCTION = 200  # Build-time search depth (high-quality index, affects build time, not runtime memory)


def get_connection_config() -> GlideClientConfiguration:
    """Build connection configuration from environment variables."""
    host = os.environ.get("ELASTICACHE_ENDPOINT", "localhost")
    port = int(os.environ.get("ELASTICACHE_PORT", "6379"))

    return GlideClientConfiguration(
        addresses=[NodeAddress(host=host, port=port)],
        client_name="vector-index-creator",
    )


def index_exists(client: GlideClient, index_name: str) -> bool:
    """Check if a vector index already exists."""
    try:
        cmd = cast(list[TEncodable], ["FT.INFO", index_name])
        result = client.custom_command(cmd)
        return result is not None
    except Exception as e:
        error_msg = str(e).lower()
        if (
            "Unknown index name" in error_msg
            or "unknown command" in error_msg
            or "not found" in error_msg
        ):
            return False
        raise


def drop_index(client: GlideClient, index_name: str, delete_docs: bool = False) -> bool:
    """Drop an existing index.

    Args:
        client: Valkey client
        index_name: Name of index to drop
        delete_docs: If True, also delete the indexed documents (DD flag)

    Returns:
        True if index was dropped, False if it didn't exist
    """
    try:
        cmd = cast(list[TEncodable], ["FT.DROPINDEX", index_name])
        if delete_docs:
            cmd.append("DD")
        client.custom_command(cmd)
        print(f"✓ Dropped existing index: {index_name}")
        return True
    except Exception as e:
        if "Unknown index name" in str(e):
            return False
        raise


def create_vector_index(client: GlideClient) -> None:
    """Create the HNSW vector index for semantic search.

    Index Schema:
        - request_id (TAG): Unique identifier for the request
        - embedding (VECTOR HNSW): Vector for similarity search (VECTOR_DIM from cache_constants)
        - timestamp (NUMERIC): Unix timestamp for TTL/ordering

    The index is created on HASH keys with prefix 'request:vector:'
    """
    # Build FT.CREATE command
    # fmt: off
    create_cmd = cast(list[TEncodable], [
        "FT.CREATE", INDEX_NAME,
        "ON", "HASH",
        "PREFIX", "1", KEY_PREFIX,
        "SCHEMA",
            "request_id", "TAG",
            "embedding", "VECTOR", "HNSW", "10", # 10 stands for the number of arguments: two for each below = 10
                "TYPE", "FLOAT32",
                "DIM", str(VECTOR_DIM),
                "DISTANCE_METRIC", DISTANCE_METRIC,
                "M", str(HNSW_M),
                "EF_CONSTRUCTION", str(HNSW_EF_CONSTRUCTION),
            "timestamp", "NUMERIC",
    ])
    # fmt: on

    client.custom_command(create_cmd)
    print(f"✓ Created vector index: {INDEX_NAME}")
    print(f"  - Dimensions: {VECTOR_DIM}")
    print(f"  - Distance metric: {DISTANCE_METRIC}")
    print(f"  - HNSW M: {HNSW_M}")
    print(f"  - HNSW EF_CONSTRUCTION: {HNSW_EF_CONSTRUCTION}")
    print(f"  - Key prefix: {KEY_PREFIX}")


def verify_index(client: GlideClient, index_name: str) -> None:
    """Verify index was created correctly by fetching its info."""
    client.custom_command(["FT.INFO", index_name])


def main() -> int:
    """Main entry point for index creation."""
    print("=" * 60)
    print("Semantic Cache Demo - Vector Index Setup")
    print("=" * 60)

    # Get configuration
    config = get_connection_config()
    print(f"\nConnecting to: {config.addresses[0].host}:{config.addresses[0].port}")

    try:
        # Connect to Valkey
        client = GlideClient.create(config)
        print("✓ Connected to ElastiCache cluster")

        # Check for existing index
        if index_exists(client, INDEX_NAME):
            print(f"\n⚠ Index '{INDEX_NAME}' already exists")
            response = input("Drop and recreate? [y/N]: ").strip().lower()
            if response == "y":
                drop_index(client, INDEX_NAME, delete_docs=False)
            else:
                print("Skipping index creation (existing index preserved)")
                return 0

        # Create the index
        print(f"\nCreating index '{INDEX_NAME}'...")
        create_vector_index(client)

        # Verify
        print("\nVerifying index...")
        verify_index(client, INDEX_NAME)
        print(f"✓ Index verified: {INDEX_NAME}")

        print("\n" + "=" * 60)
        print("Vector index setup complete!")
        print("=" * 60)
        return 0

    except Exception as e:
        print(f"\n✗ Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
