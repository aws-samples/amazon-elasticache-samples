#!/usr/bin/env python3

import os
import csv
import typer
import time
import json
import sys
import logging
import asyncio
import contextlib
import io
import threading
import re
from datetime import datetime
from pathlib import Path

# Redis-py imports and setup
import redis
import redis.asyncio as redis_async
from redis.asyncio.cluster import RedisCluster as AsyncRedisCluster, ClusterNode
from redis.crc import key_slot
import redis.exceptions
from dataclasses import dataclass
from typing import Union

from rich.console import Console
from rich.table import Table
from rich.logging import RichHandler

from inmemory_assessment_metadata import APP_NAME, VERSION

app = typer.Typer(add_completion=False)
console = Console()


# Redis/Valkey command categorization for workload analysis
READ_COMMANDS = {
    "asking", "auth", "bitcount", "bitfield_ro", "bitpos", "client", "command", "config",
    "dbsize", "debug", "dump", "echo", "eval_ro", "evalsha_ro", "exists", "expiretime", "fcall_ro",
    "function",
    "geodist", "geohash", "geopos", "georadius_ro", "georadiusbymember_ro",
    "geosearch", "get", "getbit", "getrange", "hexists", "hget", "hgetall", "hkeys", "hlen",
    "hmget", "hrandfield", "hscan", "hstrlen", "hvals", "info", "keys", "lastsave", "latency",
    "lcs", "lindex", "llen", "lolwut", "lpos", "lrange", "memory", "memory|usage", "mget",
    "module", "monitor", "object|encoding", "object|freq", "object|idletime", "object|refcount",
    "pexpiretime", "pfcount", "ping", "psync", "pttl", "pubsub", "randomkey", "readonly", "readwrite",
    "replicaof", "reset", "replconf", "role", "scan", "scard", "script", "sdiff", "select",
    "sinter", "sintercard", "sismember", "slaveof", "slowlog", "smembers", "smismember", "sort_ro",
    "srandmember", "sscan", "strlen", "substr", "sunion", "sync", "time", "touch", "ttl", "type",
    "wait",
    "xinfo", "xinfo|consumers", "xinfo|groups", "xinfo|stream", "xlen", "xpending", "xrange",
    "xread", "xrevrange", "zcard", "zcount", "zdiff", "zinter", "zintercard", "zlexcount",
    "zmscore", "zrandmember", "zrange", "zrangebylex", "zrangebyscore", "zrank", "zrevrange",
    "zrevrangebylex", "zrevrangebyscore", "zrevrank", "zscan", "zscore", "zunion"
}

WRITE_COMMANDS = {
    "acl", "append", "bgrewriteaof", "bgsave", "zpopmax", "incrbyfloat", "lpush", "xadd",
    "renamenx", "pfmerge", "rpoplpush", "hincrbyfloat", "lmove", "expire", "xdel", "bzpopmin",
    "decrby", "ltrim", "rpop", "xgroup", "xgroup|destroy", "xgroup|delconsumer", "xgroup|create",
    "xgroup|setid", "xgroup|createconsumer", "smove", "psetex", "getset", "rpushx", "restore-asking",
    "zrem", "hsetnx", "lset", "set", "setbit", "flushdb", "expireat", "del", "geoadd", "move",
    "zinterstore", "flushall", "sunionstore", "msetnx", "xsetid", "spop", "hmset", "zremrangebyscore",
    "zremrangebyrank", "blpop", "swapdb", "lmpop", "zunionstore", "zadd", "bitfield", "linsert",
    "zpopmin", "sort", "xtrim", "sdiffstore", "blmove", "pfadd", "setnx", "rpush", "brpoplpush",
    "getdel", "bzpopmax", "rename", "pexpire", "lpushx", "sinterstore", "xack", "xackdel",
    "xreadgroup", "decr", "pexpireat", "zrangestore", "blmpop", "zremrangebylex", "persist", "setex",
    "setrange", "hincrby", "incr", "copy", "unlink", "restore", "pfdebug", "xclaim", "sadd",
    "getex", "zincrby", "incrby", "hset", "geosearchstore", "zmpop", "lrem", "bitop", "brpop",
    "bzmpop", "xautoclaim", "georadius", "georadiusbymember", "hgetdel", "hpersist", "migrate",
    "publish", "quit", "restore-asking", "zdiffstore", "hdel", "lpop", "mset", "save", "srem",
    "spublish", "xdelex", "xgroup"
}

# Background/maintenance commands to exclude from application workload metrics
# These are typical managed-service overhead operations, not actual user traffic
BACKGROUND_COMMANDS = {
    "ping",           # Health checks from AWS monitoring
    "info",           # Metrics collection for CloudWatch
    "replconf",       # Replication heartbeats
    "client|setinfo", # Connection management  
    "auth",           # Authentication from monitoring tools
    "config",         # Configuration queries
    "cluster",        # Cluster management commands
    "hello",          # Protocol negotiation
    "command",        # Command introspection
}

def setup_logging(log_level: str = "INFO", quiet: bool = False):
    """Setup logging with Rich handler for colored output"""
    # Validate and convert string level to logging constant
    valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    log_level_upper = log_level.upper()
    
    if log_level_upper not in valid_levels:
        if not quiet:
            console.print(f"[red]Invalid log level: {log_level}[/red]")
            console.print(f"[yellow]Valid levels: {', '.join(valid_levels)}[/yellow]")
        sys.exit(1)
    
    numeric_level = getattr(logging, log_level_upper)
    
    # Configure the root logger
    logging.basicConfig(
        level=numeric_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)]
    )
    
    # Get logger for this module
    logger = logging.getLogger("inmemory_assessment")
    logger.setLevel(numeric_level)
    
    return logger

def ensure_output_directory(file_path: str, quiet: bool = False) -> str:
    """
    Ensure the directory for the output file exists.
    
    Args:
        file_path: The full path to the output file
        quiet: Whether to suppress console output
        
    Returns:
        The validated file path with proper directory structure
        
    Raises:
        typer.Exit: If the directory can't be created due to permissions
    """
    path = Path(file_path)
    directory = path.parent
    
    # If no directory specified (just filename), use 'output' directory
    if str(directory) == '.':
        directory = Path('output')
        file_path = str(directory / path.name)
        path = Path(file_path)
        directory = path.parent
    
    # Create directory if it doesn't exist
    try:
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            if not quiet:
                console.print(f"[blue]Created output directory:[/blue] {directory}")
        return file_path
    except PermissionError:
        console.print(f"[red]Permission denied creating directory:[/red] {directory}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error creating directory {directory}:[/red] {e}")
        raise typer.Exit(1)

def validate_custom_output_path(file_path: str) -> str:
    """
    Validate that the directory for a custom output file path exists.
    
    Args:
        file_path: The full path to the output file
        
    Returns:
        The validated file path
        
    Raises:
        typer.Exit: If the directory doesn't exist
    """
    path = Path(file_path)
    directory = path.parent
    
    # If the directory is explicitly specified but doesn't exist, fail gracefully
    if str(directory) != '.' and not directory.exists():
        console.print(f"[red]Directory does not exist:[/red] {directory}")
        console.print(f"[yellow]Please create the directory first or use the default output location[/yellow]")
        raise typer.Exit(1)
    
    return file_path

# Initialize logger (will be reconfigured in main() based on log level)
logger = logging.getLogger("inmemory_assessment")

@dataclass
class RedisConnection:
    """Simple container for client and metadata - no complex wrapper"""
    client: Union['redis_async.Redis', 'AsyncRedisCluster']
    source_host: str
    is_cluster: bool
    
    async def close(self):
        """Close the underlying client"""
        if hasattr(self.client, 'close'):
            await self.client.aclose()

# Redis-py native utility functions to replace UnifiedRedisClient wrapper
async def collect_info_from_cluster(cluster_client, section):
    """Collect INFO section from all cluster nodes using redis-py native patterns"""
    result = {}
    for node in cluster_client.get_nodes():
        # Normalize 127.0.0.1 to localhost for consistency
        host = "localhost" if node.host == "127.0.0.1" else node.host
        node_addr = f"{host}:{node.port}"
        info_raw = await cluster_client.execute_command("INFO", section, target_nodes=[node])
        result[node_addr] = parse_info_response(info_raw)
    return result

async def collect_info_from_standalone(standalone_client, source_host, section):
    """Collect INFO section from standalone client using redis-py native patterns"""
    info_raw = await standalone_client.info(section)
    return {source_host: parse_info_response(info_raw)}

async def execute_command_on_cluster(cluster_client, command):
    """Execute command on cluster using redis-py native patterns"""
    result = {}
    for node in cluster_client.get_nodes():
        # Normalize 127.0.0.1 to localhost for consistency
        host = "localhost" if node.host == "127.0.0.1" else node.host
        node_addr = f"{host}:{node.port}"
        response = await cluster_client.execute_command(*command, target_nodes=[node])
        result[node_addr] = response
    return result

async def execute_command_on_standalone(standalone_client, source_host, command):
    """Execute command on standalone client using redis-py native patterns"""
    response = await standalone_client.execute_command(*command)
    return {source_host: response}

async def collect_commandstats_from_cluster(cluster_client):
    """Collect COMMANDSTATS from all cluster nodes using redis-py native patterns"""
    result = {}
    for node in cluster_client.get_nodes():
        # Normalize 127.0.0.1 to localhost for consistency
        host = "localhost" if node.host == "127.0.0.1" else node.host
        node_addr = f"{host}:{node.port}"
        commandstats_raw = await cluster_client.execute_command("INFO", "commandstats", target_nodes=[node])
        result[node_addr] = commandstats_raw
    return result

async def collect_commandstats_from_standalone(standalone_client, source_host):
    """Collect COMMANDSTATS from standalone client using redis-py native patterns"""
    commandstats_raw = await standalone_client.execute_command("INFO", "commandstats")
    return {source_host: commandstats_raw}

def get_cluster_node_addresses(cluster_client):
    """Get cluster node addresses using redis-py native patterns"""
    addresses = []
    for node in cluster_client.get_nodes():
        # Normalize 127.0.0.1 to localhost for consistency
        host = "localhost" if node.host == "127.0.0.1" else node.host
        addresses.append(f"{host}:{node.port}")
    return addresses

def fmt_node(node_addr):
    """Format node address with consistent width for better log readability"""
    if node_addr is None:
        return "[Unknown              ]"
    # Maximum: 255.255.255.255:65535 = 21 chars, pad to 21 for consistency
    return f"[{node_addr:<21}]"

def build_connection_config(host, port, user, password, tls, socket_timeout=None, cluster_mode=False):
    """Helper function to build redis-py connection configuration consistently"""
    config = {
        'decode_responses': True,
        'socket_connect_timeout': socket_timeout / 1000 if socket_timeout else 10  # Convert ms to seconds
    }
    
    # Add credentials if provided
    if user:
        config['username'] = user
    if password:
        config['password'] = password
    
    # Add TLS settings if needed
    if tls:
        config['ssl'] = True
        config['ssl_check_hostname'] = False
        config['ssl_cert_reqs'] = None
    
    if cluster_mode:
        # For cluster mode, we need startup nodes
        config['startup_nodes'] = [ClusterNode(host, port)]
    else:
        # For standalone mode
        config['host'] = host
        config['port'] = port
    
    return config

async def detect_mode_and_connect(host, port, user, password, tls, quiet=False):
    """Auto-detect cluster deployment mode and return appropriate client"""
    
    # Build connection config for probing
    config = build_connection_config(host, port, user, password, tls, socket_timeout=10000, cluster_mode=True)
    source_host = f"{host}:{port}"
    
    # Always try cluster first
    try:
        cluster = AsyncRedisCluster(**config)
        
        # Test with CLUSTER INFO command
        try:
            info = await cluster.execute_command('CLUSTER INFO')
            
            # redis-py 6.2.0 can return dict or string
            if isinstance(info, str) and 'cluster_state:ok' in info:
                if not quiet:
                    logger.debug("Detected cluster mode (string response)")
                return RedisConnection(cluster, source_host, is_cluster=True)
            elif isinstance(info, dict) and info.get('cluster_state') == 'ok':
                if not quiet:
                    logger.debug("Detected cluster mode (dict response)")
                return RedisConnection(cluster, source_host, is_cluster=True)
                
        except (redis.exceptions.MovedError, redis.exceptions.RedisClusterException):
            # MovedError indicates cluster mode
            if not quiet:
                logger.debug("Detected cluster mode via MovedError")
            return RedisConnection(cluster, source_host, is_cluster=True)
            
    except Exception as e:
        # Check for specific "cluster not enabled" errors
        if any(phrase in str(e).lower() for phrase in [
            "cluster mode is not enabled",
            "cluster support disabled", 
            "cluster cannot be connected"
        ]):
            if not quiet:
                logger.debug("Cluster mode not enabled - trying standalone")
        else:
            if not quiet:
                logger.debug(f"Cluster connection failed: {e} - trying standalone")
    
    # Fallback to standalone
    try:
        standalone_config = build_connection_config(host, port, user, password, tls, socket_timeout=10000, cluster_mode=False)
        standalone = redis_async.Redis(**standalone_config)
        # Test connection
        await standalone.ping()
        if not quiet:
            logger.debug("Detected standalone mode")
        return RedisConnection(standalone, source_host, is_cluster=False)
    except Exception as e:
        if not quiet:
            console.print(f"[bold red]Connection failed:[/bold red] {e}")
        sys.exit(1)

async def connect(host, port, user, password, tls, quiet=False, cluster_mode=None):
    # Use auto-detection if cluster_mode not specified
    if cluster_mode is None:
        return await detect_mode_and_connect(host, port, user, password, tls, quiet)
    
    # Use explicit cluster mode if specified
    config = build_connection_config(host, port, user, password, tls, socket_timeout=10000, cluster_mode=cluster_mode)
    source_host = f"{host}:{port}"
    
    try:
        if cluster_mode:
            raw_client = AsyncRedisCluster(**config)
            return RedisConnection(raw_client, source_host, is_cluster=True)
        else:
            raw_client = redis_async.Redis(**config)
            await raw_client.ping()  # Test connection
            return RedisConnection(raw_client, source_host, is_cluster=False)
    except Exception as e:
        if not quiet:
            console.print(f"[bold red]Connection failed:[/bold red] {e}")
        sys.exit(1)


async def verify_authentication(host, port, user, password, tls, cluster_mode=None):
    """Verify authentication credentials work before starting assessment"""
    node_addr = f"{host}:{port}"
    
    # Auto-detect cluster mode by probing if not provided
    if cluster_mode is None:
        cluster_mode = await detect_cluster_mode_by_probing(host, port, user, password, tls)
    
    # Try cluster mode first if indicated, then fall back to standalone
    if cluster_mode:
        logger.debug(f"{fmt_node(node_addr)} Testing cluster mode connection")
        try:
            config = build_connection_config(host, port, user, password, tls, socket_timeout=10000, cluster_mode=True)
            client = AsyncRedisCluster(**config)
            await client.ping()
            await client.aclose()
            logger.debug(f"{fmt_node(node_addr)} Cluster mode authentication successful")
            return True
        except Exception as e:
            error_msg = str(e).lower()
            if ("cluster support disabled" in error_msg or 
                "no topology views found" in error_msg or
                "this instance has cluster support disabled" in error_msg):
                logger.info(f"{fmt_node(node_addr)} Cluster mode not available, trying standalone mode")
            else:
                logger.debug(f"{fmt_node(node_addr)} Cluster connection failed: {str(e)}")
    
    # Try standalone mode
    try:
        logger.debug(f"{fmt_node(node_addr)} Testing standalone mode connection")
        config = build_connection_config(host, port, user, password, tls, socket_timeout=10000, cluster_mode=False)
        client = redis_async.Redis(**config)
        await client.ping()
        await client.aclose()
        logger.debug(f"{fmt_node(node_addr)} Standalone authentication successful")
        return True
    except redis.exceptions.ConnectionError as e:
        error_msg = str(e).lower()
        if "name or service not known" in error_msg or "nodename nor servname provided" in error_msg:
            logger.error(f"{fmt_node(node_addr)} Hostname resolution failed: Unable to resolve hostname")
            return False
        elif "connection refused" in error_msg:
            logger.error(f"{fmt_node(node_addr)} Connection refused: Port may be closed or service not running")
            return False
        elif "timeout" in error_msg:
            logger.error(f"{fmt_node(node_addr)} Connection timeout: Host may be unreachable")
            return False
        else:
            logger.error(f"{fmt_node(node_addr)} Connection failed: {str(e)}")
            return False
    except redis.exceptions.ResponseError as e:
        error_msg = str(e).lower()
        if "invalid username-password pair" in error_msg or "auth" in error_msg:
            logger.error(f"{fmt_node(node_addr)} Authentication failed: {str(e)}")
            return False
        else:
            logger.error(f"{fmt_node(node_addr)} Command failed: {str(e)}")
            return False
    except Exception as e:
        logger.error(f"{fmt_node(node_addr)} Unexpected error connecting: {str(e)}")
        return False


async def _detect_elasticache_roles(node_addresses, collected_roles, user, password, tls):
    """
    Detect primary/replica roles for ElastiCache clusters by querying each node individually.
    ElastiCache restricts CLUSTER NODES without target specification, so we use INFO replication.
    """
    import redis.asyncio as redis_async
    
    for node_addr in node_addresses:
        try:
            host, port = node_addr.split(":")
            port = int(port)
            
            # Create a direct connection to this specific node
            config = build_connection_config(host, port, user, password, tls, socket_timeout=5000, cluster_mode=False)
            client = redis_async.Redis(**config)
            
            try:
                # Get replication info to determine if this is primary or replica
                info = await client.execute_command("INFO", "replication")
                
                # Handle different response formats (string, bytes, or dict)
                if isinstance(info, bytes):
                    info = info.decode('utf-8')
                elif isinstance(info, dict):
                    # If it's already parsed as a dict, extract the role directly
                    role = info.get('role', None)
                else:
                    # String format - need to parse
                    lines = info.strip().split('\n')
                    role = None
                    for line in lines:
                        if line.startswith('role:'):
                            role = line.split(':')[1].strip()
                            break
                
                # Map engine terminology to our terminology
                if role == "master":
                    collected_roles[node_addr] = "primary"
                    logger.debug(f"ElastiCache node {node_addr} identified as primary via INFO replication")
                elif role == "slave":
                    collected_roles[node_addr] = "replica"
                    logger.debug(f"ElastiCache node {node_addr} identified as replica via INFO replication")
                else:
                    collected_roles[node_addr] = "unknown"
                    logger.warning(f"ElastiCache node {node_addr} has unknown role: {role}")
                    
            finally:
                await client.aclose()
                
        except Exception as e:
            logger.warning(f"Failed to detect role for ElastiCache node {node_addr}: {e}")
            collected_roles[node_addr] = "unknown"

async def detect_cluster_mode_by_probing(host, port, user, password, tls):
    """
    Detect cluster mode by actually connecting to the node and checking if clustering is enabled.
    
    This is more reliable than hostname-based detection since it works with any cluster setup.
    """
    try:
        # Try standalone connection first and check CLUSTER INFO - this is more reliable
        try:
            config = build_connection_config(host, port, user, password, tls, socket_timeout=5000, cluster_mode=False)
            standalone_client = redis_async.Redis(**config)
            
            # Try to get cluster info - this will work if clustering is enabled
            try:
                cluster_info = await standalone_client.execute_command("CLUSTER", "INFO")
                await standalone_client.aclose()
                
                # Parse cluster info to see if clustering is enabled
                cluster_state = None
                if isinstance(cluster_info, (str, bytes)):
                    if isinstance(cluster_info, bytes):
                        cluster_info = cluster_info.decode('utf-8')
                    lines = cluster_info.strip().split('\n')
                    for line in lines:
                        if line.startswith('cluster_state:'):
                            cluster_state = line.split(':')[1].strip()
                            break
                elif isinstance(cluster_info, dict):
                    # Handle dict response format
                    for node_addr, info in cluster_info.items():
                        if isinstance(info, str):
                            lines = info.strip().split('\n')
                            for line in lines:
                                if line.startswith('cluster_state:'):
                                    cluster_state = line.split(':')[1].strip()
                                    break
                
                # If cluster_state is 'ok' or 'fail', clustering is enabled
                if cluster_state in ['ok', 'fail']:
                    logger.debug(f"Cluster mode confirmed: cluster_state={cluster_state}")
                    await standalone_client.aclose()
                    return True
                else:
                    logger.debug(f"Single node appears to be standalone: cluster_state={cluster_state}")
                    await standalone_client.aclose()
                    return False
                
            except Exception as e:
                # If CLUSTER INFO fails with "cluster support disabled", it's definitely standalone
                error_msg = str(e).lower()
                if "cluster support disabled" in error_msg or "cluster mode is not enabled" in error_msg:
                    await standalone_client.aclose()
                    logger.info(f"Standalone mode confirmed: {str(e)}")
                    return False
                else:
                    # Other errors might indicate connectivity issues, try cluster approach
                    await standalone_client.aclose()
                    logger.debug(f"CLUSTER INFO failed with unexpected error: {e}, trying cluster approach")
            
        except Exception as standalone_e:
            logger.debug(f"Standalone connection failed: {standalone_e}")
        
        # If standalone + CLUSTER INFO didn't work, try cluster client with actual cluster command
        try:
            config = build_connection_config(host, port, user, password, tls, socket_timeout=5000, cluster_mode=True)
            cluster_client = AsyncRedisCluster(**config)
            
            # Actually test cluster functionality with CLUSTER NODES command
            try:
                await cluster_client.execute_command("CLUSTER", "NODES")
                await cluster_client.aclose()
                logger.info("Cluster mode detected via CLUSTER NODES command")
                return True
            except Exception as e:
                await cluster_client.aclose()
                error_msg = str(e).lower()
                if "cluster support disabled" in error_msg or "cluster mode is not enabled" in error_msg:
                    logger.info(f"Standalone mode confirmed via cluster client: {str(e)}")
                    return False
                else:
                    # Other cluster errors might still indicate a cluster (e.g., cluster down)
                    logger.debug(f"Cluster command failed: {e}")
                    return False
            
        except Exception as cluster_e:
            logger.debug(f"Cluster client creation failed: {cluster_e}")
            return False
    
    except Exception as e:
        logger.debug(f"Detection failed entirely: {e}")
        return False

