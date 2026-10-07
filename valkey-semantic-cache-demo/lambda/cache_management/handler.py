"""Cache management Lambda for semantic caching demo.

Dual-mode: runs as Lambda (cloud) or HTTP server (local).
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
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
    host = os.environ.get("ELASTICACHE_ENDPOINT", "localhost")
    config = GlideClientConfiguration(
        addresses=[NodeAddress(host=host, port=6379)],
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


# --- Local HTTP server mode ---

class CacheHandler(BaseHTTPRequestHandler):
    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()

    def do_POST(self):
        if self.path == "/reset":
            result = handler({"action": "reset-cache"}, None)
            self._send_json(result)
        elif self.path == "/health":
            result = handler({"action": "health-check"}, None)
            self._send_json(result)
        else:
            self._send_json({"error": "not found"}, 404)

    def log_message(self, format, *args):
        print(f"[cache-mgmt] {args[0]}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8082"))
    print(f"Cache management server running on http://localhost:{port}")
    print("Endpoints: POST /reset, POST /health")
    HTTPServer(("", port), CacheHandler).serve_forever()
