# Cluster Node Discovery and Metrics API

This document describes the three new cluster node discovery and metrics endpoints that have been added to the API server.

## Overview

The implementation provides comprehensive cluster node monitoring capabilities through three main endpoints:

1. **Node Discovery** - Discover all nodes in the cluster
2. **All Node Metrics** - Get comprehensive metrics for all cluster nodes  
3. **Individual Node Metrics** - Get detailed metrics for a specific node

## Implementation Details

### Backend Components

#### ValkeyClient Extensions (`valkey_client.py`)

**New Methods Added:**

- `discover_cluster_nodes()` - Uses `CLUSTER NODES` command to discover all nodes
- `connect_to_node(node_address)` - Creates direct connections to individual nodes
- `get_node_metrics(node_address, node_id, role, slots)` - Collects metrics from specific nodes
- `get_all_nodes_metrics()` - Aggregates metrics from all cluster nodes

**Key Features:**
- Parses `CLUSTER NODES` output to extract node information
- Handles master/slave relationships and replica mapping
- Creates temporary connections to individual nodes for metrics collection
- Proper connection cleanup and error handling
- Memory formatting (GB/MB/KB/B) and uptime formatting
- CPU metrics with fallback calculations when psutil unavailable

#### API Endpoints (`app.py`)

**New Endpoints Added:**

- `GET /api/cluster/nodes`
- `GET /api/cluster/nodes/metrics`  
- `GET /api/nodes/{nodeId}/metrics`

## API Reference

### 1. GET /api/cluster/nodes - Node Discovery

Discovers all nodes in the cluster using the `CLUSTER NODES` command.

**Response Structure:**
```json
{
  "status": "success",
  "data": {
    "nodes": [
      {
        "nodeId": "a1b2c3d4e5f6...", 
        "nodeAddress": "10.0.1.100:6379",
        "role": "master",
        "status": "connected",
        "slots": "0-5460",
        "flags": ["master"],
        "replicas": ["replica1_id"]
      },
      {
        "nodeId": "b2c3d4e5f6a1...",
        "nodeAddress": "10.0.1.101:6379", 
        "role": "slave",
        "status": "connected",
        "masterNodeId": "a1b2c3d4e5f6...",
        "flags": ["slave"]
      }
    ],
    "timestamp": "2025-01-08T22:32:00Z"
  },
  "timestamp": "2025-01-08T22:32:00Z"
}
```

**Usage:**
```bash
curl http://localhost:8000/api/cluster/nodes
```

### 2. GET /api/cluster/nodes/metrics - All Node Metrics

Gets comprehensive metrics for all cluster nodes including cluster summary information.

**Response Structure:**
```json
{
  "status": "success", 
  "data": {
    "nodes": [
      {
        "nodeId": "a1b2c3d4e5f6...",
        "nodeAddress": "10.0.1.100:6379",
        "role": "master",
        "status": "online", 
        "memory": {
          "used": "2.1GB",
          "max": "8GB",
          "usedBytes": 2252341248,
          "maxBytes": 8589934592,
          "percent": 26
        },
        "cpu": {
          "percent": 45.2,
          "cores": 4
        },
        "connections": 1247,
        "opsPerSec": 2341,
        "uptime": "3d 14h",
        "keyCount": 1250789,
        "slots": "0-5460"
      }
    ],
    "clusterInfo": {
      "totalNodes": 6,
      "mastersCount": 3,
      "slavesCount": 3,
      "clusterState": "ok"
    },
    "timestamp": "2025-01-08T22:32:00Z"
  },
  "timestamp": "2025-01-08T22:32:00Z"
}
```

**Usage:**
```bash
curl http://localhost:8000/api/cluster/nodes/metrics
```

### 3. GET /api/nodes/{nodeId}/metrics - Individual Node Metrics

Gets detailed metrics for a specific cluster node identified by its node ID.

**Parameters:**
- `nodeId` (path parameter) - The unique identifier of the cluster node

**Response Structure:**
```json
{
  "status": "success",
  "data": {
    "nodeId": "a1b2c3d4e5f6...",
    "nodeAddress": "10.0.1.100:6379",
    "role": "master",
    "status": "online",
    "memory": {
      "used": "2.1GB",
      "max": "8GB", 
      "usedBytes": 2252341248,
      "maxBytes": 8589934592,
      "percent": 26
    },
    "cpu": {
      "percent": 45.2,
      "cores": 4
    },
    "connections": 1247,
    "opsPerSec": 2341,
    "uptime": "3d 14h",
    "keyCount": 1250789,
    "slots": "0-5460"
  },
  "timestamp": "2025-01-08T22:32:00Z"
}
```

**Usage:**
```bash
curl http://localhost:8000/api/nodes/a1b2c3d4e5f6.../metrics
```

**Error Responses:**
- `404 Not Found` - Node with specified ID not found in cluster
- `500 Internal Server Error` - Failed to discover nodes or collect metrics
- `503 Service Unavailable` - Valkey client not initialized

## Implementation Features

### Node Discovery
- Parses `CLUSTER NODES` command output
- Extracts node ID, address, role, status, and slot assignments
- Maps master-replica relationships
- Handles cluster bus port stripping (`@16379`)

### Metrics Collection
- Creates individual connections to each node for metrics collection
- Collects server, memory, stats, and client information
- Calculates memory usage percentages and formats sizes
- Determines CPU usage with fallback calculations
- Formats uptime as "Xd Yh" format
- Gets key counts (masters use DBSIZE, slaves typically return 0)

### Error Handling
- Graceful handling of unreachable nodes
- Proper connection cleanup after metrics collection
- Comprehensive error reporting with specific error messages
- Fallback behavior for missing system metrics

### Cluster State Detection
- Determines cluster health based on node availability
- Reports "ok" if all nodes are online, "degraded" otherwise
- Provides summary statistics (total nodes, masters, slaves)

## Testing

A test script `test_cluster_nodes.py` is provided to verify the endpoints:

```bash
# Test against local server
python test_cluster_nodes.py

# Test against remote server
python test_cluster_nodes.py http://remote-server:8000
```

The test script will:
1. Test all three endpoints
2. Validate response structures  
3. Show detailed node information
4. Test individual node metrics with real node IDs discovered from the cluster

## Requirements

- **Cluster Mode**: Endpoints require `VALKEY_USE_CLUSTER=true` in configuration
- **Connectivity**: Server must be able to connect to individual cluster nodes
- **Permissions**: Requires permissions to execute `CLUSTER NODES` and node info commands

## Error Scenarios

The API handles several error scenarios:

1. **Not in Cluster Mode**: Returns empty nodes list with error message
2. **Cluster Unavailable**: Returns error in discovery/metrics collection
3. **Individual Node Down**: Marks node as "offline" or "error" in metrics
4. **Connection Issues**: Proper error reporting and connection cleanup
5. **Invalid Node ID**: Returns 404 for non-existent nodes

## Security Considerations

- Individual node connections use the same TLS and authentication settings as the main cluster connection
- Temporary connections are properly cleaned up to avoid connection leaks
- Error messages are sanitized to avoid information disclosure
- No sensitive cluster topology information is unnecessarily exposed
