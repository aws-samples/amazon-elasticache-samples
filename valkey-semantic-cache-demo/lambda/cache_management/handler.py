"""Cache management Lambda for semantic caching demo."""

import os
from typing import Any, cast

from glide_sync import GlideClient, GlideClientConfiguration, NodeAddress, TEncodable

# Constants (aligned with agents/cache_constants.py)
INDEX_NAME = "idx:requests"
KEY_PREFIX_VECTOR = "request:vector:"
KEY_PREFIX_RR = "rr:"
VECTOR_DIM = 1024
DISTANCE_METRIC = "COSINE"
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 200


def get_client() -> GlideClient:
    config = GlideClientConfiguration(
        addresses=[NodeAddress(host=os.environ["ELASTICACHE_ENDPOINT"], port=6379)],
        client_name="cache-management-lambda",
    )
    return GlideClient.create(config)


def handler(event: dict[str, Any], context: Any) -> dict[str, str]:
    action = event.get("action", "")
    client = get_client()

    try:
        if action == "health-check":
            client.ping()
            dbsize = client.dbsize()
            indexes = client.custom_command(cast(list[TEncodable], ["FT._LIST"]))
            return {"message": f"healthy - dbsize: {dbsize}, indexes: {indexes}"}

        elif action == "reset-cache":
            deleted = 0
            for pattern in [f"{KEY_PREFIX_VECTOR}*", f"{KEY_PREFIX_RR}*"]:
                cursor = "0"
                while True:
                    result = cast(
                        list[Any],
                        client.custom_command(
                            cast(
                                list[TEncodable],
                                ["SCAN", cursor, "MATCH", pattern, "COUNT", "100"],
                            )
                        ),
                    )
                    cursor = (
                        result[0].decode()
                        if isinstance(result[0], bytes)
                        else str(result[0])
                    )
                    keys = cast(list[TEncodable], result[1])
                    if keys:
                        deleted += client.delete(keys)
                    if cursor == "0":
                        break
            deleted += client.delete(["metrics:global"])
            
            # Verification info
            dbsize = client.dbsize()
            indexes = client.custom_command(cast(list[TEncodable], ["FT._LIST"]))
            print(f"Cache reset complete - deleted {deleted} keys")
            print(f"Verification: DB Size: {dbsize}, Index: {indexes}")
            return {"message": f"cache reset - deleted {deleted} keys, dbsize: {dbsize}"}

        elif action == "create-index":
            # fmt: off
            create_cmd = cast(list[TEncodable], [
                'FT.CREATE', INDEX_NAME,
                'ON', 'HASH',
                'PREFIX', '1', KEY_PREFIX_VECTOR,
                'SCHEMA',
                    'request_id', 'TAG',
                    'embedding', 'VECTOR', 'HNSW', '10',
                        'TYPE', 'FLOAT32',
                        'DIM', str(VECTOR_DIM),
                        'DISTANCE_METRIC', DISTANCE_METRIC,
                        'M', str(HNSW_M),
                        'EF_CONSTRUCTION', str(HNSW_EF_CONSTRUCTION),
                    'timestamp', 'NUMERIC',
            ])
            # fmt: on
            client.custom_command(create_cmd)
            return {"message": f"index {INDEX_NAME} created"}

        else:
            return {"error": f"unknown action: {action}"}

    finally:
        client.close()
