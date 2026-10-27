# ShopMind Demo

E-commerce application demonstrating JDBC query caching with the AWS Advanced JDBC Wrapper's Remote Query Cache Plugin and Amazon ElastiCache for Valkey.

## Architecture

```
┌──────────┐     ┌──────────────┐     ┌────────────┐
│  React   │────▶│  Spring Boot │────▶│ PostgreSQL │
│ Frontend │     │   Backend    │     └────────────┘
└──────────┘     │              │
                 │  AWS JDBC    │     ┌────────────┐
                 │  Wrapper     │────▶│  Valkey    │
                 └──────────────┘     │  (Cache)   │
                        │             └────────────┘
                        ▼
                 ┌──────────────┐
                 │  WebSocket   │──▶ Live Performance Dashboard
                 └──────────────┘
```

## Quick Start

### 1. Start infrastructure
```bash
docker compose up -d
```

### 2. Start backend
```bash
cd backend
./mvnw spring-boot:run
```

### 3. Start frontend
```bash
cd frontend
npm install
npm start
```

App runs at http://localhost:3000

## Demo Flow

1. Browse products and search — observe all queries hitting the database (red indicators)
2. Note the response times in the performance dashboard
3. Flip the cache toggle ON
4. Repeat the same searches — watch cache hits (green) appear and response times drop
5. Observe the hit rate climbing as the cache warms up

## How Caching Works

The only code change to enable caching is a SQL hint:

```sql
-- Without cache
SELECT * FROM products WHERE category = 'Laptops'

-- With cache (300s TTL)
/* CACHE_PARAM(ttl=300s) */ SELECT * FROM products WHERE category = 'Laptops'
```

The AWS JDBC Wrapper's Remote Query Cache Plugin handles everything else:
- Cache lookup in Valkey
- Database fallback on cache miss
- Async cache write-back
- TTL-based expiration

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| DB_HOST | localhost | PostgreSQL host |
| DB_PORT | 5432 | PostgreSQL port |
| DB_NAME | shopmind | Database name |
| DB_USERNAME | shopmind | Database user |
| DB_PASSWORD | shopmind | Database password |
| CACHE_ENABLED | false | Enable query caching |
| CACHE_ENDPOINT | localhost:6379 | Valkey endpoint |
| CACHE_USE_SSL | false | Use TLS for cache |
| CACHE_TTL | 300 | Cache TTL in seconds |

## Production Setup with ElastiCache for Valkey

```properties
cache.enabled=true
cache.endpoint=my-cache.xxxxx.valkey.use1.cache.amazonaws.com:6379
cache.use-ssl=true
```

For IAM authentication, set `cacheName` and `cacheIamRegion` in the JDBC wrapper properties.
