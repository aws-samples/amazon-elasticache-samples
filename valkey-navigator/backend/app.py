from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
import time
import re
import shlex
import json
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import os
import threading
from datetime import timezone
from influxdb_writer import write_valkey_metrics, InfluxConfig
from influxdb_query import query_metric

from valkey_client import ValkeyClient
from metrics_collector import MetricsCollector
from config_manager import get_config, get_valkey_config, get_server_config, get_logging_config, get_app_config, get_execute_config, ValkeyConfig

import agent as agt
from chatagent import converse

# Load environment variables from .env.example first (defaults)
load_dotenv('.env.example')
# Load environment variables from .env (overrides, if it exists)
load_dotenv('.env')

# Global variables
valkey_client: Optional[ValkeyClient] = None
metrics_collector: Optional[MetricsCollector] = None

# Metrics collection scheduling
METRICS_COLLECTION_INTERVAL_SECONDS = int(os.getenv("METRICS_COLLECTION_INTERVAL_SECONDS", "15"))
_metrics_thread: Optional[threading.Thread] = None
_metrics_stop_event = threading.Event()

# ===== Connection pool
# Note: initially only one connection in pool. To be enhanced later
active_connection_indx = 0
connection_pool = []

# Simple in-memory cache for slot stats with 30-second TTL
slot_stats_cache = {}
CACHE_TTL_SECONDS = 30

# Caching for metrics endpoint
metrics_cache = {}
METRICS_CACHE_TTL_SECONDS = 8  # Cache metrics for 8 seconds


def get_cached_metrics() -> tuple[dict, bool]:
    """
    Get cached metrics if available and fresh
    Returns (metrics_data, is_from_cache)
    """
    current_time = time.time()
    
    if "all_metrics" in metrics_cache:
        cache_entry = metrics_cache["all_metrics"]
        cache_age = current_time - cache_entry["timestamp"]
        
        if cache_age < METRICS_CACHE_TTL_SECONDS:
            logger.info(f"Serving cached metrics (age: {cache_age:.1f}s)")
            return cache_entry["data"], True
    
    return {}, False

def cache_metrics(metrics_data: dict):
    """Cache metrics data with timestamp"""
    metrics_cache["all_metrics"] = {
        "data": metrics_data,
        "timestamp": time.time()
    }
    
    # Simple cleanup - remove if we have too many entries
    if len(metrics_cache) > 5:
        oldest_key = min(metrics_cache.keys(), key=lambda k: metrics_cache[k]["timestamp"])
        del metrics_cache[oldest_key]

# Load configuration
config = get_config()
valkey_config = get_valkey_config()
server_config = get_server_config()
logging_config = get_logging_config()
app_config = get_app_config()


def collect_metrics():
    """Scheduled function: iterate connection_pool, collect metrics, write to InfluxDB"""
    influxConfig = InfluxConfig(
        valkey_config.influxEndpointUrl,
        valkey_config.influxPort,
        valkey_config.influxToken,
        valkey_config.influxBucket,
        valkey_config.influxOrg
    )
