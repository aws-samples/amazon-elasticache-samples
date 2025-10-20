# SCAN Implementation: Replacing KEYS with Production-Safe Key Retrieval

## Overview

This document describes the implementation of SCAN-based key retrieval to replace the blocking KEYS command in the Valkey/Redis client. This change improves production safety and performance by using non-blocking operations.

## Problem Statement

The original `get_all_keys` method used the Redis `KEYS *` command, which has several critical issues:

### Issues with KEYS Command
- **Blocking Operation**: KEYS blocks the entire Redis/Valkey server while searching
- **Performance Impact**: Can freeze the database on large datasets
- **Memory Usage**: Returns all matching keys at once, consuming significant memory
- **Production Risk**: Can cause timeouts and service disruptions

### Example of Problem
```python
# OLD IMPLEMENTATION (DANGEROUS)
def get_all_keys(self, pattern: str = "*") -> list:
    return self.client.keys(pattern)  # ❌ Blocks entire server
```

## Solution: SCAN-Based Implementation

The new implementation uses Redis `SCAN` command with the following benefits:

### Benefits of SCAN Command
- **Non-blocking**: Server remains responsive during operation
- **Memory efficient**: Processes keys in configurable batches
- **Production safe**: Suitable for large datasets
- **Cursor-based**: Iterates through keyspace without blocking
- **Configurable**: Tunable batch size and iteration limits

## Implementation Details

### Updated ValkeyClient Method

```python
def get_all_keys(self, pattern: str = "*", count: int = 100, 
                max_iterations: int = 10000, use_scan: bool = True) -> list:
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
```

### Key Features

#### 1. **SCAN Implementation**
- Uses cursor-based iteration
- Processes keys in batches (configurable with `count` parameter)
- Includes progress logging for large datasets
- Handles cluster-specific errors gracefully

#### 2. **Backwards Compatibility**
- `use_scan=False` parameter allows fallback to KEYS command
- Same return format as original implementation
- Drop-in replacement for existing code

#### 3. **Safety Features**
- **Max iteration limit**: Prevents infinite loops
- **Error handling**: Graceful handling of cluster redirects
- **Progress logging**: Logs every 100 iterations for monitoring
- **Cluster awareness**: Works correctly in both single-node and cluster mode

#### 4. **Performance Tuning**
- **Configurable batch size**: `count` parameter controls trade-off between memory and network
- **Iteration limits**: `max_iterations` prevents runaway operations
- **Progress monitoring**: Built-in logging for large datasets

### Updated API Endpoint

The `/api/cache/keys` endpoint now supports SCAN parameters:

```python
@app.get("/api/cache/keys")
async def get_all_keys(
    pattern: str = "*", 
    count: int = 100, 
    max_iterations: int = 10000, 
    use_scan: bool = True
):
```

#### API Response Format
```json
{
  "pattern": "*",
  "keys": ["key1", "key2", "key3"],
  "count": 1250,
  "scan_method": "SCAN",
  "scan_parameters": {
    "count": 100,
    "max_iterations": 10000
  },
  "timestamp": "2025-01-05T18:15:00.000Z"
}
```

## Usage Examples

### Basic Usage (Recommended)

```python
# Use SCAN (default, production-safe)
keys = client.get_all_keys("*")

# With custom batch size
keys = client.get_all_keys("user:*", count=50)

# With pattern matching
keys = client.get_all_keys("session:*", count=200)
```

### Advanced Configuration

```python
# Fine-tuned for large datasets
keys = client.get_all_keys(
    pattern="*",
    count=500,           # Larger batches for fewer network calls
    max_iterations=50000 # Allow more iterations for very large datasets
)

# Fallback to KEYS for small datasets (development only)
keys = client.get_all_keys("test:*", use_scan=False)
```

### API Usage

```bash
# Default SCAN usage
curl "http://localhost:8000/api/cache/keys"

# With pattern
curl "http://localhost:8000/api/cache/keys?pattern=user:*"

# Custom SCAN parameters
curl "http://localhost:8000/api/cache/keys?pattern=*&count=200&max_iterations=5000"

# Fallback to KEYS (not recommended for production)
curl "http://localhost:8000/api/cache/keys?pattern=*&use_scan=false"
```

## Performance Characteristics

### SCAN vs KEYS Comparison

| Aspect | SCAN | KEYS |
|--------|------|------|
| **Blocking** | ❌ Non-blocking | ✅ Blocks server |
| **Memory Usage** | 🟡 Batched (configurable) | 🔴 All at once |
| **Production Safe** | ✅ Yes | ❌ No |
| **Large Datasets** | ✅ Efficient | 🔴 Can freeze server |
| **Network Calls** | 🟡 Multiple (batch-based) | ✅ Single call |
| **Cluster Support** | ✅ Full support | ✅ Basic support |

### Performance Tuning Guidelines