async def get_cluster_info(redis_client, source_host, is_cluster):
    """
    Get cluster information including all node addresses and roles.
    
    For standalone instances, returns the single node.
    For cluster instances, discovers all nodes and their roles.
    """
    logger.debug(f"Client reports is_cluster: {is_cluster}")
    
    # If this is a cluster client, discover cluster topology using CLUSTER NODES command
    if is_cluster:
        # Try to discover all cluster nodes using CLUSTER NODES command (more reliable than get_nodes())
        try:
            logger.debug("Discovering cluster topology using CLUSTER NODES command")
            cluster_nodes_response = await redis_client.execute_command("CLUSTER", "NODES")
            
            node_list = []
            roles = {}
            primary_replica_connections = {}
            
            # Parse the cluster nodes response
            if isinstance(cluster_nodes_response, (str, bytes)):
                if isinstance(cluster_nodes_response, bytes):
                    cluster_nodes_response = cluster_nodes_response.decode('utf-8')
                
                logger.debug(f"CLUSTER NODES response:\n{cluster_nodes_response}")
                lines = cluster_nodes_response.splitlines()
                
                # Build nodeid -> ip:port mapping and extract nodes
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 3:
                        ip_port = parts[1].split("@")[0]
                        flags = parts[2]
                        
                        # Skip failed nodes
                        if "fail" not in flags:
                            # Normalize 127.0.0.1 to localhost for consistency
                            if ip_port.startswith("127.0.0.1:"):
                                ip_port = ip_port.replace("127.0.0.1:", "localhost:")
                            
                            if ip_port not in node_list:
                                node_list.append(ip_port)
                            
                            if "master" in flags:
                                roles[ip_port] = "primary"
                            elif "slave" in flags:
                                roles[ip_port] = "replica"
                
                logger.debug(f"Discovered {len(node_list)} cluster nodes: {node_list}")
                logger.debug(f"Node roles: {roles}")
                
                if len(node_list) > 1:
                    # Multi-node cluster discovered
                    return {
                        "cluster_mode": True,
                        "primaries": len([r for r in roles.values() if r == "primary"]),
                        "replicas": len([r for r in roles.values() if r == "replica"]),
                        "node_addresses": node_list,
                        "roles": roles,
                        "primary_replica_connections": primary_replica_connections
                    }
                else:
                    # Single-node cluster
                    logger.debug("Single-node cluster detected from CLUSTER NODES")
                    return {
                        "cluster_mode": True,
                        "primaries": 1,
                        "replicas": 0,
                        "node_addresses": [source_host],
                        "roles": {source_host: "primary"},
                        "primary_replica_connections": {}
                    }
            
        except Exception as e:
            logger.debug(f"CLUSTER NODES command failed: {e}, falling back to simple logic")
            # Fall back to simple single-node cluster logic
            return {
                "cluster_mode": True,
                "primaries": 1,
                "replicas": 0,
                "node_addresses": [source_host],
                "roles": {source_host: "primary"},
                "primary_replica_connections": {}
            }
        
        # This is the old logic that we're keeping as a fallback
        node_addresses = get_cluster_node_addresses(redis_client)
        logger.debug(f"Cluster client knows about {len(node_addresses)} nodes: {node_addresses}")
        
        if len(node_addresses) > 1:
            logger.debug("Multiple nodes detected via get_nodes() - proceeding with cluster node discovery")
        else:
            # Single node reported by cluster client - might be standalone after all
            logger.debug("Only single node from cluster client - checking CLUSTER INFO")
            try:
                # Try to get cluster info to double-check using native redis-py
                cluster_info_response = await redis_client.execute_command("CLUSTER", "INFO")
                
                # Parse cluster info to see if clustering is enabled
                cluster_state = None
                if isinstance(cluster_info_response, (str, bytes)):
                    if isinstance(cluster_info_response, bytes):
                        cluster_info_response = cluster_info_response.decode('utf-8')
                    lines = cluster_info_response.strip().split('\n')
                    for line in lines:
                        if line.startswith('cluster_state:'):
                            cluster_state = line.split(':')[1].strip()
                            break
                elif isinstance(cluster_info_response, dict):
                    # Handle dict response format
                    for node_addr, info in cluster_info_response.items():
                        if isinstance(info, str):
                            lines = info.strip().split('\n')
                            for line in lines:
                                if line.startswith('cluster_state:'):
                                    cluster_state = line.split(':')[1].strip()
                                    break
                
                # If cluster_state is 'ok' or 'fail', clustering is enabled
                if cluster_state in ['ok', 'fail']:
                    logger.info(f"Cluster mode detected: cluster_state={cluster_state}")
                else:
                    logger.debug(f"Single node appears to be standalone: cluster_state={cluster_state}")
                    return {
                        "cluster_mode": False,
                        "primaries": 1,
                        "replicas": await get_replica_count(redis_client, source_host, is_cluster=False),
                        "node_addresses": [source_host],
                        "roles": {source_host: "primary"},
                        "primary_replica_connections": {}
                    }
                logger.debug(f"CLUSTER INFO cluster_state: {cluster_state}, is_cluster: {is_cluster}")
                
            except Exception as e:
                # If CLUSTER INFO fails, trust the cluster client creation
                logger.debug(f"CLUSTER INFO failed ({str(e)}) - trusting cluster client creation")
                is_cluster = True  # Trust the original cluster client creation
    else:
        # Client was created as standalone
        is_cluster = False
    
    if not is_cluster:
        # Standalone instance - discover replica addresses for complete node list
        logger.debug("Detected standalone Redis/Valkey instance")
        
        # Discover replica nodes using INFO replication
        node_addresses = [source_host]
        roles = {source_host: "primary"}
        replica_count = 0
        
        try:
            repl_info = await collect_info_from_standalone(redis_client, source_host, "replication")
            if source_host in repl_info and isinstance(repl_info[source_host], dict):
                repl_data = repl_info[source_host]
                replica_count = repl_data.get("connected_slaves", 0)
                
                # Extract replica addresses from slave entries  
                for key, value in repl_data.items():
                    if key.startswith("slave") and isinstance(value, dict):
                        # redis-py already parses slave info into dict: {'ip': '127.0.0.1', 'port': 50010, ...}
                        ip = value.get('ip')
                        port = value.get('port')
                        
                        if ip and port:
                            # Normalize 127.0.0.1 to localhost for consistency
                            if ip == "127.0.0.1":
                                ip = "localhost"
                            replica_addr = f"{ip}:{port}"
                            if replica_addr not in node_addresses:
                                node_addresses.append(replica_addr)
                                roles[replica_addr] = "replica"
                                logger.debug(f"Discovered replica: {replica_addr}")
        except Exception as e:
            logger.debug(f"Error discovering replicas: {e}")
        
        logger.debug(f"Standalone mode: discovered {len(node_addresses)} total nodes ({1} primary, {replica_count} replicas)")
        return {
            "cluster_mode": False,
            "primaries": 1,
            "replicas": replica_count,
            "node_addresses": node_addresses,
            "roles": roles,
            "primary_replica_connections": {}
        }
    
    # Cluster instance - proceed with cluster commands
    logger.debug("Detected cluster-enabled Redis/Valkey instance")
    try:
        # Use the cluster client's own node discovery if available
        if hasattr(redis_client, 'get_node_addresses') and len(redis_client.get_node_addresses()) > 1:
            # Use the cluster client's knowledge of nodes
            node_addresses = redis_client.get_node_addresses()
            logger.debug(f"Using cluster client node discovery: {node_addresses}")
            
            # Initialize with cluster client's node knowledge
            node_list = node_addresses
            roles = {}
            primary_replica_connections = {}
            nodeid_to_addr = {}
            
            # Try to get detailed node info from CLUSTER NODES to determine roles
            try:
                cluster_nodes_response = await redis_client.execute_command("CLUSTER", "NODES")
                
                # Process cluster nodes data if available
                cluster_nodes_data = None
                if isinstance(cluster_nodes_response, (str, bytes)):
                    cluster_nodes_data = cluster_nodes_response
                elif isinstance(cluster_nodes_response, dict):
                    for node_addr, cluster_nodes in cluster_nodes_response.items():
                        if isinstance(cluster_nodes, (str, bytes)):
                            cluster_nodes_data = cluster_nodes
                            break
                
                if cluster_nodes_data:
                    if isinstance(cluster_nodes_data, bytes):
                        cluster_nodes_data = cluster_nodes_data.decode('utf-8')
                    
                    logger.debug(f"Parsing CLUSTER NODES data for roles:\n{cluster_nodes_data}")
                    lines = cluster_nodes_data.splitlines()
                    
                    # Parse roles from cluster nodes data
                    for line in lines:
                        parts = line.split()
                        if len(parts) >= 3:
                            node_id = parts[0]
                            ip_port = parts[1].split("@")[0]
                            flags = parts[2]
                            nodeid_to_addr[node_id] = ip_port
                            
                            # Skip failed nodes
                            if "fail" not in flags:
                                if "master" in flags:
                                    roles[ip_port] = "primary"
                                elif "slave" in flags:
                                    roles[ip_port] = "replica"
                                    # Map replica to its primary
                                    if len(parts) > 3:
                                        primary_id = parts[3]
                                        if primary_id in nodeid_to_addr:
                                            primary_addr = nodeid_to_addr[primary_id]
                                            primary_replica_connections[primary_addr] = ip_port
                    
                    logger.debug(f"Parsed roles from CLUSTER NODES: {roles}")
                else:
                    logger.debug("No cluster nodes data available, assuming all nodes are primaries")
                    
            except Exception as e:
                logger.debug(f"CLUSTER NODES failed ({e}), assuming all nodes are primaries")
            
            # Fill in missing roles (assume primary if not specified)
            for addr in node_list:
                if addr not in roles:
                    roles[addr] = "primary"
                    logger.debug(f"Assuming {addr} is primary (role not found in CLUSTER NODES)")
            
            logger.debug(f"Final node discovery: {len(node_list)} nodes: {node_list}")
            logger.debug(f"Final roles: {roles}")
        
        else:
            # Fallback to CLUSTER NODES command only
            logger.debug("Using CLUSTER NODES command fallback")
            cluster_nodes_response = await redis_client.execute_command("CLUSTER", "NODES")

            node_list = []
            roles = {}
            primary_replica_connections = {}
            nodeid_to_addr = {}

            # Process the cluster nodes response
            logger.debug(f"CLUSTER NODES response type: {type(cluster_nodes_response)}")
            
            # Extract the actual cluster nodes data from the response
            cluster_nodes_data = None
            if isinstance(cluster_nodes_response, (str, bytes)):
                # Direct string/bytes response (from fixed custom_command)
                cluster_nodes_data = cluster_nodes_response
            elif isinstance(cluster_nodes_response, dict):
                # Dictionary response: get the cluster nodes data from any node's response
                # All nodes should return the same cluster topology information
                for node_addr, cluster_nodes in cluster_nodes_response.items():
                    if isinstance(cluster_nodes, (str, bytes)):
                        cluster_nodes_data = cluster_nodes
                        break
            else:
                logger.warning(f"Unexpected CLUSTER NODES response type: {type(cluster_nodes_response)}, falling back to single node")
                node_list = [source_host]
                roles[source_host] = "primary"
            
            # Parse the cluster nodes data to extract all nodes
            if cluster_nodes_data:
                if isinstance(cluster_nodes_data, bytes):
                    cluster_nodes_data = cluster_nodes_data.decode('utf-8')
                
                logger.debug(f"Parsing CLUSTER NODES data:\n{cluster_nodes_data}")
                lines = cluster_nodes_data.splitlines()
                
                # First pass: build nodeid -> ip:port mapping
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 3:
                        node_id = parts[0]
                        ip_port = parts[1].split("@")[0]
                        nodeid_to_addr[node_id] = ip_port
                
                # Second pass: build roles and connections
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 3:
                        node_id = parts[0]
                        ip_port = parts[1].split("@")[0]
                        flags = parts[2]
                        
                        # Skip failed nodes
                        if "fail" not in flags:
                            if ip_port not in node_list:
                                node_list.append(ip_port)
                            
                            if "master" in flags:
                                roles[ip_port] = "primary"
                            elif "slave" in flags:
                                roles[ip_port] = "replica"
                                # Map replica to its primary
                                if len(parts) > 3:
                                    primary_id = parts[3]
                                    if primary_id in nodeid_to_addr:
                                        primary_addr = nodeid_to_addr[primary_id]
                                        primary_replica_connections[primary_addr] = ip_port
                
                logger.debug(f"Discovered {len(node_list)} cluster nodes: {node_list}")
                logger.debug(f"Node roles: {roles}")
            
            # Fallback if no nodes were discovered
            if not node_list:
                logger.warning("No nodes discovered from CLUSTER NODES, falling back to single node")
                node_list = [source_host]
                roles[source_host] = "primary"

        return {
            "cluster_mode": True,
            "primaries": len([r for r in roles.values() if r == "primary"]),
            "replicas": len([r for r in roles.values() if r == "replica"]),
            "node_addresses": node_list,
            "roles": roles,
            "primary_replica_connections": primary_replica_connections
        }
        
    except Exception as e:
        logger.error(f"Failed to get cluster nodes info: {e}")
        # Fallback to standalone mode
        return {
            "cluster_mode": False,
            "primaries": 1,
            "replicas": await get_replica_count(redis_client, source_host, is_cluster=True),
            "node_addresses": [source_host],
            "roles": {source_host: "primary"},
            "primary_replica_connections": {}
        }
        

async def gather_metrics_for_all_nodes(node_addresses, user, password, tls, duration, quiet=False, baseline_traffic=None, roles=None, cluster_mode=None):
    logger.info(f"Starting metrics collection for {len(node_addresses)} nodes")
    baseline = {}
    
    # Default to empty roles if not provided
    if roles is None:
        roles = {}
    
    # Always show collecting metrics status - regardless of quiet mode
    with console.status("[bold green]Collecting initial metrics..."):
        # Parallel collection of initial metrics - use the provided cluster_mode
        tasks = [get_node_metrics(addr, user, password, tls, cluster_mode=cluster_mode, node_role=roles.get(addr)) for addr in node_addresses]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for addr, result in zip(node_addresses, results):
            if isinstance(result, Exception):
                logger.error(f"{fmt_node(addr)} Failed to collect initial metrics: {str(result)}")
            elif result:
                baseline[addr] = result
                logger.debug(f"{fmt_node(addr)} Collected baseline metrics")
                # DEBUG: Show commandstats snapshot that was stored for this node
                if "__commandstats_snapshot" in baseline[addr]:
                    cmdstats = baseline[addr]["__commandstats_snapshot"]
                    logger.debug(f"{fmt_node(addr)} DEBUG_BASELINE: Stored {len(cmdstats)} cmdstat entries")
                    if 'cmdstat_set' in cmdstats:
                        logger.debug(f"{fmt_node(addr)} DEBUG_BASELINE: cmdstat_set = {cmdstats['cmdstat_set']}")
                    else:
                        logger.debug(f"{fmt_node(addr)} DEBUG_BASELINE: No cmdstat_set found")
                else:
                    logger.debug(f"{fmt_node(addr)} DEBUG_BASELINE: No __commandstats_snapshot stored")

    logger.info(f"Waiting {duration} seconds for delta measurement...")
    
    if duration <= 0:
        # Skip waiting if duration is 0 or negative
        logger.debug("Duration is 0 or negative, skipping wait")
    elif quiet:
        # Silent mode - just sleep without status display
        time.sleep(duration)
    else:
        # Normal mode with countdown
        with console.status(f"[bold cyan]Waiting {duration} seconds for delta collection...") as status:
            for remaining in range(duration, 0, -1):
                status.update(f"[bold cyan]Collecting metrics... {remaining}s remaining")
                time.sleep(1)

    logger.info(f"Collecting final metrics from {len(node_addresses)} nodes")
    delta_results = {}
    after_metrics = {}

    # Always show collecting metrics status - regardless of quiet mode
    with console.status("[bold green]Collecting final metrics..."):
        # Parallel collection of final metrics - use the provided cluster_mode
        tasks = [get_node_metrics(addr, user, password, tls, cluster_mode=cluster_mode, node_role=roles.get(addr)) for addr in node_addresses]
        gather_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for addr, result in zip(node_addresses, gather_results):
            if isinstance(result, Exception):
                logger.error(f"{fmt_node(addr)} Failed to collect final metrics: {str(result)}")
            elif result:
                after_metrics[addr] = result
                logger.debug(f"{fmt_node(addr)} Collected final metrics")
                # DEBUG: Show commandstats snapshot that was stored for this node
                if "__commandstats_snapshot" in after_metrics[addr]:
                    cmdstats = after_metrics[addr]["__commandstats_snapshot"]
                    logger.debug(f"{fmt_node(addr)} DEBUG_FINAL: Stored {len(cmdstats)} cmdstat entries")
                    if 'cmdstat_set' in cmdstats:
                        logger.debug(f"{fmt_node(addr)} DEBUG_FINAL: cmdstat_set = {cmdstats['cmdstat_set']}")
                    else:
                        logger.debug(f"{fmt_node(addr)} DEBUG_FINAL: No cmdstat_set found")
                else:
                    logger.debug(f"{fmt_node(addr)} DEBUG_FINAL: No __commandstats_snapshot stored")

    # Process the deltas
    logger.info(f"Processing metrics deltas for {len(node_addresses)} nodes")
    for addr in node_addresses:
        if addr in baseline and addr in after_metrics:
            b = baseline[addr]
            a = after_metrics[addr]

            mem_delta = int(a.get("used_memory", 0)) - int(b.get("used_memory", 0))
            
            # Calculate network deltas with counter wraparound detection
            before_net_in = int(b.get("total_net_input_bytes", 0))
            after_net_in = int(a.get("total_net_input_bytes", 0))
            before_net_out = int(b.get("total_net_output_bytes", 0))
            after_net_out = int(a.get("total_net_output_bytes", 0))
            before_net_repl_in = int(b.get("total_net_repl_input_bytes", 0))
            after_net_repl_in = int(a.get("total_net_repl_input_bytes", 0))
            before_net_repl_out = int(b.get("total_net_repl_output_bytes", 0))
            after_net_repl_out = int(a.get("total_net_repl_output_bytes", 0))
            

            
            # Calculate raw deltas
            raw_net_in_delta = after_net_in - before_net_in
            raw_net_out_delta = after_net_out - before_net_out
            raw_net_repl_in_delta = after_net_repl_in - before_net_repl_in
            raw_net_repl_out_delta = after_net_repl_out - before_net_repl_out
            
            # Check for negative deltas (counter wraparound/reset)
            counter_reset_detected = False
            if raw_net_in_delta < 0 or raw_net_out_delta < 0 or raw_net_repl_in_delta < 0 or raw_net_repl_out_delta < 0:
                counter_reset_detected = True
                logger.warning(f"{fmt_node(addr)} Counter wraparound/reset detected:")
                logger.warning(f"{fmt_node(addr)}   net_in: {before_net_in:,} → {after_net_in:,} (delta: {raw_net_in_delta:,})")
                logger.warning(f"{fmt_node(addr)}   net_out: {before_net_out:,} → {after_net_out:,} (delta: {raw_net_out_delta:,})")
                logger.warning(f"{fmt_node(addr)}   net_repl_in: {before_net_repl_in:,} → {after_net_repl_in:,} (delta: {raw_net_repl_in_delta:,})")
                logger.warning(f"{fmt_node(addr)}   net_repl_out: {before_net_repl_out:,} → {after_net_repl_out:,} (delta: {raw_net_repl_out_delta:,})")
            else:
                logger.debug(f"{fmt_node(addr)} Network deltas calculated successfully")
            
            # Handle counter reset by using instantaneous metrics as fallback
            if counter_reset_detected:
                logger.info(f"{fmt_node(addr)} Using instantaneous metrics as fallback for network calculations")
                
                # Use instantaneous rates to estimate traffic over measurement period
                instantaneous_input_kbps = a.get("instantaneous_input_kbps", 0)
                instantaneous_output_kbps = a.get("instantaneous_output_kbps", 0)
                
                # Convert kbps to bytes and estimate total for duration
                # Note: instantaneous rates are current point-in-time, so this is an approximation
                net_in_delta = int((instantaneous_input_kbps * 1000 / 8) * duration)  # kbps to bytes/sec to total bytes
                net_out_delta = int((instantaneous_output_kbps * 1000 / 8) * duration)
                net_repl_in_delta = 0  # No instantaneous metric available for replication
                net_repl_out_delta = 0  # No instantaneous metric available for replication
                
                logger.info(f"{fmt_node(addr)} Fallback estimates: in={net_in_delta:,}B, out={net_out_delta:,}B over {duration}s")
                logger.info(f"{fmt_node(addr)} Based on instantaneous rates: in={instantaneous_input_kbps}kbps, out={instantaneous_output_kbps}kbps")
            else:
                # Use calculated deltas when no counter reset detected
                net_in_delta = raw_net_in_delta
                net_out_delta = raw_net_out_delta
                net_repl_in_delta = raw_net_repl_in_delta
                net_repl_out_delta = raw_net_repl_out_delta
            
            # Subtract baseline traffic if available
            # Standard ElastiCache baseline is ~28K bytes/sec per node for background operations
            baseline_traffic_bytes = 0
            if baseline_traffic and addr in baseline_traffic:
                baseline_rate_per_sec = baseline_traffic[addr]
                baseline_traffic_bytes = int(baseline_rate_per_sec * duration)
                logger.debug(f"{fmt_node(addr)} Baseline traffic: {baseline_rate_per_sec:.0f} bytes/sec × {duration}s = {baseline_traffic_bytes:,} bytes")
            elif baseline_traffic is None:
                # Default baseline for ElastiCache nodes (~28K bytes/sec)
                default_baseline_per_sec = 28000
                baseline_traffic_bytes = int(default_baseline_per_sec * duration)
                logger.debug(f"{fmt_node(addr)} Using default baseline: {default_baseline_per_sec} bytes/sec × {duration}s = {baseline_traffic_bytes:,} bytes")
            
            # Store the final calculated deltas for baseline adjustment
            final_net_out_delta = net_out_delta
            final_net_in_delta = net_in_delta
            
            # Apply baseline subtraction to outbound traffic (most significant for capacity planning)
            # Only apply baseline when we have reliable delta measurements (no counter reset)
            if baseline_traffic_bytes > 0 and not counter_reset_detected:
                net_out_delta = max(0, net_out_delta - baseline_traffic_bytes)
                # For inbound traffic, apply a smaller baseline (typically ~40% of outbound)
                baseline_in_bytes = int(baseline_traffic_bytes * 0.4)
                net_in_delta = max(0, net_in_delta - baseline_in_bytes)
                
                logger.debug(f"{fmt_node(addr)} Baseline adjustment:")
                logger.debug(f"{fmt_node(addr)}   Raw out: {final_net_out_delta:,} → Client out: {net_out_delta:,} (subtracted {baseline_traffic_bytes:,})")
                logger.debug(f"{fmt_node(addr)}   Raw in: {final_net_in_delta:,} → Client in: {net_in_delta:,} (subtracted {baseline_in_bytes:,})")
            elif counter_reset_detected:
                logger.info(f"{fmt_node(addr)} Skipping baseline adjustment due to counter reset - using fallback estimates as-is")

            # Cross-validation using additional metrics from Valkey INFO documentation
            total_commands_delta = int(a.get("total_commands_processed", 0)) - int(b.get("total_commands_processed", 0))
            instantaneous_ops_before = b.get("instantaneous_ops_per_sec", 0)
            instantaneous_ops_after = a.get("instantaneous_ops_per_sec", 0)
            instantaneous_input_kbps_after = a.get("instantaneous_input_kbps", 0)
            instantaneous_output_kbps_after = a.get("instantaneous_output_kbps", 0)

            # Get commandstats from both snapshots
            before_cmdstats = b.get("__commandstats_snapshot", {})
            after_cmdstats = a.get("__commandstats_snapshot", {})

            # DEBUG: Show cmdstats collection info
            logger.debug(f"{fmt_node(addr)} DEBUG_DELTA: ===============================")
            logger.debug(f"{fmt_node(addr)} DEBUG_DELTA: Calculating deltas for node {addr}")
            logger.debug(f"{fmt_node(addr)} DEBUG_DELTA: Before cmdstats keys: {len(before_cmdstats)}")
            logger.debug(f"{fmt_node(addr)} DEBUG_DELTA: After cmdstats keys: {len(after_cmdstats)}")
            
            # Check for cmdstat_set specifically in both snapshots
            if 'cmdstat_set' in before_cmdstats:
                logger.debug(f"{fmt_node(addr)} DEBUG_DELTA: BEFORE cmdstat_set: {before_cmdstats['cmdstat_set']}")
            else:
                logger.debug(f"{fmt_node(addr)} DEBUG_DELTA: BEFORE cmdstat_set: NOT FOUND")
                
            if 'cmdstat_set' in after_cmdstats:
                logger.debug(f"{fmt_node(addr)} DEBUG_DELTA: AFTER cmdstat_set: {after_cmdstats['cmdstat_set']}")
            else:
                logger.debug(f"{fmt_node(addr)} DEBUG_DELTA: AFTER cmdstat_set: NOT FOUND")
            
            logger.debug(f"{fmt_node(addr)} Cmdstats debug:")
            logger.debug(f"{fmt_node(addr)}   Before cmdstats keys: {len(before_cmdstats)}")
            logger.debug(f"{fmt_node(addr)}   After cmdstats keys: {len(after_cmdstats)}")
            
            # Check for read commands with activity
            active_read_commands = []
            for cmd_name in READ_COMMANDS:
                cmdstat_key = f"cmdstat_{cmd_name}"
                if cmdstat_key in before_cmdstats and cmdstat_key in after_cmdstats:
                    before_calls = before_cmdstats[cmdstat_key].get('calls', 0)
                    after_calls = after_cmdstats[cmdstat_key].get('calls', 0)
                    before_rejected = before_cmdstats[cmdstat_key].get('rejected_calls', 0)
                    after_rejected = after_cmdstats[cmdstat_key].get('rejected_calls', 0)
                    successful_delta = after_calls - before_calls
                    rejected_delta = after_rejected - before_rejected
                    total_delta = successful_delta + rejected_delta
                    if total_delta >= 1:
                        active_read_commands.append((cmd_name, successful_delta, rejected_delta, total_delta))
                elif cmdstat_key in after_cmdstats:
                    calls = after_cmdstats[cmdstat_key].get('calls', 0)
                    rejected = after_cmdstats[cmdstat_key].get('rejected_calls', 0)
                    total_delta = calls + rejected
                    if total_delta >= 1:
                        active_read_commands.append((cmd_name, calls, rejected, total_delta))
            
            # Check for write commands with activity
            active_write_commands = []
            for cmd_name in WRITE_COMMANDS:
                cmdstat_key = f"cmdstat_{cmd_name}"
                if cmdstat_key in before_cmdstats and cmdstat_key in after_cmdstats:
                    before_calls = before_cmdstats[cmdstat_key].get('calls', 0)
                    after_calls = after_cmdstats[cmdstat_key].get('calls', 0)
                    before_rejected = before_cmdstats[cmdstat_key].get('rejected_calls', 0)
                    after_rejected = after_cmdstats[cmdstat_key].get('rejected_calls', 0)
                    successful_delta = after_calls - before_calls
                    rejected_delta = after_rejected - before_rejected
                    total_delta = successful_delta + rejected_delta
                    if total_delta >= 1:
                        active_write_commands.append((cmd_name, successful_delta, rejected_delta, total_delta))
                elif cmdstat_key in after_cmdstats:
                    calls = after_cmdstats[cmdstat_key].get('calls', 0)
                    rejected = after_cmdstats[cmdstat_key].get('rejected_calls', 0)
                    total_delta = calls + rejected
                    if total_delta >= 1:
                        active_write_commands.append((cmd_name, calls, rejected, total_delta))
            
            # Log active read commands
            if active_read_commands:
                logger.debug(f"{fmt_node(addr)}   Read commands with activity:")
                for cmd_name, successful, rejected, total in sorted(active_read_commands, key=lambda x: x[3], reverse=True):
                    if rejected > 0:
                        logger.debug(f"{fmt_node(addr)}     {cmd_name}: {successful} successful, {rejected} rejected ({total} total attempts)")
                    else:
                        logger.debug(f"{fmt_node(addr)}     {cmd_name}: {successful} successful ({total} total attempts)")
            else:
                logger.debug(f"{fmt_node(addr)}   No read commands found with activity")
            
            # Log active write commands
            if active_write_commands:
                logger.debug(f"{fmt_node(addr)}   Write commands with activity:")
                for cmd_name, successful, rejected, total in sorted(active_write_commands, key=lambda x: x[3], reverse=True):
                    if rejected > 0:
                        logger.debug(f"{fmt_node(addr)}     {cmd_name}: {successful} successful, {rejected} rejected ({total} total attempts)")
                    else:
                        logger.debug(f"{fmt_node(addr)}     {cmd_name}: {successful} successful ({total} total attempts)")
            else:
                logger.debug(f"{fmt_node(addr)}   No write commands found with activity")

            # Calculate command deltas (including rejected calls for network traffic calculation)
            cmd_deltas = {}
            for cmd in set(before_cmdstats.keys()) | set(after_cmdstats.keys()):
                before_calls = before_cmdstats.get(cmd, {}).get("calls", 0)
                after_calls = after_cmdstats.get(cmd, {}).get("calls", 0)
                before_rejected = before_cmdstats.get(cmd, {}).get("rejected_calls", 0)
                after_rejected = after_cmdstats.get(cmd, {}).get("rejected_calls", 0)
                
                successful_delta = after_calls - before_calls
                rejected_delta = after_rejected - before_rejected
                total_attempts_delta = successful_delta + rejected_delta
                
                if total_attempts_delta > 0:
                    cmd_deltas[cmd] = {
                        "calls": successful_delta,
                        "rejected_calls": rejected_delta,
                        "total_attempts": total_attempts_delta
                    }

            # Summarize read/write deltas
            summary = summarize_read_write(cmd_deltas)
            
            # Get node role for context early
            node_role = roles.get(addr, "unknown")
            
            # Cross-validation logging
            cmdstats_total_ops = summary["total_read_ops"] + summary["total_write_ops"]
            if cmdstats_total_ops > 0 or total_commands_delta > 0:
                logger.debug(f"{fmt_node(addr)} Cross-validation:")
                logger.debug(f"{fmt_node(addr)}   Command deltas: {summary['total_read_ops']} reads, {summary['total_write_ops']} writes, {cmdstats_total_ops} total")
                logger.debug(f"{fmt_node(addr)}   total_commands_processed: before={b.get('total_commands_processed', 0)}, after={a.get('total_commands_processed', 0)}, delta={total_commands_delta}")
                logger.debug(f"{fmt_node(addr)}   Network deltas: in={net_in_delta:,}B, out={net_out_delta:,}B")
                logger.debug(f"{fmt_node(addr)}   Instantaneous ops: before={instantaneous_ops_before}, after={instantaneous_ops_after}")
                logger.debug(f"{fmt_node(addr)}   Instantaneous network: in={instantaneous_input_kbps_after}kbps, out={instantaneous_output_kbps_after}kbps")
                
                # Show raw command stats for debugging
                logger.debug(f"{fmt_node(addr)}   Raw command breakdown: {summary.get('write_ops_breakdown', {})}")
                logger.debug(f"{fmt_node(addr)}   Background commands: {summary.get('background_ops_breakdown', {})}")
                
                # Show breakdown of successful vs rejected calls for major commands
                if cmd_deltas:
                    major_commands = {k: v for k, v in cmd_deltas.items() if v.get("total_attempts", 0) > 100}
                    if major_commands:
                        logger.debug(f"{fmt_node(addr)}   Command success/rejection breakdown:")
                        for cmd, stats in major_commands.items():
                            successful = stats.get("calls", 0)
                            rejected = stats.get("rejected_calls", 0)
                            total = stats.get("total_attempts", 0)
                            if rejected > 0:
                                logger.debug(f"{fmt_node(addr)}     {cmd}: {successful} successful, {rejected} rejected ({total} total attempts)")
                            else:
                                logger.debug(f"{fmt_node(addr)}     {cmd}: {successful} successful ({total} total attempts)")
                
                # For replicas, also show the replication context
                if node_role == "replica" and summary.get('write_ops_breakdown', {}):
                    logger.debug(f"{fmt_node(addr)}   Note: Replica write commands are replication traffic, not client operations")
                
                # Check for discrepancies - handle replica replication separately
                
                # Only warn if we have application commands but they don't match total_commands_processed
                # If cmdstats_total_ops = 0, this means only background commands were processed (expected)
                if cmdstats_total_ops > 0 and abs(cmdstats_total_ops - total_commands_delta) > (cmdstats_total_ops * 0.1):  # 10% tolerance
                    if node_role == "replica":
                        # For replicas, large cmdstats with low total_commands_processed is normal (replication traffic)
                        logger.debug(f"{fmt_node(addr)} Replica replication detected: {cmdstats_total_ops} replicated commands, {total_commands_delta} direct client commands")
                    else:
                        # For primaries, this is a genuine discrepancy
                        logger.warning(f"{fmt_node(addr)} Command count discrepancy: cmdstats={cmdstats_total_ops}, total_commands_processed={total_commands_delta}")
                        logger.warning(f"{fmt_node(addr)}    This suggests a timing issue or different measurement scopes between metrics")
                elif cmdstats_total_ops == 0 and total_commands_delta > 0:
                    # Check if this is because COMMANDSTATS is unavailable (e.g., ElastiCache)
                    if not cmd_deltas:  # No commandstats data available
                        logger.debug(f"{fmt_node(addr)} COMMANDSTATS unavailable - using total_commands_processed fallback")
                        # For ElastiCache, estimate read/write split based on typical patterns
                        # Most workloads are read-heavy, assume 70% reads, 30% writes as default
                        estimated_reads = int(total_commands_delta * 0.7)
                        estimated_writes = int(total_commands_delta * 0.3)
                        logger.debug(f"{fmt_node(addr)} Estimated operations: {estimated_reads} reads, {estimated_writes} writes (70/30 split)")
                        
                        # Update the summary to include these estimated values
                        summary["total_read_ops"] = estimated_reads
                        summary["total_write_ops"] = estimated_writes
                        summary["estimated_from_total_commands"] = True
                    else:
                        logger.debug(f"{fmt_node(addr)} No application workload detected: {total_commands_delta} background commands processed")
            else:
                logger.debug(f"{fmt_node(addr)} {summary['total_read_ops']} reads, {summary['total_write_ops']} writes, {mem_delta:,} bytes memory change")

            # DEBUG: Log replication metrics for this node
            logger.debug(f"{fmt_node(addr)} Replication metrics:")
            logger.debug(f"{fmt_node(addr)}   Role: {node_role}")
            logger.debug(f"{fmt_node(addr)}   net_repl_out_bytes delta: {net_repl_out_delta:,} bytes")
            logger.debug(f"{fmt_node(addr)}   net_repl_in_bytes delta: {net_repl_in_delta:,} bytes")
            if net_repl_out_delta > 0:
                logger.debug(f"{fmt_node(addr)}   Replication out rate: {net_repl_out_delta / duration:,.0f} bytes/sec")
            if net_repl_in_delta > 0:
                logger.debug(f"{fmt_node(addr)}   Replication in rate: {net_repl_in_delta / duration:,.0f} bytes/sec")

            # Store deltas in the results
            logger.debug(f"{fmt_node(addr)} DEBUG_DELTA: Final summary for {addr}:")
            logger.debug(f"{fmt_node(addr)} DEBUG_DELTA:   total_read_ops: {summary['total_read_ops']}")
            logger.debug(f"{fmt_node(addr)} DEBUG_DELTA:   total_write_ops: {summary['total_write_ops']}")
            logger.debug(f"{fmt_node(addr)} DEBUG_DELTA:   write_ops_breakdown: {summary.get('write_ops_breakdown', {})}")
            logger.debug(f"{fmt_node(addr)} DEBUG_DELTA: ===============================")
            
            delta_results[addr] = {
                "memory_delta": mem_delta,
                "net_in_bytes": net_in_delta,
                "net_out_bytes": net_out_delta,
                "net_repl_in_bytes": net_repl_in_delta,
                "net_repl_out_bytes": net_repl_out_delta,
                "duration_seconds": duration,
                "command_deltas": cmd_deltas,
                "commandstats_total_read_ops": summary["total_read_ops"],
                "commandstats_total_write_ops": summary["total_write_ops"],
                "commandstats_total_background_ops": summary["total_background_ops"],
                "estimated_from_total_commands": summary.get("estimated_from_total_commands", False),
                # Store values under the keys expected by aggregation code
                "total_read_ops": summary["total_read_ops"],
                "total_write_ops": summary["total_write_ops"],
                "connected_clients": a.get("connected_clients", "N/A"),
                "total_keys_all_dbs": a.get("total_keys_all_dbs", "N/A"),
                # Add cross-validation metrics
                "total_commands_processed_delta": total_commands_delta,
                "instantaneous_ops_before": instantaneous_ops_before,
                "instantaneous_ops_after": instantaneous_ops_after,
                "instantaneous_input_kbps_after": instantaneous_input_kbps_after,
                "instantaneous_output_kbps_after": instantaneous_output_kbps_after,
                # Add raw metrics for reference
                "raw_net_in_bytes": raw_net_in_delta if not counter_reset_detected else final_net_in_delta,
                "raw_net_out_bytes": raw_net_out_delta if not counter_reset_detected else final_net_out_delta,
                "counter_reset_detected": counter_reset_detected,
                "baseline_traffic_subtracted": baseline_traffic_bytes,
                "__delta_info": {
                    "duration_seconds": duration,
                    "total_write_ops": summary["total_write_ops"],
                    "total_read_ops": summary["total_read_ops"],
                    "total_background_ops": summary["total_background_ops"],
                    "net_in_bytes": net_in_delta,
                    "net_out_bytes": net_out_delta,
                    "net_repl_in_bytes": net_repl_in_delta,
                    "net_repl_out_bytes": net_repl_out_delta,
                    "repl_backlog_bytes": a.get("repl_backlog_bytes", 0),
                    "raw_net_in_bytes": raw_net_in_delta if not counter_reset_detected else final_net_in_delta,
                    "raw_net_out_bytes": raw_net_out_delta if not counter_reset_detected else final_net_out_delta,
                    "counter_reset_detected": counter_reset_detected,
                    "baseline_traffic_subtracted": baseline_traffic_bytes
                }
            }

        else:
            logger.warning(f"{fmt_node(addr)} Skipping - missing baseline or final metrics")

    logger.info(f"Metrics collection completed for {len(delta_results)} nodes")
    return delta_results


