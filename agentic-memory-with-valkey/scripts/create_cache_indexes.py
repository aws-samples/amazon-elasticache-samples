"""
Create ShopNow semantic cache vector indexes in ElastiCache (Valkey 8.2).

Run this once before starting the demo app:
    SHOPNOW_CACHE_ENDPOINT=localhost SHOPNOW_CACHE_PORT=6379 \
    hatch run python3 scripts/create_cache_indexes.py

Requires the SSM tunnel to be open first.
"""

import os
import struct
import sys

from glide_sync import (
    GlideClient,
    GlideClientConfiguration,
    NodeAddress,
    TlsAdvancedConfiguration,
    AdvancedGlideClientConfiguration,
)

ENDPOINT = os.environ.get("SHOPNOW_CACHE_ENDPOINT", "localhost")
PORT = int(os.environ.get("SHOPNOW_CACHE_PORT", "6379"))
VECTOR_DIM = 1024

INDEXES = [
    {"name": "idx:shopnow:subagent:hot",  "prefix": "shopnow:subagent:hot:vec:",  "desc": "KB cache — hot",                   "full": False},
    {"name": "idx:shopnow:subagent:temp", "prefix": "shopnow:subagent:temp:vec:", "desc": "KB cache — temp (flushable)",       "full": False},
    {"name": "idx:shopnow:full:hot",      "prefix": "shopnow:full:hot:vec:",      "desc": "Full response cache — hot",         "full": True},
    {"name": "idx:shopnow:full:temp",     "prefix": "shopnow:full:temp:vec:",     "desc": "Full response cache — temp (flushable)", "full": True},
]


def get_client() -> GlideClient:
    tls_config = TlsAdvancedConfiguration(use_insecure_tls=True)
    advanced = AdvancedGlideClientConfiguration(tls_config=tls_config)
    config = GlideClientConfiguration(
        addresses=[NodeAddress(host=ENDPOINT, port=PORT)],
        client_name="shopnow-index-creator",
        use_tls=True,
        advanced_config=advanced,
    )
    return GlideClient.create(config)


def create_index(client: GlideClient, name: str, prefix: str, full: bool = False) -> None:
    """Create HNSW vector index using FT.CREATE."""
    try:
        schema = [
            "request_id", "TAG",
            "embedding", "VECTOR", "HNSW", "6",
                "TYPE", "FLOAT32",
                "DIM", str(VECTOR_DIM),
                "DISTANCE_METRIC", "COSINE",
        ]
        if full:
            schema += [
                # Comma-separated namespaced state tokens — TAG for exact/prefix filtering
                "state_tags", "TAG", "SEPARATOR", ",",
                # Numeric fields for range queries
                "slot_budget_usd",    "NUMERIC",
                "slot_radius_miles",  "NUMERIC",
            ]
        result = client.custom_command([
            "FT.CREATE", name,
            "ON", "HASH",
            "PREFIX", "1", prefix,
            "SCHEMA",
            *schema,
        ])
        print(f"  ✅ Created: {name} ({result})")
    except Exception as e:
        if "already exists" in str(e).lower() or "index already exists" in str(e).lower():
            print(f"  ✓  Already exists: {name}")
        else:
            print(f"  ❌ Failed to create {name}: {e}")
            raise


def list_indexes(client: GlideClient) -> None:
    """List all FT indexes."""
    try:
        result = client.custom_command(["FT._LIST"])
        indexes = [r.decode() if isinstance(r, bytes) else r for r in (result or [])]
        print(f"  Indexes in cluster: {indexes}")
    except Exception as e:
        print(f"  Could not list indexes: {e}")


def main():
    print(f"Connecting to {ENDPOINT}:{PORT}...")
    try:
        client = get_client()
        print("Connected!\n")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        print("\nMake sure the SSM tunnel is running:")
        print("  Make sure the SSM tunnel is running. See INITIAL_SETUP.md for the exact command.")
        print(f"  Expected: SHOPNOW_CACHE_ENDPOINT={ENDPOINT} SHOPNOW_CACHE_PORT={PORT}")
        sys.exit(1)

    print("Creating vector indexes...")
    for idx in INDEXES:
        print(f"\n  [{idx['desc']}]")
        create_index(client, idx["name"], idx["prefix"], full=idx.get("full", False))

    print("\nVerifying indexes:")
    list_indexes(client)

    # Check DB size
    try:
        size = client.dbsize()
        print(f"  DB size: {size} keys")
    except Exception as e:
        print(f"  Could not get DB size: {e}")

    print("\n✅ Done. Indexes are ready.")


if __name__ == "__main__":
    main()