#### Count Parameter Selection
- **Small datasets** (< 1,000 keys): `count=50`
- **Medium datasets** (1,000-10,000 keys): `count=100` (default)
- **Large datasets** (10,000+ keys): `count=200-500`

#### Max Iterations Calculation
- **Estimate**: `max_iterations = total_keys / count * 2`
- **Safety buffer**: Always include 2x buffer for cursor behavior
- **Default 10,000**: Suitable for most datasets up to ~500,000 keys

## Error Handling

### Cluster-Specific Handling

The implementation includes special handling for cluster environments:

```python
except Exception as scan_error:
    logger.error(f"SCAN iteration {iteration_count} failed: {scan_error}")
    # Handle cluster redirects gracefully
    if "MOVED" in str(scan_error) or "ASK" in str(scan_error):
        logger.info(f"Cluster redirect during SCAN, continuing with next iteration")
        continue
    else:
        raise scan_error
```

### Infinite Loop Protection

```python
if iteration_count > max_iterations:
    logger.warning(f"SCAN reached maximum iterations ({max_iterations}) for pattern '{pattern}'. "
                 f"Found {len(keys)} keys so far. Consider increasing max_iterations or using a more specific pattern.")
    break
```

## Migration Guide

### For Existing Code

**Before (Risky):**
```python
keys = client.get_all_keys("*")  # Uses KEYS command
```

**After (Safe):**
```python
keys = client.get_all_keys("*")  # Uses SCAN command by default
```

**For Backwards Compatibility:**
```python
keys = client.get_all_keys("*", use_scan=False)  # Still uses KEYS if needed
```

### For API Clients

**Before:**
```bash
curl "http://localhost:8000/api/cache/keys?pattern=*"
```

**After (Same URL, Better Performance):**
```bash
# Same URL, now uses SCAN automatically
curl "http://localhost:8000/api/cache/keys?pattern=*"

# With custom parameters
curl "http://localhost:8000/api/cache/keys?pattern=*&count=200"
```

## Testing

Use the provided test script to verify functionality:

```bash
python test_scan_keys.py
```

### Test Coverage

The test script validates:
- ✅ SCAN vs KEYS result consistency
- ✅ Performance comparison
- ✅ Pattern matching functionality
- ✅ Different batch size performance
- ✅ Error handling and edge cases

## Monitoring and Logging

### Log Messages to Watch

**Normal Operation:**
```
INFO - Using SCAN command (non-blocking) for pattern: *, count: 100
INFO - SCAN completed: found 1250 keys matching pattern '*' in 8 iterations
```

**Progress for Large Datasets:**
```
INFO - SCAN progress: iteration 100, found 8750 keys so far
INFO - SCAN progress: iteration 200, found 17500 keys so far
```

**Warning Conditions:**
```
WARNING - SCAN reached maximum iterations (10000) for pattern '*'. Found 45000 keys so far. Consider increasing max_iterations or using a more specific pattern.
```

### Metrics to Monitor

- **Iteration count**: Number of SCAN iterations required
- **Execution time**: Time taken for complete key retrieval
- **Key count**: Total keys returned
- **Pattern efficiency**: Keys found vs iterations for specific patterns

## Best Practices

### Production Recommendations

1. **Always use SCAN** in production (`use_scan=True`)
2. **Tune batch size** based on your dataset and network characteristics
3. **Use specific patterns** to reduce scan scope
4. **Monitor iteration counts** to optimize parameters
5. **Set appropriate max_iterations** for your use case

### Development Guidelines

```python
# ✅ GOOD: Production-safe SCAN with reasonable parameters
keys = client.get_all_keys("user:*", count=100, max_iterations=1000)

# ⚠️ ACCEPTABLE: KEYS for small development datasets only
keys = client.get_all_keys("test:*", use_scan=False)  

# ❌ BAD: KEYS with wildcard in production
keys = client.get_all_keys("*", use_scan=False)
```

### Pattern Optimization

```python
# ✅ GOOD: Specific patterns reduce scan scope
keys = client.get_all_keys("user:session:*")
keys = client.get_all_keys("cache:2024:*")

# 🟡 OK: Moderate scope
keys = client.get_all_keys("user:*")

# ⚠️ CAREFUL: Wide scope, tune parameters accordingly
keys = client.get_all_keys("*", count=500, max_iterations=50000)
```

## Conclusion

The SCAN implementation provides a production-safe, performant alternative to the blocking KEYS command while maintaining full backwards compatibility. This change significantly improves the reliability and scalability of key retrieval operations in Redis/Valkey environments.

### Key Benefits Summary

- **🚀 Performance**: Non-blocking operations maintain server responsiveness
- **🔒 Safety**: Production-ready for large datasets
- **⚙️ Configurable**: Tunable parameters for different use cases
- **🔄 Compatible**: Drop-in replacement with fallback option
- **📊 Observable**: Comprehensive logging and monitoring capabilities

For questions or issues related to this implementation, refer to the test script (`test_scan_keys.py`) for usage examples and validation.
