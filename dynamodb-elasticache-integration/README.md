# DynamoDB ElastiCache Integration

## Purpose

This repository provides a reference implementation (in Python) showing how to
integrate DynamoDB and Elasticache. It accompanies the AWS Prescriptive
Guidance "A pattern for Amazon DynamoDB and Amazon ElastiCache integration
using read-through caching" available at https://docs.aws.amazon.com/prescriptive-guidance/latest/dynamodb-elasticache-integration/.

The CacheClient included here presents the usual DynamoDB client API to callers
and internally provides read-through caching using ElastiCache / Redis as the
data store.

## Getting started

The CacheClient class works as a client-side shim that presents the same
interface as the usual DynamoDB client. Construct a CacheClient by passing a
DynamoDB client and a Redis client, then use the shim instead of the usual
DynamoDB client. Optionally specify a TTL duration in seconds.

```
dynamodb_client = boto3.client("dynamodb")
redis_client = Redis(host='hostname', port=6379, decode_responses=True, ssl=True, read_from_replicas=True)
cache_client = CacheClient(dynamodb_client, redis_client, ttl=60*60)
```

To use the higher-level Resource interface, construct the resource then replace
its internal client reference with a CacheClient that wraps it:

```
dynamodb_resource = boto3.resource("dynamodb")
redis_client = Redis(host='hostname', port=6379, decode_responses=True, ssl=True, read_from_replicas=True)
dynamodb_resource.meta.client = CacheClient(dynamodb_resource.meta.client, redis_client, ttl=60*60)
```

There's an optional `namespace` which isolates the cache reads and writes to
within a namespace. Using a namespace is beneficial for testing because each
run with a different namespace will appear to start with an empty cache, no
side effects from previous runs. It's implemented as an automatically-added
prefix to each lookup key.

```
cache_client = CacheClient(dynamodb_client, redis_client, namespace='yourRandomString')
```

Fine-grained control of the TTL behaviors can by achieved by passing in a
`ttl_config` with different TTL values for different types of requests.

```
cache_client = CacheClient(dynamodb_client, redis_client, 
  ttl_config={'item': 24*60*60, 'item_negative': 60*60, 'query': 5*60, 'scan': 10*60})
```

## Behavior

The CacheClient only attempts to cache "eventually consistent" (EC) read calls
made to DynamoDB. This includes get-item, batch-get-item, query, and scan. It
does not cache "strongly consistent" (SC) reads calls or transactional read
calls.

All empty responses (such as a `get-item` or `query` that returns no items)
will be cached. You can consider this as negative caching, although in fact
it's just caching a response that happens to not have Item entries inside.

Any write made to any item will immediately invalidate all item cache entries
related to that item. This includes any put-item, update-item, delete-item,
batch-write-item, or transact-write-items call.

These writes will NOT invalidate any query cache or scan cache entries. Query
and scan cache entries are expired based only on TTL.

The `batch-get-item` call is implemented by performing a loop of the `get-item`
calls held within. A more robust implementation could multi-thread these calls
for parallel execution or perform a careful `batch-get-item` on any items not
found already in the cache.

### Error handling

The constructor pings the ElastiCache / Redis service to ensure connectivity
and will error out immediately if the cache is not available. If the cache
becomes unavailable later for any reason, it will behave as if all items were
cache misses. Remember: a cache layer should not add fragility to an
application.

### Scan purgatory 

Scan calls implement a "purgatory" system where it takes two calls with the
same signature (within the TTL time period) for the resulting data to be
cached. This avoids filling the cache with large pages of scanned data during a
one-time full table scan while still enabling people who do frequent scans to
get the benefit of caching. The first scan puts a small placeholder in the
cache holding the request count. The second scan replaces the counter with the
actual payload, which can be as large as 1 MB.

### Metadata in the response

When providing a response, the CacheClient removes the `ResponseMetadata` from
all cache hit responses. This avoids the potential for someone to see a
RequestId and think it real when it was actually from hours previously.

The CacheClient also adjusts any `ConsumedCapacity` metrics to indicate "0"
consumption for cache hits. This allows intelligent clients (that want to track
or limit their DynamoDB read consumption) to correctly track the consumption
even with a mix of cache hits and misses.

Finally, the CacheClient adds a `CacheMetadata` section for any cache hit. It
holds the time the value was stored (useful to measure staleness) and the
client library and version that stored the value (which may be useful when
performing a seamless upgrade from one version to another where the format
changes).

```
  {
    "CacheMetadata": {
      "CacheHit": True,
      "CachedTime": 2024-02-14 13:20:00Z",
      "Client": "Python 0.0.1"
  }
```

The `batch-get-item` call includes extra metadata in the `CacheMetadata`
indicating how many of the returned items were cache hits, misses, or strongly
consistent reads (not eligible for caching). It also includes `Time` (not
`CachedTime`) because the cache hits in the batch may have been cached at many
time points, so this is just the time of the response generation:

```
  {
    "CacheMetadata": {
      "CacheHitCount": 25,
      "CacheMissCount": 10,
      "StronglyConsistentCount": 0,
      "Time": 2024-02-14 13:20:00Z",
      "Client": "Python 0.0.1"
  }
```

### Determining the table schema 

On first use against a given table, the CacheClient performs a `describe-table`
call to learn the schema of each table, specifically its primary key
attributes. That result will be cached from that point forward so it's only
needed once. The item cache depends on the key schema not changing. If you
delete a table and create it again with a different key schema, you should
start again with an empty cache.



## Authors and acknowledgment

Code by Jason Hunter, test work by Arjan Schaaf, helpful technical advice by
Damon LaCaille and Roberto Luna Rojas.


## License

Refer to the LICENSE file in the root of the repository. Please note specifically
this part of the LICENSE:

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