async def gather_metrics_efficiently_for_cluster(node_addresses, user, password, tls, duration, quiet=False, baseline_traffic=None, roles=None):
    """
    Efficiently gather metrics for cluster using single cluster client
    This fixes the data attribution issue by using valkey-glide's built-in cluster data organization
    """
    logger.info(f"Starting efficient cluster metrics collection for {len(node_addresses)} nodes")
    baseline = {}
    
    # Default to empty roles if not provided
    if roles is None:
        roles = {}
    
    # Create single cluster client using first node as entry point
    first_node = node_addresses[0]
    host, port = first_node.split(":")
    port = int(port)
    
    config = build_connection_config(host, port, user, password, tls, cluster_mode=True)
    cluster_client = AsyncRedisCluster(**config)
    
    logger.info("Collecting baseline metrics from all nodes via single cluster client...")
    
    # Always show collecting metrics status - regardless of quiet mode
    with console.status("[bold green]Collecting initial metrics..."):
        try:
            # Get cluster-wide info in one call - automatically organized by node
            server_info_raw = await cluster_client.info("server")
            memory_info_raw = await cluster_client.info("memory")
            persistence_info_raw = await cluster_client.info("persistence")
            stats_info_raw = await cluster_client.info("stats")
            clients_info_raw = await cluster_client.info("clients")
            keyspace_info_raw = await cluster_client.info("keyspace")
            
            # Get commandstats using custom command for ElastiCache compatibility
            commandstats_raw = await cluster_client.execute_command("INFO", "COMMANDSTATS")
            
            logger.debug(f"Cluster client returned data for {len(server_info_raw)} nodes")
            
            # Process each node's data from the cluster-wide response
            for node_addr in node_addresses:
                if node_addr in server_info_raw:
                    logger.debug(f"{fmt_node(node_addr)} Processing cluster-collected data")
                    
                    # Parse each section for this node
                    node_data = {}
                    
                    # Server info
                    server_data = parse_info_response(server_info_raw[node_addr])
                    node_data.update(server_data)
                    
                    # Memory info
                    memory_data = parse_info_response(memory_info_raw[node_addr])
                    node_data.update(memory_data)
                    
                    # Persistence info
                    persistence_data = parse_info_response(persistence_info_raw[node_addr])
                    node_data.update(persistence_data)
                    
                    # Stats info
                    stats_data = parse_info_response(stats_info_raw[node_addr])
                    node_data.update(stats_data)
                    
                    # Clients info
                    clients_data = parse_info_response(clients_info_raw[node_addr])
                    node_data.update(clients_data)
                    
                    # Keyspace info
                    keyspace_data = parse_info_response(keyspace_info_raw[node_addr])
                    node_data["keyspace_info"] = keyspace_data
                    total_keys = sum(info.get("keys", 0) for info in keyspace_data.values() if isinstance(info, dict))
                    node_data["total_keys"] = total_keys
                    
                    # Commandstats for this node
                    if node_addr in commandstats_raw:
                        commandstats_data = parse_info_response(commandstats_raw[node_addr])
                        parsed_commandstats = parse_commandstats(commandstats_data)
                        node_data["__commandstats_snapshot"] = parsed_commandstats
                    else:
                        node_data["__commandstats_snapshot"] = {}
                    
                    # Add derived metrics
                    node_data["total_net_input_bytes"] = stats_data.get("total_net_input_bytes", 0)
                    node_data["total_net_output_bytes"] = stats_data.get("total_net_output_bytes", 0)
                    node_data["total_net_repl_input_bytes"] = stats_data.get("total_net_repl_input_bytes", 0)
                    node_data["total_net_repl_output_bytes"] = stats_data.get("total_net_repl_output_bytes", 0)
                    node_data["total_commands_processed"] = stats_data.get("total_commands_processed", 0)
                    node_data["instantaneous_ops_per_sec"] = stats_data.get("instantaneous_ops_per_sec", 0)
                    
                    # Store baseline for this node
                    baseline[node_addr] = node_data
                    
                    logger.debug(f"{fmt_node(node_addr)} Collected baseline metrics efficiently")
                    
                    # DEBUG: Show commandstats snapshot
                    if "__commandstats_snapshot" in baseline[node_addr]:
                        cmdstats = baseline[node_addr]["__commandstats_snapshot"]
                        logger.debug(f"{fmt_node(node_addr)} DEBUG_BASELINE: Stored {len(cmdstats)} cmdstat entries")
                        if 'cmdstat_set' in cmdstats:
                            logger.debug(f"{fmt_node(node_addr)} DEBUG_BASELINE: cmdstat_set = {cmdstats['cmdstat_set']}")
                        else:
                            logger.debug(f"{fmt_node(node_addr)} DEBUG_BASELINE: No cmdstat_set found")
                    else:
                        logger.debug(f"{fmt_node(node_addr)} DEBUG_BASELINE: No __commandstats_snapshot stored")
                else:
                    logger.error(f"{fmt_node(node_addr)} No data found in cluster response")
                    
        except Exception as e:
            logger.error(f"Failed to collect cluster metrics: {e}")
            await cluster_client.aclose()
            raise
    
    logger.info(f"Waiting {duration} seconds for delta measurement...")
    
    if duration <= 0:
        # Skip waiting if duration is 0 or negative
        logger.debug("Duration is 0 or negative, skipping wait")
    elif quiet:
        # Silent mode - just sleep without status display
        time.sleep(duration)
    else:
        # Normal mode with countdown
        with console.status(f"[bold cyan]Waiting {duration} seconds for delta collection...") as status:
            for remaining in range(duration, 0, -1):
                status.update(f"[bold cyan]Collecting metrics... {remaining}s remaining")
                time.sleep(1)
    
    # Collect final metrics
    final = {}
    with console.status("[bold green]Collecting final metrics..."):
        try:
            # Get cluster-wide info again
            server_info_raw = await cluster_client.info("server")
            memory_info_raw = await cluster_client.info("memory")
            persistence_info_raw = await cluster_client.info("persistence")
            stats_info_raw = await cluster_client.info("stats")
            clients_info_raw = await cluster_client.info("clients")
            keyspace_info_raw = await cluster_client.info("keyspace")
            
            # Get commandstats using custom command for ElastiCache compatibility
            commandstats_raw = await cluster_client.execute_command("INFO", "COMMANDSTATS")
            
            # Process each node's final data
            for node_addr in node_addresses:
                if node_addr in server_info_raw:
                    logger.debug(f"{fmt_node(node_addr)} Processing final cluster-collected data")
                    
                    # Parse each section for this node
                    node_data = {}
                    
                    # Server info
                    server_data = parse_info_response(server_info_raw[node_addr])
                    node_data.update(server_data)
                    
                    # Memory info
                    memory_data = parse_info_response(memory_info_raw[node_addr])
                    node_data.update(memory_data)
                    
                    # Persistence info
                    persistence_data = parse_info_response(persistence_info_raw[node_addr])
                    node_data.update(persistence_data)
                    
                    # Stats info
                    stats_data = parse_info_response(stats_info_raw[node_addr])
                    node_data.update(stats_data)
                    
                    # Clients info
                    clients_data = parse_info_response(clients_info_raw[node_addr])
                    node_data.update(clients_data)
                    
                    # Keyspace info
                    keyspace_data = parse_info_response(keyspace_info_raw[node_addr])
                    node_data["keyspace_info"] = keyspace_data
                    total_keys = sum(info.get("keys", 0) for info in keyspace_data.values() if isinstance(info, dict))
                    node_data["total_keys"] = total_keys
                    
                    # Commandstats for this node
                    if node_addr in commandstats_raw:
                        commandstats_data = parse_info_response(commandstats_raw[node_addr])
                        parsed_commandstats = parse_commandstats(commandstats_data)
                        node_data["__commandstats_snapshot"] = parsed_commandstats
                    else:
                        node_data["__commandstats_snapshot"] = {}
                    
                    # Add derived metrics
                    node_data["total_net_input_bytes"] = stats_data.get("total_net_input_bytes", 0)
                    node_data["total_net_output_bytes"] = stats_data.get("total_net_output_bytes", 0)
                    node_data["total_net_repl_input_bytes"] = stats_data.get("total_net_repl_input_bytes", 0)
                    node_data["total_net_repl_output_bytes"] = stats_data.get("total_net_repl_output_bytes", 0)
                    node_data["total_commands_processed"] = stats_data.get("total_commands_processed", 0)
                    node_data["instantaneous_ops_per_sec"] = stats_data.get("instantaneous_ops_per_sec", 0)
                    
                    # Store final for this node
                    final[node_addr] = node_data
                    
                    logger.debug(f"{fmt_node(node_addr)} Collected final metrics efficiently")
                    
                    # DEBUG: Show commandstats snapshot
                    if "__commandstats_snapshot" in final[node_addr]:
                        cmdstats = final[node_addr]["__commandstats_snapshot"]
                        logger.debug(f"{fmt_node(node_addr)} DEBUG_FINAL: Stored {len(cmdstats)} cmdstat entries")
                        if 'cmdstat_set' in cmdstats:
                            logger.debug(f"{fmt_node(node_addr)} DEBUG_FINAL: cmdstat_set = {cmdstats['cmdstat_set']}")
                        else:
                            logger.debug(f"{fmt_node(node_addr)} DEBUG_FINAL: No cmdstat_set found")
                    else:
                        logger.debug(f"{fmt_node(node_addr)} DEBUG_FINAL: No __commandstats_snapshot stored")
                else:
                    logger.error(f"{fmt_node(node_addr)} No data found in final cluster response")
                    
        except Exception as e:
            logger.error(f"Failed to collect final cluster metrics: {e}")
            raise
        finally:
            await cluster_client.aclose()
    
    # Calculate deltas per node using existing delta calculation logic
    deltas = {}
    logger.info(f"Processing metrics deltas for {len(node_addresses)} nodes")
    for node_addr in node_addresses:
        if node_addr in baseline and node_addr in final:
            b = baseline[node_addr]
            a = final[node_addr]
            
            # Use existing delta calculation logic from gather_metrics_for_all_nodes
            delta_result = calculate_delta(b, a, duration, node_addr)
            deltas[node_addr] = delta_result
            
            logger.info(f"{fmt_node(node_addr)} Calculated delta: {delta_result.get('writes_per_second', 0)} writes/sec, {delta_result.get('reads_per_second', 0)} reads/sec")
    
    logger.info(f"Efficient cluster metrics collection complete for {len(deltas)} nodes")
    return baseline, final, deltas


async def collect_info_from_multiple_standalone_nodes(primary_client, source_host, node_addresses, section, user=None, password=None, tls=False):
    """Collect INFO section from multiple standalone nodes (primary + replicas)"""
    result = {}
    
    for addr in node_addresses:
        logger.debug(f"Collecting {section} info from standalone node {addr}")
        
        # For the primary node, use the existing client
        if addr == source_host:
            node_info = await collect_info_from_standalone(primary_client, addr, section)
            result.update(node_info)
        else:
            # For replica nodes, create individual connections
            host, port = addr.split(":")
            port = int(port)
            replica_config = build_connection_config(host, port, user, password, tls, cluster_mode=False)
            replica_client = redis_async.Redis(**replica_config)
            
            try:
                # INFO commands work on replicas without READONLY command
                node_info = await collect_info_from_standalone(replica_client, addr, section)
                result.update(node_info)
            except Exception as e:
                logger.warning(f"Failed to collect {section} from replica {addr}: {e}")
            finally:
                await replica_client.aclose()
    
    return result

async def collect_commandstats_from_multiple_standalone_nodes(primary_client, source_host, node_addresses, user=None, password=None, tls=False):
    """Collect COMMANDSTATS from multiple standalone nodes (primary + replicas)"""
    result = {}
    
    for addr in node_addresses:
        logger.debug(f"Collecting commandstats from standalone node {addr}")
        
        # For the primary node, use the existing client
        if addr == source_host:
            node_stats = await collect_commandstats_from_standalone(primary_client, addr)
            result.update(node_stats)
        else:
            # For replica nodes, create individual connections
            host, port = addr.split(":")
            port = int(port)
            replica_config = build_connection_config(host, port, user, password, tls, cluster_mode=False)
            replica_client = redis_async.Redis(**replica_config)
            
            try:
                # INFO commands work on replicas without READONLY command
                node_stats = await collect_commandstats_from_standalone(replica_client, addr)
                result.update(node_stats)
            except Exception as e:
                logger.warning(f"Failed to collect commandstats from replica {addr}: {e}")
            finally:
                await replica_client.aclose()
    
    return result

