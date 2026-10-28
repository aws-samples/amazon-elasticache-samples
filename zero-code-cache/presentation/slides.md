# ShopMind Demo Presentation
## Zero Code Cache: Supercharge Your Agentic AI Apps with JDBC Query Caching and Amazon ElastiCache for Valkey

---

## Slide 1: Title

**Zero Code Cache**
Supercharge Your Agentic AI Apps with JDBC Query Caching
and Amazon ElastiCache for Valkey

---

## Slide 2: The Problem

**Your AI Agent's Hidden Bottleneck**

Every agentic AI request triggers a cascade of database queries:
- User profile lookup
- Product catalog search
- Inventory check
- Recommendation engine (3-5 queries)
- Complementary product lookup

One agent request = 5-10 database round-trips
At scale: thousands of users x 10 queries = database overload

**Result:** Sluggish response times, high database costs, poor user experience

---

## Slide 3: Architecture — Without Cache

```
┌──────────┐     ┌──────────────┐     ┌──────────────────┐
│  User    │────▶│  Spring Boot │────▶│ Aurora PostgreSQL │
│ Browser  │     │   Backend    │     │                  │
└──────────┘     │              │     │  Every query     │
                 │  Bedrock     │     │  hits the DB     │
                 │  Agent       │     │  every time      │
                 │  (tool calls)│────▶│                  │
                 └──────────────┘     └──────────────────┘

Agent fires 5-10 JDBC queries per request
Each query: 30-200ms
Total DB time per request: 200-500ms
```

---

## Slide 4: The Solution — AWS Advanced JDBC Wrapper

**What is it?**
- Open-source JDBC driver wrapper
- Wraps your existing PostgreSQL/MySQL driver
- Adds cloud-native capabilities transparently
- No application code changes required

**Key Capabilities:**
- Failover handling for Aurora
- Read-write splitting
- IAM authentication
- NEW: Remote Query Cache Plugin (v4.4.0)

---

## Slide 5: Remote Query Cache Plugin

**How it works:**

1. You add a SQL hint: /* CACHE_PARAM(ttl=300s) */
2. The wrapper intercepts the query
3. Checks Amazon ElastiCache for Valkey
4. Cache HIT → returns result in sub-millisecond (DB never touched)
5. Cache MISS → queries DB, returns result, writes to Valkey async

**What you DON'T need to write:**
- No cache client code
- No serialization/deserialization
- No cache invalidation logic
- No connection pool management
- No health monitoring code

---

## Slide 6: Amazon ElastiCache for Valkey

**What is Valkey?**
- Open-source successor to Redis
- Sub-millisecond read/write latency
- In-memory data store

**Why ElastiCache for Valkey?**
- Fully managed — no servers to manage
- Serverless scaling option
- TLS encryption + IAM authentication
- Automatic failover and replication
- Production-ready from day one

---

## Slide 7: Architecture — With Cache

```
┌──────────┐     ┌──────────────┐     ┌──────────────────┐
│  User    │────▶│  Spring Boot │     │ Aurora PostgreSQL │
│ Browser  │     │   Backend    │     │                  │
└──────────┘     │              │     │  Only on cache   │
                 │  Bedrock     │     │  MISS            │
                 │  Agent       │     └──────────────────┘
                 │  (tool calls)│              ▲
                 │      │       │              │ miss
                 │      ▼       │              │
                 │  AWS JDBC    │     ┌──────────────────┐
                 │  Wrapper     │────▶│ ElastiCache for  │
                 │  + Remote    │     │ Valkey           │
                 │  Query Cache │     │                  │
                 │  Plugin      │◀────│ Cache HIT: <1ms  │
                 └──────────────┘     └──────────────────┘

First request: DB query + async write to Valkey
Subsequent requests: served from Valkey in <1ms
```

---

## Slide 8: Agent Flow — Without Cache

```
User: "Find me a gaming laptop under $1000"
         │
         ▼
┌─────────────────┐
│  Bedrock Agent  │  (Claude Haiku — reasoning)
│  ~3 seconds     │
└────────┬────────┘
         │ Tool calls
         ▼
┌─────────────────────────────────────────┐
│  Tool: search_products                  │
│  SQL: SELECT * FROM products WHERE ...  │──▶ PostgreSQL  ──▶ 173ms
├─────────────────────────────────────────┤
│  Tool: browse_category                  │
│  SQL: SELECT * FROM products WHERE ...  │──▶ PostgreSQL  ──▶ 60ms
└─────────────────────────────────────────┘

Total JDBC time: 211ms
Every request hits the database
```

---

## Slide 9: Agent Flow — With Cache (First Request)

```
User: "Find me a gaming laptop under $1000"
         │
         ▼
┌─────────────────┐
│  Bedrock Agent  │  (Claude Haiku — reasoning)
│  ~3 seconds     │
└────────┬────────┘
         │ Tool calls
         ▼
┌─────────────────────────────────────────────────────┐
│  Tool: search_products                              │
│  /* CACHE_PARAM(ttl=300s) */ SELECT * FROM ...      │
│  JDBC Wrapper → Valkey MISS → PostgreSQL → 173ms    │
│  (async write to Valkey in background)              │
├─────────────────────────────────────────────────────┤
│  Tool: browse_category                              │
│  /* CACHE_PARAM(ttl=300s) */ SELECT * FROM ...      │
│  JDBC Wrapper → Valkey MISS → PostgreSQL → 60ms     │
│  (async write to Valkey in background)              │
└─────────────────────────────────────────────────────┘

Total JDBC time: 211ms (same as without cache — first request)
But results are now cached in Valkey
```

---

## Slide 10: Agent Flow — With Cache (Second Request)

