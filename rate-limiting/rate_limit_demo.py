# /// script
# requires-python = ">=3.10"
# dependencies = ["valkey", "developer-toolkit-elasticache", "boto3", "certifi"]
# ///
"""Rate limiting demo against an ElastiCache Serverless for Valkey cache.

Runs each algorithm described in README.md and prints which requests are
allowed or rejected.

    export CACHE_NAME=<your-serverless-cache-name>
    export REGION=us-east-1
    uv run rate_limit_demo.py
"""
import os
import sys
import time
import uuid

import boto3
import certifi
import valkey
from valkey.credentials import CredentialProvider
from developer_toolkit_elasticache import ElastiCacheIAMAuthTokenProvider

CACHE_NAME = os.environ.get("CACHE_NAME")
REGION = os.environ.get("REGION", "us-east-1")
USER_ID = "default.iam-user"


class ElastiCacheCredentialProvider(CredentialProvider):
    def __init__(self, auth):
        self._auth = auth

    def get_credentials(self):
        return self._auth.user_id, self._auth.get_token()


def connect():
    cache = boto3.client("elasticache", region_name=REGION).describe_serverless_caches(
        ServerlessCacheName=CACHE_NAME
    )["ServerlessCaches"][0]
    endpoint = cache["Endpoint"]["Address"]
    print(f"Connecting to {endpoint} ({cache['ConnectionType']} endpoint)")

    auth = ElastiCacheIAMAuthTokenProvider(
        serverless_cache_name=CACHE_NAME, user_id=USER_ID, region=REGION
    )
    return valkey.ValkeyCluster(
        host=endpoint,
        port=cache["Endpoint"]["Port"],
        ssl=True,
        # python.org builds on macOS don't use the system CA store
        ssl_ca_certs=certifi.where(),
        credential_provider=ElastiCacheCredentialProvider(auth),
    )


# ---------------------------------------------------------------- Lua scripts

FIXED_WINDOW_LUA = """
local count = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1], 'NX')
return count
"""

SLIDING_LOG_LUA = """
local key    = KEYS[1]
local window = tonumber(ARGV[1])
local limit  = tonumber(ARGV[2])
local member = ARGV[3]

local t   = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, window)
    return {1, count + 1, 0}
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
return {0, count, tonumber(oldest[2]) + window - now}
"""

SLIDING_COUNTER_LUA = """
local window  = tonumber(ARGV[1])
local limit   = tonumber(ARGV[2])
local elapsed = tonumber(ARGV[3])

local current  = tonumber(redis.call('GET', KEYS[1]) or '0')
local previous = tonumber(redis.call('GET', KEYS[2]) or '0')

local estimated = previous * ((window - elapsed) / window) + current

if estimated < limit then
    redis.call('INCR', KEYS[1])
    redis.call('EXPIRE', KEYS[1], window * 2, 'NX')
    return {1, math.floor(estimated) + 1}
end

return {0, math.floor(estimated)}
"""

TOKEN_BUCKET_LUA = """
local key       = KEYS[1]
local capacity  = tonumber(ARGV[1])
local rate      = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])

local t   = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(bucket[1]) or capacity
local ts     = tonumber(bucket[2]) or now

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * rate / 1000)

local allowed = 0
local retry_after = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
else
    retry_after = math.ceil((requested - tokens) * 1000 / rate)
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil(capacity * 1000 / rate))

return {allowed, math.floor(tokens), retry_after}
"""


# ----------------------------------------------------------------- algorithms

def fixed_window(script, caller_id, limit, window_seconds):
    now = int(time.time())
    window_start = now - (now % window_seconds)
    key = f"ratelimit:fixed:{{{caller_id}}}:{window_start}"

    count = script(keys=[key], args=[window_seconds])

    allowed = count <= limit
    retry_after = 0 if allowed else window_start + window_seconds - now
    return allowed, max(limit - count, 0), retry_after


def sliding_log(script, caller_id, limit, window_seconds):
    key = f"ratelimit:log:{{{caller_id}}}"
    allowed, count, retry_ms = script(
        keys=[key], args=[window_seconds * 1000, limit, uuid.uuid4().hex]
    )
    return bool(allowed), limit - count, retry_ms / 1000


def sliding_window_counter(script, caller_id, limit, window_seconds):
    now = time.time()
    current_start = int(now) - (int(now) % window_seconds)
    previous_start = current_start - window_seconds
    elapsed = now - current_start

    keys = [
        f"ratelimit:swc:{{{caller_id}}}:{current_start}",
        f"ratelimit:swc:{{{caller_id}}}:{previous_start}",
    ]
    allowed, estimated = script(keys=keys, args=[window_seconds, limit, elapsed])
    return bool(allowed), max(limit - estimated, 0), None  # no exact retry time


def token_bucket(script, caller_id, capacity, refill_per_second, cost=1):
    key = f"ratelimit:bucket:{{{caller_id}}}"
    allowed, remaining, retry_ms = script(
        keys=[key], args=[capacity, refill_per_second, cost]
    )
    return bool(allowed), remaining, retry_ms / 1000


# ----------------------------------------------------------------------- demo

def run(title, check, requests, delay=0.0):
    print(f"\n== {title}")
    allowed_count = 0
    for i in range(1, requests + 1):
        allowed, remaining, retry_after = check()
        allowed_count += allowed
        mark = "ALLOW " if allowed else "REJECT"
        if allowed:
            extra = f"remaining={remaining}"
        elif retry_after is None:
            extra = "retry_after=unknown"
        else:
            extra = f"retry_after={retry_after:.2f}s"
        print(f"  request {i:2d}: {mark} {extra}")
        if delay:
            time.sleep(delay)
    print(f"  -> {allowed_count}/{requests} allowed")


def main():
    if not CACHE_NAME:
        sys.exit("Set CACHE_NAME to the name of your ElastiCache Serverless cache")

    client = connect()
    print("PING ->", client.ping())

    # Unique caller per run so repeated demos don't share state
    run_id = uuid.uuid4().hex[:8]

    fixed_window_script = client.register_script(FIXED_WINDOW_LUA)
    sliding_log_script = client.register_script(SLIDING_LOG_LUA)
    sliding_counter_script = client.register_script(SLIDING_COUNTER_LUA)
    token_bucket_script = client.register_script(TOKEN_BUCKET_LUA)

    run("Fixed window: 5 requests / 10 s",
        lambda: fixed_window(fixed_window_script, f"user:{run_id}", 5, 10), 8)

    run("Sliding window log: 5 requests / 10 s",
        lambda: sliding_log(sliding_log_script, f"user:{run_id}", 5, 10), 8)

    run("Sliding window counter: 5 requests / 10 s",
        lambda: sliding_window_counter(sliding_counter_script, f"user:{run_id}", 5, 10), 8)

    run("Token bucket: capacity 5, refill 2/s, one request every 0.25 s",
        lambda: token_bucket(token_bucket_script, f"user:{run_id}", 5, 2), 12, delay=0.25)


if __name__ == "__main__":
    main()