async def collect_metrics_native(redis_client, source_host, is_cluster, node_addresses, duration, quiet=False, roles=None, user=None, password=None, tls=False):
    """
    Collect metrics from all nodes using redis-py native patterns.
    
    Args:
        redis_client: Raw redis.asyncio.Redis or AsyncRedisCluster client
        source_host: The source host:port string  
        is_cluster: Boolean indicating if this is a cluster
        node_addresses: List of node addresses to collect from
        duration: How long to sleep between baseline and final measurements
        quiet: Whether to suppress progress output
        roles: Optional dict mapping node addresses to their roles
        
    Returns:
        Tuple of (baseline_metrics, final_metrics, delta_summary)
    """
    
    # Collect baseline metrics
    logger.info("Collecting baseline metrics from all nodes...")
    baseline_metrics = {}
    
    with console.status("[bold green]Collecting initial metrics..."):
        try:
            # Use redis-py native patterns instead of unified wrapper
            # For single-node clusters, use standalone collection even if is_cluster=True
            cluster_nodes = []
            if is_cluster:
                try:
                    cluster_nodes = redis_client.get_nodes()
                except:
                    cluster_nodes = []
            
            if is_cluster and len(cluster_nodes) > 0:
                # For multi-node cluster: collect from each node directly
                server_info = await collect_info_from_cluster(redis_client, "server")
                memory_info = await collect_info_from_cluster(redis_client, "memory")
                persistence_info = await collect_info_from_cluster(redis_client, "persistence")
                stats_info = await collect_info_from_cluster(redis_client, "stats")
                clients_info = await collect_info_from_cluster(redis_client, "clients")
                keyspace_info = await collect_info_from_cluster(redis_client, "keyspace")
                commandstats_info = await collect_commandstats_from_cluster(redis_client)
            else:
                # For standalone mode: collect from all nodes (primary + any replicas)
                server_info = await collect_info_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, "server", user, password, tls)
                memory_info = await collect_info_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, "memory", user, password, tls)
                persistence_info = await collect_info_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, "persistence", user, password, tls)
                stats_info = await collect_info_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, "stats", user, password, tls)
                clients_info = await collect_info_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, "clients", user, password, tls)
                keyspace_info = await collect_info_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, "keyspace", user, password, tls)
                commandstats_info = await collect_commandstats_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, user, password, tls)
            
            # Debug: Show what keys we got back
            logger.debug(f"Server info keys: {list(server_info.keys())}")
            logger.debug(f"Stats info keys: {list(stats_info.keys())}")
            logger.debug(f"Expected node addresses: {node_addresses}")
            
            # MULTI-NODE CLUSTER DETECTION: If we collected from more nodes than discovered, we have a multi-node cluster
            actual_nodes = list(server_info.keys())
            if len(actual_nodes) > len(node_addresses) and is_cluster:
                logger.info(f"Multi-node cluster detected! Collected from {len(actual_nodes)} nodes but only expected {len(node_addresses)}")
                logger.info(f"Updating node list from {node_addresses} to {actual_nodes}")
                node_addresses = actual_nodes
                
                # Store updated cluster data for later use
                # Get actual roles from CLUSTER NODES command
                collected_roles = {}
                primary_count = 0
                replica_count = 0
                
                try:
                    # Query cluster topology to get actual primary/replica roles
                    logger.debug("Querying CLUSTER NODES for accurate role information")
                    
                    # First, attempt via the current client (cluster client)
                    try:
                        cluster_nodes_response = await redis_client.execute_command("CLUSTER", "NODES")
                    except Exception as ex:
                        # redis-py may throw parsing errors (e.g., range() arg 3 must not be zero)
                        # or cluster routing errors. Fall back to a direct standalone connection
                        # against a real node using the same auth/TLS settings.
                        logger.debug(f"CLUSTER NODES via cluster client failed: {ex} — attempting standalone against a real node")
                        import redis.asyncio as redis_async
                        # Prefer a discovered node; otherwise, use the provided source_host
                        try:
                            if actual_nodes:
                                target_host, target_port_str = actual_nodes[0].split(":")
                            else:
                                target_host, target_port_str = source_host.split(":")
                            target_port = int(target_port_str)
                        except Exception:
                            target_host = source_host.split(":")[0]
                            target_port = int(source_host.split(":")[1])
                        # Build a proper standalone config honoring credentials/TLS
                        temp_config = build_connection_config(target_host, target_port, user, password, tls, socket_timeout=5000, cluster_mode=False)
                        temp_client = redis_async.Redis(**temp_config)
                        try:
                            cluster_nodes_response = await temp_client.execute_command("CLUSTER", "NODES")
                        finally:
                            await temp_client.aclose()
                    
                    if isinstance(cluster_nodes_response, (str, bytes)):
                        if isinstance(cluster_nodes_response, bytes):
                            cluster_nodes_response = cluster_nodes_response.decode('utf-8')
                        
                        logger.debug(f"CLUSTER NODES response for role detection:\n{cluster_nodes_response}")
                        lines = cluster_nodes_response.splitlines()
                        
                        # Parse each node's role from CLUSTER NODES output
                        for line in lines:
                            parts = line.split()
                            if len(parts) >= 3:
                                ip_port = parts[1].split("@")[0]
                                flags = parts[2]
                                
                                # Normalize 127.0.0.1 to localhost for consistency  
                                if ip_port.startswith("127.0.0.1:"):
                                    ip_port = ip_port.replace("127.0.0.1:", "localhost:")
                                
                                # Skip failed nodes
                                if "fail" not in flags and ip_port in actual_nodes:
                                    if ("master" in flags) or ("primary" in flags):
                                        collected_roles[ip_port] = "primary"
                                        primary_count += 1
                                        logger.debug(f"Node {ip_port} identified as primary from CLUSTER NODES")
                                    elif ("slave" in flags) or ("replica" in flags):
                                        collected_roles[ip_port] = "replica"
                                        replica_count += 1
                                        logger.debug(f"Node {ip_port} identified as replica from CLUSTER NODES")
                        
                        logger.info(f"CLUSTER NODES role detection: {primary_count} primaries, {replica_count} replicas")
                        
                except Exception as e:
                    # Any failure to obtain CLUSTER NODES should fall back to INFO-based detection.
                    logger.warning(f"CLUSTER NODES role detection failed: {e}")
                    logger.info("Falling back to INFO-based role detection on each node")
                    try:
                        await _detect_elasticache_roles(actual_nodes, collected_roles, user, password, tls)
                        # Count the roles
                        primary_count = sum(1 for role in collected_roles.values() if role == "primary")
                        replica_count = sum(1 for role in collected_roles.values() if role == "replica")
                        logger.info(f"ElastiCache role detection: {primary_count} primaries, {replica_count} replicas")
                    except Exception as role_e:
                        logger.warning(f"INFO-based role detection also failed: {role_e}")
                        # Mark all nodes as unknown since we can't determine roles reliably
                        for node_addr in actual_nodes:
                            collected_roles[node_addr] = "unknown"
                
                updated_cluster_info = {
                    "cluster_mode": True,
                    "primaries": primary_count,
                    "replicas": replica_count,
                    "node_addresses": actual_nodes,
                    "roles": collected_roles
                }
                # Store it in baseline_metrics for later retrieval
                baseline_metrics["__cluster_update__"] = updated_cluster_info
            
            # Process each node's data
            for node_addr in node_addresses:
                # With decode_responses=True, keys should always be strings
                if node_addr in server_info:
                    logger.debug(f"{fmt_node(node_addr)} Processing baseline metrics")
                    
                    # Parse each section for this node
                    node_data = {}
                    
                    # Server info
                    if node_addr in server_info:
                        server_data = parse_info_response(server_info[node_addr])
                        node_data.update(server_data)
                    
                    # Memory info
                    if node_addr in memory_info:
                        memory_data = parse_info_response(memory_info[node_addr])
                        node_data.update(memory_data)
                    
                    # Persistence info
                    if node_addr in persistence_info:
                        persistence_data = parse_info_response(persistence_info[node_addr])
                        node_data.update(persistence_data)
                    
                    # Stats info
                    if node_addr in stats_info:
                        stats_data = parse_info_response(stats_info[node_addr])
                        node_data.update(stats_data)
                    
                    # Clients info
                    if node_addr in clients_info:
                        clients_data = parse_info_response(clients_info[node_addr])
                        node_data.update(clients_data)
                    
                    # Keyspace info
                    if node_addr in keyspace_info:
                        keyspace_data = parse_info_response(keyspace_info[node_addr])
                        node_data["keyspace_info"] = keyspace_data
                        total_keys = sum(info.get("keys", 0) for info in keyspace_data.values() if isinstance(info, dict))
                        node_data["total_keys"] = total_keys
                    
                    # Commandstats
                    if node_addr in commandstats_info:
                        commandstats_data = parse_info_response(commandstats_info[node_addr])
                        parsed_commandstats = parse_commandstats(commandstats_data)
                        node_data["__commandstats_snapshot"] = parsed_commandstats
                    else:
                        node_data["__commandstats_snapshot"] = {}
                    
                    # Add derived metrics from stats_data
                    if 'stats_data' in locals():
                        node_data["total_net_input_bytes"] = stats_data.get("total_net_input_bytes", 0)
                        node_data["total_net_output_bytes"] = stats_data.get("total_net_output_bytes", 0)
                        node_data["total_net_repl_input_bytes"] = stats_data.get("total_net_repl_input_bytes", 0)
                        node_data["total_net_repl_output_bytes"] = stats_data.get("total_net_repl_output_bytes", 0)
                        node_data["total_commands_processed"] = stats_data.get("total_commands_processed", 0)
                        node_data["instantaneous_ops_per_sec"] = stats_data.get("instantaneous_ops_per_sec", 0)
                    
                    # Store baseline for this node
                    baseline_metrics[node_addr] = node_data
                    
                    logger.debug(f"{fmt_node(node_addr)} Collected baseline metrics")
                    
        except Exception as e:
            logger.error(f"Failed to collect baseline metrics: {e}")
            raise
    
    # Wait for measurement period
    logger.info(f"Waiting {duration} seconds for delta measurement...")
    if duration <= 0:
        # Skip waiting if duration is 0 or negative
        logger.debug("Duration is 0 or negative, skipping wait")
    elif quiet:
        time.sleep(duration)
    else:
        with console.status(f"[bold cyan]Waiting {duration} seconds for delta collection...") as status:
            for remaining in range(duration, 0, -1):
                status.update(f"[bold cyan]Collecting metrics... {remaining}s remaining")
                time.sleep(1)
    
    # Collect final metrics
    logger.info("Collecting final metrics from all nodes...")
    final_metrics = {}
    
    with console.status("[bold green]Collecting final metrics..."):
        try:
            # Use same redis-py native patterns for final collection
            # For single-node clusters, use standalone collection even if is_cluster=True
            if is_cluster and len(cluster_nodes) > 0:
                # For multi-node cluster: collect from each node directly
                server_info = await collect_info_from_cluster(redis_client, "server")
                memory_info = await collect_info_from_cluster(redis_client, "memory")
                persistence_info = await collect_info_from_cluster(redis_client, "persistence")
                stats_info = await collect_info_from_cluster(redis_client, "stats")
                clients_info = await collect_info_from_cluster(redis_client, "clients")
                keyspace_info = await collect_info_from_cluster(redis_client, "keyspace")
                commandstats_info = await collect_commandstats_from_cluster(redis_client)
            else:
                # For standalone mode: collect from all nodes (primary + any replicas)
                server_info = await collect_info_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, "server", user, password, tls)
                memory_info = await collect_info_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, "memory", user, password, tls)
                persistence_info = await collect_info_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, "persistence", user, password, tls)
                stats_info = await collect_info_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, "stats", user, password, tls)
                clients_info = await collect_info_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, "clients", user, password, tls)
                keyspace_info = await collect_info_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, "keyspace", user, password, tls)
                commandstats_info = await collect_commandstats_from_multiple_standalone_nodes(redis_client, source_host, node_addresses, user, password, tls)
            
            # MULTI-NODE CLUSTER DETECTION: If we collected from more nodes than discovered, we have a multi-node cluster
            actual_nodes = list(server_info.keys())
            if len(actual_nodes) > len(node_addresses) and is_cluster:
                logger.info(f"Multi-node cluster detected in final collection! Found {len(actual_nodes)} nodes: {actual_nodes}")
                node_addresses = actual_nodes
            
            # Process each node's data
            for node_addr in node_addresses:
                # With decode_responses=True, keys should always be strings
                if node_addr in server_info:
                    logger.debug(f"{fmt_node(node_addr)} Processing final metrics")
                    
                    # Parse each section for this node
                    node_data = {}
                    
                    # Server info
                    if node_addr in server_info:
                        server_data = parse_info_response(server_info[node_addr])
                        node_data.update(server_data)
                    
                    # Memory info
                    if node_addr in memory_info:
                        memory_data = parse_info_response(memory_info[node_addr])
                        node_data.update(memory_data)
                    
                    # Persistence info
                    if node_addr in persistence_info:
                        persistence_data = parse_info_response(persistence_info[node_addr])
                        node_data.update(persistence_data)
                    
                    # Stats info
                    if node_addr in stats_info:
                        stats_data = parse_info_response(stats_info[node_addr])
                        node_data.update(stats_data)
                    
                    # Clients info
                    if node_addr in clients_info:
                        clients_data = parse_info_response(clients_info[node_addr])
                        node_data.update(clients_data)
                    
                    # Keyspace info
                    if node_addr in keyspace_info:
                        keyspace_data = parse_info_response(keyspace_info[node_addr])
                        node_data["keyspace_info"] = keyspace_data
                        total_keys = sum(info.get("keys", 0) for info in keyspace_data.values() if isinstance(info, dict))
                        node_data["total_keys"] = total_keys
                    
                    # Commandstats
                    if node_addr in commandstats_info:
                        commandstats_data = parse_info_response(commandstats_info[node_addr])
                        parsed_commandstats = parse_commandstats(commandstats_data)
                        node_data["__commandstats_snapshot"] = parsed_commandstats
                    else:
                        node_data["__commandstats_snapshot"] = {}
                    
                    # Add derived metrics from stats_data
                    if 'stats_data' in locals():
                        node_data["total_net_input_bytes"] = stats_data.get("total_net_input_bytes", 0)
                        node_data["total_net_output_bytes"] = stats_data.get("total_net_output_bytes", 0)
                        node_data["total_net_repl_input_bytes"] = stats_data.get("total_net_repl_input_bytes", 0)
                        node_data["total_net_repl_output_bytes"] = stats_data.get("total_net_repl_output_bytes", 0)
                        node_data["total_commands_processed"] = stats_data.get("total_commands_processed", 0)
                        node_data["instantaneous_ops_per_sec"] = stats_data.get("instantaneous_ops_per_sec", 0)
                    
                    # Store final for this node
                    final_metrics[node_addr] = node_data
                    
                    logger.debug(f"{fmt_node(node_addr)} Collected final metrics")
                    
        except Exception as e:
            logger.error(f"Failed to collect final metrics: {e}")
            raise
    
    # Calculate deltas
    logger.info(f"Processing metrics deltas for {len(node_addresses)} nodes")
    logger.debug(f"Baseline metrics keys: {list(baseline_metrics.keys())}")
    logger.debug(f"Final metrics keys: {list(final_metrics.keys())}")
    delta_summary = {}
    
    for node_addr in node_addresses:
        logger.debug(f"Processing node {node_addr}")
        logger.debug(f"  Node in baseline: {node_addr in baseline_metrics}")
        logger.debug(f"  Node in final: {node_addr in final_metrics}")
        
        if node_addr in baseline_metrics and node_addr in final_metrics:
            baseline = baseline_metrics[node_addr]
            final = final_metrics[node_addr]
            
            logger.debug(f"  Baseline data type: {type(baseline)}")
            logger.debug(f"  Final data type: {type(final)}")
            
            # Calculate delta using existing logic
            delta_result = calculate_delta(baseline, final, duration, node_addr)
            
            logger.debug(f"  Delta result keys: {list(delta_result.keys())}")
            logger.debug(f"  Delta result: {delta_result}")
            
            # Structure the delta to match expected format for __delta_info
            delta_info = {
                "duration_seconds": duration,
                "total_write_ops": delta_result.get("write_operations", 0),
                "total_read_ops": delta_result.get("read_operations", 0),
                "net_in_bytes": delta_result.get("network_in_bytes", 0),
                "net_out_bytes": delta_result.get("network_out_bytes", 0),
                "net_repl_in_bytes": delta_result.get("net_repl_in_bytes", 0),  # FIX: Use actual value from calculate_delta
                "net_repl_out_bytes": delta_result.get("net_repl_out_bytes", 0),  # FIX: Use actual value from calculate_delta
                "memory_delta_bytes": delta_result.get("memory_delta", 0),
                "counter_reset_detected": delta_result.get("counter_reset_detected", False)
            }
            
            delta_summary[node_addr] = delta_info
            
            logger.debug(f"{fmt_node(node_addr)} Calculated delta: {delta_info.get('total_write_ops', 0)} writes, {delta_info.get('total_read_ops', 0)} reads")
            logger.debug(f"{fmt_node(node_addr)} Network traffic: {delta_info.get('net_out_bytes', 0):,} bytes out, {delta_info.get('net_in_bytes', 0):,} bytes in")
        else:
            logger.warning(f"  Skipping {node_addr} - missing in baseline or final metrics")
    
    logger.info(f"Unified metrics collection complete for {len(delta_summary)} nodes")
    
    # Return cluster update info if it was detected
    cluster_update_info = baseline_metrics.get("__cluster_update__")
    return baseline_metrics, final_metrics, delta_summary, cluster_update_info


def calculate_delta(before_metrics, after_metrics, duration, node_addr):
    """Calculate the delta between before and after metrics.
    
    Args:
        before_metrics: Metrics collected before the measurement period
        after_metrics: Metrics collected after the measurement period
        duration: Duration of the measurement period in seconds
        node_addr: Node address for logging
        
    Returns:
        Dict containing calculated deltas and metadata
    """
    b = before_metrics
    a = after_metrics
    
    # Network traffic deltas
    net_out_before = int(b.get("total_net_output_bytes", 0))
    net_out_after = int(a.get("total_net_output_bytes", 0))
    net_in_before = int(b.get("total_net_input_bytes", 0))
    net_in_after = int(a.get("total_net_input_bytes", 0))
    
    # Replication traffic deltas
    net_repl_out_before = int(b.get("total_net_repl_output_bytes", 0))
    net_repl_out_after = int(a.get("total_net_repl_output_bytes", 0))
    net_repl_in_before = int(b.get("total_net_repl_input_bytes", 0))
    net_repl_in_after = int(a.get("total_net_repl_input_bytes", 0))
    
    # Check for counter reset (including replication metrics)
    counter_reset_detected = False
    if (net_out_after < net_out_before or net_in_after < net_in_before or 
        net_repl_out_after < net_repl_out_before or net_repl_in_after < net_repl_in_before):
        counter_reset_detected = True
        logger.warning(f"{fmt_node(node_addr)} Counter reset detected - using fallback estimates")
    
    # Calculate raw deltas
    if counter_reset_detected:
        # Use fallback: estimate based on commands processed
        cmdstats_before = b.get("__commandstats_snapshot", {})
        cmdstats_after = a.get("__commandstats_snapshot", {})
        
        total_commands = 0
        for cmd_key, cmd_data in cmdstats_after.items():
            if cmd_key.startswith("cmdstat_"):
                cmd_name = cmd_key.replace("cmdstat_", "")
                before_calls = cmdstats_before.get(cmd_key, {}).get("calls", 0)
                after_calls = cmd_data.get("calls", 0)
                delta_calls = max(0, after_calls - before_calls)
                total_commands += delta_calls
        
        # Estimate traffic based on commands (rough approximation)
        # Average command size varies, but use conservative estimates
        avg_cmd_size = 100  # bytes per command (request + response)
        net_out_delta = total_commands * avg_cmd_size
        net_in_delta = total_commands * avg_cmd_size
        net_repl_out_delta = 0  # No replication data available for fallback
        net_repl_in_delta = 0   # No replication data available for fallback
        
        logger.debug(f"{fmt_node(node_addr)} Fallback estimate: {total_commands} commands × {avg_cmd_size} bytes = {net_out_delta:,} bytes")
    else:
        # Use actual counter deltas
        net_out_delta = max(0, net_out_after - net_out_before)
        net_in_delta = max(0, net_in_after - net_in_before)
        net_repl_out_delta = max(0, net_repl_out_after - net_repl_out_before)
        net_repl_in_delta = max(0, net_repl_in_after - net_repl_in_before)
    
    # Calculate command stats
    before_cmdstats = b.get("__commandstats_snapshot", {})
    after_cmdstats = a.get("__commandstats_snapshot", {})
    
    # Calculate read/write operations from commandstats
    read_operations = 0
    write_operations = 0
    
    for cmd_key, cmd_data in after_cmdstats.items():
        if cmd_key.startswith("cmdstat_"):
            cmd_name = cmd_key.replace("cmdstat_", "")
            before_calls = before_cmdstats.get(cmd_key, {}).get("calls", 0)
            after_calls = cmd_data.get("calls", 0)
            delta_calls = max(0, after_calls - before_calls)
                
            if cmd_name in READ_COMMANDS:
                read_operations += delta_calls
            elif cmd_name in WRITE_COMMANDS:
                write_operations += delta_calls
    
    # Calculate ECPU estimates
    # ElastiCache Serverless ECPU calculations based on AWS documentation
    # Read operations: 1 ECPU per 1000 operations
    # Write operations: 1 ECPU per 1000 operations  
    # Network: 1 ECPU per 1 GB of data transfer
    ecpu_read = read_operations / 1000.0
    ecpu_write = write_operations / 1000.0
    ecpu_network = (net_out_delta + net_in_delta) / (1024 * 1024 * 1024)  # Convert to GB
    total_ecpu = ecpu_read + ecpu_write + ecpu_network
    
    # Memory usage (current values, not deltas)
    memory_used = int(a.get("used_memory", 0))
    memory_rss = int(a.get("used_memory_rss", 0))
    memory_peak = int(a.get("used_memory_peak", 0))
    
    # Calculate average rates per second
    net_out_rate = net_out_delta / duration if duration > 0 else 0
    net_in_rate = net_in_delta / duration if duration > 0 else 0
    read_ops_rate = read_operations / duration if duration > 0 else 0
    write_ops_rate = write_operations / duration if duration > 0 else 0
    
    return {
        "network_out_bytes": net_out_delta,
        "network_in_bytes": net_in_delta,
        "net_repl_out_bytes": net_repl_out_delta,
        "net_repl_in_bytes": net_repl_in_delta,
        "network_out_rate_per_sec": net_out_rate,
        "network_in_rate_per_sec": net_in_rate,
        "read_operations": read_operations,
        "write_operations": write_operations,
        "read_ops_rate_per_sec": read_ops_rate,
        "write_ops_rate_per_sec": write_ops_rate,
        "memory_used_bytes": memory_used,
        "memory_rss_bytes": memory_rss,
        "memory_peak_bytes": memory_peak,
        "ecpu_read": ecpu_read,
        "ecpu_write": ecpu_write,
        "ecpu_network": ecpu_network,
        "total_ecpu": total_ecpu,
        "duration_seconds": duration,
        "counter_reset_detected": counter_reset_detected,
        "total_commands": read_operations + write_operations
    }