```
User: "Find me a gaming laptop under $1000"
         │
         ▼
┌─────────────────┐
│  Bedrock Agent  │  (Claude Haiku — reasoning)
│  ~3 seconds     │
└────────┬────────┘
         │ Tool calls
         ▼
┌─────────────────────────────────────────────────────┐
│  Tool: search_products                              │
│  /* CACHE_PARAM(ttl=300s) */ SELECT * FROM ...      │
│  JDBC Wrapper → Valkey HIT → 4ms                    │
│  (database never touched)                           │
├─────────────────────────────────────────────────────┤
│  Tool: browse_category                              │
│  /* CACHE_PARAM(ttl=300s) */ SELECT * FROM ...      │
│  JDBC Wrapper → Valkey HIT → 6ms                    │
│  (database never touched)                           │
└─────────────────────────────────────────────────────┘

Total JDBC time: 10ms (vs 211ms — 21x faster)
Database load: ZERO
```

---

## Slide 11: The Code Changes

**Change 1: DataSource Configuration (5 lines)**

BEFORE:
```java
config.setDriverClassName("org.postgresql.Driver");
config.setJdbcUrl("jdbc:postgresql://mydb:5432/shop");
```

AFTER:
```java
config.setDriverClassName("software.amazon.jdbc.Driver");
config.setJdbcUrl("jdbc:aws-wrapper:postgresql://mydb:5432/shop");
config.addDataSourceProperty("wrapperPlugins", "remoteQueryCache");
config.addDataSourceProperty("cacheEndpointAddrRw", "mycache:6379");
config.addDataSourceProperty("cacheUseSSL", "true");
```

**Change 2: SQL Query Hint (1 line per query)**

BEFORE:
```sql
SELECT * FROM products WHERE category = 'Laptops'
```

AFTER:
```sql
/* CACHE_PARAM(ttl=300s) */ SELECT * FROM products WHERE category = 'Laptops'
```

That's it. Zero application logic changes.

---

## Slide 12: Performance Results (Live Demo)

| Metric | Without Cache | With Cache | Improvement |
|--------|-------------|------------|-------------|
| Category browse | 60ms | 6ms | 10x faster |
| Product search | 173ms | 4ms | 43x faster |
| Agent tool calls (total) | 211ms | 10ms | 21x faster |
| Cache hit rate | 0% | 50-100% | — |
| Database load | Every query | First query only | up to 90%+ reduction |

> Results are from this specific live demo configuration (local Valkey + PostgreSQL, warm cache). Actual numbers vary with workload, cache hit rate, data size, and network. Treat them as illustrative, not guaranteed.

---

## Slide 13: Production-Ready Features

**Built into the Remote Query Cache Plugin:**
- Async write-back (no added latency on cache miss)
- TTL-based expiration (configurable per query)
- Connection pooling to Valkey
- TLS encryption
- IAM authentication for ElastiCache
- Health monitoring (HEALTHY → SUSPECT → DEGRADED)
- Graceful degradation (falls back to DB if cache is down)
- Cache key isolation (prefix-based multi-tenancy)
- Max query size limits (16KB default)

**You get all of this for free. No code required.**

---

## Slide 14: When to Use This

**Great for:**
- Read-heavy workloads (product catalogs, user profiles)
- Agentic AI with repeated tool calls
- Recommendation engines
- Dashboard/reporting queries
- Any query where data doesn't change every second

**Not ideal for:**
- Write-heavy transactional workloads
- Queries inside multi-statement transactions
- Real-time data that changes per request
- Large object queries (BLOB/CLOB)

---

## Slide 15: Getting Started

**Prerequisites — AWS account setup (before you begin):**
- An **Amazon ElastiCache for Valkey** cluster (or Serverless cache) provisioned, with its endpoint reachable from where the app runs.
- An **Aurora PostgreSQL** (or Amazon RDS for PostgreSQL) cluster that is accessible from the app, with credentials for the application database user.
- **Amazon Bedrock model access enabled** in your account/region for the model the agent uses (request access in the Bedrock console if not already granted).
- Network connectivity in place: the app's security group/VPC must allow outbound access to the cache and database endpoints.

**Prerequisites — deployer IAM permissions:**
The identity performing the deployment needs permissions to provision or access the services above, for example:
- `elasticache:CreateCacheCluster` / `elasticache:Describe*` (or Serverless equivalents) to create and read the cache.
- `rds:CreateDBCluster` / `rds:Describe*` (and `rds-db:connect` if using IAM database authentication) for Aurora.
- `bedrock:InvokeModel` (and `bedrock:ListFoundationModels`) for the model in use.
- If deploying via CloudFormation: `cloudformation:*` on the stack plus `ec2`, `iam:PassRole`, and the service permissions above. Scope these to least privilege for your environment.

**Steps:**

1. Add the JDBC wrapper dependency (with valkey-glide and commons-pool2)
2. Switch your JDBC driver class and URL prefix
3. Add 3 properties: wrapperPlugins, cacheEndpointAddrRw, cacheUseSSL
4. Add SQL hints to cacheable queries
5. Deploy — done

**Maven:**
```xml
<dependency>
    <groupId>software.amazon.jdbc</groupId>
    <artifactId>aws-advanced-jdbc-wrapper</artifactId>
    <version>4.4.0</version>
</dependency>
```

**Documentation:**
github.com/aws/aws-advanced-jdbc-wrapper

---

## Slide 16: Summary

**Two code changes. Zero complexity. Real results.**

- AWS Advanced JDBC Wrapper: open-source, transparent, production-ready
- Remote Query Cache Plugin: automatic query caching in Valkey
- Amazon ElastiCache for Valkey: fully managed, sub-millisecond, scalable
- Your AI agent gets faster without knowing caching exists

**A low-friction path to better database performance for Java applications.**
