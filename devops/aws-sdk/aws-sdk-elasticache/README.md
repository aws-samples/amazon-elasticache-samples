# AWS SDK for Amazon ElastiCache

__Install packages__
```bash
poetry add python-dotenv boto3 pydantic
# or
poetry update
```

__Create ElastiCache for Redis with Cluster Mode Disabled deployment__
```bash
poetry run python aws_sdk_elasticache/elasticache_redis_cmd.py
poetry run python aws_sdk_elasticache/redis.py
```
