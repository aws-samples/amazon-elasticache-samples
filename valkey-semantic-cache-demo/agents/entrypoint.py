import logging
import os
import time
import uuid
import json
import struct
from typing import cast
import boto3
from mypy_boto3_bedrock_runtime import BedrockRuntimeClient
from bedrock_agentcore import BedrockAgentCoreApp
from glide_sync import (
    FtSearchLimit,
    FtSearchOptions,
    GlideClient,
    GlideClientConfiguration,
    NodeAddress,
    ReturnField,
)
from glide_sync.sync_commands import ft
from cache_constants import (
    INDEX_NAME,
    KEY_PREFIX_REQUEST_RESPONSE,
    KEY_PREFIX_VECTOR,
    VECTOR_DIM,
    CLAUDE_SONNET_4_INPUT_COST,
    CLAUDE_SONNET_4_OUTPUT_COST,
)
from support_agent import invoke_agent

app = BedrockAgentCoreApp()

# Configure root logger to capture all module logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ELASTICACHE_ENDPOINT = os.environ.get("ELASTICACHE_ENDPOINT", "localhost")
ELASTICACHE_PORT = int(os.environ.get("ELASTICACHE_PORT", "6379"))
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.80"))
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")

# This is the serverless runtime for running Titan Embeddings only - not to
# confuse with Bedrock AgentCore runtime
bedrock_runtime = cast(
    BedrockRuntimeClient, boto3.client("bedrock-runtime", region_name=AWS_REGION)
)

cloudwatch = boto3.client("cloudwatch", region_name=AWS_REGION)
CLOUDWATCH_NAMESPACE = os.environ.get("CLOUDWATCH_NAMESPACE", "SemanticSupportDesk")

# Lazy-load cache client to prevent startup failures if ElastiCache is unreachable
_cache_client = None


def get_cache_client():
    """Get or create the cache client connection."""
    global _cache_client
    if _cache_client is None:
        config = GlideClientConfiguration(
            addresses=[NodeAddress(host=ELASTICACHE_ENDPOINT, port=ELASTICACHE_PORT)],
            client_name="semantic-cache-entrypoint",
        )
        _cache_client = GlideClient.create(config)
    return _cache_client


def estimate_tokens(text: str) -> int:
    """Estimate token count using ~4 characters per token heuristic."""
    return len(text) // 4


def generate_embedding(text: str) -> list[float]:
    """Generate Titan embeddings for the passed in text parameter."""
    response = bedrock_runtime.invoke_model(
        modelId=EMBEDDING_MODEL,
        body=json.dumps({"inputText": text, "dimensions": VECTOR_DIM}),
    )
    return json.loads(response["body"].read())["embedding"]


def search_cache(embedding: list[float], k: int = 1) -> tuple[str | None, float]:
    """Search vector index for similar cached requests. Returns (request_id, store)."""

    # Convert embedding to bytes (float32 binary format)
    embedding_bytes = struct.pack(f"{len(embedding)}f", *embedding)

    # KNN (K-nearest neighbors) query with vector parameter
    query = f"*=>[KNN {k} @embedding $vec AS score]"

    options = FtSearchOptions(
        params={"vec": embedding_bytes},
        return_fields=[
            ReturnField(field_identifier="request_id"),
            ReturnField(field_identifier="score"),
        ],
        limit=FtSearchLimit(offset=0, count=1),
    )
    result = ft.search(get_cache_client(), INDEX_NAME, query, options)

    # since result is [count, {doc_key: {field: value}}]
    if (
        isinstance(result, list)
        and len(result) > 1
        and isinstance(result[0], int)
        and result[0] > 0
    ):
        # get the first result only
        doc_data = list(result[1].values())[0]  # type: ignore[union-attr]
        request_id = doc_data[b"request_id"].decode()  # type: ignore[union-attr]
        distance = float(doc_data[b"score"])
        similarity = 1.0 - distance  # Convert distance to similarity
        return request_id, similarity

    return None, 0.0


def get_cached_response(request_id: str) -> dict | None:
    """Retrieve cached response by request_id"""
    key = f"{KEY_PREFIX_REQUEST_RESPONSE}{request_id}"
    result = get_cache_client().hgetall(key)
    if result:
        return {k.decode(): v.decode() for k, v in result.items()}
    return None


def cache_response(request_text: str, response_text: str, embedding: list[float], 
                   input_tokens: int = 0, output_tokens: int = 0):
    f"""
    Store request-response pair with embedding and token usage.

    This stores both the embeding in {INDEX_NAME}
    and the request-response pair with cost metadata.
    """
    request_id = str(uuid.uuid4())
    vector_key = f"{KEY_PREFIX_VECTOR}{request_id}"
    rr_key = f"{KEY_PREFIX_REQUEST_RESPONSE}{request_id}"

    client = get_cache_client()

    # Convert embedding to binary format (float32)
    embedding_bytes = struct.pack(f"{len(embedding)}f", *embedding)

    client.hset(
        vector_key,
        {
            "request_id": request_id,
            "embedding": embedding_bytes,
            "timestamp": str(time.time()),
        },
    )
    
    # Calculate cost in dollars
    cost = (input_tokens * CLAUDE_SONNET_4_INPUT_COST / 1_000_000 + 
            output_tokens * CLAUDE_SONNET_4_OUTPUT_COST / 1_000_000)

    client.hset(
        rr_key,
        {
            "request_text": request_text,
            "response_text": response_text,
            "tokens_input": str(input_tokens),
            "tokens_output": str(output_tokens),
            "cost_dollars": str(cost),
            "created_at": str(time.time()),
        },
    )


