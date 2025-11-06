import redis
import logging
import ssl
import os
from typing import Optional, Dict, Any
import json
import time
import base64
import concurrent.futures
from datetime import datetime

# Try to import retry functionality for newer redis versions
try:
    from redis.retry import Retry
    from redis.backoff import ExponentialBackoff
    HAS_RETRY = True
except ImportError:
    HAS_RETRY = False

# Try to import cluster-specific exceptions
try:
    from redis.exceptions import MovedError, AskError, ClusterDownError, TryAgainError
    HAS_CLUSTER_EXCEPTIONS = True
except ImportError:
    HAS_CLUSTER_EXCEPTIONS = False

# Try to import ClusterNode for proper cluster initialization
try:
    from redis.cluster import ClusterNode
    HAS_CLUSTER_NODE = True
except ImportError:
    HAS_CLUSTER_NODE = False

logger = logging.getLogger(__name__)

class ValkeyClient:
    def __init__(self, name: str, host: str, port: int = 6379, decode_responses: bool = True, use_tls: bool = True, use_cluster: bool = False):
        """
        Initialize Valkey client with cluster configuration
        """
        self.host = host
        self.port = port
        self.decode_responses = decode_responses
        self.use_tls = use_tls
        self.use_cluster = use_cluster
        self.client = None
        self.binary_client = None  # For handling binary data
        self.max_retries = 3
        self._commandlog_supported = None  # Cache for CommandLog support detection
        self.name = name
        
        # Performance optimization caches
        self._node_clients_cache = {}  # Cache for reusable node connections
        self._cluster_topology_cache = None  # Cache for cluster topology
        self._cluster_topology_cache_time = 0  # Cache timestamp
        self._cluster_topology_cache_ttl = 30  # Cache TTL in seconds
        
        self.connect()

    def connect(self):
        """
        Establish connection to Valkey cluster
        """
        try:
            # Base connection parameters
            connection_params = {
                'decode_responses': self.decode_responses,
                'socket_connect_timeout': 10,
                'socket_timeout': 10,
                'retry_on_timeout': True
            }
            
            # Add retry functionality if available (redis >= 4.0.0)
            if HAS_RETRY:
                connection_params['retry'] = Retry(ExponentialBackoff(), retries=self.max_retries)
            
            # Add TLS configuration if enabled
            if self.use_tls:
                # Enhanced SSL parameters for AWS ElastiCache compatibility
                ssl_params = {
                    'ssl': True,
                    'ssl_cert_reqs': ssl.CERT_NONE,  # ElastiCache doesn't require client certificates
                    'ssl_check_hostname': False,     # ElastiCache uses internal hostnames
                    'ssl_ca_certs': None,           # No custom CA needed for ElastiCache
                    'ssl_keyfile': None,            # No client key needed
                    'ssl_certfile': None,           # No client cert needed
                }
                
                # Special handling for ElastiCache endpoints
                if 'cache.amazonaws.com' in self.host:
                    # ElastiCache-specific SSL optimization
                    ssl_params.update({
                        'ssl_cert_reqs': None,  # ElastiCache works better with this
                    })
                
                connection_params.update(ssl_params)
            
            # Determine if we should use cluster mode
            if self.use_cluster:
                # For cluster mode, use RedisCluster with enhanced settings
                if HAS_CLUSTER_NODE:
                    # Use proper ClusterNode objects for redis-py 5.0.1+
                    startup_nodes = [ClusterNode(host=self.host, port=self.port)]
                else:
                    # Fallback to dictionary format for older versions
                    startup_nodes = [{"host": self.host, "port": self.port}]
                    logger.warning(f"Using dictionary format for cluster startup (ClusterNode not available)")
                
                # Enhanced cluster connection parameters
                cluster_params = {
                    **connection_params,
                    'skip_full_coverage_check': True,  # Allow connection even if not all slots are covered
                    'max_connections_per_node': 16,    # Limit connections per node
                    'readonly_mode': False,            # Allow write operations
                    'reinitialize_steps': 10,          # Steps to reinitialize cluster topology
                    'cluster_error_retry_attempts': 3,  # Retry attempts for cluster errors
                    'startup_nodes': startup_nodes
                }
                
                self.client = redis.RedisCluster(**cluster_params)
            else:
                # For single node or ElastiCache replication group
                connection_params.update({
                    'host': self.host,
                    'port': self.port
                })
                self.client = redis.StrictRedis(**connection_params)
                logger.info(f"Using single Redis instance mode")
            
            # Test connection
            self.client.ping()
            
            # Create binary client for handling binary data (decode_responses=False)
            binary_connection_params = connection_params.copy()
            binary_connection_params['decode_responses'] = False
            
            if self.use_cluster:
                binary_cluster_params = {
                    **binary_connection_params,
                    'skip_full_coverage_check': True,
                    'max_connections_per_node': 16,
                    'readonly_mode': False,
                    'reinitialize_steps': 10,
                    'cluster_error_retry_attempts': 3,
                    'startup_nodes': startup_nodes
                }
                self.binary_client = redis.RedisCluster(**binary_cluster_params)
            else:
                binary_connection_params.update({
                    'host': self.host,
                    'port': self.port
                })
                self.binary_client = redis.StrictRedis(**binary_connection_params)
            
            # Test binary client connection
            self.binary_client.ping()
            
            
        except Exception as e:
            logger.error(f"Failed to connect to Valkey: {str(e)}")
            raise

    def _execute_with_retry(self, operation, *args, **kwargs):
        """
        Execute operation with retry logic for cluster errors
        """
        max_attempts = self.max_retries
        for attempt in range(max_attempts):
            try:
                return operation(*args, **kwargs)
            except ValueError as val_error:
                error_msg = str(val_error).lower()
                
                # Check if this is the specific redis-py parsing bug for CLUSTER SLOT-STATS or COMMANDLOG
                # Only apply this transformation if we're actually dealing with these specific commands
                if "range()" in error_msg and "arg 3 must not be zero" in error_msg:
                    # Check if this is likely a CLUSTER SLOT-STATS command based on the args
                    is_cluster_slot_command = False
                    is_commandlog_command = False
                    
                    if len(args) >= 2 and hasattr(args[0], '__name__'):
                        # Check if it's execute_command with specific arguments
                        if args[0].__name__ == 'execute_command' and len(args) >= 3:
                            command_args = args[1:]
                            
                            # Check for CLUSTER SLOT-STATS
                            if (len(command_args) >= 2 and 
                                str(command_args[0]).upper() == 'CLUSTER' and 
                                str(command_args[1]).upper() == 'SLOT-STATS'):
                                is_cluster_slot_command = True
                            
                            # Check for COMMANDLOG commands
                            elif (len(command_args) >= 1 and 
                                  str(command_args[0]).upper() == 'COMMANDLOG'):
                                is_commandlog_command = True
                    
                    if is_cluster_slot_command:
                        logger.error(f"Caught 'range() arg 3 must not be zero' error in _execute_with_retry: {val_error}")
                        logger.error("This is the redis-py command parsing bug for CLUSTER SLOT-STATS")
                        # Re-raise with a cleaner message that get_cluster_slot_stats can catch
                        raise ValueError("CLUSTER SLOT-STATS command not supported by this Redis/Valkey version")
                    elif is_commandlog_command:
                        logger.error(f"Caught 'range() arg 3 must not be zero' error in _execute_with_retry: {val_error}")
                        logger.error("This is the redis-py command parsing bug for COMMANDLOG")
                        # Re-raise with a cleaner message that get_commandlog can catch
                        raise ValueError("COMMANDLOG command not supported by this Redis/Valkey version")
                    else:
                        # For other commands, just re-raise the original error
                        logger.warning(f"ValueError in _execute_with_retry (not cluster slot-stats or commandlog): {val_error}")
                        raise val_error
                else:
                    # For other ValueError exceptions, check if it's the last attempt
                    if attempt == max_attempts - 1:
                        raise
                    logger.warning(f"ValueError on attempt {attempt + 1}: {val_error}")
                    time.sleep(0.2 * (2 ** attempt))
            except Exception as e:
                if HAS_CLUSTER_EXCEPTIONS:
                    # Handle cluster-specific exceptions
                    if isinstance(e, (MovedError, AskError)):
                        logger.warning(f"Cluster redirect on attempt {attempt + 1}: {e}")
                        # For MOVED/ASK errors, the redis-py client should handle automatically
                        # but we'll add a small delay and retry
                        if attempt < max_attempts - 1:
                            time.sleep(0.1 * (2 ** attempt))  # Exponential backoff
                            continue
                    elif isinstance(e, (ClusterDownError, TryAgainError)):
                        logger.warning(f"Cluster temporarily unavailable on attempt {attempt + 1}: {e}")
                        if attempt < max_attempts - 1:
                            time.sleep(0.5 * (2 ** attempt))  # Longer delay for cluster issues
                            continue
                
                # Check if it's a general connection error
                if "MOVED" in str(e) or "ASK" in str(e):
                    logger.warning(f"Cluster redirect detected on attempt {attempt + 1}: {e}")
                    if attempt < max_attempts - 1:
                        time.sleep(0.1 * (2 ** attempt))
                        continue
                
                # Check for the range error in wrapped exceptions - but only for CLUSTER SLOT-STATS commands
                error_msg = str(e).lower()
                if ("range()" in error_msg and "arg 3 must not be zero" in error_msg and
                    len(args) >= 3 and hasattr(args[0], '__name__') and args[0].__name__ == 'execute_command'):
                    
                    # Check if this is actually a CLUSTER SLOT-STATS command
                    command_args = args[1:]
                    if (len(command_args) >= 2 and 
                        str(command_args[0]).upper() == 'CLUSTER' and 
                        str(command_args[1]).upper() == 'SLOT-STATS'):
                        
                        logger.error(f"Caught 'range() arg 3 must not be zero' error wrapped in {type(e)}: {e}")
                        logger.error("This is the redis-py command parsing bug for CLUSTER SLOT-STATS")
                        # Re-raise with a cleaner message that get_cluster_slot_stats can catch
                        raise ValueError("CLUSTER SLOT-STATS command not supported by this Redis/Valkey version")
                
                # If it's the last attempt or an unrecoverable error, re-raise
                if attempt == max_attempts - 1:
                    raise
                
                # For other errors, wait and retry
                logger.warning(f"Operation failed on attempt {attempt + 1}: {e}")
                time.sleep(0.2 * (2 ** attempt))
        
        # This should not be reached, but just in case
        raise Exception(f"Operation failed after {max_attempts} attempts")

    def _execute_cluster_management_command(self, *command_args):
        """
        Execute cluster management commands by connecting directly to a master node.
        This bypasses the cluster routing logic that can cause issues with commands
        like CLUSTER SLOT-STATS that don't operate on specific keys.
        
        Args:
            *command_args: Command arguments to execute
        
        Returns:
            Command result from the master node
        """
        try:
            if not self.use_cluster:
                # For non-cluster mode, use regular client
                return self._execute_with_retry(self.client.execute_command, *command_args)
            
            # First, try to get cluster nodes to find a master
            master_nodes = []
            try:
                # Try to discover cluster nodes to find a master
                discovery_result = self.discover_cluster_nodes()
                if "error" not in discovery_result and discovery_result.get("nodes"):
                    master_nodes = [
                        node for node in discovery_result["nodes"] 
                        if node.get("role") == "master" and node.get("status") == "connected"
                    ]
            except Exception as discovery_error:
                logger.warning(f"Could not discover cluster nodes, will try fallback: {discovery_error}")
            
            # If we have master nodes, try connecting to one
            if master_nodes:
                for master_node in master_nodes:
                    node_address = master_node.get("nodeAddress")
                    if not node_address:
                        continue
                    
                    logger.debug(f"Attempting to execute command on master node: {node_address}")
                    node_client = None
                    try:
                        node_client = self.connect_to_node(node_address)
                        if node_client:
                            result = node_client.execute_command(*command_args)
                            logger.debug(f"Successfully executed command on node {node_address}")
                            return result
                    except Exception as node_error:
                        logger.warning(f"Failed to execute command on node {node_address}: {node_error}")
                        continue
                    finally:
                        if node_client:
                            try:
                                node_client.close()
                            except:
                                pass
            
            # Fallback: try the cluster client with special error handling
            logger.info("No master nodes available, trying fallback execution on cluster client")
            try:
                return self._execute_with_retry(self.client.execute_command, *command_args)
            except ValueError as val_error:
                error_msg = str(val_error).lower()
                
                # Check if this is the specific "range() arg 3 must not be zero" error AND it's a CLUSTER SLOT-STATS command
                if ("range()" in error_msg and "arg 3 must not be zero" in error_msg and
                    len(command_args) >= 2 and 
                    str(command_args[0]).upper() == 'CLUSTER' and 
                    str(command_args[1]).upper() == 'SLOT-STATS'):
                    
                    logger.error(f"Encountered the known CLUSTER SLOT-STATS routing bug: {val_error}")
                    logger.error("This error occurs because the Redis client cannot parse CLUSTER SLOT-STATS command parameters")
                    logger.error("The command should be executed directly on a master node, but no master nodes were accessible")
                    
                    # Raise a cleaner exception that will be caught in get_cluster_slot_stats
                    raise ValueError("CLUSTER SLOT-STATS command not supported by this Redis/Valkey version")
                else:
                    # Re-raise other ValueError exceptions (including COMMANDLOG errors)
                    raise val_error
            except Exception as fallback_error:
                error_msg = str(fallback_error).lower()
                
                # Check if this is also the range error AND it's a CLUSTER SLOT-STATS command (could be wrapped in another exception)
                if ("range()" in error_msg and "arg 3 must not be zero" in error_msg and
                    len(command_args) >= 2 and 
                    str(command_args[0]).upper() == 'CLUSTER' and 
                    str(command_args[1]).upper() == 'SLOT-STATS'):
                    
                    logger.error(f"Encountered the known CLUSTER SLOT-STATS routing bug (wrapped): {fallback_error}")
                    logger.error("This error occurs because the Redis client cannot parse CLUSTER SLOT-STATS command parameters")
                    logger.error("The command should be executed directly on a master node, but no master nodes were accessible")
                    
                    # Raise a cleaner exception that will be caught in get_cluster_slot_stats
                    raise ValueError("CLUSTER SLOT-STATS command not supported by this Redis/Valkey version")
                
                # Try one more approach: use the raw connection from cluster client
                try:
                    # Get a random connection from the cluster client's connection pool
                    if hasattr(self.client, 'nodes_manager') and hasattr(self.client.nodes_manager, 'nodes_cache'):
                        nodes = self.client.nodes_manager.nodes_cache
                        master_nodes = [node for node in nodes.values() if not getattr(node, 'server_type', None) == 'slave']
                        
                        if master_nodes:
                            master_node = master_nodes[0]  # Use first available master
                            if hasattr(master_node, 'redis_connection'):
                                connection = master_node.redis_connection
                                logger.info(f"Attempting to execute command using direct connection from cluster client")
                                result = connection.execute_command(*command_args)
                                logger.info("Successfully executed command using direct connection")
                                return result
                except Exception as direct_error:
                    logger.warning(f"Direct connection attempt also failed: {direct_error}")
                
                # Re-raise the original error if we can't handle it
                raise fallback_error
            
        except Exception as e:
            logger.error(f"Failed to execute cluster management command {' '.join(command_args)}: {e}")
            raise

    def is_connected(self) -> bool:
        """
        Check if client is connected to Valkey
        """
        try:
            return self._execute_with_retry(self.client.ping)
        except:
            return False

    def get_info(self, section: Optional[str] = None) -> Dict[str, Any]:
        """
        Get Redis/Valkey server information using INFO command
        
        Args:
            section: Specific info section to retrieve (e.g., 'server', 'memory', 'clients', etc.)
                    If None, retrieves all sections
        
        Returns:
            Dictionary containing server information
        """
        try:
            if not self.is_connected():
                self.connect()
            
            logger.debug(f"Executing INFO command for section: {section}")
            
            if section:
                # Get specific section
                info_result = self._execute_with_retry(self.client.info, section)
            else:
                # Get all info
                info_result = self._execute_with_retry(self.client.info)
            
            logger.debug(f"INFO command successful, retrieved {len(info_result)} fields for section '{section}'")
            return info_result
            
        except Exception as e:
            logger.error(f"Failed to execute INFO command for section '{section}': {str(e)}")
            logger.error(f"Error type: {type(e).__name__}")
            # Re-raise to let MetricsCollector handle the error appropriately
            raise

    def get_client_list(self) -> list:
        """
        Get list of connected clients using CLIENT LIST command
        
        Returns:
            List of client connection information
        """
        try:
            if not self.is_connected():
                self.connect()
            
            logger.debug("Executing CLIENT LIST command")
            client_list_raw = self._execute_with_retry(self.client.execute_command, "CLIENT", "LIST")
            
            # Parse CLIENT LIST response
            clients = []
            if client_list_raw:
                for line in client_list_raw.strip().split('\n'):
                    if line.strip():
                        client_info = {}
                        # Parse client info line: "id=3 addr=127.0.0.1:52555 ..."
                        for part in line.strip().split():
                            if '=' in part:
                                key, value = part.split('=', 1)
                                # Convert numeric values with proper error handling
                                if value.isdigit():
                                    client_info[key] = int(value)
                                else:
                                    # Try to convert to float, but handle failures gracefully
                                    try:
                                        # Only attempt float conversion if it looks like a number
                                        if '.' in value and value.replace('.', '').replace('-', '').isdigit():
                                            client_info[key] = float(value)
                                        else:
                                            client_info[key] = value
                                    except (ValueError, AttributeError):
                                        # Keep as string if conversion fails
                                        client_info[key] = value
                        if client_info:
                            clients.append(client_info)
            
            logger.debug(f"CLIENT LIST successful, found {len(clients)} clients")
            return clients
            
        except Exception as e:
            logger.error(f"Failed to execute CLIENT LIST command: {str(e)}")
            logger.error(f"Error type: {type(e).__name__}")
            # Re-raise to let MetricsCollector handle the error appropriately
            raise

    def _is_likely_binary_key(self, key: str) -> bool:
        """
        Determine if a key is likely to contain binary data based on patterns
        """
        binary_patterns = [
            'hll_data',      # HyperLogLog data
            'hyperloglog',   # HyperLogLog variations
            'bitmap',        # Bitmap data
            'bitfield',      # Bitfield data
            'stream',        # Stream data
            'binary',        # Explicit binary indication
            'bloom',         # Bloom filter data
            'cuckoo',        # Cuckoo filter data
        ]
        
        key_lower = key.lower()
        return any(pattern in key_lower for pattern in binary_patterns)

    def _handle_binary_data(self, key: str, key_type: str) -> Dict[str, Any]:
        """
        Handle binary data by using the binary client and base64 encoding
        """
        try:
            if key_type == 'string':
                # For string type, use GET with binary client
                binary_value = self._execute_with_retry(self.binary_client.get, key)
                if binary_value is None:
                    return None
                
                # Base64 encode the binary data for safe transport
                encoded_value = base64.b64encode(binary_value).decode('ascii')
                return {
                    'value': encoded_value,
                    'encoding': 'base64',
                    'data_type': key_type,
                    'binary': True,
                    'original_size': len(binary_value)
                }
            elif key_type == 'stream':
                # For stream type, get stream entries
                try:
                    stream_data = self._execute_with_retry(self.binary_client.xrange, key)
                    # Convert stream data to a serializable format
                    serializable_data = []
                    for entry_id, fields in stream_data:
                        serializable_entry = {
                            'id': entry_id.decode('utf-8') if isinstance(entry_id, bytes) else entry_id,
                            'fields': {}
                        }
                        for field_key, field_value in fields.items():
                            field_key_str = field_key.decode('utf-8') if isinstance(field_key, bytes) else field_key
                            # Try to decode field value as UTF-8, otherwise base64 encode
                            try:
                                field_value_str = field_value.decode('utf-8') if isinstance(field_value, bytes) else field_value
                                serializable_entry['fields'][field_key_str] = field_value_str
                            except UnicodeDecodeError:
                                # If it can't be decoded as UTF-8, base64 encode it
                                field_value_b64 = base64.b64encode(field_value).decode('ascii')
                                serializable_entry['fields'][field_key_str] = {
                                    'value': field_value_b64,
                                    'encoding': 'base64'
                                }
                        serializable_data.append(serializable_entry)
                    
                    return {
                        'value': serializable_data,
                        'encoding': 'mixed',
                        'data_type': key_type,
                        'binary': True,
                        'stream_length': len(serializable_data)
                    }
                except Exception as e:
                    logger.warning(f"Error reading stream data for key '{key}': {e}")
                    return {"error": f"Error reading stream: {e}", "type": key_type}
            else:
                # For other types, return error
                return {"error": f"Binary handling not implemented for type: {key_type}", "type": key_type}
                
        except Exception as e:
            logger.error(f"Error handling binary data for key '{key}': {str(e)}")
            raise

    def get_value(self, key: str) -> Optional[Any]:
        """
        Get value by key from Valkey, handling multiple data types including binary data
        """
        try:
            if not self.is_connected():
                self.connect()
            
            # First, check the type of the key
            key_type = self._execute_with_retry(self.client.type, key)
            logger.info(f"Key '{key}' has type: '{key_type}' (type: {type(key_type)})")
            
            if key_type == 'none':
                # Key doesn't exist
                return None
            
            # Check if this is likely binary data that needs special handling
            is_likely_binary = self._is_likely_binary_key(key)
            
            if key_type == 'string':
                # String value - check if it's binary data
                if is_likely_binary:
                    logger.info(f"Key '{key}' detected as likely binary data, using binary client")
                    return self._handle_binary_data(key, key_type)
                else:
                    # Try regular text handling first
                    try:
                        value = self._execute_with_retry(self.client.get, key)
                        return {"type": "string", "data": value}
                    except UnicodeDecodeError as e:
                        logger.warning(f"UTF-8 decode error for key '{key}', falling back to binary handling: {e}")
                        return self._handle_binary_data(key, key_type)
            elif key_type == 'stream':
                # Stream type - always use binary handling since streams can contain binary data
                logger.info(f"Key '{key}' is stream type, using binary client")
                return self._handle_binary_data(key, key_type)
            elif key_type == 'list':
                # List value - use LRANGE to get all elements
                try:
                    list_data = self._execute_with_retry(self.client.lrange, key, 0, -1)
                    return {"type": "list", "data": list_data}
                except UnicodeDecodeError as e:
                    logger.warning(f"UTF-8 decode error for list key '{key}', some elements may be binary: {e}")
                    # For lists with binary data, we'd need more complex handling
                    # For now, return an error message
                    return {"error": f"List contains binary data that cannot be decoded as UTF-8", "type": key_type}
            elif key_type == 'set':
                # Set value - use SMEMBERS to get all members
                try:
                    set_data = list(self._execute_with_retry(self.client.smembers, key))
                    return {"type": "set", "data": set_data}
                except UnicodeDecodeError as e:
                    logger.warning(f"UTF-8 decode error for set key '{key}', some members may be binary: {e}")
                    return {"error": f"Set contains binary data that cannot be decoded as UTF-8", "type": key_type}
            elif key_type == 'hash':
                # Hash value - use HGETALL to get all fields and values
                try:
                    hash_data = self._execute_with_retry(self.client.hgetall, key)
                    return {"type": "hash", "data": hash_data}
                except UnicodeDecodeError as e:
                    logger.warning(f"UTF-8 decode error for hash key '{key}', some fields may be binary: {e}")
                    return {"error": f"Hash contains binary data that cannot be decoded as UTF-8", "type": key_type}
            elif key_type == 'zset':
                # Sorted set value - use ZRANGE with scores
                try:
                    zset_data = self._execute_with_retry(self.client.zrange, key, 0, -1, withscores=True)
                    return {"type": "zset", "data": zset_data}
                except UnicodeDecodeError as e:
                    logger.warning(f"UTF-8 decode error for zset key '{key}', some members may be binary: {e}")
                    return {"error": f"Sorted set contains binary data that cannot be decoded as UTF-8", "type": key_type}
            elif key_type in ['ReJSON-RL', 'JSON']:
                # JSON value - use JSON.GET command
                try:
                    # Try to get the JSON value
                    json_result = self._execute_with_retry(self.client.execute_command, 'JSON.GET', key)
                    if json_result is not None:
                        # Parse the JSON string to return structured data
                        json_data = json.loads(json_result)
                        return {"type": "json", "data": json_data}
                    return None
                except Exception as e:
                    logger.warning(f"Error getting JSON data for key '{key}': {e}")
                    # Fallback to treating it as a string
                    try:
                        fallback_data = self._execute_with_retry(self.client.get, key)
                        return {"type": "json", "data": fallback_data}
                    except Exception as fallback_e:
                        logger.error(f"Fallback failed for JSON key '{key}': {fallback_e}")
                        return {"error": f"Error reading JSON data: {str(e)}", "type": key_type}
            elif key_type == 'hll':
                # HyperLogLog value - get cardinality and raw data
                try:
                    cardinality = self._execute_with_retry(self.client.execute_command, 'PFCOUNT', key)
                    
                    # Also get the raw HLL data as base64 for completeness
                    hll_raw = self._execute_with_retry(self.binary_client.get, key)
                    encoded_raw = base64.b64encode(hll_raw).decode('ascii') if hll_raw else None
                    
                    return {
                        'cardinality': cardinality,
                        'raw_data': encoded_raw,
                        'encoding': 'base64',
                        'data_type': key_type,
                        'binary': True,
                        'original_size': len(hll_raw) if hll_raw else 0
                    }
                except Exception as e:
                    logger.warning(f"Error getting HyperLogLog data for key '{key}': {e}")
                    return {"error": f"Error reading HyperLogLog data: {str(e)}", "type": key_type}
            else:
                # Unknown or unsupported type - try to get raw data
                logger.warning(f"Unsupported key type '{key_type}' for key '{key}', attempting raw retrieval")
                try:
                    # Try to get as string first
                    raw_value = self._execute_with_retry(self.client.get, key)
                    if raw_value is not None:
                        return {
                            'value': raw_value,
                            'data_type': key_type,
                            'note': f"Retrieved as string - unsupported type '{key_type}'"
                        }
                    
                    # If that fails, try binary
                    binary_value = self._execute_with_retry(self.binary_client.get, key)
                    if binary_value is not None:
                        encoded_value = base64.b64encode(binary_value).decode('ascii')
                        return {
                            'value': encoded_value,
                            'encoding': 'base64',
                            'data_type': key_type,
                            'binary': True,
                            'original_size': len(binary_value),
                            'note': f"Retrieved as binary - unsupported type '{key_type}'"
                        }
                    
                    return None
                    
                except Exception as e:
                    logger.error(f"Error getting unsupported type '{key_type}' for key '{key}': {e}")
                    return {"error": f"Unsupported key type: {key_type}", "type": key_type}
                
        except Exception as e:
            logger.error(f"Error getting key '{key}': {str(e)}")
            raise

    def set_value(self, key: str, value: str, ttl: Optional[int] = None) -> bool:
        """
        Set key-value pair in Valkey
        """
        try:
            if not self.is_connected():
                self.connect()
            
            if ttl:
                return self._execute_with_retry(self.client.setex, key, ttl, value)
            else:
                return self._execute_with_retry(self.client.set, key, value)
        except Exception as e:
            logger.error(f"Error setting key '{key}': {str(e)}")
            raise

    def delete_key(self, key: str) -> int:
        """
        Delete key from Valkey
        """
        try:
            if not self.is_connected():
                self.connect()
            return self._execute_with_retry(self.client.delete, key)
        except Exception as e:
            logger.error(f"Error deleting key '{key}': {str(e)}")
            raise

    def get_paginated_keys(self, cursor: str = "0", pattern: str = "*", count: int = 100, use_scan: bool = True) -> dict:
        """
        Get keys using cursor-based pagination with SCAN (single iteration)
        
        Args:
            cursor: Starting cursor for pagination (default: "0")
            pattern: Pattern to match keys against (supports Redis glob patterns)
            count: Hint for number of keys to return per SCAN iteration (default: 100)
            use_scan: If True, use SCAN command; if False, fallback to KEYS command
        
        Returns:
            Dict containing:
            - cursor: Next cursor for pagination ("0" means complete)
            - keys: List of keys from this iteration
            - complete: Boolean indicating if scan is complete
        """
        try:
            if not self.is_connected():
                self.connect()
            
            # Fallback to KEYS command if requested
            if not use_scan:
                logger.info(f"Using KEYS command (blocking) for pattern: {pattern}")
                try:
                    all_keys = self._execute_with_retry(self.client.keys, pattern)
                    return {
                        "cursor": "0",
                        "keys": all_keys if all_keys is not None else [],
                        "complete": True
                    }
                except Exception as keys_error:
                    logger.error(f"KEYS command failed: {keys_error}")
                    return {
                        "cursor": "0", 
                        "keys": [],
                        "complete": True
                    }
            
            # Use SCAN command (non-blocking, production-safe)
            logger.info(f"Using SCAN command (paginated) for pattern: {pattern}, cursor: {cursor}, count: {count}")
            
            # Convert cursor to appropriate type with better error handling
            scan_cursor = 0
            try:
                if cursor == "0" or cursor == 0:
                    scan_cursor = 0
                else:
                    scan_cursor = int(cursor)
            except (ValueError, TypeError) as cursor_error:
                logger.error(f"Invalid cursor value: {cursor}, error: {cursor_error}")
                # Return empty result with completion flag for invalid cursor
                return {
                    "cursor": "0",
                    "keys": [],
                    "complete": True
                }
            
            logger.debug(f"SCAN iteration: cursor={scan_cursor} (type: {type(scan_cursor)})")
            
            # Execute single SCAN iteration with MATCH pattern and COUNT hint
            try:
                scan_result = self._execute_with_retry(
                    self.client.scan, 
                    scan_cursor, 
                    match=pattern, 
                    count=count
                )
                
                # 🔍 DEBUG: Log raw SCAN result from Redis
                logger.info(f"🔍 RAW SCAN RESULT: {scan_result} (type: {type(scan_result)})")
                
            except Exception as scan_error:
                logger.error(f"SCAN command failed: {scan_error}")
                # Return empty result with completion flag for failed SCAN
                return {
                    "cursor": "0",
                    "keys": [],
                    "complete": True
                }
            
            # Validate scan result structure
            if not isinstance(scan_result, (tuple, list)) or len(scan_result) != 2:
                logger.error(f"SCAN returned unexpected result structure: {scan_result} (type: {type(scan_result)})")
                # Return empty result with completion flag for invalid scan result
                return {
                    "cursor": "0",
                    "keys": [],
                    "complete": True
                }
            
            new_cursor, batch_keys = scan_result
            
            # 🔍 DEBUG: Log raw cursor and keys before processing
            logger.info(f"🔍 RAW CURSOR: {new_cursor} (type: {type(new_cursor)})")
            logger.info(f"🔍 RAW KEYS: Found {len(batch_keys) if batch_keys else 0} keys")
            if batch_keys and len(batch_keys) > 0:
                logger.info(f"🔍 SAMPLE KEYS: {batch_keys[:3]}{'...' if len(batch_keys) > 3 else ''}")
            
            # Process cursor for next iteration with error handling
            try:
                logger.info(f"🔍 PROCESSING CURSOR: input={new_cursor}, cluster_mode={self.use_cluster}")
                processed_cursor = self._process_scan_cursor(new_cursor, 1)
                logger.info(f"🔍 PROCESSED CURSOR: {processed_cursor} (type: {type(processed_cursor)})")
            except Exception as cursor_proc_error:
                logger.error(f"🚨 CURSOR PROCESSING ERROR: {cursor_proc_error}")
                logger.error(f"🚨 ORIGINAL CURSOR: {new_cursor} (type: {type(new_cursor)})")
                # Return current batch but mark as complete due to cursor processing error
                return {
                    "cursor": "0",
                    "keys": list(batch_keys) if batch_keys else [],
                    "complete": True
                }
            
            # Validate batch_keys type
            if not isinstance(batch_keys, (list, tuple)):
                logger.warning(f"SCAN returned non-list batch_keys: {batch_keys} (type: {type(batch_keys)})")
                batch_keys = list(batch_keys) if batch_keys else []
            
            # Determine if scan is complete with error handling
            try:
                is_complete = self._is_scan_complete(processed_cursor)
                logger.info(f"🔍 SCAN COMPLETION CHECK: processed_cursor={processed_cursor}, is_complete={is_complete}")
            except Exception as complete_check_error:
                logger.error(f"🚨 COMPLETION CHECK ERROR: {complete_check_error}")
                # Default to complete to avoid infinite loops
                is_complete = True
                processed_cursor = 0
            
            logger.debug(f"SCAN result: found {len(batch_keys)} keys, new cursor: {processed_cursor}, complete: {is_complete}")
            
            # Ensure all required fields are present
            result = {
                "cursor": str(processed_cursor),
                "keys": list(batch_keys) if batch_keys else [],
                "complete": is_complete if is_complete is not None else True
            }
            
            logger.info(f"🔍 FINAL RESULT: {result}")
            logger.debug(f"Returning paginated result: {result}")
            return result
            
        except Exception as e:
            logger.error(f"Error getting paginated keys with pattern '{pattern}', cursor '{cursor}': {str(e)}")
            # Return a safe default response instead of raising to prevent API errors
            logger.error("Returning safe default response due to error")
            return {
                "cursor": "0",
                "keys": [],
                "complete": True
            }

    def get_all_keys(self, pattern: str = "*", count: int = 100, max_iterations: int = 10000, use_scan: bool = True) -> list:
        """
        Get all keys matching pattern using SCAN (non-blocking) or KEYS (blocking)
        
        Args:
            pattern: Pattern to match keys against (supports Redis glob patterns)
            count: Hint for number of keys to return per SCAN iteration (default: 100)
            max_iterations: Maximum SCAN iterations to prevent infinite loops (default: 10000)
            use_scan: If True, use SCAN command; if False, fallback to KEYS command
        
        Returns:
            List of keys matching the pattern
        """
        try:
            if not self.is_connected():
                self.connect()
            
            # Fallback to KEYS command if requested
            if not use_scan:
                logger.info(f"Using KEYS command (blocking) for pattern: {pattern}")
                return self._execute_with_retry(self.client.keys, pattern)
            
            # Use SCAN command (non-blocking, production-safe)
            logger.info(f"Using SCAN command (non-blocking) for pattern: {pattern}, count: {count}")
            
            keys = []
            cursor = 0  # Start with integer cursor for single node, will be dict for cluster
            iteration_count = 0
            
            while True:
                iteration_count += 1
                
                # Protect against infinite loops
                if iteration_count > max_iterations:
                    logger.warning(f"SCAN reached maximum iterations ({max_iterations}) for pattern '{pattern}'. "
                                 f"Found {len(keys)} keys so far. Consider increasing max_iterations or using a more specific pattern.")
                    break
                
                # Execute SCAN with MATCH pattern and COUNT hint
                try:
                    logger.debug(f"SCAN iteration {iteration_count}: cursor={cursor} (type: {type(cursor)})")
                    
                    # Call SCAN with positional arguments to avoid parameter issues
                    scan_result = self._execute_with_retry(
                        self.client.scan, 
                        cursor, 
                        match=pattern, 
                        count=count
                    )
                    
                    # Validate scan result structure
                    if not isinstance(scan_result, (tuple, list)) or len(scan_result) != 2:
                        logger.error(f"SCAN returned unexpected result structure: {scan_result} (type: {type(scan_result)})")
                        raise ValueError(f"Invalid SCAN result format: expected tuple/list with 2 elements, got {type(scan_result)}")
                    
                    new_cursor, batch_keys = scan_result
                    
                    # Handle both single-node and cluster cursor formats
                    cursor = self._process_scan_cursor(new_cursor, iteration_count)
                    
                    # Validate batch_keys type
                    if not isinstance(batch_keys, (list, tuple)):
                        logger.warning(f"SCAN returned non-list batch_keys: {batch_keys} (type: {type(batch_keys)})")
                        batch_keys = list(batch_keys) if batch_keys else []
                    
                    # Add batch keys to results
                    keys.extend(batch_keys)
                    
                    logger.debug(f"SCAN iteration {iteration_count}: found {len(batch_keys)} keys, new cursor: {cursor}")
                    
                    # Log progress for large datasets
                    if iteration_count % 100 == 0:
                        logger.info(f"SCAN progress: iteration {iteration_count}, found {len(keys)} keys so far")
                    
                    # Check for scan completion
                    if self._is_scan_complete(cursor):
                        logger.debug(f"SCAN completed: cursor indicates end of scan")
                        break
                        
                except Exception as scan_error:
                    logger.error(f"SCAN iteration {iteration_count} failed: {scan_error}")
                    logger.error(f"SCAN parameters: cursor={cursor} (type: {type(cursor)}), pattern={pattern}, count={count}")
                    
                    # For cluster mode, some SCAN operations might fail on individual nodes
                    # Continue with next iteration rather than failing completely
                    if "MOVED" in str(scan_error) or "ASK" in str(scan_error):
                        logger.info(f"Cluster redirect during SCAN, continuing with next iteration")
                        continue
                    else:
                        # For non-cluster errors, re-raise the exception
                        raise scan_error
            
            logger.info(f"SCAN completed: found {len(keys)} keys matching pattern '{pattern}' in {iteration_count} iterations")
            return keys
            
        except Exception as e:
            logger.error(f"Error getting keys with pattern '{pattern}': {str(e)}")
            raise

    def _process_scan_cursor(self, new_cursor, iteration_count: int):
        """
        Process SCAN cursor handling both single-node and cluster formats
        
        Args:
            new_cursor: Cursor returned from SCAN command (int, str, or dict for cluster)
            iteration_count: Current iteration number for logging
            
        Returns:
            Processed cursor for next iteration
        """
        try:
            logger.info(f"🔍 CURSOR PROCESSING INPUT: {new_cursor} (type: {type(new_cursor)})")
            
            # For cluster mode, handle different cursor formats properly
            if self.use_cluster:
                logger.debug(f"Cluster mode cursor: {new_cursor} (type: {type(new_cursor)})")
                
                # In cluster mode, redis-py can return different cursor formats:
                # - int/str: Simple cursor (0 means complete)
                # - dict: Node-specific cursors (problematic format)
                # - None: Scan complete
                
                if new_cursor is None:
                    logger.debug("Cluster mode: cursor is None, scan complete")
                    return 0
                
                elif isinstance(new_cursor, (int, str)):
                    # Simple cursor format - can be used directly
                    cursor_int = int(new_cursor) if isinstance(new_cursor, str) else new_cursor
                    logger.debug(f"Cluster mode: using simple cursor: {cursor_int}")
                    return cursor_int
                
                elif isinstance(new_cursor, dict):
                    # Dictionary cursor format - this is problematic
                    # In this case, we need to determine if scanning is complete
                    logger.debug(f"🔍 DICT CURSOR DETAILS: {new_cursor}")
                    
                    # Check if all node cursors are 0 (complete)
                    all_complete = True
                    incomplete_nodes = []
                    for node_address, node_cursor in new_cursor.items():
                        try:
                            if int(node_cursor) != 0:
                                all_complete = False
                                incomplete_nodes.append(f"{node_address}:{node_cursor}")
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid node cursor for {node_address}: {node_cursor}")
                            all_complete = False
                            incomplete_nodes.append(f"{node_address}:{node_cursor}")
                    
                    if all_complete:
                        logger.info("🔍 CLUSTER DICT CURSOR: All node cursors are 0, scan complete")
                        return 0
                    else:
                        # For single primary node, extract the first cursor value from dict
                        logger.info(f"🔍 EXTRACTING CURSOR from dict for single primary node: {incomplete_nodes}")
                        
                        # Since we have only one primary node, use the first cursor value
                        first_cursor_value = next(iter(new_cursor.values()))
                        logger.info(f"🔍 EXTRACTED CURSOR: {first_cursor_value} from dict cursor")
                        
                        try:
                            cursor_int = int(first_cursor_value)
                            logger.info(f"🔍 CONVERTED CURSOR to int: {cursor_int}")
                            return cursor_int
                        except (ValueError, TypeError) as cursor_error:
                            logger.error(f"Invalid cursor value in dict: {first_cursor_value}, error: {cursor_error}")
                            return 0
                
                else:
                    logger.warning(f"Cluster mode: unknown cursor type {type(new_cursor)}, treating as complete")
                    return 0
            
            # Handle single-node cursor (int or str)
            if isinstance(new_cursor, (int, str)):
                if isinstance(new_cursor, str):
                    try:
                        result = int(new_cursor)
                        logger.debug(f"Single-node: converted string cursor '{new_cursor}' to int {result}")
                        return result
                    except ValueError:
                        logger.error(f"Failed to convert string cursor to int: {new_cursor}")
                        raise ValueError(f"Invalid cursor value: {new_cursor}")
                logger.debug(f"Single-node: using int cursor {new_cursor}")
                return new_cursor
            
            else:
                # Unexpected cursor type for single-node mode
                logger.error(f"Single-node mode: unexpected cursor type: {new_cursor} (type: {type(new_cursor)})")
                raise ValueError(f"Invalid cursor type for single-node mode: expected int or str, got {type(new_cursor)}")
                
        except Exception as e:
            logger.error(f"🚨 CURSOR PROCESSING FAILED on iteration {iteration_count}: {e}")
            raise

    def _is_scan_complete(self, cursor):
        """
        Check if SCAN operation is complete based on cursor value
        
        Args:
            cursor: Current cursor value (int, str, or dict for cluster)
            
        Returns:
            bool: True if scan is complete, False otherwise
        """
        try:
            logger.debug(f"🔍 CHECKING COMPLETION: cursor={cursor} (type: {type(cursor)})")
            
            # For cluster mode, let redis-py determine completion
            # The cluster client handles cursor completion internally
            if self.use_cluster:
                # For cluster mode, redis-py returns different cursor formats
                # We should check for the specific completion indicators it uses
                if cursor is None:
                    logger.debug("Cluster completion check: cursor is None -> complete")
                    return True
                if isinstance(cursor, (int, str)):
                    is_complete = int(cursor) == 0
                    logger.debug(f"Cluster completion check: cursor={cursor} -> complete={is_complete}")
                    return is_complete
                if isinstance(cursor, dict):
                    # Empty dict typically means complete
                    if not cursor:
                        logger.debug("Cluster completion check: empty dict -> complete")
                        return True
                    # Check if all node cursors are 0
                    try:
                        for node_address, node_cursor in cursor.items():
                            if int(node_cursor) != 0:
                                logger.debug(f"Cluster completion check: node {node_address} cursor {node_cursor} != 0 -> not complete")
                                return False
                        logger.debug("Cluster completion check: all node cursors are 0 -> complete")
                        return True  # All cursors are 0
                    except (ValueError, TypeError):
                        # If we can't parse cursors, rely on redis-py's management
                        logger.debug(f"Unable to parse cluster cursors: {cursor}, letting redis-py handle completion")
                        return False
                
                # For unknown cursor types in cluster mode, continue scanning
                logger.debug(f"Unknown cluster cursor type: {type(cursor)}, continuing scan")
                return False
            
            # For single-node Redis, cursor 0 means scan complete
            if isinstance(cursor, (int, str)):
                is_complete = int(cursor) == 0
                logger.debug(f"Single-node completion check: cursor={cursor} -> complete={is_complete}")
                return is_complete
            
            else:
                logger.warning(f"Unknown cursor type in single-node mode: {type(cursor)}")
                # Default to incomplete to avoid infinite loops
                return False
                
        except Exception as e:
            logger.error(f"🚨 COMPLETION CHECK FAILED: {e}")
            # Default to incomplete to avoid infinite loops
            return False

    def discover_cluster_nodes(self, use_cache: bool = True) -> Dict[str, Any]:
        """
        Discover all nodes in the cluster using CLUSTER NODES command
        
        Args:
            use_cache: If True, use cached results if available and fresh
        
        Returns:
            Dict containing cluster nodes information and metadata
        """
        try:
            if not self.is_connected():
                self.connect()
            
            if not self.use_cluster:
                # For single-node mode, return the current node as the only node
                logger.info("Single-node mode: returning current connection as the only node")
                return {
                    "nodes": [{
                        "nodeId": "single-node",
                        "nodeAddress": f"{self.host}:{self.port}",
                        "role": "master",
                        "status": "connected",
                        "slots": "0-16383",  # Full range for single node
                        "flags": ["master"],
                        "replicaOf": None,
                        "linkState": "connected",
                        "lastPingSent": 0,
                        "lastPongReceived": 0,
                        "epoch": 0,
                        "slotsCount": 16384
                    }],
                    "totalNodes": 1,
                    "masterNodes": 1,
                    "slaveNodes": 0,
                    "clusterMode": False
                }
            
            # Check cache first if enabled
            if use_cache and self._cluster_topology_cache is not None:
                cache_age = time.time() - self._cluster_topology_cache_time
                if cache_age < self._cluster_topology_cache_ttl:
                    logger.info(f"Using cached cluster topology (age: {cache_age:.1f}s)")
                    return self._cluster_topology_cache
                else:
                    logger.info(f"Cluster topology cache expired (age: {cache_age:.1f}s), refreshing")
            
            
            # Execute CLUSTER NODES command directly to avoid recursion
            cluster_nodes_raw = self._execute_with_retry(self.client.execute_command, "CLUSTER", "NODES")
            
            if not cluster_nodes_raw:
                logger.warning("CLUSTER NODES returned empty result")
                return {"error": "No cluster nodes information available"}
            
            # Parse cluster nodes information
            nodes = []
            master_count = 0
            slave_count = 0
            
            for line in cluster_nodes_raw.strip().split('\n'):
                if not line.strip():
                    continue
                
                parts = line.strip().split()
                if len(parts) < 8:
                    logger.warning(f"Invalid cluster node line: {line}")
                    continue
                
                node_id = parts[0]
                node_address = parts[1].split('@')[0]  # Remove cluster bus port
                flags = parts[2].split(',')
                replica_of = parts[3] if parts[3] != '-' else None
                ping_sent = int(parts[4])
                pong_received = int(parts[5])
                epoch = int(parts[6])
                link_state = parts[7]
                
                # Extract slots information
                slots_info = []
                slots_count = 0
                if len(parts) > 8:
                    for slot_range in parts[8:]:
                        slots_info.append(slot_range)
                        # Count slots
                        if '-' in slot_range:
                            start, end = slot_range.split('-')
                            slots_count += int(end) - int(start) + 1
                        else:
                            slots_count += 1
                
                # Determine role
                role = "master" if "master" in flags else "slave"
                if role == "master":
                    master_count += 1
                else:
                    slave_count += 1
                
                node_info = {
                    "nodeId": node_id,
                    "nodeAddress": node_address,
                    "role": role,
                    "status": "connected" if link_state == "connected" else "disconnected",
                    "slots": ",".join(slots_info) if slots_info else None,
                    "slotsCount": slots_count,
                    "flags": flags,
                    "replicaOf": replica_of,
                    "linkState": link_state,
                    "lastPingSent": ping_sent,
                    "lastPongReceived": pong_received,
                    "epoch": epoch
                }
                
                nodes.append(node_info)
            
            
            result = {
                "nodes": nodes,
                "totalNodes": len(nodes),
                "masterNodes": master_count,
                "slaveNodes": slave_count,
                "clusterMode": True
            }
            
            # Cache the result if enabled
            if use_cache:
                self._cluster_topology_cache = result
                self._cluster_topology_cache_time = time.time()
            
            return result
            
        except Exception as e:
            logger.error(f"Error discovering cluster nodes: {str(e)}")
            return {"error": f"Failed to discover cluster nodes: {str(e)}"}


    def _process_info_response(self, info_raw, section_name: str = "info") -> Dict[str, Any]:
        """
        Process INFO command response handling both string and dict formats
        
        Args:
            info_raw: Response from INFO command (can be string or dict)
            section_name: Name of the section for logging purposes
            
        Returns:
            Dict containing parsed INFO data
        """
        try:
            if isinstance(info_raw, dict):
                # Already parsed by redis-py client
                logger.debug(f"INFO {section_name} response already parsed as dict with {len(info_raw)} fields")
                return info_raw
            
            elif isinstance(info_raw, str):
                # String format that needs parsing
                logger.debug(f"INFO {section_name} response is string format, parsing manually")
                info_dict = {}
                
                for line in info_raw.split('\n'):
                    if ':' in line and not line.startswith('#'):
                        key, value = line.strip().split(':', 1)
                        # Convert numeric values with proper error handling
                        try:
                            if value.isdigit():
                                info_dict[key] = int(value)
                            elif value.replace('.', '').replace('-', '').isdigit():
                                info_dict[key] = float(value)
                            else:
                                info_dict[key] = value
                        except (ValueError, AttributeError):
                            # Keep as string if conversion fails
                            info_dict[key] = value
                
                logger.debug(f"Parsed INFO {section_name} string into dict with {len(info_dict)} fields")
                return info_dict
            
            elif isinstance(info_raw, (list, tuple)):
                # Handle list/tuple format (less common but possible)
                logger.debug(f"INFO {section_name} response is list/tuple format with {len(info_raw)} items")
                info_dict = {}
                
                for item in info_raw:
                    if isinstance(item, str) and ':' in item:
                        try:
                            key, value = item.strip().split(':', 1)
                            info_dict[key] = value
                        except ValueError:
                            continue
                
                return info_dict
            
            else:
                # Unexpected type
                logger.error(f"Unexpected INFO {section_name} response type: {type(info_raw)}")
                logger.error(f"INFO {section_name} response content: {info_raw}")
                raise ValueError(f"Unexpected INFO response type: {type(info_raw)}. Expected string or dict, got {type(info_raw)}")
                
        except Exception as e:
            logger.error(f"Failed to process INFO {section_name} response: {str(e)}")
            logger.error(f"INFO {section_name} response type: {type(info_raw)}")
            logger.error(f"INFO {section_name} response content preview: {str(info_raw)[:200]}...")
            raise

    def connect_to_node(self, node_address: str) -> Optional[Any]:
        """
        Create a direct connection to a specific cluster node
        
        Args:
            node_address: Address in format "host:port"
            
        Returns:
            Redis client connected to the specific node, or None if failed
        """
        try:
            if ':' not in node_address:
                logger.error(f"Invalid node address format: {node_address}")
                return None
            
            host, port_str = node_address.split(':', 1)
            port = int(port_str)
            
            logger.debug(f"Creating direct connection to node: {host}:{port}")
            
            # Base connection parameters
            connection_params = {
                'host': host,
                'port': port,
                'decode_responses': True,
                'socket_connect_timeout': 10,
                'socket_timeout': 10,
                'retry_on_timeout': True
            }
            
            # Add retry functionality if available
            if HAS_RETRY:
                connection_params['retry'] = Retry(ExponentialBackoff(), retries=self.max_retries)
            
            # Add TLS configuration if enabled
            if self.use_tls:
                connection_params.update({
                    'ssl': True,
                    'ssl_cert_reqs': ssl.CERT_NONE,
                    'ssl_check_hostname': False
                })
            
            # Create direct connection (not cluster)
            node_client = redis.StrictRedis(**connection_params)
            
            # Test connection
            node_client.ping()
            logger.info(f"Successfully connected to node {host}:{port}")
            
            return node_client
            
        except Exception as e:
            logger.error(f"Failed to connect to node {node_address}: {str(e)}")
            return None

    def get_cluster_info(self) -> Dict[str, Any]:
        """
        Get cluster information using CLUSTER INFO command
        
        Returns:
            Dict containing cluster state and configuration information
        """
        try:
            if not self.is_connected():
                self.connect()
            
            if not self.use_cluster:
                # For single-node mode, return mock cluster info
                return {
                    "cluster_state": "ok",
                    "cluster_slots_assigned": 16384,
                    "cluster_slots_ok": 16384,
                    "cluster_slots_pfail": 0,
                    "cluster_slots_fail": 0,
                    "cluster_known_nodes": 1,
                    "cluster_size": 1,
                    "cluster_current_epoch": 1,
                    "cluster_my_epoch": 1,
                    "cluster_stats_messages_sent": 0,
                    "cluster_stats_messages_received": 0,
                    "mode": "single-node"
                }
            
            logger.info("Executing CLUSTER INFO command")
            
            # Execute CLUSTER INFO command
            cluster_info_raw = self._execute_cluster_management_command("CLUSTER", "INFO")
            
            if not cluster_info_raw:
                logger.warning("CLUSTER INFO returned empty result")
                return {"error": "No cluster information available"}
            
            # Parse cluster info
            cluster_info = {"mode": "cluster"}
            
            for line in cluster_info_raw.strip().split('\n'):
                if ':' in line:
                    key, value = line.strip().split(':', 1)
                    # Convert numeric values
                    try:
                        if value.isdigit():
                            cluster_info[key] = int(value)
                        elif value.replace('.', '').isdigit():
                            cluster_info[key] = float(value)
                        else:
                            cluster_info[key] = value
                    except ValueError:
                        cluster_info[key] = value
            
            logger.info(f"Retrieved cluster info: state={cluster_info.get('cluster_state')}, nodes={cluster_info.get('cluster_known_nodes')}")
            
            return cluster_info
            
        except Exception as e:
            logger.error(f"Error getting cluster info: {str(e)}")
            return {"error": f"Failed to get cluster info: {str(e)}"}

    def get_node_metrics(self, node_address: str, node_id: str, role: str, slots: Optional[str] = None) -> Dict[str, Any]:
        """
        Get comprehensive metrics from a specific cluster node
        
        Args:
            node_address: Address in format "host:port"
            node_id: Unique node identifier
            role: Node role ("master" or "slave")
            slots: Slot ranges assigned to this node
            
        Returns:
            Dict containing node metrics and information
        """
        try:
            logger.info(f"Collecting metrics for node {node_id} ({node_address}, role: {role})")
            
            # Connect to the specific node
            node_client = self.connect_to_node(node_address)
            if not node_client:
                return {
                    "nodeId": node_id,
                    "nodeAddress": node_address,
                    "role": role,
                    "error": "Failed to connect to node",
                    "available": False
                }
            
            try:
                # Get basic info using safe processing
                logger.debug(f"Executing INFO command on node {node_address}")
                info_raw = node_client.execute_command("INFO")
                logger.debug(f"INFO response type: {type(info_raw)} for node {node_address}")
                
                # Process the INFO response safely (handles both string and dict formats)
                info_dict = self._process_info_response(info_raw, f"node-{node_id}")
                
                # Get memory info
                memory_info = {
                    "used_memory": info_dict.get("used_memory", 0),
                    "used_memory_human": info_dict.get("used_memory_human", "0B"),
                    "used_memory_rss": info_dict.get("used_memory_rss", 0),
                    "used_memory_peak": info_dict.get("used_memory_peak", 0),
                    "used_memory_peak_human": info_dict.get("used_memory_peak_human", "0B"),
                    "mem_fragmentation_ratio": info_dict.get("mem_fragmentation_ratio", 0.0)
                }
                
                # Get CPU info
                cpu_info = {
                    "used_cpu_sys": info_dict.get("used_cpu_sys", 0.0),
                    "used_cpu_user": info_dict.get("used_cpu_user", 0.0),
                    "used_cpu_sys_children": info_dict.get("used_cpu_sys_children", 0.0),
                    "used_cpu_user_children": info_dict.get("used_cpu_user_children", 0.0)
                }
                
                # Get connection info
                connection_info = {
                    "connected_clients": info_dict.get("connected_clients", 0),
                    "blocked_clients": info_dict.get("blocked_clients", 0),
                    "tracking_clients": info_dict.get("tracking_clients", 0),
                    "total_connections_received": info_dict.get("total_connections_received", 0),
                    "rejected_connections": info_dict.get("rejected_connections", 0)
                }
                
                # Get keyspace info
                keyspace_info = {}
                for key, value in info_dict.items():
                    if key.startswith("db"):
                        keyspace_info[key] = value
                
                # Get command stats
                command_stats = {
                    "total_commands_processed": info_dict.get("total_commands_processed", 0),
                    "instantaneous_ops_per_sec": info_dict.get("instantaneous_ops_per_sec", 0)
                }
                
                # Try to get replication info if it's a slave
                replication_info = {}
                if role == "slave":
                    replication_info = {
                        "master_host": info_dict.get("master_host"),
                        "master_port": info_dict.get("master_port"),
                        "master_link_status": info_dict.get("master_link_status"),
                        "master_last_io_seconds_ago": info_dict.get("master_last_io_seconds_ago"),
                        "master_sync_in_progress": info_dict.get("master_sync_in_progress", 0)
                    }
                
                # Parse slots information
                slots_count = 0
                slots_ranges = []
                if slots:
                    for slot_range in slots.split(','):
                        if '-' in slot_range:
                            start, end = slot_range.split('-')
                            slots_count += int(end) - int(start) + 1
                            slots_ranges.append({"start": int(start), "end": int(end)})
                        else:
                            slots_count += 1
                            slots_ranges.append({"start": int(slot_range), "end": int(slot_range)})
                
                node_metrics = {
                    "nodeId": node_id,
                    "nodeAddress": node_address,
                    "role": role,
                    "available": True,
                    "slots": {
                        "count": slots_count,
                        "ranges": slots_ranges,
                        "raw": slots
                    },
                    "memory": memory_info,
                    "cpu": cpu_info,
                    "connections": connection_info,
                    "keyspace": keyspace_info,
                    "commands": command_stats,
                    "replication": replication_info if replication_info else None,
                    "server_info": {
                        "redis_version": info_dict.get("redis_version"),
                        "redis_mode": info_dict.get("redis_mode"),
                        "os": info_dict.get("os"),
                        "arch_bits": info_dict.get("arch_bits"),
                        "uptime_in_seconds": info_dict.get("uptime_in_seconds", 0),
                        "uptime_in_days": info_dict.get("uptime_in_days", 0)
                    }
                }
                
                logger.info(f"Successfully collected metrics for node {node_id}")
                return node_metrics
                
            finally:
                # Always close the node connection
                try:
                    node_client.close()
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"Error getting metrics for node {node_id}: {str(e)}")
            return {
                "nodeId": node_id,
                "nodeAddress": node_address,
                "role": role,
                "error": f"Failed to get node metrics: {str(e)}",
                "available": False
            }

    def get_all_nodes_metrics(self) -> Dict[str, Any]:
        """
        Get comprehensive metrics from all cluster nodes using parallel collection
        
        Returns:
            Dict containing metrics from all nodes and cluster summary
        """
        try:
            logger.info("🚀 Starting parallel cluster nodes metrics collection")
            overall_start = time.time()
            
            # First discover all nodes
            discovery_result = self.discover_cluster_nodes()
            if "error" in discovery_result:
                logger.error(f"Failed to discover cluster nodes: {discovery_result['error']}")
                return {"error": f"Node discovery failed: {discovery_result['error']}"}
            
            nodes = discovery_result.get("nodes", [])
            if not nodes:
                logger.warning("No cluster nodes found")
                return {"error": "No cluster nodes available"}
            
            logger.info(f"Found {len(nodes)} nodes, collecting metrics in parallel")
            
            # Prepare parallel collection
            nodes_metrics = []
            successful_nodes = 0
            failed_nodes = 0
            collection_info = {
                "method": "parallel",
                "total_nodes": len(nodes),
                "successful_nodes": 0,
                "failed_nodes": 0,
                "node_timings": {}
            }
            
            # Use ThreadPoolExecutor for parallel node collection
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(len(nodes), 8))
            
            try:
                # Submit all node metric collection tasks
                future_to_node = {}
                for node in nodes:
                    future = executor.submit(
                        self.get_node_metrics,
                        node_address=node["nodeAddress"],
                        node_id=node["nodeId"],
                        role=node["role"],
                        slots=node.get("slots")
                    )
                    future_to_node[future] = node
                
                # Collect results with timeout
                node_timeout = 8.0  # 8 seconds per node
                for future in concurrent.futures.as_completed(future_to_node, timeout=20.0):
                    node = future_to_node[future]
                    node_id = node["nodeId"]
                    
                    try:
                        node_start = time.time()
                        node_metrics = future.result(timeout=node_timeout)
                        node_duration = time.time() - node_start
                        
                        if node_metrics.get("available", False):
                            successful_nodes += 1
                            collection_info["node_timings"][node_id] = f"{node_duration:.2f}s"
                            logger.info(f"✅ Node '{node_id}' metrics collected in {node_duration:.2f}s")
                        else:
                            failed_nodes += 1
                            collection_info["node_timings"][node_id] = f"failed({node_duration:.2f}s)"
                            logger.warning(f"❌ Node '{node_id}' metrics failed in {node_duration:.2f}s")
                            
                        nodes_metrics.append(node_metrics)
                        
                    except concurrent.futures.TimeoutError:
                        logger.error(f"⏱️  Node '{node_id}' timed out after {node_timeout}s")
                        failed_nodes += 1
                        collection_info["node_timings"][node_id] = f"timeout({node_timeout}s)"
                        nodes_metrics.append({
                            "nodeId": node_id,
                            "nodeAddress": node["nodeAddress"],
                            "role": node["role"],
                            "error": f"Node metrics collection timed out after {node_timeout}s",
                            "available": False
                        })
                    except Exception as e:
                        logger.error(f"❌ Node '{node_id}' failed: {str(e)}")
                        failed_nodes += 1
                        collection_info["node_timings"][node_id] = f"error"
                        nodes_metrics.append({
                            "nodeId": node_id,
                            "nodeAddress": node["nodeAddress"],
                            "role": node["role"],
                            "error": str(e),
                            "available": False
                        })
            
            except concurrent.futures.TimeoutError:
                logger.error("⏱️  Overall node metrics collection timed out after 20s")
                collection_info["overall_timeout"] = True
                # Fill missing results with timeout errors
                for future, node in future_to_node.items():
                    if future not in [f for f in concurrent.futures.as_completed(future_to_node, timeout=0)]:
                        node_id = node["nodeId"]
                        failed_nodes += 1
                        collection_info["node_timings"][node_id] = "overall_timeout"
                        nodes_metrics.append({
                            "nodeId": node_id,
                            "nodeAddress": node["nodeAddress"],
                            "role": node["role"],
                            "error": "Overall collection timeout",
                            "available": False
                        })
            
            finally:
                executor.shutdown(wait=False)
            
            # Update collection info
            collection_info["successful_nodes"] = successful_nodes
            collection_info["failed_nodes"] = failed_nodes
            
            overall_duration = time.time() - overall_start
            collection_info["total_duration_seconds"] = round(overall_duration, 2)
            
            logger.info(f"🏁 Parallel cluster nodes collection completed in {overall_duration:.2f}s "
                       f"({successful_nodes}/{len(nodes)} successful)")
            
            # Calculate cluster summary
            total_memory_used = 0
            total_connections = 0
            total_commands_processed = 0
            total_keys = 0
            master_nodes = []
            slave_nodes = []
            
            for node_metric in nodes_metrics:
                if node_metric.get("available"):
                    # Aggregate memory usage
                    memory = node_metric.get("memory", {})
                    total_memory_used += memory.get("used_memory", 0)
                    
                    # Aggregate connections
                    connections = node_metric.get("connections", {})
                    total_connections += connections.get("connected_clients", 0)
                    
                    # Aggregate commands
                    commands = node_metric.get("commands", {})
                    total_commands_processed += commands.get("total_commands_processed", 0)
                    
                    # Count keys from keyspace
                    keyspace = node_metric.get("keyspace", {})
                    for db_info in keyspace.values():
                        if isinstance(db_info, str) and "keys=" in db_info:
                            # Parse keyspace info like "keys=1000,expires=0,avg_ttl=0"
                            for part in db_info.split(','):
                                if part.startswith('keys='):
                                    total_keys += int(part.split('=')[1])
                    
                    # Categorize nodes
                    if node_metric.get("role") == "master":
                        master_nodes.append(node_metric)
                    else:
                        slave_nodes.append(node_metric)
            
            cluster_summary = {
                "total_nodes": len(nodes),
                "available_nodes": successful_nodes,
                "failed_nodes": failed_nodes,
                "master_nodes": len(master_nodes),
                "slave_nodes": len(slave_nodes),
                "total_memory_used": total_memory_used,
                "total_memory_used_human": f"{total_memory_used / (1024**2):.2f}MB" if total_memory_used > 0 else "0B",
                "total_connections": total_connections,
                "total_commands_processed": total_commands_processed,
                "total_keys": total_keys,
                "cluster_mode": discovery_result.get("clusterMode", False)
            }
            
            result = {
                "cluster_summary": cluster_summary,
                "nodes": nodes_metrics,
                "collection_info": collection_info,
                "discovery_info": {
                    "totalNodes": discovery_result.get("totalNodes", 0),
                    "masterNodes": discovery_result.get("masterNodes", 0),
                    "slaveNodes": discovery_result.get("slaveNodes", 0)
                },
                "collected_at": datetime.utcnow().isoformat()
            }
            
            logger.info(f"Successfully collected metrics from {successful_nodes}/{len(nodes)} nodes")
            return result
            
        except Exception as e:
            logger.error(f"Error getting all nodes metrics: {str(e)}")
            return {"error": f"Failed to get cluster nodes metrics: {str(e)}"}

    def validate_cluster_configuration(self) -> Dict[str, Any]:
        """
        Validate cluster configuration and connectivity
        
        Returns:
            Dict containing validation results and recommendations
        """
        try:
            logger.info("Validating cluster configuration")
            
            validation_result = {
                "valid": True,
                "warnings": [],
                "errors": [],
                "recommendations": [],
                "cluster_mode": self.use_cluster
            }
            
            if not self.use_cluster:
                validation_result["recommendations"].append(
                    "Running in single-node mode. Consider cluster mode for high availability."
                )
                return validation_result
            
            # Test basic cluster connectivity
            try:
                cluster_info = self.get_cluster_info()
                if "error" in cluster_info:
                    validation_result["valid"] = False
                    validation_result["errors"].append(f"Cannot get cluster info: {cluster_info['error']}")
                    return validation_result
                
                cluster_state = cluster_info.get("cluster_state")
                if cluster_state != "ok":
                    validation_result["valid"] = False
                    validation_result["errors"].append(f"Cluster state is not OK: {cluster_state}")
                
                # Check slot coverage
                slots_assigned = cluster_info.get("cluster_slots_assigned", 0)
                if slots_assigned < 16384:
                    validation_result["valid"] = False
                    validation_result["errors"].append(f"Not all slots assigned: {slots_assigned}/16384")
                
                slots_ok = cluster_info.get("cluster_slots_ok", 0)
                if slots_ok < 16384:
                    validation_result["warnings"].append(f"Some slots are not OK: {slots_ok}/16384")
                
            except Exception as e:
                validation_result["valid"] = False
                validation_result["errors"].append(f"Failed to validate cluster info: {str(e)}")
            
            # Discover and validate nodes
            try:
                discovery_result = self.discover_cluster_nodes()
                if "error" in discovery_result:
                    validation_result["valid"] = False
                    validation_result["errors"].append(f"Node discovery failed: {discovery_result['error']}")
                    return validation_result
                
                nodes = discovery_result.get("nodes", [])
                master_count = discovery_result.get("masterNodes", 0)
                slave_count = discovery_result.get("slaveNodes", 0)
                
                # Validate node counts
                if master_count < 3:
                    validation_result["warnings"].append(
                        f"Recommended to have at least 3 master nodes for proper failover, found: {master_count}"
                    )
                
                if slave_count == 0:
                    validation_result["warnings"].append(
                        "No slave nodes found. Consider adding replicas for high availability."
                    )
                
                # Test connectivity to each node
                connectivity_issues = []
                for node in nodes:
                    if node.get("status") != "connected":
                        connectivity_issues.append(f"Node {node['nodeId']} ({node['nodeAddress']}) is not connected")
                
                if connectivity_issues:
                    validation_result["valid"] = False
                    validation_result["errors"].extend(connectivity_issues)
                
            except Exception as e:
                validation_result["valid"] = False
                validation_result["errors"].append(f"Failed to validate cluster nodes: {str(e)}")
            
            # Add general recommendations
            if validation_result["valid"]:
                validation_result["recommendations"].extend([
                    "Cluster configuration appears healthy",
                    "Monitor cluster regularly for optimal performance",
                    "Consider setting up monitoring alerts for cluster state changes"
                ])
            
            logger.info(f"Cluster validation completed: valid={validation_result['valid']}, "
                       f"warnings={len(validation_result['warnings'])}, errors={len(validation_result['errors'])}")
            
            return validation_result
            
        except Exception as e:
            logger.error(f"Error validating cluster configuration: {str(e)}")
            return {
                "valid": False,
                "errors": [f"Validation failed: {str(e)}"],
                "warnings": [],
                "recommendations": [],
                "cluster_mode": self.use_cluster
            }

    def get_connection_stats(self) -> Dict[str, Any]:
        """
        Get connection statistics and health metrics
        
        Returns:
            Dict containing connection statistics
        """
        try:
            logger.info("Collecting connection statistics")
            
            if not self.is_connected():
                self.connect()
            
            # Get basic connection info using safe processing
            logger.debug("Executing INFO clients command for connection stats")
            info_raw = self._execute_with_retry(self.client.execute_command, "INFO", "clients")
            logger.debug(f"INFO clients response type: {type(info_raw)}")
            
            # Process the INFO response safely (handles both string and dict formats)
            connection_stats = self._process_info_response(info_raw, "clients")
            
            # Add cluster-specific stats if in cluster mode
            if self.use_cluster:
                try:
                    cluster_info = self.get_cluster_info()
                    if "error" not in cluster_info:
                        connection_stats["cluster_stats_messages_sent"] = cluster_info.get("cluster_stats_messages_sent", 0)
                        connection_stats["cluster_stats_messages_received"] = cluster_info.get("cluster_stats_messages_received", 0)
                        connection_stats["cluster_known_nodes"] = cluster_info.get("cluster_known_nodes", 0)
                except Exception as e:
                    logger.warning(f"Could not get cluster connection stats: {e}")
            
            # Test connection latency
            import time
            start_time = time.time()
            self._execute_with_retry(self.client.ping)
            ping_latency = (time.time() - start_time) * 1000  # Convert to milliseconds
            
            connection_stats["ping_latency_ms"] = round(ping_latency, 2)
            connection_stats["connection_healthy"] = True
            connection_stats["cluster_mode"] = self.use_cluster
            
            logger.info(f"Connection stats collected: {connection_stats.get('connected_clients', 0)} clients, "
                       f"ping latency: {ping_latency:.2f}ms")
            
            return connection_stats
            
        except Exception as e:
            logger.error(f"Error getting connection stats: {str(e)}")
            return {
                "error": f"Failed to get connection stats: {str(e)}",
                "connection_healthy": False,
                "cluster_mode": self.use_cluster
            }

    def reset_commandlog_support_cache(self):
        """
        Reset the COMMANDLOG support detection cache to force re-detection
        """
        logger.info("Resetting COMMANDLOG support detection cache")
        self._commandlog_supported = None

    def _get_commandlog_from_all_primary_nodes(self, count: int, log_type: str) -> list:
        """
        Get COMMANDLOG entries from all primary nodes and merge them
        
        Args:
            count: Number of entries to retrieve per node (-1 for all)
            log_type: Type of log ("large-request", "large-reply")
            
        Returns:
            List of merged command log entries from all primary nodes
        """
        try:
            logger.info(f"🚀 Starting multi-node COMMANDLOG collection for {log_type}")
            
            if not self.use_cluster:
                # For single-node mode, use the original single-node approach
                logger.info("Single-node mode: using single node COMMANDLOG execution")
                
                # 🔍 DEBUG: Log the exact command we're about to send
                if count == -1:
                    command_args = ["COMMANDLOG", "GET", "1000", log_type]
                    logger.info(f"🔍 DEBUG: Executing COMMANDLOG command: {' '.join(command_args)}")
                    entries = self._execute_cluster_management_command("COMMANDLOG", "GET", "1000", log_type)
                else:
                    command_args = ["COMMANDLOG", "GET", str(count), log_type]
                    logger.info(f"🔍 DEBUG: Executing COMMANDLOG command: {' '.join(command_args)}")
                    entries = self._execute_cluster_management_command("COMMANDLOG", "GET", str(count), log_type)
                
                # Parse entries for single node
                return self._parse_commandlog_entries(entries, log_type)
            
            # For cluster mode, discover all primary nodes
            logger.info("Cluster mode: discovering primary nodes for multi-node COMMANDLOG collection")
            discovery_result = self.discover_cluster_nodes()
            
            if "error" in discovery_result:
                logger.error(f"Failed to discover cluster nodes: {discovery_result['error']}")
                raise Exception(f"Node discovery failed: {discovery_result['error']}")
            
            nodes = discovery_result.get("nodes", [])
            primary_nodes = [
                node for node in nodes 
                if node.get("role") == "master" and node.get("status") == "connected"
            ]
            
            if not primary_nodes:
                logger.warning("No primary nodes found for COMMANDLOG collection")
                return []
            
            logger.info(f"Found {len(primary_nodes)} primary nodes for COMMANDLOG collection")
            
            # Collect COMMANDLOG entries from all primary nodes in parallel
            all_entries = []
            successful_nodes = 0
            failed_nodes = 0
            
            # Use ThreadPoolExecutor for parallel collection
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(primary_nodes), 8)) as executor:
                # Submit tasks for each primary node
                future_to_node = {}
                for node in primary_nodes:
                    future = executor.submit(
                        self._get_commandlog_from_single_node,
                        node["nodeAddress"],
                        node["nodeId"],
                        count,
                        log_type
                    )
                    future_to_node[future] = node
                
                # Collect results with timeout
                node_timeout = 8.0  # 8 seconds per node
                for future in concurrent.futures.as_completed(future_to_node, timeout=20.0):
                    node = future_to_node[future]
                    node_id = node["nodeId"]
                    
                    try:
                        node_entries = future.result(timeout=node_timeout)
                        if node_entries:
                            all_entries.extend(node_entries)
                            successful_nodes += 1
                            logger.info(f"✅ Node '{node_id}' returned {len(node_entries)} COMMANDLOG entries")
                        else:
                            successful_nodes += 1
                            logger.info(f"✅ Node '{node_id}' returned 0 COMMANDLOG entries")
                    except concurrent.futures.TimeoutError:
                        logger.error(f"⏱️  Node '{node_id}' COMMANDLOG collection timed out after {node_timeout}s")
                        failed_nodes += 1
                    except Exception as e:
                        logger.error(f"❌ Node '{node_id}' COMMANDLOG collection failed: {str(e)}")
                        failed_nodes += 1
            
            logger.info(f"🏁 Multi-node COMMANDLOG collection completed: "
                       f"{successful_nodes}/{len(primary_nodes)} nodes successful, "
                       f"total entries: {len(all_entries)}")
            
            return all_entries
            
        except Exception as e:
            logger.error(f"Error in multi-node COMMANDLOG collection: {str(e)}")
            raise

    def _get_commandlog_from_single_node(self, node_address: str, node_id: str, count: int, log_type: str) -> list:
        """
        Get COMMANDLOG entries from a single node
        
        Args:
            node_address: Address in format "host:port"
            node_id: Node identifier for logging
            count: Number of entries to retrieve (-1 for all)
            log_type: Type of log ("large-request", "large-reply")
            
        Returns:
            List of parsed command log entries from the node
        """
        try:
            logger.debug(f"Collecting COMMANDLOG {log_type} from node {node_id} ({node_address})")
            
            # Connect to the specific node
            node_client = self.connect_to_node(node_address)
            if not node_client:
                logger.error(f"Failed to connect to node {node_id} ({node_address})")
                return []
            
            try:
                # Execute COMMANDLOG command on the specific node
                if count == -1:
                    entries = node_client.execute_command("COMMANDLOG", "GET", "1000", log_type)
                else:
                    entries = node_client.execute_command("COMMANDLOG", "GET", str(count), log_type)
                
                # Parse entries from this node
                parsed_entries = self._parse_commandlog_entries(entries, log_type)
                
                logger.debug(f"Node {node_id} returned {len(parsed_entries)} COMMANDLOG entries")
                return parsed_entries
                
            finally:
                # Always close the node connection
                try:
                    node_client.close()
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"Error getting COMMANDLOG from node {node_id}: {str(e)}")
            return []

    def _parse_commandlog_entries(self, entries, log_type: str) -> list:
        """
        Parse raw COMMANDLOG entries into structured format
        
        Args:
            entries: Raw entries from COMMANDLOG command
            log_type: Type of log for metadata
            
        Returns:
            List of parsed command log entries
        """
        try:
            # 🔍 DEBUG: Log the raw response details
            logger.debug(f"🔍 DEBUG: Raw COMMANDLOG response type: {type(entries)}")
            logger.debug(f"🔍 DEBUG: Raw COMMANDLOG response length: {len(entries) if entries else 'None/Empty'}")
            
            parsed_entries = []
            if entries:
                logger.debug(f"🔍 DEBUG: Starting to parse {len(entries)} raw entries")
                for i, entry in enumerate(entries):
                    logger.debug(f"🔍 DEBUG: Entry {i}: type={type(entry)}, length={len(entry) if hasattr(entry, '__len__') else 'N/A'}")
                    
                    if isinstance(entry, list) and len(entry) >= 4:
                        parsed_entry = {
                            "id": entry[0],
                            "timestamp": entry[1],
                            "execution_time_microseconds": entry[2],
                            "command": entry[3:],  # Command and arguments
                            "log_type": log_type,
                            "source": "COMMANDLOG"
                        }
                        parsed_entries.append(parsed_entry)
                        logger.debug(f"🔍 DEBUG: Successfully parsed entry {i}")
                    else:
                        logger.warning(f"🔍 DEBUG: Skipping entry {i} - invalid format")
            else:
                logger.debug("🔍 DEBUG: No entries to parse (entries is None or empty)")
            
            logger.debug(f"🔍 DEBUG: Final parsed entries count: {len(parsed_entries)}")
            return parsed_entries
            
        except Exception as e:
            logger.error(f"Error parsing COMMANDLOG entries: {str(e)}")
            return []

    def _detect_commandlog_support(self) -> bool:
        """
        Detect if COMMANDLOG commands are supported by this Valkey/Redis instance
        
        Returns:
            bool: True if COMMANDLOG is supported, False otherwise
        """
        try:
            logger.info("Detecting COMMANDLOG support...")
            
            # First check: Get Redis/Valkey version info to make an educated guess
            try:
                info = self.get_info("server")
                redis_version = info.get("redis_version", "")
                logger.info(f"Server version: {redis_version}")
                
                # If this is Valkey 8.1+, assume COMMANDLOG is supported
                if "valkey" in redis_version.lower():
                    # Parse Valkey version
                    version_parts = redis_version.lower().replace("valkey", "").strip().split(".")
                    if len(version_parts) >= 2:
                        try:
                            major = int(version_parts[0])
                            minor = int(version_parts[1]) if len(version_parts) > 1 else 0
                            if major > 8 or (major == 8 and minor >= 1):
                                logger.info(f"Detected Valkey {major}.{minor}+ - assuming COMMANDLOG support")
                                return True
                        except (ValueError, IndexError):
                            pass
            except Exception as version_error:
                logger.debug(f"Could not get version info: {version_error}")
            
            # Try simplified detection methods in order of likelihood to work
            detection_methods = [
                # Method 1: Direct simple execution (no cluster routing)
                ("Direct COMMANDLOG LENGTH slow", 
                 lambda: self.client.execute_command("COMMANDLOG", "LENGTH", "slow")),
                
                # Method 2: Try with _execute_with_retry wrapper  
                ("Retry wrapper COMMANDLOG LENGTH slow",
                 lambda: self._execute_with_retry(self.client.execute_command, "COMMANDLOG", "LENGTH", "slow")),
                
                # Method 3: Try COMMANDLOG GET with minimal count
                ("Direct COMMANDLOG GET slow 0",
                 lambda: self.client.execute_command("COMMANDLOG", "GET", "slow", "0")),
                 
                # Method 4: Try direct execution without retry wrapper
                ("Direct COMMANDLOG LENGTH slow (no retry)",
                 lambda: self.client.execute_command("COMMANDLOG", "LENGTH", "slow")),
            ]
            
            for method_name, method in detection_methods:
                try:
                    if method is not None:
                        logger.debug(f"Trying detection method: {method_name}")
                        result = method()
                        logger.info(f"✅ COMMANDLOG detection succeeded with {method_name}: {result}")
                        return True
                except Exception as method_error:
                    error_msg = str(method_error).lower()
                    
                    # Distinguish between "command not found" vs "other errors"
                    if any(phrase in error_msg for phrase in ["unknown command", "command not found", "err unknown", "err wrong number"]):
                        logger.info(f"❌ {method_name} failed - command not recognized: {method_error}")
                        # If we get "unknown command", it's definitely not supported
                        if "unknown command" in error_msg:
                            logger.info("COMMANDLOG command not supported by this Redis/Valkey version")
                            return False
                    else:
                        logger.debug(f"⚠️  {method_name} failed with error (may still be supported): {method_error}")
                        # Other errors might mean the command exists but failed for other reasons
                        continue
            
            # All methods failed - try one final check to see if we can determine support
            try:
                # Try to get command info/help - this might give us a hint
                help_result = self.client.execute_command("COMMAND", "INFO", "COMMANDLOG")
                if help_result and len(help_result) > 0:
                    logger.info("✅ COMMANDLOG found in COMMAND INFO - supported")
                    return True
            except Exception as help_error:
                logger.debug(f"COMMAND INFO check failed: {help_error}")
            
            # Final fallback: if all methods failed but no "unknown command" error, 
            # and we're on what appears to be Valkey 8.1+, assume it's supported
            # This handles cases where command routing or permissions cause detection to fail
            try:
                info = self.get_info("server")
                redis_version = info.get("redis_version", "").lower()
                if "valkey" in redis_version and any(v in redis_version for v in ["8.1", "8.2", "8.3", "9.", "10."]):
                    logger.warning("All detection methods failed, but this appears to be Valkey 8.1+ - assuming COMMANDLOG support")
                    return True
            except:
                pass
            
            logger.info("All COMMANDLOG detection methods failed - COMMANDLOG not supported")
            return False
            
        except Exception as e:
            logger.error(f"Error during COMMANDLOG support detection: {e}")
            return False

    def get_commandlog(self, count: int = -1, log_type: str = "slow") -> Dict[str, Any]:
        """
        Get command log entries using COMMANDLOG or fallback to SLOWLOG
        Now scans all primary nodes and merges results
        
        Args:
            count: Number of entries to retrieve (-1 for all)
            log_type: Type of log ("slow", "large-request", "large-reply")
            
        Returns:
            Dict containing command log entries and metadata
        """
        try:
            logger.info(f"Getting {log_type} command log entries (count: {count})")
            
            if not self.is_connected():
                self.connect()
            
            # Check CommandLog support if not already cached or if we need to retry
            if self._commandlog_supported is None:
                self._commandlog_supported = self._detect_commandlog_support()
                if self._commandlog_supported:
                    logger.info("COMMANDLOG command is supported")
                else:
                    logger.info("COMMANDLOG command not supported, will use SLOWLOG fallback")
            
            if self._commandlog_supported and log_type in ["slow", "large-request", "large-reply"]:
                # Use COMMANDLOG for Valkey 8.1+ features - scan all primary nodes
                try:
                    # Get all primary nodes for multi-node scanning
                    all_entries = self._get_commandlog_from_all_primary_nodes(count, log_type)
                    
                    # Sort entries by timestamp (most recent first) to maintain logical ordering
                    if all_entries:
                        try:
                            all_entries.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
                        except Exception as sort_error:
                            logger.warning(f"Could not sort entries by timestamp: {sort_error}")
                    
                    logger.info(f"🔍 DEBUG: Final merged entries count from all primary nodes: {len(all_entries)}")
                    
                    return {
                        "entries": all_entries,
                        "count": len(all_entries),
                        "log_type": log_type,
                        "source": "COMMANDLOG",
                        "supported": True
                    }
                    
                except ValueError as val_error:
                    error_msg = str(val_error)
                    if "COMMANDLOG command not supported" in error_msg:
                        logger.warning(f"COMMANDLOG {log_type} not supported - falling back to SLOWLOG")
                        # Force fallback to SLOWLOG
                        self._commandlog_supported = False
                        return self.get_commandlog(count, "slow")
                    else:
                        logger.error(f"COMMANDLOG {log_type} failed with ValueError: {val_error}")
                        return {
                            "entries": [],
                            "count": 0,
                            "log_type": log_type,
                            "error": f"COMMANDLOG {log_type} failed: {str(val_error)}",
                            "source": "COMMANDLOG",
                            "supported": False
                        }
                except Exception as e:
                    logger.error(f"COMMANDLOG {log_type} failed: {e}")
                    return {
                        "entries": [],
                        "count": 0,
                        "log_type": log_type,
                        "error": f"COMMANDLOG {log_type} failed: {str(e)}",
                        "source": "COMMANDLOG",
                        "supported": False
                    }
            
            else:
                # Use SLOWLOG for backward compatibility or "slow" log type
                logger.info(f"Using SLOWLOG for {log_type} entries (COMMANDLOG not supported or slow log requested)")
                try:
                    if count == -1:
                        # Get all slow log entries
                        entries = self._execute_with_retry(self.client.execute_command, "SLOWLOG", "GET")
                    else:
                        # Get specific count
                        entries = self._execute_with_retry(self.client.execute_command, "SLOWLOG", "GET", count)
                    
                    # Parse SLOWLOG entries
                    parsed_entries = []
                    if entries:
                        for entry in entries:
                            if isinstance(entry, list) and len(entry) >= 4:
                                parsed_entry = {
                                    "id": entry[0],
                                    "timestamp": entry[1],
                                    "execution_time_microseconds": entry[2],
                                    "command": entry[3],  # Command and arguments as list
                                    "log_type": "slow",
                                    "source": "SLOWLOG"
                                }
                                # Add client info if available (6 elements in newer versions)
                                if len(entry) >= 6:
                                    parsed_entry["client_ip_port"] = entry[4]
                                    parsed_entry["client_name"] = entry[5]
                                
                                parsed_entries.append(parsed_entry)
                    
                    return {
                        "entries": parsed_entries,
                        "count": len(parsed_entries),
                        "log_type": "slow",
                        "source": "SLOWLOG",
                        "supported": True,
                        "note": f"Requested {log_type} but used SLOWLOG (COMMANDLOG not supported)"
                    }
                    
                except Exception as e:
                    logger.error(f"SLOWLOG failed: {e}")
                    return {
                        "entries": [],
                        "count": 0,
                        "log_type": log_type,
                        "error": f"SLOWLOG failed: {str(e)}",
                        "source": "SLOWLOG",
                        "supported": False
                    }
                    
        except Exception as e:
            logger.error(f"Error getting command log entries: {str(e)}")
            return {
                "entries": [],
                "count": 0,
                "log_type": log_type,
                "error": f"Failed to get command log: {str(e)}",
                "supported": False
            }

    def reset_commandlog(self, log_type: str = "slow") -> Dict[str, Any]:
        """
        Reset/clear command log entries
        
        Args:
            log_type: Type of log to reset ("slow", "large-request", "large-reply")
            
        Returns:
            Dict containing reset operation result
        """
        try:
            logger.info(f"Resetting {log_type} command log")
            
            if not self.is_connected():
                self.connect()
            
            # Check CommandLog support
            if self._commandlog_supported is None:
                try:
                    # Test if COMMANDLOG is supported
                    self._execute_with_retry(self.client.execute_command, "COMMANDLOG", "LENGTH", "slow")
                    self._commandlog_supported = True
                    logger.info("COMMANDLOG command is supported")
                except Exception as e:
                    self._commandlog_supported = False
                    logger.info(f"COMMANDLOG command not supported, using SLOWLOG fallback: {e}")
            
            if self._commandlog_supported and log_type in ["large-request", "large-reply"]:
                # Use COMMANDLOG RESET for Valkey 8.1+
                try:
                    result = self._execute_cluster_management_command("COMMANDLOG", "RESET", log_type)
                    
                    return {
                        "success": True,
                        "log_type": log_type,
                        "source": "COMMANDLOG",
                        "message": f"Successfully reset {log_type} command log"
                    }
                    
                except Exception as e:
                    logger.error(f"COMMANDLOG RESET {log_type} failed: {e}")
                    return {
                        "success": False,
                        "log_type": log_type,
                        "source": "COMMANDLOG",
                        "error": f"COMMANDLOG RESET {log_type} failed: {str(e)}"
                    }
            
            else:
                # Use SLOWLOG RESET for backward compatibility
                logger.info(f"Using SLOWLOG RESET for {log_type} (COMMANDLOG not supported or slow log requested)")
                try:
                    result = self._execute_with_retry(self.client.execute_command, "SLOWLOG", "RESET")
                    
                    return {
                        "success": True,
                        "log_type": "slow",
                        "source": "SLOWLOG",
                        "message": f"Successfully reset slow log",
                        "note": f"Requested {log_type} but used SLOWLOG (COMMANDLOG not supported)"
                    }
                    
                except Exception as e:
                    logger.error(f"SLOWLOG RESET failed: {e}")
                    return {
                        "success": False,
                        "log_type": log_type,
                        "source": "SLOWLOG",
                        "error": f"SLOWLOG RESET failed: {str(e)}"
                    }
                    
        except Exception as e:
            logger.error(f"Error resetting command log: {str(e)}")
            return {
                "success": False,
                "log_type": log_type,
                "error": f"Failed to reset command log: {str(e)}"
            }

    def get_commandlog_length(self, log_type: str = "slow") -> Dict[str, Any]:
        """
        Get the count of entries in a command log
        
        Args:
            log_type: Type of log ("slow", "large-request", "large-reply")
            
        Returns:
            Dict containing command log length and metadata
        """
        try:
            logger.info(f"Getting {log_type} command log length")
            
            if not self.is_connected():
                self.connect()
            
            # Check CommandLog support
            if self._commandlog_supported is None:
                try:
                    # Test if COMMANDLOG is supported
                    self._execute_with_retry(self.client.execute_command, "COMMANDLOG", "LENGTH", "slow")
                    self._commandlog_supported = True
                    logger.info("COMMANDLOG command is supported")
                except Exception as e:
                    self._commandlog_supported = False
                    logger.info(f"COMMANDLOG command not supported, using SLOWLOG fallback: {e}")
            
            if self._commandlog_supported and log_type in ["large-request", "large-reply"]:
                # Use COMMANDLOG LENGTH for Valkey 8.1+
                try:
                    length = self._execute_cluster_management_command("COMMANDLOG", "LENGTH", log_type)
                    
                    return {
                        "length": length,
                        "log_type": log_type,
                        "source": "COMMANDLOG",
                        "supported": True
                    }
                    
                except Exception as e:
                    logger.error(f"COMMANDLOG LENGTH {log_type} failed: {e}")
                    return {
                        "length": 0,
                        "log_type": log_type,
                        "source": "COMMANDLOG",
                        "error": f"COMMANDLOG LENGTH {log_type} failed: {str(e)}",
                        "supported": False
                    }
            
            else:
                # Use SLOWLOG LEN for backward compatibility
                logger.info(f"Using SLOWLOG LEN for {log_type} (COMMANDLOG not supported or slow log requested)")
                try:
                    length = self._execute_with_retry(self.client.execute_command, "SLOWLOG", "LEN")
                    
                    return {
                        "length": length,
                        "log_type": "slow",
                        "source": "SLOWLOG",
                        "supported": True,
                        "note": f"Requested {log_type} but used SLOWLOG (COMMANDLOG not supported)"
                    }
                    
                except Exception as e:
                    logger.error(f"SLOWLOG LEN failed: {e}")
                    return {
                        "length": 0,
                        "log_type": log_type,
                        "source": "SLOWLOG",
                        "error": f"SLOWLOG LEN failed: {str(e)}",
                        "supported": False
                    }
                    
        except Exception as e:
            logger.error(f"Error getting command log length: {str(e)}")
            return {
                "length": 0,
                "log_type": log_type,
                "error": f"Failed to get command log length: {str(e)}",
                "supported": False
            }

    def get_cluster_slot_stats(self, start_slot: Optional[int] = None, end_slot: Optional[int] = None) -> Dict[str, Any]:
        """
        Get cluster slot statistics using CLUSTER SLOT-STATS command
        
        Args:
            start_slot: Starting slot number (optional)
            end_slot: Ending slot number (optional)
            
        Returns:
            Dict containing slot statistics and metadata
        """
        try:
            logger.info(f"Getting cluster slot stats (start_slot: {start_slot}, end_slot: {end_slot})")
            
            if not self.is_connected():
                self.connect()
            
            if not self.use_cluster:
                # For single-node mode, return mock slot stats
                logger.info("Single-node mode: returning mock slot statistics")
                
                # Determine slot range
                actual_start = start_slot if start_slot is not None else 0
                actual_end = end_slot if end_slot is not None else 16383
                
                if actual_start < 0 or actual_end > 16383 or actual_start > actual_end:
                    return {
                        "error": f"Invalid slot range: {actual_start}-{actual_end}. Valid range is 0-16383",
                        "slots": [],
                        "total_slots": 0
                    }
                
                # Generate mock data for each slot in range
                mock_slots = []
                for slot_id in range(actual_start, actual_end + 1):
                    mock_slots.append({
                        "slot_id": slot_id,
                        "key_count": 0,  # Mock: no keys in single node for demo
                        "cpu_usec": 0,   # Mock CPU usage
                        "network_bytes_in": 0,
                        "network_bytes_out": 0
                    })
                
                return {
                    "slots": mock_slots,
                    "total_slots": len(mock_slots),
                    "start_slot": actual_start,
                    "end_slot": actual_end,
                    "cluster_mode": False,
                    "note": "Mock data for single-node mode"
                }
            
            try:
                # Build CLUSTER SLOT-STATS command
                if start_slot is not None and end_slot is not None:
                    if start_slot < 0 or end_slot > 16383 or start_slot > end_slot:
                        return {
                            "error": f"Invalid slot range: {start_slot}-{end_slot}. Valid range is 0-16383",
                            "slots": [],
                            "total_slots": 0
                        }
                    # Get stats for specific slot range
                    logger.info(f"Executing CLUSTER SLOT-STATS for slots {start_slot}-{end_slot}")
                    slot_stats_raw = self._execute_cluster_management_command("CLUSTER", "SLOT-STATS", "SLOTSRANGE", str(start_slot), str(end_slot))
                elif start_slot is not None:
                    if start_slot < 0 or start_slot > 16383:
                        return {
                            "error": f"Invalid slot: {start_slot}. Valid range is 0-16383",
                            "slots": [],
                            "total_slots": 0
                        }
                    # Get stats for single slot
                    logger.info(f"Executing CLUSTER SLOT-STATS for slot {start_slot}")
                    slot_stats_raw = self._execute_cluster_management_command("CLUSTER", "SLOT-STATS", "SLOTSRANGE", str(start_slot), str(start_slot))
                else:
                    # Get stats for all slots
                    logger.info("Executing CLUSTER SLOT-STATS for all slots")
                    slot_stats_raw = self._execute_cluster_management_command("CLUSTER", "SLOT-STATS", "SLOTSRANGE", "0", "16383")
                
                # Parse slot stats response
                if not slot_stats_raw:
                    logger.warning("CLUSTER SLOT-STATS returned empty result")
                    return {
                        "slots": [],
                        "total_slots": 0,
                        "error": "No slot statistics available"
                    }
                
                slots = []
                if isinstance(slot_stats_raw, list):
                    # Process each slot entry
                    for slot_entry in slot_stats_raw:
                        if isinstance(slot_entry, list) and len(slot_entry) >= 10:
                            # Expected format: [slot_id, key_count, cpu_usec, ...]
                            slot_info = {
                                "slot_id": int(slot_entry[0]),
                                "key_count": int(slot_entry[1]) if slot_entry[1] != '-' else 0,
                                "cpu_usec": int(slot_entry[2]) if slot_entry[2] != '-' else 0,
                                "network_bytes_in": int(slot_entry[3]) if slot_entry[3] != '-' else 0,
                                "network_bytes_out": int(slot_entry[4]) if slot_entry[4] != '-' else 0
                            }
                            slots.append(slot_info)
                
                # Calculate range info
                actual_start = start_slot if start_slot is not None else 0
                actual_end = end_slot if end_slot is not None else 16383
                
                result = {
                    "slots": slots,
                    "total_slots": len(slots),
                    "cluster_mode": True,
                    "command_executed": "CLUSTER SLOT-STATS"
                }
                
                if start_slot is not None or end_slot is not None:
                    result["start_slot"] = actual_start
                    result["end_slot"] = actual_end
                
                logger.info(f"Successfully retrieved slot stats for {len(slots)} slots")
                return result
                
            except ValueError as val_error:
                error_msg = str(val_error)
                
                # Check for the specific command parsing error
                if "CLUSTER SLOT-STATS command not supported" in error_msg:
                    logger.warning("CLUSTER SLOT-STATS command not supported by this Redis/Valkey version")
                    return {
                        "slots": [],
                        "total_slots": 0,
                        "error": "CLUSTER SLOT-STATS command not supported by this Redis/Valkey version",
                        "note": "This feature requires Valkey 8.0+ or a recent Redis version that supports CLUSTER SLOT-STATS"
                    }
                else:
                    # Re-raise other ValueError exceptions
                    raise val_error
                    
            except Exception as e:
                # Handle other exceptions that might occur during command execution
                logger.error(f"CLUSTER SLOT-STATS command failed: {e}")
                return {
                    "slots": [],
                    "total_slots": 0,
                    "error": f"CLUSTER SLOT-STATS failed: {str(e)}",
                    "note": "This feature requires Valkey 8.0+ or a recent Redis version that supports CLUSTER SLOT-STATS"
                }
                
        except Exception as e:
            logger.error(f"Error getting cluster slot stats: {str(e)}")
            return {
                "slots": [],
                "total_slots": 0,
                "error": f"Failed to get cluster slot stats: {str(e)}"
            }

    def close(self):
        """
        Close the connections
        """
        if self.client:
            self.client.close()
            logger.info("Valkey text client connection closed")
        if self.binary_client:
            self.binary_client.close()
            logger.info("Valkey binary client connection closed")