#     print('l.........................................................')
#     print(valkey_config.influxEndpointUrl)
#     print(influxConfig)
#     print('l.........................................................')
    if len(connection_pool) == 0:
        # If no pool yet, try current active client/collector if available
        if valkey_client and metrics_collector:
            try:
                metrics = metrics_collector.get_all_metrics()
                write_valkey_metrics(valkey_client, metrics,
                influxConfig,
                datetime.now(timezone.utc))
            except Exception as e:
                logger.error(f"Failed to collect/write metrics for active connection: {e}")
        else:
            logger.debug("collect_metrics: no connections available yet")
        return

    # Iterate through all configured connections
    for idx, cfg in enumerate(connection_pool):
        try:
            logger.info(f"===== tracker ---- connecting to {idx}: host {cfg.host} on port {cfg.port}")
            vc = ValkeyClient(
                name=f"pool-{idx}",
                host=cfg.host,
                port=cfg.port,
                use_tls=cfg.use_tls,
                use_cluster=cfg.use_cluster
            )
            mc = MetricsCollector(vc, individual_timeout=5.0)
            try:
                metrics = mc.get_all_metrics()
                influxConfig = InfluxConfig(
                    cfg.influxEndpointUrl,
                    cfg.influxPort,
                    cfg.influxToken,
                    cfg.influxBucket,
                    cfg.influxOrg
                )
                write_valkey_metrics(vc, metrics, influxConfig, datetime.now(timezone.utc))
            finally:
                try:
                    mc.close()
                except Exception:
                    pass
                try:
                    vc.close()
                except Exception:
                    pass
        except Exception as e:
            logger.info(f"connecting to {idx}: host {cfg.host} on port {cfg.port}")
            logger.error(f"Failed to collect/write metrics for pool index {idx}: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle application startup and shutdown"""
    global valkey_client, metrics_collector, _metrics_thread
    
    # Startup

    # Start background metrics thread
    def _metrics_loop():
        logger.info(f"Metrics collector background thread started, interval={METRICS_COLLECTION_INTERVAL_SECONDS}s")
        while not _metrics_stop_event.is_set():
            try:
                collect_metrics()
            except Exception as e:
                logger.error(f"collect_metrics cycle failed: {e}")
            # Wait for interval or stop
            _metrics_stop_event.wait(METRICS_COLLECTION_INTERVAL_SECONDS)
        logger.info("Metrics collector background thread stopped")

    if _metrics_thread is None:
        _metrics_thread = threading.Thread(target=_metrics_loop, name="metrics-writer", daemon=True)
        _metrics_thread.start()

    ####### Disabled startup connection, conneciton must be done in connection Tab
    ## Change as connections now can be switched
#     try:
#         tls_status = "with TLS" if valkey_config.use_tls else "without TLS"
#         cluster_mode = "cluster mode" if valkey_config.use_cluster else "single node mode"
#         logger.info(f"Connecting to Valkey at {valkey_config.host}:{valkey_config.port} {tls_status} in {cluster_mode}")
#
#         valkey_client = ValkeyClient(
#             name = "from startup",
#             host=valkey_config.host,
#             port=valkey_config.port,
#             use_tls=valkey_config.use_tls,
#             use_cluster=valkey_config.use_cluster
#         )
#         # Initialize MetricsCollector with 5-second individual timeout
#         metrics_collector = MetricsCollector(valkey_client, individual_timeout=5.0)
#         logger.info("Successfully initialized Valkey client and metrics collector")
#     except Exception as e:
#         logger.error(f"Failed to initialize Valkey client: {str(e)}")
#         raise

    try:
        tls_status = "with TLS" if valkey_config.use_tls else "without TLS"
        cluster_mode = "cluster mode" if valkey_config.use_cluster else "single node mode"
        logger.info(f"Connecting to Valkey at {valkey_config.host}:{valkey_config.port} {tls_status} in {cluster_mode}")

        if len(connection_pool) > 0:
            current_valkey_config = connection_pool[active_connection_indx]

            valkey_client = ValkeyClient(
                name = "from startup",
                host=current_valkey_config.host,
                port=current_valkey_config.port,
                use_tls=current_valkey_config.use_tls,
                use_cluster=current_valkey_config.use_cluster
            )
            # Initialize MetricsCollector with 5-second individual timeout
            metrics_collector = MetricsCollector(valkey_client, individual_timeout=5.0)
            logger.info("Successfully initialized Valkey client and metrics collector")
        else:
            logger.info("No active connection")
    except Exception as e:
        logger.error(f"Failed to initialize Valkey client: {str(e)}")
        raise


    yield
    
    # Shutdown
    _metrics_stop_event.set()
    try:
        if _metrics_thread is not None:
            _metrics_thread.join(timeout=2)
    except Exception:
        pass
    if metrics_collector:
        metrics_collector.close()
        logger.info("MetricsCollector closed")
    if valkey_client:
        valkey_client.close()
        logger.info("Valkey client connection closed")

# Configure logging
logging.basicConfig(
    level=getattr(logging, logging_config.level),
    format=logging_config.format
)
logger = logging.getLogger(__name__)

# Command validation function
def validate_command(command: str) -> tuple[bool, str]:
    """Validate Redis command against allowlist configuration"""
    execute_config = get_execute_config()
    allowlist = execute_config.allowlist
    
    # If allowlist is disabled, allow all commands
    if not allowlist.enabled:
        return True, "Allowlist disabled - all commands allowed"
    
    # Extract the Redis command name (first word) for validation
    command_parts = command.strip().split()
    if not command_parts:
        return False, "Empty command provided"
    
    redis_command = command_parts[0].upper()
    
    # Check exact matches (compare Redis command name)
    if allowlist.mode == "exact":
        if redis_command in allowlist.commands:
            return True, f"Redis command '{redis_command}' found in exact match allowlist"
        else:
            return False, f"Redis command '{redis_command}' not found in allowlist. Allowed commands: {allowlist.commands}"
    
    # Check prefix matches
    elif allowlist.mode == "prefix":
        for prefix in allowlist.prefixes:
            if redis_command.startswith(prefix.upper()):
                return True, f"Redis command '{redis_command}' matches allowed prefix: {prefix}"
        return False, f"Redis command '{redis_command}' does not match any allowed prefixes: {allowlist.prefixes}"
    
    # Check regex matches
    elif allowlist.mode == "regex":
        for pattern in allowlist.patterns:
            try:
                if re.match(pattern, redis_command):
                    return True, f"Redis command '{redis_command}' matches allowed pattern: {pattern}"
            except re.error as e:
                logger.error(f"Invalid regex pattern '{pattern}': {e}")
        return False, f"Redis command '{redis_command}' does not match any allowed patterns: {allowlist.patterns}"
    
    # Fallback - should not reach here due to config validation
    return False, f"Invalid allowlist mode: {allowlist.mode}"

# Pydantic models for request/response
class CacheSetRequest(BaseModel):
    key: str
    value: str
    ttl: Optional[int] = None

class CacheGetResponse(BaseModel):
    key: str
    value: Optional[Any]
    found: bool
    data_type: Optional[str] = None
    encoding: Optional[str] = None
    binary: Optional[bool] = None
    original_size: Optional[int] = None
    stream_length: Optional[int] = None
    cardinality: Optional[int] = None  # For HyperLogLog
    raw_data: Optional[str] = None    # For HyperLogLog raw data
    note: Optional[str] = None        # For unsupported types

class CacheSetResponse(BaseModel):
    key: str
    success: bool
    message: str

class ClusterConnectRequest(BaseModel):
    name: str
    redisEndpoint: str
    redisPort: int
    redisTls: bool
    redisCluster: bool
    # from settings
    influxEndpointUrl: str
    influxPort: int
    influxToken: str
    influxBucket: str
    influxOrg: str
    #ssl: bool
    #type: str

class ClusterConnectResponse(BaseModel):
    status: str

class RecommendationRequest(BaseModel):
    prompt: str

class RecommendationResponse(BaseModel):
    recommendation: str

class ExecuteCommandRequest(BaseModel):
    command: str
    timeout: Optional[int] = 30  # Default 30 seconds timeout

class ExecuteCommandResponse(BaseModel):
    command: str
    success: bool
    return_code: int
    stdout: str
    stderr: str
    execution_time: float
    timestamp: str
    message: str

class SlotStats(BaseModel):
    slot_id: int
    key_count: int
    cpu_usec: int
    network_bytes_in: int
    network_bytes_out: int

class ClusterSlotStatsResponse(BaseModel):
    slots: List[SlotStats]
    total_slots: int
    start_slot: Optional[int]
    end_slot: Optional[int]
    cached: bool
    cache_timestamp: Optional[str]

class ConverseRequest(BaseModel):
    prompt: str

class ConverseResponse(BaseModel):
    response: str
    success: bool
    message: str

class APIResponse(BaseModel):
    status: str
    data: Dict[str, Any]
    timestamp: str
    message: Optional[str] = None

# Initialize FastAPI app
app = FastAPI(
    title=app_config.name,
    description=app_config.description,
    version=app_config.version,
    lifespan=lifespan
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure this for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# History metrics endpoint for InfluxDB
@app.get("/api/influx-url")
async def get_influx_url():
    """Return InfluxDB dashboard base URL built from env vars.
    Uses INFLUXDB_SCHEME (default https), INFLUXDB_ENDPOINT, INFLUXDB_PORT.
    """
    try:
        scheme = os.getenv("INFLUXDB_SCHEME", "https").strip()
        endpoint = os.getenv("INFLUXDB_ENDPOINT", "").strip()
        port = os.getenv("INFLUXDB_PORT", "").strip()
        if not endpoint or not port:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail="INFLUXDB_ENDPOINT and INFLUXDB_PORT must be set")
        # Basic sanitation
        endpoint = endpoint.replace(" ", "")
        url = f"{scheme}://{endpoint}:{port}"
        return {"url": url}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@app.get("/api/history/series")
async def get_history_series(
        metric: str,
        start: str,
        end: Optional[str] = None,
        redisEndpoint: Optional[str] = None,
        influxEndpointUrl: Optional[str] = None,
        influxPort: Optional[str] = None,
        influxToken: Optional[str] = None,
        influxBucket: Optional[str] = None,
        influxOrg: Optional[str] = None,
    ):
    """
    Query a single metric time-series from InfluxDB.
    - metric: field name as stored in Influx (e.g., server.system_cpu_percentage)
    - start/end: RFC3339 timestamps (e.g., 2025-09-16T13:00:00Z). end optional.
    - redisEndpoint: host filter (matches tag 'host' written by writer)
    """

    print(f'[[[[[[{influxEndpointUrl}]]]]]')

    influxConfig = InfluxConfig(
        influxEndpointUrl,
        influxPort,
        influxToken,
        influxBucket,
        influxOrg
    )

    try:
        if not metric or not start:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Parameters 'metric' and 'start' are required")
        points = query_metric(metric_field=metric, start=start, stop=end, redis_endpoint=redisEndpoint, config=influxConfig)
        return APIResponse(
            status="success",
            data={
                "metric": metric,
                "redisEndpoint": redisEndpoint,
                "points": points
            },
            timestamp=datetime.utcnow().isoformat()
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error querying history series: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to query history: {str(e)}")

# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        if valkey_client and valkey_client.is_connected():
            return {
                "status": "healthy",
                "valkey_connected": True,
                "timestamp": datetime.utcnow().isoformat()
            }
        else:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "unhealthy",
                    "valkey_connected": False,
                    "timestamp": datetime.utcnow().isoformat()
                }
            )
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }
        )