async def get_node_metrics(addr, user, password, tls, cluster_mode=None, node_role=None):
    host, port = addr.split(":")
    port = int(port)
    
    # Use provided cluster mode or auto-detect by probing as fallback
    if cluster_mode is None:
        cluster_mode = await detect_cluster_mode_by_probing(host, port, user, password, tls)
    
    # For replica nodes in cluster mode, use cluster client with routing
    if cluster_mode and node_role == "replica":
        logger.debug(f"{fmt_node(addr)} Using cluster client with routing for replica node")
        actual_cluster_mode = True
    else:
        actual_cluster_mode = cluster_mode
    
    client = None
    
    # Try cluster mode first if indicated
    if actual_cluster_mode:
        try:
            logger.debug(f"{fmt_node(addr)} Attempting cluster mode connection")
            config = build_connection_config(host, port, user, password, tls, cluster_mode=True)
            client = AsyncRedisCluster(**config)
        except Exception as e:
            error_msg = str(e).lower()
            if ("cluster support disabled" in error_msg or 
                "no topology views found" in error_msg or
                "this instance has cluster support disabled" in error_msg):
                logger.info(f"{fmt_node(addr)} Cluster mode not available, using standalone mode")
                actual_cluster_mode = False
            else:
                logger.debug(f"{fmt_node(addr)} Cluster connection failed: {str(e)}")
                actual_cluster_mode = False
    
    # Use standalone mode if cluster failed or not requested
    if not client:
        try:
            logger.debug(f"{fmt_node(addr)} Using standalone mode connection")
            config = build_connection_config(host, port, user, password, tls, cluster_mode=False)
            client = redis_async.Redis(**config)
            actual_cluster_mode = False
        except Exception as e:
            logger.error(f"{fmt_node(addr)} Failed to connect: {str(e)}")
            return None
    
    # If this is a replica node, send READONLY command to enable read operations
    if node_role == "replica":
        logger.debug(f"{fmt_node(addr)} Sending READONLY command to replica node")
        try:
            # Use cluster client routing to send READONLY command to specific replica
            if actual_cluster_mode:
                # For redis-py cluster, we need to target specific node 
                for node in client.get_nodes():
                    if node.host == host and node.port == port:
                        await client.execute_command("READONLY", node=node)
                        break
            else:
                await client.execute_command("READONLY")
        except Exception as e:
            logger.warning(f"{fmt_node(addr)} READONLY command failed: {e}")
    
    # For replica nodes, we need to route INFO commands to the specific node
    if node_role == "replica" and actual_cluster_mode:
        # Use routing for replica nodes in cluster mode - find the target node
        target_node = None
        for node in client.get_nodes():
            if node.host == host and node.port == port:
                target_node = node
                break
        metrics = await get_all_metrics_with_routing(client, addr, target_node)
    else:
        metrics = await get_all_metrics(client, addr)

    # Add commandstats snapshot for later delta comparison
    try:
        if node_role == "replica" and actual_cluster_mode:
            # Use raw command instead of InfoSection enum for ElastiCache compatibility
            commandstats_raw_bytes = await client.execute_command("INFO", "COMMANDSTATS", node=target_node)
        else:
            commandstats_raw_bytes = await client.execute_command("INFO", "COMMANDSTATS")
        
        # Handle raw bytes response from custom_command
        if isinstance(commandstats_raw_bytes, bytes):
            commandstats_raw_str = commandstats_raw_bytes.decode('utf-8')
            commandstats_raw = parse_info_response(commandstats_raw_str)
        else:
            commandstats_raw = parse_cluster_info_response(commandstats_raw_bytes)
        
        # Parse the commandstats data properly
        parsed_commandstats = parse_commandstats(commandstats_raw)
        metrics["__commandstats_snapshot"] = parsed_commandstats
    except Exception as e:
        logger.warning(f"{fmt_node(addr)} Could not collect commandstats: {str(e)}")
        metrics["__commandstats_snapshot"] = {}

    # Add keyspace summary for all DBs
    try:
        if node_role == "replica" and actual_cluster_mode:
            keyspace_data_raw = await client.info("keyspace")
        else:
            keyspace_data_raw = await client.info("keyspace")
        keyspace_data = parse_cluster_info_response(keyspace_data_raw)
        metrics["keyspace_info"] = keyspace_data
        total_keys = sum(info.get("keys", 0) for info in keyspace_data.values() if isinstance(info, dict))
        metrics["total_keys"] = total_keys
    except Exception as e:
        logger.warning(f"{fmt_node(addr)} Could not collect keyspace info: {str(e)}")
        metrics["keyspace_info"] = {}
        metrics["total_keys"] = 0

    return metrics
    

async def get_replica_count(redis_client, source_host=None, is_cluster=False):
    """Get replica count using redis-py native patterns"""
    try:
        if is_cluster:
            # For cluster: get replication info from all nodes (though typically just need primaries)
            repl_info_raw = await collect_info_from_cluster(redis_client, "replication")
            total_replicas = 0
            for node_addr, repl_data in repl_info_raw.items():
                if isinstance(repl_data, dict):
                    total_replicas += repl_data.get("connected_slaves", 0)
            return total_replicas
        else:
            # For standalone: get replication info directly
            repl_info_raw = await collect_info_from_standalone(redis_client, source_host, "replication")
            if source_host in repl_info_raw:
                repl_info = repl_info_raw[source_host]
                if isinstance(repl_info, dict):
                    return repl_info.get("connected_slaves", 0)
            return 0
    except Exception as e:
        logger.debug(f"Error getting replica count: {e}")
        return 0


async def get_keyspace_for_node(host, port, user, password, tls):
    """Helper function to get keyspace info for a single node"""
    try:
        # Auto-detect cluster mode by probing
        cluster_mode = await detect_cluster_mode_by_probing(host, port, user, password, tls)
        config = build_connection_config(host, port, user, password, tls, cluster_mode=cluster_mode)
        
        if cluster_mode:
            client = AsyncRedisCluster(**config)
        else:
            client = redis_async.Redis(**config)
        keyspace_raw = await client.info("keyspace")
        return parse_info_response(keyspace_raw)
    except Exception as e:
        raise Exception(f"Failed to get keyspace: {e}")


async def get_node_data_and_keyspace(addr, user, password, tls, cluster_mode=None, node_role=None):
    """Helper function to get both node metrics and keyspace info for a single node"""
    host, port = addr.split(":")
    port = int(port)
    
    # Use provided cluster mode or auto-detect by probing as fallback
    if cluster_mode is None:
        cluster_mode = await detect_cluster_mode_by_probing(host, port, user, password, tls)
    
    # For replica nodes in cluster mode, use cluster client with routing
    if cluster_mode and node_role == "replica":
        logger.debug(f"{fmt_node(addr)} Using cluster client with routing for replica node")
        actual_cluster_mode = True
    else:
        actual_cluster_mode = cluster_mode
    
    client = None
    
    # Try cluster mode first if indicated
    if actual_cluster_mode:
        try:
            logger.debug(f"{fmt_node(addr)} Attempting cluster mode connection")
            config = build_connection_config(host, port, user, password, tls, cluster_mode=True)
            client = AsyncRedisCluster(**config)
        except Exception as e:
            error_msg = str(e).lower()
            if ("cluster support disabled" in error_msg or 
                "no topology views found" in error_msg or
                "this instance has cluster support disabled" in error_msg):
                logger.info(f"{fmt_node(addr)} Cluster mode not available, using standalone mode")
                actual_cluster_mode = False
            else:
                logger.debug(f"{fmt_node(addr)} Cluster connection failed: {str(e)}")
                actual_cluster_mode = False
    
    # Use standalone mode if cluster failed or not requested
    if not client:
        try:
            logger.debug(f"{fmt_node(addr)} Using standalone mode connection")
            config = build_connection_config(host, port, user, password, tls, cluster_mode=False)
            client = redis_async.Redis(**config)
            actual_cluster_mode = False
        except Exception as e:
            raise Exception(f"Failed to get data for {addr}: {e}")
    
    # If this is a replica node, send READONLY command to enable read operations
    if node_role == "replica":
        logger.debug(f"{fmt_node(addr)} Sending READONLY command to replica node")
        try:
            # Use cluster client routing to send READONLY command to specific replica
            if actual_cluster_mode:
                # For redis-py cluster, we need to target specific node 
                for node in client.get_nodes():
                    if node.host == host and node.port == port:
                        await client.execute_command("READONLY", node=node)
                        break
            else:
                await client.execute_command("READONLY")
        except Exception as e:
            logger.warning(f"{fmt_node(addr)} READONLY command failed: {e}")
    
    # Get both metrics and keyspace info
    # For replica nodes, we need to route INFO commands to the specific node
    if node_role == "replica" and actual_cluster_mode:
        # Use routing for replica nodes in cluster mode - find the target node
        target_node = None
        for node in client.get_nodes():
            if node.host == host and node.port == port:
                target_node = node
                break
        # We need to create a special version of get_all_metrics that uses routing
        node_metrics = await get_all_metrics_with_routing(client, addr, target_node)
        keyspace_raw = await client.execute_command("INFO", "keyspace", node=target_node)
    else:
        node_metrics = await get_all_metrics(client, addr)
        keyspace_raw = await client.info("keyspace")
    
    # Handle cluster vs standalone response format
    if isinstance(keyspace_raw, dict):
        # Cluster response: per-node data
        keyspace = {}
        for node_addr, raw_data in keyspace_raw.items():
            if isinstance(raw_data, (str, bytes)):
                keyspace[node_addr] = parse_info_response(raw_data)
            else:
                keyspace[node_addr] = raw_data
    else:
        # Standalone response: single response
        keyspace = parse_info_response(keyspace_raw)
    
    return node_metrics, keyspace


def parse_info_response(info_bytes):
    """
    Parse raw INFO response bytes into a dictionary
    """
    # If it's already a dictionary (redis-py with decode_responses=True), return as-is
    if isinstance(info_bytes, dict):
        return info_bytes
    
    if isinstance(info_bytes, bytes):
        info_str = info_bytes.decode('utf-8')
    else:
        info_str = str(info_bytes)
    
    result = {}
    current_section = None
    
    for line in info_str.split('\r\n'):
        line = line.strip()
        if not line:
            continue
        
        # Section header
        if line.startswith('#'):
            current_section = line[1:].strip().lower()
            continue
        
        # Key-value pair
        if ':' in line:
            key, value = line.split(':', 1)
            
            # Special handling for keyspace database entries (db0:keys=3,expires=0,avg_ttl=0)
            if key.startswith('db') and '=' in value:
                db_stats = {}
                for pair in value.split(','):
                    if '=' in pair:
                        stat_key, stat_value = pair.split('=', 1)
                        # Convert numeric values
                        if stat_value.isdigit():
                            stat_value = int(stat_value)
                        elif stat_value.replace('.', '').replace('-', '').isdigit():
                            try:
                                stat_value = float(stat_value)
                            except ValueError:
                                pass
                        db_stats[stat_key] = stat_value
                value = db_stats
            # Special handling for commandstats entries (cmdstat_set:calls=123,usec=456,...)
            elif key.startswith('cmdstat_') and '=' in value:
                cmd_stats = {}
                for pair in value.split(','):
                    if '=' in pair:
                        stat_key, stat_value = pair.split('=', 1)
                        # Convert numeric values
                        if stat_value.isdigit():
                            stat_value = int(stat_value)
                        elif '.' in stat_value and stat_value.replace('.', '').replace('-', '').isdigit():
                            try:
                                stat_value = float(stat_value)
                            except ValueError:
                                pass
                        cmd_stats[stat_key] = stat_value
                value = cmd_stats
            # Try to convert to appropriate type, but be smarter about it
            elif value.isdigit():
                value = int(value)
            elif value.lower() in ('yes', 'no'):
                value = value.lower() == 'yes'
            elif '.' in value and value.replace('.', '').replace('-', '').isdigit():
                # Only convert to float if it's clearly a number (not a version string)
                # Skip version-like strings (multiple dots, contains letters after numbers)
                if value.count('.') == 1 and not any(c.isalpha() for c in value):
                    try:
                        value = float(value)
                    except ValueError:
                        pass  # Keep as string if conversion fails
            
            result[key] = value
    
    return result


def parse_commandstats(info_output):
    """
    Parses the INFO commandstats section into a dictionary like:
    {
        'cmdstat_get': {'calls': 1000, 'usec': 3000, 'usec_per_call': 3.0},
        ...
    }
    Handles both raw string format and already-parsed dictionary format.
    """
    commandstats = {}
    for key, value in info_output.items():
        if key.startswith("cmdstat_"):
            # Check if value is already a dictionary (already parsed)
            if isinstance(value, dict):
                commandstats[key] = value
            # If it's a string, parse it
            elif isinstance(value, str):
                parts = value.split(",")
                stats = {}
                for part in parts:
                    k, v = part.split("=")
                    if "." in v:
                        stats[k] = float(v)
                    else:
                        stats[k] = int(v)
                commandstats[key] = stats
            else:
                # Skip unknown formats
                continue
    
    return commandstats


def summarize_read_write(commandstats, exclude_background=True):
    
    total_reads = 0
    total_writes = 0
    read_cmd_counts = {}
    write_cmd_counts = {}
    background_cmd_counts = {}
    
    for cmd, stats in commandstats.items():
        cmd_name = cmd.replace("cmdstat_", "")
        
        # For network traffic calculations, use total attempts (successful + rejected)
        # since rejected operations still consume bandwidth
        if isinstance(stats, dict):
            successful_calls = stats.get("calls", 0)
            rejected_calls = stats.get("rejected_calls", 0)
            total_attempts = stats.get("total_attempts", successful_calls + rejected_calls)
        else:
            # Fallback for simple integer values
            total_attempts = stats
            successful_calls = stats
            rejected_calls = 0

        # Track background commands separately
        if exclude_background and cmd_name in BACKGROUND_COMMANDS:
            background_cmd_counts[cmd_name] = total_attempts
        elif cmd_name in READ_COMMANDS:
            total_reads += total_attempts
            read_cmd_counts[cmd_name] = total_attempts
        elif cmd_name in WRITE_COMMANDS:
            total_writes += total_attempts
            write_cmd_counts[cmd_name] = total_attempts

    return {
        "total_read_ops": total_reads,
        "total_write_ops": total_writes,
        "read_ops_breakdown": read_cmd_counts,
        "write_ops_breakdown": write_cmd_counts,
        "background_ops_breakdown": background_cmd_counts,
        "total_background_ops": sum(background_cmd_counts.values()),
    }


def parse_cluster_info_response(info_response):
    """Parse cluster info response that returns per-node data"""
    logger.debug(f"DEBUG_CLUSTER_PARSE: Input type: {type(info_response)}")
    if isinstance(info_response, dict):
        logger.debug(f"DEBUG_CLUSTER_PARSE: Dict input with keys: {list(info_response.keys())}")
        # Cluster response: properly aggregate data from all nodes
        merged_data = {}
        node_count = 0
        
        # Metrics that should be summed across all nodes
        summable_metrics = {
            'total_net_input_bytes', 'total_net_output_bytes', 'total_net_repl_input_bytes', 
            'total_net_repl_output_bytes', 'total_commands_processed', 'total_connections_received',
            'used_memory', 'connected_clients', 'keyspace_hits', 'keyspace_misses',
            'expired_keys', 'evicted_keys'
        }
        
        # Metrics that should be averaged across nodes  
        averageable_metrics = {
            'instantaneous_ops_per_sec', 'instantaneous_input_kbps', 'instantaneous_output_kbps',
            'used_memory_rss_human', 'used_memory_peak_human'
        }
        
        # Metrics that should be taken from any node (same across cluster)
        single_value_metrics = {
            'redis_version', 'valkey_version', 'os', 'arch_bits', 'multiplexing_api',
            'maxmemory', 'maxmemory_policy', 'aof_enabled', 'rdb_last_save_time'
        }
        
        for node_addr, raw_data in info_response.items():
            logger.debug(f"DEBUG_CLUSTER_PARSE: Processing node {node_addr}, data type: {type(raw_data)}")
            if isinstance(raw_data, (str, bytes)):
                node_data = parse_info_response(raw_data)
                node_count += 1
                logger.debug(f"DEBUG_CLUSTER_PARSE: Node {node_addr} parsed data keys: {list(node_data.keys())[:15]}")
                
                # Check for cmdstat entries in this node's data
                cmdstat_keys = [k for k in node_data.keys() if k.startswith('cmdstat_')]
                if cmdstat_keys:
                    logger.debug(f"DEBUG_CLUSTER_PARSE: Node {node_addr} has cmdstat keys: {cmdstat_keys[:10]}")
                    if 'cmdstat_set' in node_data:
                        logger.debug(f"DEBUG_CLUSTER_PARSE: Node {node_addr} cmdstat_set: {node_data['cmdstat_set']}")
                
                # Process each metric according to its type
                for key, value in node_data.items():
                    if key.startswith('cmdstat_'):
                        # Special handling for commandstats - preserve per-node data instead of aggregating
                        if key not in merged_data:
                            merged_data[key] = value
                            logger.debug(f"DEBUG_CLUSTER_PARSE: Taking {key} from first node {node_addr}: {value}")
                        else:
                            logger.debug(f"DEBUG_CLUSTER_PARSE: Skipping {key} from {node_addr}, already have from previous node")
                    elif key in summable_metrics:
                        # Sum these metrics across all nodes
                        if isinstance(value, (int, float)):
                            merged_data[key] = merged_data.get(key, 0) + value
                        else:
                            merged_data[key] = merged_data.get(key, 0)
                    elif key in averageable_metrics:
                        # Track for averaging later
                        if isinstance(value, (int, float)):
                            if key not in merged_data:
                                merged_data[key] = []
                            merged_data[key].append(value)
                    elif key in single_value_metrics:
                        # Take the first non-None value
                        if key not in merged_data and value:
                            merged_data[key] = value
                    else:
                        # Default: take from first node
                        if key not in merged_data:
                            merged_data[key] = value
        
        # Calculate averages for averageable metrics
        for key in averageable_metrics:
            if key in merged_data and isinstance(merged_data[key], list):
                values = [v for v in merged_data[key] if isinstance(v, (int, float))]
                if values:
                    merged_data[key] = sum(values) / len(values)
                else:
                    merged_data[key] = 0
        
        # DEBUG: Log cluster aggregation results
        logger.debug(f"Cluster aggregation: processed {node_count} nodes")
        for key in ['total_net_input_bytes', 'total_net_output_bytes', 'instantaneous_ops_per_sec']:
            if key in merged_data:
                if key == 'instantaneous_ops_per_sec':
                    logger.debug(f"  {key}: {merged_data[key]:.1f} (averaged)")
                else:
                    logger.debug(f"  {key}: {merged_data[key]:,} (summed)")
        
        return merged_data
    else:
        # Standalone response: single response
        return parse_info_response(info_response)

async def get_all_metrics_with_routing(r, node_addr=None, target_node=None):
    """Get all metrics from a specific node using routing (for replica nodes)"""
    data = {}

    server_info_raw = await r.execute_command("INFO", "server", node=target_node)
    server_info = parse_cluster_info_response(server_info_raw)
    data.update(server_info)

    # Capture engine versions explicitly
    redis_version = server_info.get("redis_version", "unknown")
    valkey_version = server_info.get("valkey_version", None)
    data["engine_redis_version"] = redis_version
    if valkey_version:
        data["engine_valkey_version"] = valkey_version

    memory_info_raw = await r.execute_command("INFO", "memory", node=target_node)
    memory_info = parse_cluster_info_response(memory_info_raw)
    data.update(memory_info)
    persistence_info_raw = await r.execute_command("INFO", "persistence", node=target_node)
    persistence_info = parse_cluster_info_response(persistence_info_raw)
    data.update(persistence_info)
    stats_info_raw = await r.execute_command("INFO", "stats", node=target_node)
    stats_info = parse_cluster_info_response(stats_info_raw)
    data.update(stats_info)
    clients_info_raw = await r.execute_command("INFO", "clients", node=target_node)
    clients_info = parse_cluster_info_response(clients_info_raw)
    data.update(clients_info)

    # Extract network metrics from stats
    data["total_net_input_bytes"] = stats_info.get("total_net_input_bytes", 0)
    data["total_net_output_bytes"] = stats_info.get("total_net_output_bytes", 0)
    data["total_net_repl_input_bytes"] = stats_info.get("total_net_repl_input_bytes", 0)
    data["total_net_repl_output_bytes"] = stats_info.get("total_net_repl_output_bytes", 0)
    data["repl_backlog_bytes"] = stats_info.get("repl_backlog_bytes", 0)

    # Add cross-validation metrics
    data["total_commands_processed"] = stats_info.get("total_commands_processed", 0)
    data["instantaneous_ops_per_sec"] = stats_info.get("instantaneous_ops_per_sec", 0)
    data["instantaneous_input_kbps"] = stats_info.get("instantaneous_input_kbps", 0)
    data["instantaneous_output_kbps"] = stats_info.get("instantaneous_output_kbps", 0)
    
    # Add validation timestamp for cross-referencing
    data["metrics_timestamp"] = time.time()

    # Check if this is ElastiCache (CONFIG commands are not available)
    is_elasticache = server_info.get("os", "").startswith("Amazon ElastiCache")
    
    # For ElastiCache, skip CONFIG commands and use INFO fallbacks
    if is_elasticache:
        # Set config values as N/A for ElastiCache
        config_keys = ["maxmemory", "maxmemory-policy", "appendonly", "save"]
        for key in config_keys:
            data[f"config_{key}"] = "N/A"
        
        # Get eviction policy from INFO memory
        if "maxmemory_policy" in memory_info:
            data["config_maxmemory-policy"] = memory_info["maxmemory_policy"]
        
        # Get maxmemory from INFO memory
        if "maxmemory" in memory_info:
            data["config_maxmemory"] = memory_info["maxmemory"]
    
    # Modules (skip for ElastiCache as it's not supported)
    if is_elasticache:
        logger.debug(f"{fmt_node(node_addr)} ElastiCache detected - skipping MODULE LIST (not supported)")
        data["loaded_modules"] = "ElastiCache (modules not supported)"
    else:
        try:
            modules = await r.execute_command("MODULE", "LIST", node=target_node)
            module_names = [mod[1] for mod in modules]
            data["loaded_modules"] = ", ".join(module_names)
        except Exception:
            data["loaded_modules"] = "None or unsupported"

    # Commandstats (read/write breakdown)
    try:
        # Use raw command instead of InfoSection enum for ElastiCache compatibility
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: Requesting INFO COMMANDSTATS with target_node={target_node}...")
        commandstats_raw_bytes = await r.execute_command("INFO", "COMMANDSTATS", node=target_node)
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: Raw response type: {type(commandstats_raw_bytes)}")
        
        # Handle raw bytes response from custom_command
        if isinstance(commandstats_raw_bytes, bytes):
            commandstats_raw_str = commandstats_raw_bytes.decode('utf-8')
            logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: Bytes response sample: {commandstats_raw_str[:300]}...")
            commandstats_raw = parse_info_response(commandstats_raw_str)
            logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: After parse_info_response, keys: {list(commandstats_raw.keys())[:15]}")
        else:
            logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: Dict response keys: {list(commandstats_raw_bytes.keys()) if isinstance(commandstats_raw_bytes, dict) else 'Not a dict'}")
            if isinstance(commandstats_raw_bytes, dict):
                for node_key, node_data in list(commandstats_raw_bytes.items())[:3]:
                    logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: Node {node_key} data sample: {str(node_data)[:200]}...")
            commandstats_raw = parse_cluster_info_response(commandstats_raw_bytes)
            logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: After parse_cluster_info_response, keys: {list(commandstats_raw.keys())[:15]}")
        
        # Check for cmdstat entries before parsing
        cmdstat_keys = [k for k in commandstats_raw.keys() if k.startswith('cmdstat_')]
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: Found cmdstat keys: {cmdstat_keys[:10]}")
        if 'cmdstat_set' in commandstats_raw:
            logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: Raw cmdstat_set: {commandstats_raw['cmdstat_set']}")
        
        parsed_commandstats = parse_commandstats(commandstats_raw)
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: Parsed commandstats keys: {list(parsed_commandstats.keys())[:10]}")
        if 'cmdstat_set' in parsed_commandstats:
            logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: Parsed cmdstat_set: {parsed_commandstats['cmdstat_set']}")
        
        summary = summarize_read_write(parsed_commandstats)
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: Summary - reads: {summary['total_read_ops']}, writes: {summary['total_write_ops']}")
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: Write breakdown: {summary['write_ops_breakdown']}")
        
        data["commandstats_total_read_ops"] = summary["total_read_ops"]
        data["commandstats_total_write_ops"] = summary["total_write_ops"]
        data["commandstats_read_ops_breakdown"] = summary["read_ops_breakdown"]
        data["commandstats_write_ops_breakdown"] = summary["write_ops_breakdown"]
        data["commandstats_background_ops_breakdown"] = summary["background_ops_breakdown"]
        data["commandstats_total_background_ops"] = summary["total_background_ops"]
    except Exception as e:
        logger.warning(f"{fmt_node(node_addr)} Could not collect commandstats: {str(e)}")
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: Exception details: {type(e).__name__}: {str(e)}")
        import traceback
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS: Traceback: {traceback.format_exc()}")
        data["commandstats_total_read_ops"] = 0
        data["commandstats_total_write_ops"] = 0
        data["commandstats_read_ops_breakdown"] = {}
        data["commandstats_write_ops_breakdown"] = {}
        data["commandstats_background_ops_breakdown"] = {}
        data["commandstats_total_background_ops"] = 0

    return data

