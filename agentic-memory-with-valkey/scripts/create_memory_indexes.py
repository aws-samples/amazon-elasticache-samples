#!/usr/bin/env python3
"""
create_memory_indexes.py — Creates HNSW vector indexes for agent memory storage.

This script creates the necessary indexes on the dedicated memory ElastiCache/Valkey cluster.
Separate from the semantic cache indexes.

Usage:
    MEMORY_CACHE_ENDPOINT=localhost MEMORY_CACHE_PORT=6380 python3 scripts/create_memory_indexes.py
"""

import os
import sys
import redis

def create_memory_indexes():
    """Create HNSW vector indexes for memory storage."""
    
    # Get connection details from environment
    endpoint = os.getenv("MEMORY_CACHE_ENDPOINT", "localhost")
    port = int(os.getenv("MEMORY_CACHE_PORT", "6380"))
    
    print(f"Connecting to memory cluster: {endpoint}:{port}")
    
    try:
        # Connect to Redis/Valkey (with TLS for ElastiCache)
        client = redis.Redis(
            host=endpoint,
            port=port,
            decode_responses=True,
            socket_connect_timeout=5,
            ssl=True,
            ssl_cert_reqs=None  # Don't verify certificates for SSM tunnel
        )
        
        # Test connection
        client.ping()
        print("✅ Connected to memory cluster")
        
    except Exception as e:
        print(f"❌ Failed to connect to memory cluster: {e}")
        print("Make sure the SSM tunnel is running and the endpoint/port are correct.")
        sys.exit(1)
    
    # Define memory index
    index_name = "idx:shopnow:memory"
    key_prefix = "shopnow:memory:vec:"
    
    print(f"\nCreating memory index: {index_name}")
    
    # Check if index already exists
    try:
        info = client.execute_command("FT.INFO", index_name)
        print(f"⚠️  Index {index_name} already exists, dropping it first...")
        client.execute_command("FT.DROPINDEX", index_name)
    except redis.ResponseError as e:
        if "not found" not in str(e).lower():
            raise
    
    # Create HNSW vector index for memory
    # Vector dimension: 1024 (Titan Embed Text v2)
    # Distance metric: COSINE
    try:
        client.execute_command(
            "FT.CREATE", index_name,
            "ON", "HASH",
            "PREFIX", "1", key_prefix,
            "SCHEMA",
            "memory_id", "TAG",
            "user_id", "TAG",
            "memory_type", "TAG",
            "domain", "TAG",
            "created_at", "NUMERIC",
            "expires_at", "NUMERIC",
            "confidence", "NUMERIC",
            "embedding", "VECTOR", "HNSW", "10",
                "TYPE", "FLOAT32",
                "DIM", "1024",
                "DISTANCE_METRIC", "COSINE",
                "INITIAL_CAP", "1000",
                "M", "16"
        )
        print(f"✅ Created index: {index_name}")
        
        # Verify index creation
        info = client.execute_command("FT.INFO", index_name)
        print(f"   Index info: {len(info)} fields")
        
    except Exception as e:
        print(f"❌ Failed to create index {index_name}: {e}")
        sys.exit(1)
    
    print("\n✅ All memory indexes created successfully")
    print("\nIndex summary:")
    print(f"  - {index_name}: HNSW vector index for agent memory (1024-dim, COSINE)")
    print(f"    Key prefix: {key_prefix}")
    print(f"    Fields: embedding (vector), memory_id, user_id, memory_type, domain, created_at, expires_at, confidence")

if __name__ == "__main__":
    create_memory_indexes()