def emit_metrics(cached: bool, latency_ms: float, similarity: float, 
                 cost_avoided: float = 0.0, cost_paid: float = 0.0):
    """
    Emit metrics synchronously to CloudWatch.
    
    Args:
        cached: Whether response was served from cache (True) or agent (False)
        latency_ms: Request processing time in milliseconds
        similarity: Semantic similarity score (0.0-1.0) from vector search
        cost_avoided: Estimated Bedrock cost savings in dollars (cache hits only)
        cost_paid: Actual Bedrock cost incurred in dollars (cache misses only)
    """
    from datetime import datetime, timezone
    timestamp = datetime.now(timezone.utc)
    cache_status = "Hit" if cached else "Miss"
    
    metric_data = [
        {
            'MetricName': 'Latency',
            'Value': latency_ms,
            'Unit': 'Milliseconds',
            'Timestamp': timestamp,
            'Dimensions': [{'Name': 'CacheStatus', 'Value': cache_status}]
        },
        {
            'MetricName': 'CacheHit',
            'Value': 1.0 if cached else 0.0,
            'Unit': 'Count',
            'Timestamp': timestamp
        },
        {
            'MetricName': 'SimilarityScore',
            'Value': similarity,
            'Unit': 'None',
            'Timestamp': timestamp
        }
    ]
    
    if cached and cost_avoided > 0:
        metric_data.append({
            'MetricName': 'CostSavings',
            'Value': cost_avoided,
            'Unit': 'None',
            'Timestamp': timestamp
        })
    
    if not cached and cost_paid > 0:
        metric_data.append({
            'MetricName': 'CostPaid',
            'Value': cost_paid,
            'Unit': 'None',
            'Timestamp': timestamp
        })
    
    try:
        cloudwatch.put_metric_data(Namespace=CLOUDWATCH_NAMESPACE, MetricData=metric_data)
    except Exception as e:
        logger.error(f"[METRICS] Failed to publish: {e}")


@app.entrypoint
def invoke(request):
    """
    Main entrypoint for retail support desk system.

    Orchestrates all incoming customer support requests, leveraging semantic
    caching via Titan embeddings and ElastiCache to optimize response times
    and reduce LLM consts during traffic spikes.

    Flow:
    1. Generate 1024-dimensional embedding for incoming request using Titan
    2. Query ElastiCache (Valkey) vector index with HNSW algorithm
    3. On cache hit (0.85 similarity):
        - Return cached response immediately
        - Emit metrics to CloudWatch: latency, cost savings, match score
    4. On cache miss:
        - Forward request to Support Agent via framework
        - Cache response with embedding for future queries
        - Emit metrics to CloudWatch: latency, tokens consumed, cost

    Args:
        request: Incoming customer support request containing request_text

    Returns:
        str: Response text (cached or from agent)
    """
    logger.info(f"[ENTRYPOINT] Received request: {request}")
    start_time = time.time()
    request_text = request.get("request_text", "")
    logger.info(f"[ENTRYPOINT] Processing: {request_text[:100]}...")

    embedding = generate_embedding(request_text)
    logger.info(f"[ENTRYPOINT] Generated {len(embedding)}-dim embedding")
    cache_request_id, similarity = search_cache(embedding)
    logger.info(
        f"[ENTRYPOINT] Cache search: request_id={cache_request_id}, similarity={similarity:.4f}"
    )

    if cache_request_id and similarity >= SIMILARITY_THRESHOLD:
        cached = get_cached_response(cache_request_id)
        if cached:
            latency = (time.time() - start_time) * 1000
            
            # Estimate input tokens for current request, use cached output tokens
            input_tokens = estimate_tokens(request_text)
            output_tokens = int(cached.get("tokens_output", 0))
            cost_avoided = (input_tokens * CLAUDE_SONNET_4_INPUT_COST / 1_000_000 + 
                          output_tokens * CLAUDE_SONNET_4_OUTPUT_COST / 1_000_000)
            
            emit_metrics(cached=True, latency_ms=latency, similarity=similarity, cost_avoided=cost_avoided)
            
            logger.info(
                f"[CACHE HIT] similarity={similarity:.3f}, latency={latency:.0f}ms, "
                f"cost_avoided=${cost_avoided:.6f}, request_id={cache_request_id}"
            )
            result = {
                "response": {
                    "response": cached["response_text"],
                    "cached": True,
                    "similarity": round(similarity, 4),
                    "latency_ms": round(latency, 1),
                }
            }
            logger.info(f"[ENTRYPOINT] Returning: {result}")
            return result

    logger.info(f"[CACHE MISS] similarity={similarity:.3f}, forwarding to SupportAgent")
    response_text, input_tokens, output_tokens = invoke_agent(request_text)
    logger.info(f"[SUPPORT AGENT] Response: {response_text[:100]}")

    cache_response(request_text, response_text, embedding, input_tokens, output_tokens)
    latency = (time.time() - start_time) * 1000
    
    # Calculate actual cost paid for this agent invocation
    cost_paid = (input_tokens * CLAUDE_SONNET_4_INPUT_COST / 1_000_000 + 
                 output_tokens * CLAUDE_SONNET_4_OUTPUT_COST / 1_000_000)
    
    emit_metrics(cached=False, latency_ms=latency, similarity=similarity, cost_paid=cost_paid)
    
    logger.info(f"[ENTRYPOINT] Response cached, total latency={latency:.0f}ms, cost_paid=${cost_paid:.6f}")

    result = {
        "response": {
            "response": response_text,
            "cached": False,
            "similarity": round(similarity, 4),
            "latency_ms": round(latency, 1),
        }
    }
    logger.info(f"[ENTRYPOINT] Returning: {result}")
    return result


if __name__ == "__main__":
    app.run()