# Debug endpoint for connection diagnostics
@app.get("/api/debug/connection")
@app.get("/debug/connection")
async def debug_connection():
    """Debug endpoint to test Valkey connection and provide detailed diagnostics"""
    try:
        logger.info("=== CONNECTION DIAGNOSTIC START ===")
        
        diagnostics = {
            "config": {
                "host": valkey_config.host,
                "port": valkey_config.port,
                "use_tls": valkey_config.use_tls,
                "use_cluster": valkey_config.use_cluster
            },
            "client_status": {},
            "connection_tests": {},
            "errors": []
        }
        
        # Test client initialization status
        if valkey_client is None:
            diagnostics["errors"].append("ValkeyClient is not initialized")
            return APIResponse(
                status="error",
                data=diagnostics,
                timestamp=datetime.utcnow().isoformat()
            )
        
        # Test basic connection
        try:
            logger.info("Testing basic connection...")
            is_connected = valkey_client.is_connected()
            diagnostics["client_status"]["is_connected"] = is_connected
            logger.info(f"Connection test result: {is_connected}")
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            diagnostics["errors"].append(f"Connection test failed: {str(e)}")
        
        # Test PING command
        try:
            logger.info("Testing PING command...")
            import time
            start_time = time.time()
            ping_result = valkey_client._execute_with_retry(valkey_client.client.ping)
            ping_latency = (time.time() - start_time) * 1000
            diagnostics["connection_tests"]["ping"] = {
                "result": ping_result,
                "latency_ms": round(ping_latency, 2)
            }
            logger.info(f"PING test successful: {ping_result}, latency: {ping_latency:.2f}ms")
        except Exception as e:
            logger.error(f"PING test failed: {e}")
            diagnostics["errors"].append(f"PING test failed: {str(e)}")
        
        # Test INFO command
        try:
            logger.info("Testing INFO server command...")
            info_server = valkey_client.get_info("server")
            diagnostics["connection_tests"]["info_server"] = {
                "success": True,
                "field_count": len(info_server),
                "redis_version": info_server.get("redis_version", "unknown"),
                "sample_fields": list(info_server.keys())[:5] if info_server else []
            }
            logger.info(f"INFO server test successful: {len(info_server)} fields retrieved")
        except Exception as e:
            logger.error(f"INFO server test failed: {e}")
            diagnostics["errors"].append(f"INFO server test failed: {str(e)}")
        
        # Test INFO memory command
        try:
            logger.info("Testing INFO memory command...")
            info_memory = valkey_client.get_info("memory")
            diagnostics["connection_tests"]["info_memory"] = {
                "success": True,
                "field_count": len(info_memory),
                "used_memory": info_memory.get("used_memory", 0),
                "sample_fields": list(info_memory.keys())[:5] if info_memory else []
            }
            logger.info(f"INFO memory test successful: {len(info_memory)} fields retrieved")
        except Exception as e:
            logger.error(f"INFO memory test failed: {e}")
            diagnostics["errors"].append(f"INFO memory test failed: {str(e)}")
        
        # Test client info
        try:
            client_info = {
                "client_type": str(type(valkey_client.client)),
                "host": valkey_client.host,
                "port": valkey_client.port,
                "use_tls": valkey_client.use_tls,
                "use_cluster": valkey_client.use_cluster
            }
            diagnostics["client_status"].update(client_info)
            logger.info(f"Client info collected: {client_info}")
        except Exception as e:
            logger.error(f"Client info collection failed: {e}")
            diagnostics["errors"].append(f"Client info collection failed: {str(e)}")
        
        logger.info("=== CONNECTION DIAGNOSTIC END ===")
        
        # Determine overall status
        status = "success" if not diagnostics["errors"] else "error"
        
        return APIResponse(
            status=status,
            data=diagnostics,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Debug connection endpoint failed: {str(e)}")
        return APIResponse(
            status="error",
            data={"error": f"Debug endpoint failed: {str(e)}"},
            timestamp=datetime.utcnow().isoformat()
        )

# Cache operation endpoints
@app.get("/api/cache/get/{key}", response_model=CacheGetResponse)
@app.get("/cache/get/{key}", response_model=CacheGetResponse)
async def get_cache_value(key: str):
    """Get value from Valkey cache by key, supporting all Redis data types including binary data"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        # Get the key type first
        key_type = valkey_client._execute_with_retry(valkey_client.client.type, key)
        
        # Get the value using the enhanced get_value method
        value = valkey_client.get_value(key)
        
        # Handle enhanced response formats
        if isinstance(value, dict) and 'data_type' in value:
            # Legacy response format (binary, HyperLogLog, etc.)
            return CacheGetResponse(
                key=key,
                value=value.get('value'),
                found=True,
                data_type=value.get('data_type'),
                encoding=value.get('encoding'),
                binary=value.get('binary'),
                original_size=value.get('original_size'),
                stream_length=value.get('stream_length'),
                cardinality=value.get('cardinality'),
                raw_data=value.get('raw_data'),
                note=value.get('note')
            )
        elif isinstance(value, dict) and 'error' in value:
            # Error response format
            return CacheGetResponse(
                key=key,
                value=value,
                found=False,
                data_type=value.get('type', key_type),
                note=f"Error: {value.get('error')}"
            )
        elif isinstance(value, dict) and 'type' in value:
            # New consistent response format for standard data types
            return CacheGetResponse(
                key=key,
                value=value.get('data'),
                found=True,
                data_type=value.get('type'),
                encoding="utf-8",
                binary=False
            )
        else:
            # Regular text data response (fallback for edge cases)
            return CacheGetResponse(
                key=key,
                value=value,
                found=value is not None,
                data_type=key_type if key_type != 'none' else None,
                encoding="utf-8" if value is not None else None,
                binary=False if value is not None else None
            )
        
    except Exception as e:
        logger.error(f"Error getting cache value for key '{key}': {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get cache value: {str(e)}"
        )

@app.post("/api/cache/set", response_model=CacheSetResponse)
@app.post("/cache/set", response_model=CacheSetResponse)
async def set_cache_value(request: CacheSetRequest):
    """Set key-value pair in Valkey cache"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        success = valkey_client.set_value(request.key, request.value, request.ttl)
        
        return CacheSetResponse(
            key=request.key,
            success=bool(success),
            message="Value set successfully" if success else "Failed to set value"
        )
        
    except Exception as e:
        logger.error(f"Error setting cache value for key '{request.key}': {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to set cache value: {str(e)}"
        )



@app.post("/api/cluster/connect", response_model=ClusterConnectResponse)
@app.post("/cluster/connect", response_model=ClusterConnectResponse)
async def cluster_connect(request: ClusterConnectRequest):
    # print('====> /api/cluster/connect <====')
    global valkey_client, metrics_collector

    try:
        # 172.30.1.3  ??
        print('ok---ok---ok---ok okokokok, lets connect to')
        print(f'InfluxDB: {request.influxEndpointUrl}')
        result = "yay, connected"
        name = request.name
        redisEndpoint = request.redisEndpoint
        redisPort = request.redisPort
        redisTls = request.redisTls
        redisCluster = request.redisCluster # cluster mode true or false
        print(f'name={name} - redisEndpoint={redisEndpoint}:{redisPort} - TLS={redisTls} - cluster={redisCluster}')
        print(result)

        current_valkey_config = ValkeyConfig()
        current_valkey_config.host = redisEndpoint
        current_valkey_config.port = redisPort
        current_valkey_config.use_tls = redisTls
        current_valkey_config.use_cluster = redisCluster
        #
        #INFLUX Configure
        current_valkey_config.influxEndpointUrl = request.influxEndpointUrl
        current_valkey_config.influxPort = request.influxPort
        current_valkey_config.influxToken = request.influxToken
        current_valkey_config.influxBucket = request.influxBucket
        current_valkey_config.influxOrg = request.influxOrg
        print(f'InfluxDB in valkey_config: {current_valkey_config.influxEndpointUrl}')

        if len(connection_pool) > 0:
            print('   current connection pool [0]')
            print(connection_pool[0])
            print('   new connection pool [0]')
            connection_pool[0] = current_valkey_config
            print('********************************')
            print(connection_pool[0])
            print('********************************')
            print('********************************')
        else:
            connection_pool.append(current_valkey_config)

        valkey_client = ValkeyClient(
            name=name,
            host=redisEndpoint,
            port=redisPort,
            use_tls=redisTls,
            use_cluster=redisCluster
        )
        metrics_collector = MetricsCollector(valkey_client, individual_timeout=5.0)

        if name is None: name = 'None'

        message = "Switch conncection --- Successfully initialized Valkey client and metrics collector to '" + name + "'"

        logger.info(message)
        success = True

        return ClusterConnectResponse(
            status=json.dumps(result),
            success=bool(success),
            message="Connected successfully" if success else "Failed to connect"
        )

    except Exception as e:
        logger.error(f"Error {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to connect to cluster: {str(e)}"
        )

@app.post("/api/cache/recommend", response_model=RecommendationResponse)
@app.post("/cache/recommend", response_model=RecommendationResponse)
async def recommendation(request: RecommendationRequest):
    """Set key-value pair in Valkey cache"""

    try:
        prompt = request.prompt

        result = agt.perform_recommendation(prompt)

        # result = "test"

        recommendations = result['body'].replace('\\n','\n')

        list_of_lines = recommendations.splitlines()

        print(result)

        success = True

        return RecommendationResponse(
            recommendation=json.dumps(list_of_lines),
            success=bool(success),
            message="LLM successfully" if success else "Failed to process LLM"
        )

    except Exception as e:
        logger.error(f"Error {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to set cache value: {str(e)}"
        )

# Chat/Converse endpoint
@app.post("/api/chat/converse", response_model=ConverseResponse)
@app.post("/chat/converse", response_model=ConverseResponse)
async def chat_converse(request: ConverseRequest):
    """Chat with the Elasticache chatbot using the converse method"""
    try:
        # Log the chat request
        logger.info(f"Chat converse request received with prompt length: {len(request.prompt)}")
        
        # Validate input
        if not request.prompt.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Prompt cannot be empty"
            )
        
        # Call the converse function from chatagent.py
        response_text = converse(request.prompt)
        
        # Log successful completion
        logger.info(f"Chat converse completed successfully, response length: {len(response_text)}")
        
        return ConverseResponse(
            response=response_text,
            success=True,
            message="Chat response generated successfully"
        )
        
    except Exception as e:
        logger.error(f"Error in chat converse: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate chat response: {str(e)}"
        )