async def get_all_metrics(r, node_addr=None):
    data = {}

    server_info_raw = await r.info("server")
    server_info = parse_cluster_info_response(server_info_raw)
    data.update(server_info)

    # Capture engine versions explicitly
    redis_version = server_info.get("redis_version", "unknown")
    valkey_version = server_info.get("valkey_version", None)
    data["engine_redis_version"] = redis_version
    if valkey_version:
        data["engine_valkey_version"] = valkey_version

    memory_info_raw = await r.info("memory")
    memory_info = parse_cluster_info_response(memory_info_raw)
    data.update(memory_info)
    persistence_info_raw = await r.info("persistence")
    persistence_info = parse_cluster_info_response(persistence_info_raw)
    data.update(persistence_info)
    stats_info_raw = await r.info("stats")
    stats_info = parse_cluster_info_response(stats_info_raw)
    data.update(stats_info)
    clients_info_raw = await r.info("clients")
    clients_info = parse_cluster_info_response(clients_info_raw)
    data.update(clients_info)

    # DEBUG: Log raw stats response format to understand valkey-glide behavior
    logger.debug(f"{fmt_node(node_addr)} Raw stats response type: {type(stats_info_raw)}")
    if isinstance(stats_info_raw, dict):
        logger.debug(f"{fmt_node(node_addr)} Stats response is dict with keys: {list(stats_info_raw.keys())}")
        # Log a sample of the content
        sample_keys = list(stats_info_raw.keys())[:3]
        for key in sample_keys:
            logger.debug(f"{fmt_node(node_addr)} Stats[{key}] type: {type(stats_info_raw[key])}")
            if isinstance(stats_info_raw[key], (str, bytes)):
                content = stats_info_raw[key][:200] if len(str(stats_info_raw[key])) > 200 else stats_info_raw[key]
                logger.debug(f"{fmt_node(node_addr)} Stats[{key}] content sample: {content}")
    else:
        logger.debug(f"{fmt_node(node_addr)} Stats response content sample: {str(stats_info_raw)[:200]}")
    
    logger.debug(f"{fmt_node(node_addr)} Parsed stats_info keys: {list(stats_info.keys())[:10]}")
    
    # Extract network metrics from stats
    data["total_net_input_bytes"] = stats_info.get("total_net_input_bytes", 0)
    data["total_net_output_bytes"] = stats_info.get("total_net_output_bytes", 0)
    data["total_net_repl_input_bytes"] = stats_info.get("total_net_repl_input_bytes", 0)
    data["total_net_repl_output_bytes"] = stats_info.get("total_net_repl_output_bytes", 0)
    data["repl_backlog_bytes"] = stats_info.get("repl_backlog_bytes", 0)
    
    # DEBUG: Log the actual network metric values we found
    logger.debug(f"{fmt_node(node_addr)} Network metrics extracted:")
    logger.debug(f"{fmt_node(node_addr)}   total_net_input_bytes: {data['total_net_input_bytes']}")
    logger.debug(f"{fmt_node(node_addr)}   total_net_output_bytes: {data['total_net_output_bytes']}")
    logger.debug(f"{fmt_node(node_addr)}   total_net_repl_input_bytes: {data['total_net_repl_input_bytes']}")
    logger.debug(f"{fmt_node(node_addr)}   total_net_repl_output_bytes: {data['total_net_repl_output_bytes']}")

    # Add cross-validation metrics from Valkey INFO documentation
    data["total_commands_processed"] = stats_info.get("total_commands_processed", 0)
    data["instantaneous_ops_per_sec"] = stats_info.get("instantaneous_ops_per_sec", 0)
    data["instantaneous_input_kbps"] = stats_info.get("instantaneous_input_kbps", 0)
    data["instantaneous_output_kbps"] = stats_info.get("instantaneous_output_kbps", 0)
    
    # Add validation timestamp for cross-referencing
    data["metrics_timestamp"] = time.time()

    # Check if this is ElastiCache (CONFIG commands are not available)
    is_elasticache = server_info.get("os", "").startswith("Amazon ElastiCache")
    
    # Get eviction policy and other relevant configs
    # Skip CONFIG commands for ElastiCache since they're not available
    config_keys = ["maxmemory", "maxmemory-policy", "appendonly", "save"]
    if not is_elasticache:
        for key in config_keys:
            try:
                val = await r.execute_command("CONFIG", "GET", key)
                # valkey-glide returns a list, convert to dict format for compatibility
                if isinstance(val, list) and len(val) >= 2:
                    data[f"config_{key}"] = val[1]
                else:
                    data[f"config_{key}"] = "N/A"
            except Exception as e:
                logger.debug(f"[{node_addr}] CONFIG GET {key} failed: {str(e)}")
                data[f"config_{key}"] = "N/A"
    else:
        # ElastiCache - CONFIG commands not available, set all to N/A
        for key in config_keys:
            data[f"config_{key}"] = "N/A"

    # Fallback: try to get eviction policy from INFO memory
    # This is always needed for ElastiCache, and used as fallback for other systems
    if data.get("config_maxmemory-policy") == "N/A":
        if is_elasticache:
            logger.debug(f"{fmt_node(node_addr)} ElastiCache detected - getting eviction policy from INFO memory...")
        else:
            logger.debug(f"{fmt_node(node_addr)} Checking INFO memory for eviction policy...")
        
        # Some managed Valkey / Redis OSS services include eviction policy in memory info
        if "maxmemory_policy" in memory_info:
            data["config_maxmemory-policy"] = memory_info["maxmemory_policy"]
            logger.debug(f"{fmt_node(node_addr)} Found maxmemory_policy: {memory_info['maxmemory_policy']}")
        elif "eviction_policy" in memory_info:
            data["config_maxmemory-policy"] = memory_info["eviction_policy"]
            logger.debug(f"{fmt_node(node_addr)} Found eviction_policy: {memory_info['eviction_policy']}")

    # Fallback: try to get maxmemory from INFO memory
    # This is always needed for ElastiCache, and used as fallback for other systems
    if data.get("config_maxmemory") == "N/A":
        if is_elasticache:
            logger.debug(f"{fmt_node(node_addr)} ElastiCache detected - getting maxmemory from INFO memory...")
        else:
            logger.debug(f"{fmt_node(node_addr)} Checking INFO memory for maxmemory...")
        # ElastiCache might expose maxmemory in memory info
        if "maxmemory" in memory_info:
            data["config_maxmemory"] = memory_info["maxmemory"]
            logger.debug(f"{fmt_node(node_addr)} Found maxmemory in memory info: {memory_info['maxmemory']}")

    # Try to get additional cluster info that might contain configuration
    try:
        cluster_info = await r.execute_command("CLUSTER", "INFO")
        logger.debug(f"{fmt_node(node_addr)} CLUSTER INFO available")
    except Exception:
        logger.debug(f"{fmt_node(node_addr)} CLUSTER INFO not available")

    # Modules (skip for ElastiCache as it's not supported)
    if is_elasticache:
        logger.debug(f"{fmt_node(node_addr)} ElastiCache detected - skipping MODULE LIST (not supported)")
        data["loaded_modules"] = "ElastiCache (modules not supported)"
    else:
        try:
            modules = await r.execute_command("MODULE", "LIST")
            module_names = [mod[1] for mod in modules]
            data["loaded_modules"] = ", ".join(module_names)
        except Exception:
            data["loaded_modules"] = "None or unsupported"

    # Commandstats (read/write breakdown)
    try:
        # Use raw command instead of InfoSection enum for ElastiCache compatibility
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS_DIRECT: Requesting INFO COMMANDSTATS (no route)...")
        commandstats_raw_bytes = await r.execute_command("INFO", "COMMANDSTATS")
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS_DIRECT: Raw response type: {type(commandstats_raw_bytes)}")
        
        # Handle raw bytes response from custom_command
        if isinstance(commandstats_raw_bytes, bytes):
            commandstats_raw_str = commandstats_raw_bytes.decode('utf-8')
            logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS_DIRECT: Bytes response sample: {commandstats_raw_str[:300]}...")
            commandstats_raw = parse_info_response(commandstats_raw_str)
            logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS_DIRECT: After parse_info_response, keys: {list(commandstats_raw.keys())[:15]}")
        else:
            logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS_DIRECT: Dict response keys: {list(commandstats_raw_bytes.keys()) if isinstance(commandstats_raw_bytes, dict) else 'Not a dict'}")
            if isinstance(commandstats_raw_bytes, dict):
                for node_key, node_data in list(commandstats_raw_bytes.items())[:3]:
                    logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS_DIRECT: Node {node_key} data sample: {str(node_data)[:200]}...")
            commandstats_raw = parse_cluster_info_response(commandstats_raw_bytes)
            logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS_DIRECT: After parse_cluster_info_response, keys: {list(commandstats_raw.keys())[:15]}")
        
        # Check for cmdstat entries before parsing
        cmdstat_keys = [k for k in commandstats_raw.keys() if k.startswith('cmdstat_')]
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS_DIRECT: Found cmdstat keys: {cmdstat_keys[:10]}")
        if 'cmdstat_set' in commandstats_raw:
            logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS_DIRECT: Raw cmdstat_set: {commandstats_raw['cmdstat_set']}")
        
        # Parse the commandstats data properly
        parsed_commandstats = parse_commandstats(commandstats_raw)
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS_DIRECT: Parsed commandstats keys: {list(parsed_commandstats.keys())[:10]}")
        if 'cmdstat_set' in parsed_commandstats:
            logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS_DIRECT: Parsed cmdstat_set: {parsed_commandstats['cmdstat_set']}")
        
        summary = summarize_read_write(parsed_commandstats)
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS_DIRECT: Summary - reads: {summary['total_read_ops']}, writes: {summary['total_write_ops']}")
        logger.debug(f"{fmt_node(node_addr)} DEBUG_CMDSTATS_DIRECT: Write breakdown: {summary['write_ops_breakdown']}")
        
        data["commandstats_total_read_ops"] = summary["total_read_ops"]
        data["commandstats_total_write_ops"] = summary["total_write_ops"]
        data["commandstats_read_ops_breakdown"] = summary["read_ops_breakdown"]
        data["commandstats_write_ops_breakdown"] = summary["write_ops_breakdown"]
        data["commandstats_background_ops_breakdown"] = summary["background_ops_breakdown"]
        data["commandstats_total_background_ops"] = summary["total_background_ops"]
    except Exception as e:
        logger.warning(f"{fmt_node(node_addr)} Could not collect commandstats: {str(e)}")
        data["commandstats_total_read_ops"] = "N/A"
        data["commandstats_total_write_ops"] = "N/A"
        data["commandstats_read_ops_breakdown"] = {}
        data["commandstats_write_ops_breakdown"] = {}

    try:
        # Use raw command instead of InfoSection enum for ElastiCache compatibility
        commandstats_raw_bytes = await r.execute_command("INFO", "COMMANDSTATS")
        
        # Handle raw bytes response from custom_command
        if isinstance(commandstats_raw_bytes, bytes):
            commandstats_raw_str = commandstats_raw_bytes.decode('utf-8')
            commandstats_parsed = parse_info_response(commandstats_raw_str)
        else:
            commandstats_parsed = parse_cluster_info_response(commandstats_raw_bytes)
        
        data.update(commandstats_parsed)
    except Exception as e:
        logger.warning(f"{fmt_node(node_addr)} Could not collect raw commandstats: {str(e)}")

    # DB key count across all DBs
    try:
        keyspace_info_raw = await r.info(["keyspace"])
        keyspace_info = parse_cluster_info_response(keyspace_info_raw)
        data["keyspace_dbs"] = keyspace_info  # include full per-db breakdown
        total_keys = sum(db.get("keys", 0) for db in keyspace_info.values() if isinstance(db, dict))
        data["total_keys_all_dbs"] = total_keys
    except Exception:
        data["keyspace_dbs"] = {}
        data["total_keys_all_dbs"] = "N/A"

    return data