@app.delete("/api/cache/{key}")
@app.delete("/cache/{key}")
async def delete_cache_value(key: str):
    """Delete key from Valkey cache"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        deleted_count = valkey_client.delete_key(key)
        
        return {
            "key": key,
            "deleted": deleted_count > 0,
            "count": deleted_count,
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error deleting cache key '{key}': {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete cache key: {str(e)}"
        )

@app.get("/api/cache/keys")
@app.get("/cache/keys")
async def get_all_keys(
    pattern: str = "*", 
    count: int = 100, 
    max_iterations: int = 10000, 
    use_scan: bool = True,
    cursor: str = "0",
    paginated: bool = False
):
    """
    Get keys matching pattern using SCAN (non-blocking) or KEYS (blocking)
    
    Args:
        pattern: Pattern to match keys against (supports Redis glob patterns)
        count: Hint for number of keys to return per SCAN iteration (default: 100)
        max_iterations: Maximum SCAN iterations to prevent infinite loops (default: 10000)
        use_scan: If True, use SCAN command; if False, fallback to KEYS command (default: True)
        cursor: Starting cursor for pagination (default: "0") - only used when paginated=True
        paginated: Enable cursor-based pagination mode (default: False)
    """
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        # Choose between paginated and full scan modes
        if paginated:
            # Paginated mode - return single SCAN iteration with cursor
            result = valkey_client.get_paginated_keys(
                cursor=cursor,
                pattern=pattern,
                count=count,
                use_scan=use_scan
            )
            
            # Defensive programming - ensure all required fields exist
            result_cursor = result.get("cursor", "0")
            result_keys = result.get("keys", [])
            result_complete = result.get("complete", True)  # Default to True for safety
            
            return {
                "cursor": result_cursor,
                "keys": result_keys,
                "pattern": pattern,
                "count": len(result_keys),
                "complete": result_complete,
                "scan_method": "SCAN" if use_scan else "KEYS",
                "paginated": True,
                "timestamp": datetime.utcnow().isoformat()
            }
        else:
            # Current mode - return all keys (backward compatible)
            keys = valkey_client.get_all_keys(
                pattern=pattern,
                count=count,
                max_iterations=max_iterations,
                use_scan=use_scan
            )
            
            return {
                "pattern": pattern,
                "keys": keys,
                "count": len(keys),
                "scan_method": "SCAN" if use_scan else "KEYS",
                "scan_parameters": {
                    "count": count,
                    "max_iterations": max_iterations
                } if use_scan else None,
                "timestamp": datetime.utcnow().isoformat()
            }
        
    except Exception as e:
        logger.error(f"Error getting keys with pattern '{pattern}': {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get keys: {str(e)}"
        )

# Execute command endpoint
@app.post("/api/execute", response_model=ExecuteCommandResponse)
@app.post("/execute", response_model=ExecuteCommandResponse)
async def execute_command(request: ExecuteCommandRequest):
    """Execute a Redis/Valkey command and return the result"""
    start_time = time.time()
    timestamp = datetime.utcnow().isoformat()
    
    try:
        # Log the command execution attempt
        logger.info(f"Executing Redis command: {request.command}")
        
        # Validate command against allowlist
        is_allowed, validation_message = validate_command(request.command)
        if not is_allowed:
            execution_time = time.time() - start_time
            logger.warning(f"Command blocked by allowlist: {request.command} - {validation_message}")
            
            return ExecuteCommandResponse(
                command=request.command,
                success=False,
                return_code=-4,
                stdout="",
                stderr=f"Command blocked by allowlist: {validation_message}",
                execution_time=execution_time,
                timestamp=timestamp,
                message=f"Command blocked by allowlist: {validation_message}"
            )
        
        logger.info(f"Command allowed: {validation_message}")
        
        # Ensure ValkeyClient is available
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        # Parse command using shlex for proper handling of quoted strings
        try:
            command_parts = shlex.split(request.command)
            logger.info(f"🔍 DEBUG: Raw command string: '{request.command}'")
            logger.info(f"🔍 DEBUG: Parsed command parts ({len(command_parts)}): {command_parts}")
        except ValueError as e:
            execution_time = time.time() - start_time
            error_msg = f"Invalid command syntax: {str(e)}"
            logger.error(f"🚨 DEBUG: Command parsing failed: {error_msg}")
            
            return ExecuteCommandResponse(
                command=request.command,
                success=False,
                return_code=-6,
                stdout="",
                stderr=error_msg,
                execution_time=execution_time,
                timestamp=timestamp,
                message=error_msg
            )
        
        if not command_parts:
            execution_time = time.time() - start_time
            logger.error(f"🚨 DEBUG: Empty command parts after parsing")
            return ExecuteCommandResponse(
                command=request.command,
                success=False,
                return_code=-5,
                stdout="",
                stderr="Empty command provided",
                execution_time=execution_time,
                timestamp=timestamp,
                message="Empty command provided"
            )
        
        # Log the Redis command that will be executed
        redis_command = command_parts[0].upper()
        logger.info(f"🔍 DEBUG: Redis command: '{redis_command}' with {len(command_parts)-1} argument(s)")
        
        # Execute Redis command using parsed parts
        try:
            logger.info(f"🔍 DEBUG: Executing Redis command with parts: {command_parts}")
            result = valkey_client._execute_with_retry(
                valkey_client.client.execute_command,
                *command_parts
            )
            
            execution_time = time.time() - start_time
            
            # Format result for output
            stdout_str = str(result) if result is not None else ""
            
            logger.info(f"✅ DEBUG: Redis command executed successfully in {execution_time:.2f}s")
            logger.info(f"✅ DEBUG: Command result: {stdout_str[:100]}..." if len(stdout_str) > 100 else f"✅ DEBUG: Command result: {stdout_str}")
            
            return ExecuteCommandResponse(
                command=request.command,
                success=True,
                return_code=0,
                stdout=stdout_str,
                stderr="",
                execution_time=execution_time,
                timestamp=timestamp,
                message="Redis command executed successfully"
            )
            
        except Exception as redis_error:
            execution_time = time.time() - start_time
            error_message = str(redis_error)
            
            logger.error(f"🚨 DEBUG: Redis command failed in {execution_time:.2f}s")
            logger.error(f"🚨 DEBUG: Original command: '{request.command}'")
            logger.error(f"🚨 DEBUG: Parsed parts: {command_parts}")
            logger.error(f"🚨 DEBUG: Redis error: {error_message}")
            
            # Check if it's a common argument error
            if "wrong number of arguments" in error_message.lower():
                logger.error(f"🚨 DEBUG: Argument count error detected!")
                logger.error(f"🚨 DEBUG: Command '{redis_command}' received {len(command_parts)-1} arguments: {command_parts[1:]}")
                
                # Add helpful debugging info for HSET specifically
                if redis_command == "HSET":
                    if len(command_parts) < 4:
                        logger.error(f"🚨 DEBUG: HSET requires at least 3 arguments (key, field, value), got {len(command_parts)-1}")
                    elif (len(command_parts) - 1) % 2 != 1:
                        logger.error(f"🚨 DEBUG: HSET requires odd number of arguments (key + field/value pairs), got {len(command_parts)-1}")
                        logger.error(f"🚨 DEBUG: Expected format: HSET key field1 value1 [field2 value2 ...]")
            
            return ExecuteCommandResponse(
                command=request.command,
                success=False,
                return_code=-1,
                stdout="",
                stderr=error_message,
                execution_time=execution_time,
                timestamp=timestamp,
                message=f"Redis command failed: {error_message}"
            )
            
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        # Handle any other unexpected errors
        execution_time = time.time() - start_time
        logger.error(f"Unexpected error executing Redis command '{request.command}': {str(e)}")
        
        return ExecuteCommandResponse(
            command=request.command,
            success=False,
            return_code=-999,
            stdout="",
            stderr=f"Unexpected error: {str(e)}",
            execution_time=execution_time,
            timestamp=timestamp,
            message=f"Unexpected error: {str(e)}"
        )

# Metrics endpoints
@app.get("/api/metrics/server")
@app.get("/metrics/server")
async def get_server_metrics():
    """Get server metrics including CPU and system information"""
    try:
        if not metrics_collector:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Metrics collector not initialized"
            )
        
        metrics = metrics_collector.get_server_metrics()
        
        return APIResponse(
            status="success",
            data=metrics,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error collecting server metrics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to collect server metrics: {str(e)}"
        )

@app.get("/api/metrics/memory")
@app.get("/metrics/memory")
async def get_memory_metrics():
    """Get memory metrics"""
    try:
        if not metrics_collector:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Metrics collector not initialized"
            )
        
        metrics = metrics_collector.get_memory_metrics()
        
        return APIResponse(
            status="success",
            data=metrics,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error collecting memory metrics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to collect memory metrics: {str(e)}"
        )

@app.get("/api/metrics/connections")
@app.get("/metrics/connections")
async def get_connection_metrics():
    """Get connection metrics"""
    try:
        if not metrics_collector:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Metrics collector not initialized"
            )
        
        metrics = metrics_collector.get_connection_metrics()
        
        return APIResponse(
            status="success",
            data=metrics,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error collecting connection metrics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to collect connection metrics: {str(e)}"
        )

@app.get("/api/metrics/commands")
@app.get("/metrics/commands")
async def get_command_metrics():
    """Get command statistics"""
    try:
        if not metrics_collector:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Metrics collector not initialized"
            )
        
        metrics = metrics_collector.get_command_stats()
        
        return APIResponse(
            status="success",
            data=metrics,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error collecting command metrics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to collect command metrics: {str(e)}"
        )

@app.get("/api/metrics/cluster")
@app.get("/metrics/cluster")
async def get_cluster_metrics():
    """Get cluster metrics"""
    try:
        if not metrics_collector:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Metrics collector not initialized"
            )
        
        metrics = metrics_collector.get_cluster_metrics()
        
        return APIResponse(
            status="success",
            data=metrics,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error collecting cluster metrics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to collect cluster metrics: {str(e)}"
        )

@app.get("/api/metrics/performance")
@app.get("/metrics/performance")
async def get_performance_metrics():
    """Get performance metrics"""
    try:
        if not metrics_collector:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Metrics collector not initialized"
            )
        
        metrics = metrics_collector.get_performance_metrics()
        
        return APIResponse(
            status="success",
            data=metrics,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error collecting performance metrics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to collect performance metrics: {str(e)}"
        )

@app.get("/api/metrics/keyspace")
@app.get("/metrics/keyspace")
async def get_keyspace_metrics():
    """Get keyspace metrics"""
    try:
        if not metrics_collector:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Metrics collector not initialized"
            )
        
        metrics = metrics_collector.get_keyspace_metrics()
        
        return APIResponse(
            status="success",
            data=metrics,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error collecting keyspace metrics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to collect keyspace metrics: {str(e)}"
        )

@app.get("/api/api/metrics/all")
@app.get("/api/metrics/all")
@app.get("/metrics/all")
async def get_all_metrics():
    """Get comprehensive metrics from all categories with caching"""
    try:
        if not metrics_collector:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Metrics collector not initialized"
            )
        
        # Check for cached metrics first
        cached_metrics, is_from_cache = get_cached_metrics()
        
        # If we have fresh cached data, return it
        if is_from_cache:
            logger.info("Serving cached metrics")
            
            response_data = cached_metrics.copy()
            response_data["cache_status"] = "hit"
            
            response = APIResponse(
                status="success",
                data=response_data,
                timestamp=datetime.utcnow().isoformat(),
                message="Serving cached metrics data"
            )
            
            # Convert to JSONResponse to add cache headers
            json_response = JSONResponse(content=response.dict())
            json_response.headers["X-Cache-Status"] = "HIT"
            return json_response
        
        # Collect fresh metrics
        logger.info("Collecting fresh metrics")
        metrics = metrics_collector.get_all_metrics()
        
        # Cache the fresh metrics
        cache_metrics(metrics)
        
        # Add cache status to response
        metrics["cache_status"] = "miss"
        
        response = APIResponse(
            status="success",
            data=metrics,
            timestamp=datetime.utcnow().isoformat(),
            message="Fresh metrics data collected"
        )
        
        # Convert to JSONResponse to add cache headers
        json_response = JSONResponse(content=response.dict())
        json_response.headers["X-Cache-Status"] = "MISS"
        return json_response
        
    except Exception as e:
        logger.error(f"Error collecting all metrics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to collect all metrics: {str(e)}"
        )

# Cluster monitoring and validation endpoints
@app.get("/api/cluster/info")
@app.get("/cluster/info")
async def get_cluster_info():
    """Get cluster information and topology"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        cluster_info = valkey_client.get_cluster_info()
        
        return APIResponse(
            status="success",
            data=cluster_info,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error getting cluster info: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get cluster info: {str(e)}"
        )

@app.get("/api/cluster/validate")
@app.get("/cluster/validate")
async def validate_cluster_configuration():
    """Validate cluster configuration and connectivity"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        validation_result = valkey_client.validate_cluster_configuration()
        
        return APIResponse(
            status="success",
            data=validation_result,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error validating cluster configuration: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to validate cluster configuration: {str(e)}"
        )

@app.get("/api/cluster/stats")
@app.get("/cluster/stats")
async def get_connection_stats():
    """Get connection statistics and health metrics"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        connection_stats = valkey_client.get_connection_stats()
        
        return APIResponse(
            status="success",
            data=connection_stats,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error getting connection stats: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get connection stats: {str(e)}"
        )

@app.get("/api/commandlog/{log_type}")
@app.get("/commandlog/{log_type}")
async def get_commandlog(log_type: str, count: Optional[int] = -1):
    """Get command log entries from Redis/Valkey using COMMANDLOG or fallback to SLOWLOG"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        # Validate log_type parameter
        valid_log_types = ["slow", "large-request", "large-reply"]
        if log_type not in valid_log_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid log_type '{log_type}'. Must be one of: {valid_log_types}"
            )
        
        # Get command log entries
        commandlog_data = valkey_client.get_commandlog(count, log_type)
        
        return APIResponse(
            status="success",
            data=commandlog_data,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except ValueError as ve:
        logger.error(f"Validation error getting command log entries: {str(ve)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )
    except Exception as e:
        logger.error(f"Error getting command log entries (type: {log_type}): {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get command log entries: {str(e)}"
        )

@app.delete("/api/commandlog/{log_type}")
@app.delete("/commandlog/{log_type}")
async def reset_commandlog(log_type: str):
    """Reset/clear command log entries"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        # Validate log_type parameter
        valid_log_types = ["slow", "large-request", "large-reply"]
        if log_type not in valid_log_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid log_type '{log_type}'. Must be one of: {valid_log_types}"
            )
        
        # Reset command log
        reset_result = valkey_client.reset_commandlog(log_type)
        
        return APIResponse(
            status="success",
            data=reset_result,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except ValueError as ve:
        logger.error(f"Validation error resetting command log: {str(ve)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )
    except Exception as e:
        logger.error(f"Error resetting command log (type: {log_type}): {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reset command log: {str(e)}"
        )

@app.get("/api/commandlog/{log_type}/count")
@app.get("/commandlog/{log_type}/count")
async def get_commandlog_length(log_type: str):
    """Get the count of entries in a command log"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        # Validate log_type parameter
        valid_log_types = ["slow", "large-request", "large-reply"]
        if log_type not in valid_log_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid log_type '{log_type}'. Must be one of: {valid_log_types}"
            )
        
        # Get command log length
        length_result = valkey_client.get_commandlog_length(log_type)
        
        return APIResponse(
            status="success",
            data=length_result,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except ValueError as ve:
        logger.error(f"Validation error getting command log length: {str(ve)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )
    except Exception as e:
        logger.error(f"Error getting command log length (type: {log_type}): {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get command log length: {str(e)}"
        )

# Cluster node discovery and metrics endpoints
@app.get("/api/cluster/nodes")
@app.get("/cluster/nodes")
async def get_cluster_nodes():
    """Discover all nodes in the cluster"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        nodes_info = valkey_client.discover_cluster_nodes()
        
        return APIResponse(
            status="success",
            data=nodes_info,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error discovering cluster nodes: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to discover cluster nodes: {str(e)}"
        )

@app.get("/api/cluster/nodes/metrics")
@app.get("/cluster/nodes/metrics")
async def get_cluster_nodes_metrics():
    """Get comprehensive metrics for all cluster nodes"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        nodes_metrics = valkey_client.get_all_nodes_metrics()
        
        return APIResponse(
            status="success",
            data=nodes_metrics,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error getting cluster nodes metrics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get cluster nodes metrics: {str(e)}"
        )

@app.get("/api/nodes/{node_id}/metrics")
@app.get("/nodes/{node_id}/metrics")
async def get_node_metrics(node_id: str):
    """Get detailed metrics for a specific cluster node"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        # First discover nodes to find the requested node
        discovery_result = valkey_client.discover_cluster_nodes()
        if "error" in discovery_result:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to discover nodes: {discovery_result.get('error', 'Unknown error')}"
            )
        
        # Find the requested node
        target_node = None
        for node in discovery_result.get("nodes", []):
            if node["nodeId"] == node_id:
                target_node = node
                break
        
        if not target_node:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Node with ID '{node_id}' not found in cluster"
            )
        
        # Get metrics for the specific node
        node_metrics = valkey_client.get_node_metrics(
            node_address=target_node["nodeAddress"],
            node_id=target_node["nodeId"],
            role=target_node["role"],
            slots=target_node.get("slots")
        )
        
        return APIResponse(
            status="success",
            data=node_metrics,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Error getting metrics for node '{node_id}': {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get node metrics: {str(e)}"
        )

# Cluster slot stats endpoint
@app.get("/api/cluster/slot-stats")
@app.get("/cluster/slot-stats")
async def get_cluster_slot_stats(start_slot: Optional[int] = None, end_slot: Optional[int] = None):
    """Get cluster slot statistics with optional slot range filtering and caching"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        # Generate cache key based on parameters
        cache_key = f"slot_stats_{start_slot}_{end_slot}"
        current_time = time.time()
        
        # Check if we have cached data that's still valid
        if cache_key in slot_stats_cache:
            cache_entry = slot_stats_cache[cache_key]
            cache_age = current_time - cache_entry["timestamp"]
            
            if cache_age < CACHE_TTL_SECONDS:
                logger.info(f"Returning cached slot stats (age: {cache_age:.1f}s)")
                
                # Add cache metadata to the response
                cached_data = cache_entry["data"].copy()
                cached_data["cached"] = True
                cached_data["cache_timestamp"] = datetime.fromtimestamp(cache_entry["timestamp"]).isoformat()
                
                return APIResponse(
                    status="success",
                    data=cached_data,
                    timestamp=datetime.utcnow().isoformat()
                )
        
        # Get fresh data from Valkey
        logger.info(f"Fetching fresh slot stats (start_slot: {start_slot}, end_slot: {end_slot})")
        slot_stats = valkey_client.get_cluster_slot_stats(start_slot, end_slot)
        
        # Add cache metadata to the response
        slot_stats["cached"] = False
        slot_stats["cache_timestamp"] = None
        
        # Cache the result
        slot_stats_cache[cache_key] = {
            "data": slot_stats,
            "timestamp": current_time
        }
        
        # Clean up old cache entries (simple cleanup)
        keys_to_remove = []
        for key, entry in slot_stats_cache.items():
            if current_time - entry["timestamp"] > CACHE_TTL_SECONDS * 2:  # Keep cleanup threshold higher
                keys_to_remove.append(key)
        
        for key in keys_to_remove:
            del slot_stats_cache[key]
        
        if keys_to_remove:
            logger.info(f"Cleaned up {len(keys_to_remove)} expired cache entries")
        
        return APIResponse(
            status="success",
            data=slot_stats,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error getting cluster slot stats: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get cluster slot stats: {str(e)}"
        )

# Execute allowlist endpoint
@app.get("/api/execute/allowlist")
@app.get("/execute/allowlist")
async def get_execute_allowlist():
    """Get current execute allowlist configuration"""
    try:
        execute_config = get_execute_config()
        allowlist = execute_config.allowlist
        
        return APIResponse(
            status="success",
            data={
                "enabled": allowlist.enabled,
                "mode": allowlist.mode,
                "commands": allowlist.commands,
                "patterns": allowlist.patterns,
                "prefixes": allowlist.prefixes
            },
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error getting execute allowlist: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get execute allowlist: {str(e)}"
        )

# COMMANDLOG support cache management endpoint
@app.post("/api/commandlog/reset-support-cache")
@app.post("/commandlog/reset-support-cache")
async def reset_commandlog_support_cache():
    """Reset COMMANDLOG support detection cache to force re-detection"""
    try:
        if not valkey_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Valkey client not initialized"
            )
        
        # Reset the support cache
        valkey_client.reset_commandlog_support_cache()
        
        # Force re-detection by trying to get a commandlog entry
        detection_result = valkey_client.get_commandlog(count=0, log_type="slow")
        
        return APIResponse(
            status="success",
            data={
                "message": "COMMANDLOG support cache reset successfully",
                "detection_result": {
                    "supported": detection_result.get("supported", False),
                    "source": detection_result.get("source", "unknown"),
                    "note": detection_result.get("note", "")
                }
            },
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Error resetting COMMANDLOG support cache: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reset COMMANDLOG support cache: {str(e)}"
        )

# Root endpoint
@app.get("/")
async def root():
    """Root endpoint with API information"""
    return {
        "name": app_config.name,
        "version": app_config.version,
        "description": app_config.description,
        "endpoints": {
            "health": "/health",
            "cache_operations": {
                "get": "/api/cache/get/{key}",
                "set": "/api/cache/set",
                "delete": "/api/cache/{key}",
                "keys": "/api/cache/keys",
                "keys_pagination": {
                    "all_keys": "/api/cache/keys?pattern=*",
                    "paginated": "/api/cache/keys?paginated=true&cursor=0&count=25",
                    "next_page": "/api/cache/keys?paginated=true&cursor={returned_cursor}&count=25",
                    "note": "Use paginated=true for cursor-based pagination matching native Redis SCAN behavior"
                }
            },
            "chat": {
                "converse": "/api/chat/converse",
                "note": "Chat with the Elasticache chatbot using natural language prompts"
            },
            "execute": "/api/execute",
            "metrics": {
                "server": "/api/metrics/server",
                "memory": "/api/metrics/memory",
                "connections": "/api/metrics/connections",
                "commands": "/api/metrics/commands",
                "cluster": "/api/metrics/cluster",
                "performance": "/api/metrics/performance",
                "keyspace": "/api/metrics/keyspace",
                "all": "/api/metrics/all"
            },
            "cluster": {
                "info": "/api/cluster/info",
                "validate": "/api/cluster/validate",
                "stats": "/api/cluster/stats",
                "nodes": "/api/cluster/nodes",
                "nodes_metrics": "/api/cluster/nodes/metrics",
                "node_metrics": "/api/nodes/{nodeId}/metrics",
                "slot_stats": "/api/cluster/slot-stats"
            },
            "commandlog": {
                "get": "/api/commandlog/{log_type}?count=N",
                "reset": "/api/commandlog/{log_type}",
                "count": "/api/commandlog/{log_type}/count",
                "note": "log_type options: slow, large-request, large-reply. large-request and large-reply require Valkey 8.1+"
            },
            "execute_allowlist": "/api/execute/allowlist"
        },
        "docs": "/docs",
        "timestamp": datetime.utcnow().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    log_level = "debug" if server_config.debug_mode else "info"
    uvicorn.run(app, host=server_config.host, port=server_config.port, log_level=log_level)