def flatten_value(metric_prefix, value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from flatten_value(f"{metric_prefix}_{k}", v)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from flatten_value(f"{metric_prefix}_{i}", v)
    else:
        yield (metric_prefix, value)


def write_csv(filename, cluster_data, keyspace_info, node_data, source_host, cluster_update_info=None):
    # Use updated cluster information when available (same logic as print_summary)
    cluster_data_to_write = cluster_data.copy()
    
    if cluster_update_info:
        # Update with accurate cluster topology discovered during metrics collection
        cluster_data_to_write["cluster_mode"] = cluster_update_info.get("cluster_mode", cluster_data.get("cluster_mode", False))
        cluster_data_to_write["primaries"] = cluster_update_info.get("primaries", 0)
        cluster_data_to_write["replicas"] = cluster_update_info.get("replicas", 0)
        
        # Update node addresses and roles if available
        if "node_addresses" in cluster_update_info:
            cluster_data_to_write["node_addresses"] = cluster_update_info["node_addresses"]
        if "roles" in cluster_update_info:
            cluster_data_to_write["roles"] = cluster_update_info["roles"]
    
    with open(filename, mode="w", newline='') as file:
        writer = csv.writer(file)
        # Remove 'role' from the header
        writer.writerow(["category", "metric", "value", "source"])

        def write_flattened(category, value_dict, node_identifier=None, for_cluster=False, role=None):
            for flat_key, flat_val in flatten_value("", value_dict):
                clean_key = flat_key.lstrip("_")
                row = [
                    category,
                    clean_key,
                    flat_val,
                    source_host if for_cluster else node_identifier
                ]
                writer.writerow(row)

        # Write summary metrics first (most important metrics)
        summary_metrics = {k: v for k, v in cluster_data_to_write.items() if k.startswith("summary_metric_")}
        for key, val in summary_metrics.items():
            write_flattened("cluster", {key: val}, for_cluster=True)
        
        # Write remaining cluster-level data
        for key, val in cluster_data_to_write.items():
            if key != "roles" and not key.startswith("summary_metric_"):
                write_flattened("cluster", {key: val}, for_cluster=True)

        # Write keyspace data
        for node, db_info in keyspace_info.get("per_node", {}).items():
            node_total_keys = 0
            for db, stats in db_info.items():
                for stat_key, stat_val in stats.items():
                    writer.writerow(["keyspace", f"{db}_{stat_key}", stat_val, node])
                    if stat_key == "keys":
                        node_total_keys += stat_val
            writer.writerow(["keyspace", "db_keys_total", node_total_keys, node])

        writer.writerow(["cluster", "cluster_db_keys_total", keyspace_info.get("cluster_total", 0), source_host])

        # Write node metrics
        for node, val in node_data.items():
            write_flattened("node", val, node_identifier=node)

        # Write delta metrics (extract from __delta_info in node_data)
        roles = cluster_data.get("roles", {})
        for node, node_info in node_data.items():
            delta_info = node_info.get("__delta_info", {})
            if delta_info:  # Only write if delta_info exists
                for metric, value in delta_info.items():
                    if metric == "command_deltas":
                        for cmd, count in value.items():
                            writer.writerow(["delta", f"command_deltas_{cmd}", count, node])
                    else:
                        write_flattened("delta", {metric: value}, node_identifier=node)
                        

def flatten_dict(data, prefix=''):
    flat = {}
    if isinstance(data, dict):
        for k, v in data.items():
            new_key = f"{prefix}_{k}" if prefix else k
            flat.update(flatten_dict(v, new_key))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            new_key = f"{prefix}_{i}"
            flat.update(flatten_dict(v, new_key))
    else:
        flat[prefix] = data
    return flat


def write_json(filename, cluster_data, keyspace_info, node_data, source_host=None, cluster_update_info=None):
    # Use updated cluster information when available (same logic as print_summary and write_csv)
    cluster_data_to_write = cluster_data.copy()
    
    if cluster_update_info:
        # Update with accurate cluster topology discovered during metrics collection
        cluster_data_to_write["cluster_mode"] = cluster_update_info.get("cluster_mode", cluster_data.get("cluster_mode", False))
        cluster_data_to_write["primaries"] = cluster_update_info.get("primaries", 0)
        cluster_data_to_write["replicas"] = cluster_update_info.get("replicas", 0)
        
        # Update node addresses and roles if available
        if "node_addresses" in cluster_update_info:
            cluster_data_to_write["node_addresses"] = cluster_update_info["node_addresses"]
        if "roles" in cluster_update_info:
            cluster_data_to_write["roles"] = cluster_update_info["roles"]
    
    out = {}

    # Write summary metrics first, then remaining cluster data
    cluster_dict = {}
    
    # Add summary metrics first (most important metrics)
    summary_metrics = {k: v for k, v in cluster_data_to_write.items() if k.startswith("summary_metric_")}
    cluster_dict.update(flatten_dict(summary_metrics))
    
    # Add remaining cluster-level data
    remaining_data = {k: v for k, v in cluster_data_to_write.items() 
                     if k != "roles" and k != "primary_replica_connections" and not k.startswith("summary_metric_")}
    cluster_dict.update(flatten_dict(remaining_data))
    
    out["cluster"] = cluster_dict

    # Add node roles if present
    if "roles" in cluster_data_to_write:
        out["cluster"]["roles"] = cluster_data_to_write["roles"]
    # Add primary_replica_connections if present
    if "primary_replica_connections" in cluster_data:
        out["cluster"]["primary_replica_connections"] = cluster_data["primary_replica_connections"]

    # Flatten and write keyspace info
    out["keyspace"] = {}
    out["keyspace"]["cluster_total"] = keyspace_info.get("cluster_total", 0)
    out["keyspace"]["per_node"] = {}
    for node, db_info in keyspace_info.get("per_node", {}).items():
        node_flat = {}
        for db, stats in db_info.items():
            for stat_key, stat_val in stats.items():
                node_flat[f"{db}_{stat_key}"] = stat_val
        out["keyspace"]["per_node"][node] = node_flat

    # Flatten and write node data
    out["nodes"] = {}
    for node, data in node_data.items():
        out["nodes"][node] = flatten_dict(data)

    # Flatten and write deltas (extract from __delta_info in node_data)
    out["deltas"] = {}
    roles = cluster_data.get("roles", {})
    for node, node_info in node_data.items():
        delta_info = node_info.get("__delta_info", {})
        if delta_info:  # Only write if delta_info exists
            flat = {}
            for k, v in delta_info.items():
                if k == "command_deltas":
                    flat.update({f"command_deltas_{cmd}": count for cmd, count in v.items()})
                else:
                    flat.update(flatten_dict({k: v}))
            # Add role field
            flat["role"] = roles.get(node, "")
            out["deltas"][node] = flat

    # Write to file
    with open(filename, "w") as f:
        json.dump(out, f, indent=2)
        

def bytes_to_gb_string(num_bytes):
    return f"{num_bytes / 1_000_000_000:.2f} GB"

def bytes_to_gb_string_with_limit_note(num_bytes):
    """Format bytes to GB with '(no limit)' annotation when value is 0"""
    gb_value = f"{num_bytes / 1_000_000_000:.2f} GB"
    if num_bytes == 0:
        return f"{gb_value} (no limit)"
    return gb_value


def print_summary(cluster_data, all_node_data, keyspace_summary, quiet=False, cluster_update_info=None, legacy_units=False):
    """
    Print a comprehensive summary of the cluster assessment results.
    
    Args:
        cluster_data: Dictionary containing cluster-level metrics and info
        all_node_data: Dictionary containing per-node metrics 
        keyspace_summary: Dictionary containing keyspace analysis results
        quiet: Whether to suppress detailed output
    """
    
    if quiet:
        return
    
    engine_versions = set()
    eviction_policies = set()
    persistence_modes = set()
    total_memory = 0
    used_memory = 0
    used_memory_min_total = 0
    used_memory_max_total = 0

    def safe_divide(numerator, denominator):
        """Helper function to safely divide and avoid division by zero"""
        return numerator / denominator if denominator > 0 else 0

    def fmt_rate(val, per_sec=False):
        if val >= 1_000_000:
            return f"{val / 1_000_000:.1f}M" + ("/sec" if per_sec else "")
        elif val >= 1_000:
            return f"{val / 1_000:.1f}K" + ("/sec" if per_sec else "")
        else:
            return f"{val:.1f}" + ("/sec" if per_sec else "")

    def fmt_bytes(bytes_val):
        """Format bytes in human readable format"""
        if bytes_val >= 1_000_000:
            return f"{bytes_val / 1_000_000:.1f} MB"
        elif bytes_val >= 1_000:
            return f"{bytes_val / 1_000:.1f} KB"
        else:
            return f"{bytes_val:.0f} B"
    
    def fmt_traffic_rate(bytes_per_sec, legacy_units=False):
        """Format traffic rate in Gbps by default or legacy units if requested"""
        if legacy_units:
            # Legacy format: KB/sec, MB/sec, B/sec
            if bytes_per_sec >= 1_000_000:
                return f"{bytes_per_sec / 1_000_000:.2f} MB/sec"
            elif bytes_per_sec >= 1_000:
                return f"{bytes_per_sec / 1_000:.1f} KB/sec"
            else:
                return f"{bytes_per_sec:.0f} B/sec"
        else:
            # Default format: Gbps (need more decimal places for small values)
            gbps = (bytes_per_sec * 8) / 1_000_000_000  # Convert bytes/sec to Gbps
            if gbps >= 0.001:
                return f"{gbps:.3f} Gbps"
            elif gbps >= 0.0001:
                return f"{gbps:.4f} Gbps"
            elif gbps >= 0.00001:
                return f"{gbps:.5f} Gbps"
            else:
                return f"{gbps:.6f} Gbps"

    # Default baseline traffic if not provided
    logger.debug("Using raw measurements without baseline traffic subtraction")

    # Extract delta information from embedded __delta_info in all_node_data
    data_source = {}
    logger.debug("Using __delta_info from all_node_data")
    for addr, node_data in all_node_data.items():
        # Extract the nested __delta_info structure which contains the operation counts
        delta_info = node_data.get("__delta_info", {})
        data_source[addr] = delta_info

    logger.debug(f"Processing metrics for {len(data_source)} nodes")
    
    # Get roles from cluster_data
    roles = cluster_data.get("roles", {})
    logger.debug(f"Node roles: {len([r for r in roles.values() if r == 'primary'])} primaries, {len([r for r in roles.values() if r == 'replica'])} replicas")
    
    # Calculate memory usage from primary nodes
    for addr, node_data in all_node_data.items():
        role = roles.get(addr, "unknown")
        if role == "primary":
            node_used_memory = node_data.get("used_memory", 0)
            node_used_memory_min = node_data.get("used_memory_min", node_used_memory)
            node_used_memory_max = node_data.get("used_memory_max", node_used_memory)

            used_memory += node_used_memory
            used_memory_min_total += node_used_memory_min
            used_memory_max_total += node_used_memory_max
            total_memory += node_data.get("maxmemory", 0)
            # Collect engine versions and policies
            if "redis_version" in node_data:
                engine_versions.add(f"Redis OSS {node_data['redis_version']}")
            if "valkey_version" in node_data:
                engine_versions.add(f"Valkey {node_data['valkey_version']}")
            if "maxmemory_policy" in node_data:
                eviction_policies.add(node_data["maxmemory_policy"])
            elif "config_maxmemory-policy" in node_data and node_data["config_maxmemory-policy"] != "N/A":
                eviction_policies.add(node_data["config_maxmemory-policy"])
            aof_status = node_data.get("aof_enabled", "N/A")
            persistence_modes.add(str(aof_status))
    
    # Initialize aggregation variables
    prim_duration_sum = 0
    rep_duration_sum = 0
    prim_keys_written_sum = 0
    prim_keys_read_sum = 0
    rep_keys_read_sum = 0
    prim_in_bytes_sum = 0
    prim_out_bytes_sum = 0
    prim_repl_out_bytes_sum = 0
    rep_repl_in_bytes_sum = 0
    rep_in_bytes_sum = 0
    rep_out_bytes_sum = 0
    prim_count = 0
    rep_count = 0
    
    # Track baseline-adjusted traffic
    prim_client_out_bytes_sum = 0
    rep_client_out_bytes_sum = 0
    
    # DEBUG: Add detailed logging for network metrics
    logger.debug("=== NETWORK METRICS DEBUG ===")
    
    # NEW APPROACH: Direct measurement - only count traffic from nodes with operations
    # This ensures consistent results regardless of primary vs replica workload
    
    # Initialize aggregation variables
    prim_duration_sum = 0
    rep_duration_sum = 0
    prim_keys_written_sum = 0
    prim_keys_read_sum = 0
    rep_keys_read_sum = 0
    prim_in_bytes_sum = 0
    prim_out_bytes_sum = 0
    prim_repl_out_bytes_sum = 0
    rep_repl_in_bytes_sum = 0
    rep_in_bytes_sum = 0
    rep_out_bytes_sum = 0
    prim_count = 0
    rep_count = 0
    
    # Aggregate operations first to get totals
    for addr, delta in data_source.items():
        role = roles.get(addr, "unknown")
        read_ops = delta.get("total_read_ops", 0)
        write_ops = delta.get("total_write_ops", 0)
        
        # Log if using fallback estimation
        if delta.get("estimated_from_total_commands", False):
            logger.debug(f"{fmt_node(addr)} Using fallback estimation: {read_ops} reads, {write_ops} writes")
        
        # Handle replica write ops (which are replication, not client ops)
        if role == "replica" and write_ops > 0:
            write_ops = 0  # Don't count replica writes as client writes
        
        if role == "primary":
            prim_duration_sum += delta.get("duration_seconds", 0)
            prim_keys_written_sum += write_ops
            prim_keys_read_sum += read_ops
            prim_in_bytes_sum += delta.get("net_in_bytes", 0)
            prim_out_bytes_sum += delta.get("net_out_bytes", 0)
            prim_repl_out_bytes_sum += delta.get("net_repl_out_bytes", 0)
            prim_count += 1
        elif role == "replica":
            rep_duration_sum += delta.get("duration_seconds", 0)
            rep_keys_read_sum += read_ops  # Count client read operations from replicas
            rep_repl_in_bytes_sum += delta.get("net_repl_in_bytes", 0)
            rep_in_bytes_sum += delta.get("net_in_bytes", 0)
            rep_out_bytes_sum += delta.get("net_out_bytes", 0)
            rep_count += 1
    
    total_client_operations = prim_keys_written_sum + prim_keys_read_sum + rep_keys_read_sum
    total_client_traffic_bytes = 0
    
    # Variables for final calculations
    total_write_ops = prim_keys_written_sum
    total_read_ops = prim_keys_read_sum + rep_keys_read_sum
    
    # Calculate network rates for legacy compatibility
    prim_duration = safe_divide(prim_duration_sum, prim_count)
    rep_duration = safe_divide(rep_duration_sum, rep_count)
    prim_in_rate = safe_divide(prim_in_bytes_sum, prim_duration)
    prim_out_rate = safe_divide(prim_out_bytes_sum, prim_duration)
    prim_repl_out_rate = safe_divide(prim_repl_out_bytes_sum, prim_duration)
    rep_repl_in_rate = safe_divide(rep_repl_in_bytes_sum, rep_duration)
    rep_in_rate = safe_divide(rep_in_bytes_sum, rep_duration)
    rep_out_rate = safe_divide(rep_out_bytes_sum, rep_duration)
    
    # Calculate operation rates for legacy compatibility
    prim_write_rate = safe_divide(prim_keys_written_sum, prim_duration)
    prim_read_rate = safe_divide(prim_keys_read_sum, prim_duration)
    rep_read_rate = safe_divide(rep_keys_read_sum, rep_duration)
    total_write_rate = prim_write_rate
    total_read_rate = prim_read_rate + rep_read_rate
    
    # DEBUG: Add detailed logging for network metrics
    logger.debug("=== DIRECT MEASUREMENT ANALYSIS ===")
    
    # Direct measurement: calculate bytes per operation from nodes with operations
    for addr, delta in data_source.items():
        role = roles.get(addr, "unknown")
        read_ops = delta.get("total_read_ops", 0)
        write_ops = delta.get("total_write_ops", 0)
        
        # Handle replica write ops (which are replication, not client ops)
        if role == "replica" and write_ops > 0:
            write_ops = 0  # Don't count replica writes as client writes
        
        total_ops_for_node = read_ops + write_ops
        
        logger.debug(f"{fmt_node(addr)} Node ({role}): {read_ops} reads, {write_ops} writes")
        
        if total_ops_for_node > 0:
            # This node has client operations, so count its traffic
            net_out = delta.get("net_out_bytes", 0)
            net_in = delta.get("net_in_bytes", 0)
            net_repl_out = delta.get("net_repl_out_bytes", 0)
            
            # Calculate pure client output traffic by subtracting replication when possible
            client_net_out = net_out
            if role == "primary" and net_repl_out > 0:
                # We have reliable replication metrics, subtract replication traffic
                client_net_out = max(0, net_out - net_repl_out)
            
            # Calculate traffic correctly for mixed workloads by avoiding double-counting
            # Mixed workload: calculate as if they were separate pure workloads, then combine
            if read_ops > 0 and write_ops > 0:
                total_ops_node = read_ops + write_ops
                
                # Calculate what each operation type would contribute if they were pure workloads
                bytes_per_read = safe_divide(client_net_out, read_ops)
                bytes_per_write = safe_divide(net_in, write_ops)
                
                # Only count the actual data payload for each operation type
                read_traffic_bytes = bytes_per_read * read_ops
                write_traffic_bytes = bytes_per_write * write_ops
                
                # But since reads and writes share the connection, we get overhead on both directions
                # Take the average to avoid double-counting the shared connection overhead
                mixed_traffic_bytes = (read_traffic_bytes + write_traffic_bytes) / 2
                total_client_traffic_bytes += mixed_traffic_bytes
                
                mixed_traffic_per_op = safe_divide(mixed_traffic_bytes, total_ops_node)
                logger.debug(f"{fmt_node(addr)}   Mixed workload: {total_ops_node} ops × {mixed_traffic_per_op:.1f} bytes/op = {mixed_traffic_bytes:.0f} bytes (avg of read:{read_traffic_bytes:.0f} + write:{write_traffic_bytes:.0f})")
            elif read_ops > 0:
                # Read-only: count output traffic (responses)
                bytes_per_read = safe_divide(client_net_out, read_ops)
                read_traffic_bytes = bytes_per_read * read_ops
                total_client_traffic_bytes += read_traffic_bytes
                
                logger.debug(f"{fmt_node(addr)}   Read-only: {read_ops} reads × {bytes_per_read:.1f} bytes/read = {read_traffic_bytes:.0f} bytes")
            elif write_ops > 0:
                # Write-only: count input traffic (requests)
                bytes_per_write = safe_divide(net_in, write_ops)
                write_traffic_bytes = bytes_per_write * write_ops
                total_client_traffic_bytes += write_traffic_bytes
                
                logger.debug(f"{fmt_node(addr)}   Write-only: {write_ops} writes × {bytes_per_write:.1f} bytes/write = {write_traffic_bytes:.0f} bytes")
    
    # ENGINE-BASED CLIENT TRAFFIC CALCULATION
    # Use Redis/Valkey engine metrics to accurately separate client vs replication traffic
    total_client_traffic_bytes_engine = 0
    measurement_duration = prim_duration if prim_duration > 0 else 10  # fallback to 10s
    
    logger.debug("=== ENGINE-BASED CLIENT TRAFFIC CALCULATION ===")
    
    for addr, delta_info in data_source.items():
        # delta_info is already the __delta_info dict (extracted in lines 3183-3188)
        # Client traffic = Total - Replication
        net_in = delta_info.get("net_in_bytes", 0)
        net_out = delta_info.get("net_out_bytes", 0) 
        net_repl_in = delta_info.get("net_repl_in_bytes", 0)
        net_repl_out = delta_info.get("net_repl_out_bytes", 0)
        
        client_net_in = max(0, net_in - net_repl_in)
        client_net_out = max(0, net_out - net_repl_out)
        node_client_traffic = client_net_in + client_net_out
        
        total_client_traffic_bytes_engine += node_client_traffic
        
        logger.debug(f"{fmt_node(addr)}   Client traffic: {node_client_traffic:,} bytes")
    
    # Calculate total client traffic rate from engine metrics  
    total_client_traffic_rate = safe_divide(total_client_traffic_bytes_engine, measurement_duration)
    
    logger.debug(f"Total client traffic (engine): {total_client_traffic_bytes_engine:,} bytes over {measurement_duration}s")
    logger.debug(f"Total client traffic rate (engine): {total_client_traffic_rate:,.0f} bytes/sec")
    
    # Update avg_bytes_per_client_op to use engine-based calculation
    avg_bytes_per_client_op = safe_divide(total_client_traffic_bytes_engine, total_client_operations)
    
    # For legacy compatibility with existing code, still provide split estimates
    write_ratio = safe_divide(total_write_ops, total_write_ops + total_read_ops)
    read_ratio = safe_divide(total_read_ops, total_write_ops + total_read_ops)
    
    client_write_traffic = total_client_traffic_rate * write_ratio
    client_read_traffic = total_client_traffic_rate * read_ratio
    
    # For legacy compatibility, split read traffic into primary/replica components
    # Base estimates on actual read operation distribution
    primary_read_ops = prim_keys_read_sum
    replica_read_ops = rep_keys_read_sum  
    total_read_ops_for_split = primary_read_ops + replica_read_ops
    
    if total_read_ops_for_split > 0:
        primary_read_ratio = safe_divide(primary_read_ops, total_read_ops_for_split)
        replica_read_ratio = safe_divide(replica_read_ops, total_read_ops_for_split)
    else:
        primary_read_ratio = 0.5
        replica_read_ratio = 0.5
    
    client_read_traffic_from_primaries = client_read_traffic * primary_read_ratio  
    client_read_traffic_from_replicas = client_read_traffic * replica_read_ratio
    replication_traffic = prim_repl_out_rate

    # DEBUG: Log replication traffic calculations
    logger.debug("=== REPLICATION TRAFFIC DEBUG ===")
    logger.debug(f"Primary replication out bytes sum: {prim_repl_out_bytes_sum:,} bytes")
    logger.debug(f"Primary duration: {prim_duration} seconds")
    logger.debug(f"Primary replication out rate: {prim_repl_out_rate:,.0f} bytes/sec")
    logger.debug(f"Replication traffic rate: {replication_traffic:,.0f} bytes/sec")
    logger.debug(f"Replication bandwidth: {(replication_traffic * 8) / 1_000_000_000:.6f} Gbps")
    
    # FALLBACK: Calculate replication traffic from replica data when dedicated metrics are unavailable
    if replication_traffic == 0 and prim_keys_written_sum > 0:
        logger.debug("=== REPLICATION TRAFFIC FALLBACK CALCULATION ===")
        
        # Calculate replication traffic from replica inbound traffic for replicated operations
        estimated_replication_traffic = 0
        total_replicated_ops = 0
        
        for addr, delta in data_source.items():
            role = roles.get(addr, "unknown")
            if role == "replica":
                # Get replica metrics
                write_ops = delta.get("total_write_ops", 0)  # These are replicated writes
                net_in = delta.get("net_in_bytes", 0)
                read_ops = delta.get("total_read_ops", 0)
                duration = delta.get("duration_seconds", 10)
                
                if write_ops > 0:
                    # Calculate bytes per replicated operation for this replica
                    # We need to subtract read traffic from total inbound traffic
                    read_traffic_estimate = 0
                    if read_ops > 0:
                        # Estimate read request size (typically much smaller than responses)
                        avg_read_request_size = 50  # Conservative estimate for GET request
                        read_traffic_estimate = read_ops * avg_read_request_size
                    
                    # Remaining inbound traffic is likely replication
                    replication_traffic_for_replica = max(0, net_in - read_traffic_estimate)
                    estimated_replication_traffic += replication_traffic_for_replica
                    total_replicated_ops += write_ops
                    
                    logger.debug(f"{fmt_node(addr)}   Replica: {write_ops:,} replicated writes, {net_in:,}B in, estimated {replication_traffic_for_replica:,}B replication")
        
        if estimated_replication_traffic > 0:
            # Use the available duration (prim_duration should be available since we have writes)
            duration_for_calc = max(prim_duration, rep_duration) if prim_duration > 0 or rep_duration > 0 else 10
            replication_traffic = safe_divide(estimated_replication_traffic, duration_for_calc)
            logger.debug(f"  Total estimated replication traffic: {estimated_replication_traffic:,} bytes")
            logger.debug(f"  Estimated replication rate: {replication_traffic:,.0f} bytes/sec")
            logger.debug(f"  Total replicated operations: {total_replicated_ops:,}")
            logger.debug(f"  Avg bytes per replicated operation: {safe_divide(estimated_replication_traffic, total_replicated_ops):,.1f} bytes")
            
            # Update the replication traffic value
            logger.debug(f"  Using fallback replication calculation: {replication_traffic:,.0f} bytes/sec")
        else:
            logger.debug("  No replication traffic detected in fallback calculation")

    # DEBUG: Log client traffic calculations using NEW direct measurement method
    logger.debug("=== CLIENT TRAFFIC CALCULATIONS (DIRECT MEASUREMENT METHOD) ===")
    logger.debug(f"Total client operations: {total_client_operations}")
    logger.debug(f"Total client traffic bytes (direct measurement): {total_client_traffic_bytes:,.0f} bytes")
    logger.debug(f"Average bytes per client operation (direct): {avg_bytes_per_client_op:,.1f} bytes")
    logger.debug(f"Client write traffic rate: {client_write_traffic:,.0f} bytes/sec")
    logger.debug(f"Client read traffic from primaries: {client_read_traffic_from_primaries:,.0f} bytes/sec")
    logger.debug(f"Client read traffic from replicas: {client_read_traffic_from_replicas:,.0f} bytes/sec")
    logger.debug(f"Total client read traffic: {client_read_traffic:,.0f} bytes/sec")
    logger.debug(f"Replication traffic rate: {replication_traffic:,.0f} bytes/sec")
    
    # Additional debugging for network metrics validation
    logger.debug("=== NETWORK METRICS VALIDATION ===")
    for addr, delta in data_source.items():
        role = roles.get(addr, "unknown")
        read_ops = delta.get("total_read_ops", 0)
        write_ops = delta.get("total_write_ops", 0)
        
        if role == "replica" and write_ops > 0:
            write_ops = 0
        
        total_ops_for_node = read_ops + write_ops
        
        if total_ops_for_node > 0:
            net_out = delta.get("net_out_bytes", 0)
            net_in = delta.get("net_in_bytes", 0)
            net_repl_out = delta.get("net_repl_out_bytes", 0)
            net_repl_in = delta.get("net_repl_in_bytes", 0)
            
            if read_ops > 0:
                bytes_per_read = safe_divide(net_out, read_ops)
                logger.debug(f"{fmt_node(addr)}   ({role}): {read_ops} reads, {bytes_per_read:.1f} bytes/read")
                logger.debug(f"{fmt_node(addr)}     Total network out: {net_out:,} bytes, replication out: {net_repl_out:,} bytes")

    # For calculations that need durations, get them from the data
    measurement_duration = 10  # default fallback
    for delta in data_source.values():
        if delta.get("duration_seconds", 0) > 0:
            measurement_duration = delta["duration_seconds"]
            break
    
    # For mixed workloads, use the maximum of actual durations or the intended duration
    effective_duration = max(prim_duration, rep_duration) if prim_duration > 0 or rep_duration > 0 else measurement_duration
    
    # Ensure non-negative values
    client_read_traffic_from_primaries = max(0, client_read_traffic_from_primaries)
    client_read_traffic_from_replicas = max(0, client_read_traffic_from_replicas)
    client_read_traffic = max(0, client_read_traffic)
    client_write_traffic = max(0, client_write_traffic)
    total_client_traffic_rate = client_write_traffic + client_read_traffic
    total_network_bandwidth = total_client_traffic_rate + replication_traffic
    total_bandwidth_gbps = (total_network_bandwidth * 8) / 1_000_000_000
    


    # Create and display cluster summary table
    table = Table(title="Cluster Summary")
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", style="magenta")

    # Cluster mode
    # Check if multi-node cluster was detected during collection
    updated_cluster_info = cluster_update_info
    
    if updated_cluster_info:
        cluster_mode = "Enabled" if updated_cluster_info.get("cluster_mode", False) else "Disabled"
        primaries_count = updated_cluster_info.get("primaries", 0)
        replicas_count = updated_cluster_info.get("replicas", 0)
        logger.debug(f"Using updated cluster data: {primaries_count} primaries, {replicas_count} replicas")
    else:
        cluster_mode = "Enabled" if cluster_data.get("cluster_mode", False) else "Disabled"
        primaries_count = cluster_data.get("primaries", 0)
        replicas_count = cluster_data.get("replicas", 0)
        logger.debug(f"Using original cluster data: {primaries_count} primaries, {replicas_count} replicas")
    table.add_row("Cluster Mode", cluster_mode)
    
    # Primaries and replicas
    table.add_row("Primaries", str(primaries_count))
    table.add_row("Replicas", str(replicas_count))
    
    # Engine versions
    engine_str = ", ".join(sorted(engine_versions)) if engine_versions else "Unknown"
    table.add_row("Engine(s)", engine_str)
    
    # Memory info
    table.add_row("Total Primary Memory Configured", bytes_to_gb_string_with_limit_note(total_memory))
    table.add_row("Total Primary Memory Used", bytes_to_gb_string(used_memory_max_total))
    
    # Total keys
    table.add_row("Total Primary Keys", f"{keyspace_summary.get('cluster_total', 0):,}")
    
    # Eviction policy
    eviction_str = ", ".join(sorted(eviction_policies)) if eviction_policies else "Unknown"
    table.add_row("Eviction Policy", eviction_str)

    # Always show cluster summary table
    console.print(table)
    
    # Check for counter resets and warn about measurement accuracy
    counter_reset_nodes = []
    for addr, delta in data_source.items():
        if delta.get("counter_reset_detected", False):
            counter_reset_nodes.append(addr)
    
    if counter_reset_nodes:
        console.print()
        console.print("[bold yellow]WARNING: Counter Reset/Wraparound Detected[/bold yellow]")
        console.print(f"[yellow]Network counters were reset on {len(counter_reset_nodes)} node(s) during measurement:[/yellow]")
        for addr in counter_reset_nodes:
            console.print(f"[yellow]  - {addr}[/yellow]")
        console.print()
        console.print("[yellow]This typically happens due to:[/yellow]")
        console.print("[yellow]  • Service restart during measurement[/yellow]")
        console.print("[yellow]  • Counter overflow (rare on 64-bit systems)[/yellow]")
        console.print("[yellow]  • System maintenance operations[/yellow]")
        console.print()
        console.print("[yellow]Network metrics for these nodes use instantaneous rate estimates[/yellow]")
        console.print("[yellow]and may be less accurate than delta-based measurements.[/yellow]")
        console.print()
    
    # Check for Valkey 8.0.x and warn about replication traffic bug (fixed in 8.1+)
    valkey_8_0_detected = False
    for version_str in engine_versions:
        if "Valkey" in version_str:
            # Extract version number (e.g., "Valkey 8.0.1" -> "8.0.1")
            try:
                version_part = version_str.split("Valkey ")[1]
                version_parts = version_part.split(".")
                major_version = int(version_parts[0])
                minor_version = int(version_parts[1]) if len(version_parts) > 1 else 0
                
                # Only warn for Valkey 8.0.x (bug was fixed in 8.1+)
                if major_version == 8 and minor_version == 0:
                    valkey_8_0_detected = True
                    break
            except (IndexError, ValueError):
                # If we can't parse the version, skip the check
                continue
    
    if valkey_8_0_detected:
        console.print()
        console.print("[bold yellow]WARNING: Valkey 8.0.x Network Metrics Bug Detected[/bold yellow]")
        console.print("[yellow]Valkey 8.0.x versions have a bug where replication network traffic[/yellow]")
        console.print("[yellow]metrics (total_net_repl_output_bytes) are not properly updated, which can[/yellow]")
        console.print("[yellow]affect the accuracy of read traffic calculations in this assessment.[/yellow]")
        console.print()
        console.print("[yellow]This bug was fixed in Valkey 8.1+. For more details, see:[/yellow]")
        console.print("[blue]https://github.com/valkey-io/valkey/pull/1486[/blue]")
        console.print()
    
    # Always show workload summary - regardless of quiet mode
    # This will be added after the excel_v2 metrics are calculated
    # Calculate values from the already computed excel_v2 metrics
    total_ops_sec = round(safe_divide(total_write_ops + total_read_ops, effective_duration), 0)
    client_bandwidth_gbps = round((client_write_traffic + client_read_traffic) * 8 / 1_000_000_000, 3)
    replication_bandwidth_gbps = round((replication_traffic * 8) / 1_000_000_000, 6)  # Increased precision for small values
    total_bandwidth_gbps = round(((total_client_traffic_rate + replication_traffic) * 8) / 1_000_000_000, 3)
    
    # Calculate ECPUs per second for ElastiCache Serverless assessment
    # ECPU = 1 command + up to 1024 bytes. Formula: max(1.0, bytes_per_command / 1024)
    ecpus_per_second = 0
    if total_ops_sec > 0:
        # Use the avg_bytes_per_client_op calculated from total traffic method
        ecpus_per_operation = max(1.0, avg_bytes_per_client_op / 1024)
        ecpus_per_second = total_ops_sec * ecpus_per_operation
    
    # DEBUG: Log final calculations
    logger.debug("=== FINAL CALCULATIONS ===")
    logger.debug(f"Total operations: {total_write_ops + total_read_ops} over {effective_duration}s = {total_ops_sec} ops/sec")
    logger.debug(f"Client bandwidth: {client_write_traffic + client_read_traffic:,.0f} bytes/sec = {client_bandwidth_gbps} Gbps")
    logger.debug(f"Avg bytes per operation: {avg_bytes_per_client_op:,.1f} bytes/op (from total traffic method)")
    logger.debug(f"ECPUs per second: {ecpus_per_second:,.0f}")
    
    console.print(f"\n[bold green]Cluster workload information[/bold green]")
    console.print(f"    [cyan]Client commands      [/cyan] : {total_ops_sec:,} / sec")
    if total_ops_sec > 0:
        console.print(f"    [cyan]Avg bytes per command[/cyan] : {avg_bytes_per_client_op:,.0f} bytes")
    console.print(f"    [cyan]Client bandwidth     [/cyan] : {client_bandwidth_gbps} Gbps")
    
    # Format replication bandwidth with appropriate precision
    if replication_bandwidth_gbps < 0.001:
        console.print(f"    [cyan]Replication bandwidth[/cyan] : {replication_bandwidth_gbps:.6f} Gbps")
    else:
        console.print(f"    [cyan]Replication bandwidth[/cyan] : {replication_bandwidth_gbps:.3f} Gbps")
    
    console.print(f"    [cyan]Total bandwidth      [/cyan] : {total_bandwidth_gbps} Gbps")
    if total_ops_sec > 0:
        console.print(f"    [cyan]Estimated ECPUs/sec  [/cyan] : {ecpus_per_second:,.0f}")
    
    # Add version-specific notes about calculation accuracy
    console.print()
    
    # Determine calculation accuracy based on engine versions
    has_reliable_replication_metrics = False
    has_unreliable_metrics = False
    has_no_replication_metrics = False
    
    for version_str in engine_versions:
        if "Redis" in version_str:
            try:
                version_part = version_str.split("Redis ")[1]
                major_version = int(version_part.split(".")[0])
                minor_version = int(version_part.split(".")[1]) if "." in version_part else 0
                
                if major_version >= 7:
                    has_reliable_replication_metrics = True
                else:
                    has_no_replication_metrics = True
            except (IndexError, ValueError):
                continue
                
        elif "Valkey" in version_str:
            try:
                version_part = version_str.split("Valkey ")[1]
                version_parts = version_part.split(".")
                major_version = int(version_parts[0])
                minor_version = int(version_parts[1]) if len(version_parts) > 1 else 0
                
                if major_version == 8 and minor_version == 0:
                    has_unreliable_metrics = True
                elif major_version >= 7:
                    has_reliable_replication_metrics = True
                else:
                    has_no_replication_metrics = True
            except (IndexError, ValueError):
                continue
    
    # Display appropriate accuracy note
    if has_unreliable_metrics:
        console.print("[yellow]Note: 'Avg bytes per command' may be inaccurate due to Valkey 8.0.x replication metrics bug.[/yellow]")
    elif has_no_replication_metrics:
        console.print("[yellow]Note: 'Avg bytes per command' includes replication traffic for mixed workloads on primaries\n   (Redis OSS 5.0-6.2 appear to lack dedicated replication metrics for pure client traffic calculation).[/yellow]")
    elif has_reliable_replication_metrics:
        console.print("[green]Note: 'Avg bytes per command' represents pure client traffic (replication excluded).[/green]")
    
    # Show per-node breakdown for all nodes (including those with no activity)
    if data_source:
        console.print(f"\n[bold green]Per-node operation breakdown[/bold green]")
        
        node_metrics = []
        for addr, delta_info in data_source.items():
            role = roles.get(addr, "unknown")
            read_ops = delta_info.get("total_read_ops", 0)
            write_ops = delta_info.get("total_write_ops", 0)
            
            # Handle replica write ops (which are replication, not client ops)
            if role == "replica" and write_ops > 0:
                # For replicas, write_ops represents replication, not client operations
                client_writes = 0
            else:
                client_writes = write_ops
            
            # Calculate client traffic for this node using engine metrics
            net_in = delta_info.get("net_in_bytes", 0)
            net_out = delta_info.get("net_out_bytes", 0) 
            net_repl_in = delta_info.get("net_repl_in_bytes", 0)
            net_repl_out = delta_info.get("net_repl_out_bytes", 0)
            
            client_net_in = max(0, net_in - net_repl_in)
            client_net_out = max(0, net_out - net_repl_out)
            node_client_traffic_bytes = client_net_in + client_net_out
            
            # Calculate client traffic rate for this node
            node_client_traffic_rate = safe_divide(node_client_traffic_bytes, measurement_duration)
            
            node_metrics.append({
                "addr": addr,
                "role": role,
                "read_ops": read_ops,
                "client_write_ops": client_writes,
                "client_traffic_rate": node_client_traffic_rate,
                "client_traffic_bytes": node_client_traffic_bytes
            })
        
        # Sort primaries first, then replicas (each group sorted by client traffic rate descending)
        primaries = [node for node in node_metrics if node["role"] == "primary"]
        replicas = [node for node in node_metrics if node["role"] == "replica"]
        
        # Sort each group by client traffic rate (busiest first)
        primaries.sort(key=lambda x: x["client_traffic_rate"], reverse=True)
        replicas.sort(key=lambda x: x["client_traffic_rate"], reverse=True)
        
        # Display primaries first, then replicas
        sorted_nodes = primaries + replicas
        
        for node in sorted_nodes:
            read_ops_total = node["read_ops"]
            write_ops_total = node["client_write_ops"]
            traffic_rate = node["client_traffic_rate"]
            
            # Calculate operations per second for consistency with traffic rate
            read_ops_per_sec = safe_divide(read_ops_total, measurement_duration)
            write_ops_per_sec = safe_divide(write_ops_total, measurement_duration)
            
            # Format operations per second with appropriate precision
            if read_ops_per_sec < 1:
                read_str = f"{read_ops_per_sec:.1f} reads/sec"
            else:
                read_str = f"{read_ops_per_sec:.0f} reads/sec"
                
            if write_ops_per_sec < 1:
                write_str = f"{write_ops_per_sec:.1f} writes/sec"
            else:
                write_str = f"{write_ops_per_sec:.0f} writes/sec"
            
            # Format traffic rate using the selected unit format
            traffic_str = fmt_traffic_rate(traffic_rate, legacy_units)
            
            console.print(f"    [cyan]{node['addr']} ({node['role']})[/cyan]: {read_str}, {write_str}, {traffic_str} client traffic")
    
    console.print()
    
    # Keep the "no writes/reads detected" messages if applicable
    if total_write_ops == 0:
        console.print(f"[bold cyan]No write activity detected[/bold cyan]")
    if total_read_ops == 0:
        console.print(f"[bold cyan]No read activity detected[/bold cyan]")

    # Rename and add new fields in cluster_data for output
    cluster_data["engines"] = list(sorted(engine_versions))
    cluster_data["eviction_policy"] = list(sorted(eviction_policies))
    cluster_data["aof_enabled"] = list(sorted(persistence_modes))
    cluster_data["total_primaries_configured_memory"] = total_memory
    cluster_data["total_primaries_configured_memory_human"] = bytes_to_gb_string(total_memory)
    cluster_data["total_primaries_used_memory"] = used_memory_max_total
    cluster_data["total_primaries_used_memory_human"] = bytes_to_gb_string(used_memory_max_total)
    cluster_data["total_primaries_used_memory_min"] = used_memory_min_total
    cluster_data["total_primaries_used_memory_min_human"] = bytes_to_gb_string(used_memory_min_total)
    cluster_data["total_primaries_used_memory_max"] = used_memory_max_total
    cluster_data["total_primaries_used_memory_max_human"] = bytes_to_gb_string(used_memory_max_total)
    cluster_data["all_primaries_write_count_per_second"] = float(f"{prim_write_rate:.1f}")
    cluster_data["all_primaries_write_gbps_per_second"] = round((client_write_traffic * 8) / 1_000_000_000, 6)
    cluster_data["all_primaries_read_count_per_second"] = float(f"{prim_read_rate:.1f}")
    cluster_data["all_primaries_read_gbps_per_second"] = round((client_read_traffic * 8) / 1_000_000_000, 6)
    cluster_data["all_replicas_read_count_per_second"] = float(f"{rep_read_rate:.1f}")
    cluster_data["all_replicas_read_gbps_per_second"] = round((rep_in_rate * 8) / 1_000_000_000, 6)
    
    # Add summary metrics for easy consumption and capacity planning
    # Memory usage
    cluster_data["summary_metric_memory_gb"] = used_memory / 1_000_000_000  # Convert to GB
    cluster_data["summary_metric_memory_min_gb"] = used_memory_min_total / 1_000_000_000
    cluster_data["summary_metric_memory_max_gb"] = used_memory_max_total / 1_000_000_000
    
    # Operations per second
    cluster_data["summary_metric_total_write_ops_sec"] = round(safe_divide(total_write_ops, effective_duration), 2)
    cluster_data["summary_metric_total_read_ops_sec"] = round(safe_divide(total_read_ops, effective_duration), 2)
    cluster_data["summary_metric_total_ops_sec"] = round(safe_divide(total_write_ops + total_read_ops, effective_duration), 2)
    
    # Calculate separate averages for read and write operations
    total_write_traffic_bytes = 0
    total_read_traffic_bytes = 0
    
    for addr, delta in data_source.items():
        role = roles.get(addr, "unknown")
        read_ops = delta.get("total_read_ops", 0)
        write_ops = delta.get("total_write_ops", 0)
        net_out = delta.get("net_out_bytes", 0)
        net_in = delta.get("net_in_bytes", 0)
        net_repl_out = delta.get("net_repl_out_bytes", 0)
        
        # Handle replica write ops (which are replication, not client ops)
        if role == "replica" and write_ops > 0:
            write_ops = 0
        
        # Calculate pure client traffic by subtracting replication when possible
        client_net_out = net_out
        if role == "primary" and net_repl_out > 0:
            # We have reliable replication metrics, subtract replication traffic
            client_net_out = max(0, net_out - net_repl_out)
        
        # Add to read traffic calculation (from both primaries and replicas)
        if read_ops > 0:
            total_read_traffic_bytes += client_net_out
        
        # Add to write traffic calculation (only from primaries)
        if write_ops > 0 and role == "primary":
            total_write_traffic_bytes += net_in
    
    # Calculate averages
    avg_write_size = safe_divide(total_write_traffic_bytes, total_write_ops) if total_write_ops > 0 else 0
    avg_read_size = safe_divide(total_read_traffic_bytes, total_read_ops) if total_read_ops > 0 else 0
    
    cluster_data["summary_metric_avg_write_size"] = int(avg_write_size)
    cluster_data["summary_metric_avg_read_size"] = int(avg_read_size)
    
    cluster_data["summary_metric_avg_bytes_per_operation"] = round(avg_bytes_per_client_op, 1)
    
    # Client traffic bandwidth in Gbps
    cluster_data["summary_metric_total_write_bandwidth_gbps"] = round((client_write_traffic * 8) / 1_000_000_000, 3)
    cluster_data["summary_metric_total_read_bandwidth_gbps"] = round((client_read_traffic * 8) / 1_000_000_000, 3)
    cluster_data["summary_metric_total_client_bandwidth_gbps"] = round((client_write_traffic + client_read_traffic) * 8 / 1_000_000_000, 3)
    
    # Infrastructure capacity planning bandwidth in Gbps
    # Primary bandwidth = client writes + client reads from primaries + replication out
    cluster_data["summary_metric_total_primary_bandwidth_gbps"] = round((client_write_traffic + client_read_traffic_from_primaries + replication_traffic) * 8 / 1_000_000_000, 3)
    
    # Replica bandwidth = replication in + client reads from replicas
    replica_replication_in_rate = safe_divide(rep_repl_in_bytes_sum, rep_duration)
    cluster_data["summary_metric_total_replica_bandwidth_gbps"] = round((replica_replication_in_rate + client_read_traffic_from_replicas) * 8 / 1_000_000_000, 3)
    
    # Pure replication bandwidth (subset of primary and replica bandwidth)
    cluster_data["summary_metric_total_replication_bandwidth_gbps"] = round((replication_traffic * 8) / 1_000_000_000, 3)
    
    # Total cluster bandwidth = client traffic + replication traffic
    cluster_data["summary_metric_total_cluster_bandwidth_gbps"] = round(((total_client_traffic_rate + replication_traffic) * 8) / 1_000_000_000, 3)
    
    # Add ECPU metrics for ElastiCache Serverless assessment
    cluster_data["summary_metric_estimated_ecpus_per_sec"] = round(ecpus_per_second, 0)
    
    # Add per-node operation metrics for distribution analysis
    per_node_metrics = []
    for addr, delta in data_source.items():
        role = roles.get(addr, "unknown")
        read_ops = delta.get("total_read_ops", 0)
        write_ops = delta.get("total_write_ops", 0)
        
        # Handle replica write ops (which are replication, not client ops)
        if role == "replica" and write_ops > 0:
            write_ops = 0
        
        total_ops_for_node = read_ops + write_ops
        
        if total_ops_for_node > 0:
            net_out = delta.get("net_out_bytes", 0)
            net_in = delta.get("net_in_bytes", 0)
            net_repl_out = delta.get("net_repl_out_bytes", 0)
            
            # Calculate pure client traffic by subtracting replication when possible
            client_net_out = net_out
            if role == "primary" and net_repl_out > 0:
                # We have reliable replication metrics, subtract replication traffic
                client_net_out = max(0, net_out - net_repl_out)
            
            # Calculate average bytes per operation for this node using pure client traffic
            if read_ops > 0 and write_ops > 0:
                # Mixed workload - use total client traffic
                node_avg_bytes = safe_divide(client_net_out + net_in, total_ops_for_node)
                op_type = "mixed"
            elif read_ops > 0:
                # Read-only workload
                node_avg_bytes = safe_divide(client_net_out, read_ops)
                op_type = "reads"
            else:
                # Write-only workload  
                node_avg_bytes = safe_divide(net_in, write_ops)
                op_type = "writes"
            
            ops_per_sec = safe_divide(total_ops_for_node, effective_duration)
            
            per_node_metrics.append({
                "node_address": addr,
                "node_role": role,
                "total_operations": total_ops_for_node,
                "operations_per_sec": round(ops_per_sec, 2),
                "avg_bytes_per_operation": round(node_avg_bytes, 1),
                "operation_type": op_type,
                "read_operations": read_ops,
                "write_operations": write_ops,
                "net_out_bytes": net_out,
                "net_in_bytes": net_in
            })
    
    cluster_data["per_node_operation_metrics"] = per_node_metrics
    
    # Add diagnostic overhead summary
    console.print()
    console.print("[bold yellow]Monitoring Tool Overhead Analysis[/bold yellow]")
    
    # Calculate estimated diagnostic commands and traffic
    num_nodes = len(data_source)
    num_collection_rounds = 2  # baseline + final
    
    # Estimated commands per node per collection (based on our tool's queries)
    estimated_info_commands = 7  # server, memory, stats, persistence, keyspace, commandstats, clients, replication
    estimated_cluster_commands = 2  # CLUSTER NODES, CLUSTER INFO  
    estimated_commands_per_node_per_round = estimated_info_commands + estimated_cluster_commands
    
    total_diagnostic_commands = num_nodes * num_collection_rounds * estimated_commands_per_node_per_round
    
    # Estimated traffic (based on our measurements)
    estimated_bytes_per_node_per_round = 8325  # From our measurement: ~8.3KB
    total_diagnostic_traffic_bytes = num_nodes * num_collection_rounds * estimated_bytes_per_node_per_round
    diagnostic_traffic_rate = safe_divide(total_diagnostic_traffic_bytes, measurement_duration)
    
    console.print(f"    [cyan]Estimated diagnostic commands[/cyan] : ~{total_diagnostic_commands:,} total")
    console.print(f"    [cyan]Estimated diagnostic traffic [/cyan] : ~{diagnostic_traffic_rate:,.0f} bytes/sec across all nodes")
    
    per_node_traffic_rate = safe_divide(diagnostic_traffic_rate, num_nodes)
    traffic_str = fmt_traffic_rate(per_node_traffic_rate, legacy_units)
    
    console.print(f"    [cyan]Per-node diagnostic overhead  [/cyan] : ~{traffic_str} average per node")
    console.print("[dim]Note: Low-traffic nodes may show mostly diagnostic overhead from this monitoring tool.[/dim]")
    
    # Remove old cluster_* fields if present
    for k in [
        "cluster_write_count_per_second", "cluster_writes_gbps_per_second",
        "cluster_read_count_per_second", "cluster_reads_gbps_per_second"
    ]:
        if k in cluster_data:
            del cluster_data[k]


def version_callback(value: bool):
    if value:
        typer.echo(f"{APP_NAME} v{VERSION}")
        raise typer.Exit()


async def main(
    host: str,
    port: int = 6379,
    user: str = None,
    password: str = None,
    tls: bool = False,
    output: str = None,
    json_output: str = None,
    duration: int = 60,
    log_level: str = "INFO",
    verbose: bool = False,
    quiet: bool = False,
    version: bool = False,
    legacy_units: bool = False,
):
    """
    Main function to run the in-memory assessment tool.
    
    Args:
        host: Redis/Valkey hostname or cluster endpoint
        port: Redis/Valkey port (default: 6379)
        user: Username for authentication (optional)
        password: Password for authentication (optional)
        tls: Use TLS/SSL connection (default: False)
        output: Output CSV file path (optional)
        json_output: Output JSON file path (optional)
        duration: Duration in seconds to collect metrics (default: 60)
        log_level: Log level (DEBUG, INFO, WARNING, ERROR)
        verbose: Enable verbose logging
        quiet: Suppress progress output
        version: Show version information
        legacy_units: Use legacy traffic units (KB/sec, MB/sec) instead of default Gbps
    """
    
    if version:
        console.print(f"{APP_NAME} v{VERSION}")
        return
    
    # Handle verbose flag
    if verbose:
        log_level = "DEBUG"
    
    # Set up logging
    setup_logging(log_level, quiet)
    
    logger.info(f"Starting in-memory assessment for {host}:{port}")
    logger.info(f"Assessment duration: {duration} seconds")
    
    source_host = f"{host}:{port}"
    
    # Detect cluster mode by actually probing the node
    logger.info("Detecting cluster mode...")
    detected_cluster_mode = await detect_cluster_mode_by_probing(host, port, user, password, tls)
    
    if detected_cluster_mode:
        logger.info(f"Detected cluster mode: {host}:{port} - using cluster client")
    else:
        logger.info(f"Detected standalone mode: {host}:{port} - using standalone client")
    
    # Verify authentication before starting assessment
    logger.info("Verifying authentication...")
    if not await verify_authentication(host, port, user, password, tls, detected_cluster_mode):
        logger.error("Connection verification failed")
        if not quiet:
            console.print("[bold red]Connection verification failed. Please check the hostname, port, and credentials.[/bold red]")
        sys.exit(1)
    
    logger.info("Authentication successful - connecting to cluster")
    redis_conn = await connect(host, port, user, password, tls, quiet, detected_cluster_mode)

    logger.info("Discovering cluster topology...")
    cluster_data = await get_cluster_info(redis_conn.client, redis_conn.source_host, redis_conn.is_cluster)
    node_addrs = cluster_data.pop("node_addresses", [])
    roles = cluster_data.get("roles", {})
    
    logger.info(f"Discovered {len(node_addrs)} nodes ({cluster_data.get('primaries', 0)} primaries, {cluster_data.get('replicas', 0)} replicas)")
    
    # Verify authentication for all cluster nodes
    if len(node_addrs) > 1:
        logger.info(f"Verifying authentication for {len(node_addrs)} cluster nodes...")
        failed_nodes = []
        
        # Parallel authentication verification - use cluster mode for cluster nodes
        tasks = []
        is_cluster = cluster_data.get("cluster_mode", False)
        for addr in node_addrs:
            host_part, port_part = addr.split(":")
            # Use the same cluster mode as detected for the main connection
            tasks.append(verify_authentication(host_part, int(port_part), user, password, tls, is_cluster))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Collect results
        for addr, result in zip(node_addrs, results):
            if isinstance(result, Exception):
                logger.error(f"{fmt_node(addr)} Authentication check failed: {str(result)}")
                failed_nodes.append(addr)
            elif not result:
                failed_nodes.append(addr)
        
        if failed_nodes:
            logger.error(f"Connection verification failed for {len(failed_nodes)} nodes")
            if not quiet:
                console.print(f"[bold red]Connection verification failed for nodes: {', '.join(failed_nodes)}[/bold red]")
                console.print("[bold red]Cannot proceed with cluster assessment. Check hostnames, ports, and credentials.[/bold red]")
            sys.exit(1)
        
        logger.info(f"Authentication verified for all {len(node_addrs)} nodes")

    logger.info("Collecting node metrics and keyspace information...")
    keyspace_summary = {"per_node": {}, "cluster_total": 0}
    all_node_data = {}
    delta_summary = {}

    # Collect metrics using the unified client approach
    # Use efficient cluster metrics collection for cluster mode
    if cluster_data.get("cluster_mode", False) and len(node_addrs) > 1:
        logger.info("Using efficient cluster metrics collection for multi-node cluster")
        baseline_metrics, final_metrics, delta_summary = await gather_metrics_efficiently_for_cluster(
            node_addrs, user, password, tls, duration, quiet, None, roles
        )
        cluster_update_info = None  # Set to None for multi-node cluster path
    else:
        logger.info("Using redis-py native metrics collection for single node")
        baseline_metrics, final_metrics, delta_summary, cluster_update_info = await collect_metrics_native(
            redis_conn.client, redis_conn.source_host, redis_conn.is_cluster, node_addrs, duration, quiet, roles, user, password, tls
        )
    
    # Process the results - use updated node list if multi-node cluster was detected
    actual_node_addrs = node_addrs
    if cluster_update_info:
        updated_node_addrs = cluster_update_info.get("node_addresses", [])
        updated_roles = cluster_update_info.get("roles", {})
        if updated_node_addrs:
            logger.info(f"Using updated node list: {len(updated_node_addrs)} nodes instead of {len(node_addrs)}")
            actual_node_addrs = updated_node_addrs
            # Also update the roles mapping
            roles.update(updated_roles)
            logger.info(f"Updated roles mapping: {updated_roles}")
    
    def _to_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    for addr in actual_node_addrs:
        if addr in baseline_metrics:
            baseline_node = baseline_metrics[addr]
            final_node = final_metrics.get(addr, {})

            baseline_used_memory = _to_int(baseline_node.get("used_memory", 0))
            final_used_memory = _to_int(final_node.get("used_memory", baseline_used_memory))

            used_memory_min = min(baseline_used_memory, final_used_memory)
            used_memory_max = max(baseline_used_memory, final_used_memory)

            baseline_node["used_memory_min"] = used_memory_min
            baseline_node["used_memory_max"] = used_memory_max

            all_node_data[addr] = baseline_node
            
            # Extract keyspace information for primaries
            if roles.get(addr) == "primary":
                keyspace_data = baseline_metrics[addr].get("keyspace_info", {})
                keyspace_summary["per_node"][addr] = keyspace_data
                
                # Calculate total keys for cluster summary
                if isinstance(keyspace_data, dict):
                    for db, stats in keyspace_data.items():
                        if isinstance(stats, dict):
                            keyspace_summary["cluster_total"] += stats.get("keys", 0)
            
            # Add delta information
            if addr in delta_summary:
                all_node_data[addr]["__delta_info"] = delta_summary[addr]

    logger.info("Generating cluster summary and reports...")
    print_summary(cluster_data, all_node_data, keyspace_summary, quiet, cluster_update_info, legacy_units)
    
    # Handle output file directory validation and creation
    # Check if user provided custom path with specific directory structure
    output_path = Path(output)
    if output_path.parent != Path('.') and str(output_path.parent) != 'output':
        # User provided custom path with specific directory - validate directory exists
        output = validate_custom_output_path(output)
    else:
        # Using default or simple filename - ensure proper directory structure
        output = ensure_output_directory(output, quiet)
    
    write_csv(output, cluster_data, keyspace_summary, all_node_data, source_host, cluster_update_info)

    # Handle JSON output file
    if json_output is None:
        # Derive JSON filename from CSV filename
        json_output = output.rsplit('.', 1)[0] + '.json'
    else:
        # User provided custom JSON path - validate or ensure directory exists
        json_path = Path(json_output)
        if json_path.parent != Path('.') and str(json_path.parent) != 'output':
            # Custom directory specified - validate it exists
            json_output = validate_custom_output_path(json_output)
        else:
            # Default or simple filename - ensure proper directory structure
            json_output = ensure_output_directory(json_output, quiet)
    
    write_json(filename=json_output, cluster_data=cluster_data, 
               keyspace_info=keyspace_summary, node_data=all_node_data, cluster_update_info=cluster_update_info)
    
    # Show file output locations unless in quiet mode
    if not quiet:
        console.print(f"[green]JSON report written to:[/green] {json_output}")
        console.print(f"[green]CSV report written to:[/green] {output}")
    
    # Close the unified client
    await redis_conn.close()
    logger.info("Assessment completed successfully")


# Wrapper function to run async main
def main_wrapper(
    host: str = typer.Option("localhost", help="Hostname or IP address"),
    port: int = typer.Option(6379, help="Port (default: 6379)"),
    user: str = typer.Option(None, help="Username (optional, for authenticated ElastiCache)"),
    password: str = typer.Option("", help="Password (default: '')"),
    tls: bool = typer.Option(False, help="Enable TLS/SSL"),
    output: str = typer.Option(f"output/output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", help="CSV output filename (defaults to output/ directory)"),
    json_output: str = typer.Option(None, help="JSON output filename (defaults to CSV filename with .json extension)"),
    duration: int = typer.Option(120, help="Duration in seconds to collect performance metrics (default: 120 seconds)"),
    log_level: str = typer.Option("WARNING", help="Set logging level: DEBUG (detailed info), INFO (operational details), WARNING (warnings only - default), ERROR (errors only), CRITICAL (critical only)", case_sensitive=False),
    verbose: bool = typer.Option(False, help="Enable verbose output (equivalent to --log-level DEBUG)", hidden=True),
    quiet: bool = typer.Option(False, help="Suppress console output (only show final file locations)"),
    version: bool = typer.Option(None, "--version", callback=version_callback, is_eager=True),
    legacy_units: bool = typer.Option(False, "--legacy-units", help="Use legacy traffic units (KB/sec, MB/sec) instead of default Gbps"),
):
    """A workload assessment tool for Valkey and Redis OSS clusters."""
    # Check if no arguments were provided (only script name in sys.argv)
    if len(sys.argv) == 1:
        # No arguments provided, show help by calling app with --help
        import subprocess
        subprocess.run([sys.argv[0], "--help"])
        raise typer.Exit()
    
    asyncio.run(main(host, port, user, password, tls, output, json_output, duration, log_level, verbose, quiet, version, legacy_units))

app.command()(main_wrapper)
if __name__ == "__main__":
    app()
    
