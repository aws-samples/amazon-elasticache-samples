# Rate limiting with Amazon ElastiCache for Valkey

This example shows how to build a distributed rate limiter on Amazon ElastiCache for
Valkey. `rate_limit_demo.py` implements four common rate limiting algorithms as Lua
scripts and runs each one against an ElastiCache Serverless cache, printing which
requests are allowed and which are rejected.

## Why ElastiCache for rate limiting

A rate limiter answers one question on every request: *has this caller used up their
allowance?* Doing that in each application server's memory doesn't work once you have
more than one server, because every server keeps its own count. You need a shared
counter that is:

- **Fast**: it sits on the request path, so it needs sub-millisecond reads and writes.
- **Atomic**: two servers must not both see "1 request left" and both let a request through.
- **Self-cleaning**: old counters should expire on their own.

Valkey provides all three with atomic commands (`INCR`, `ZADD`, …), Lua scripts that
run atomically on the server, and key TTLs.

## Contents

- [Running the example](#running-the-example)
- [Choosing an algorithm](#choosing-an-algorithm)
- [Fixed window counter](#fixed-window-counter)
- [Sliding window log](#sliding-window-log)
- [Sliding window counter](#sliding-window-counter)
- [Token bucket](#token-bucket)
- [Production considerations](#production-considerations)

| File | Purpose |
|------|---------|
| `rate_limit_demo.py` | Runs every algorithm against your cache |
| `requirements.txt` | Dependencies for running the demo with `pip` instead of `uv` |

<a id="running-the-example"></a>

## Running the example

### Prerequisites

- Python 3.10 or later, and either [uv](https://docs.astral.sh/uv/) or `pip`.
- AWS credentials configured in your shell (for example with `aws configure` or
  `AWS_PROFILE`).
- An **ElastiCache Serverless for Valkey** cache that uses IAM authentication with the
  `default.iam-user` user, and that is reachable from where you run the demo.

The easiest way to run the demo from a laptop is a cache with a **public endpoint**.
Public endpoints are available for ElastiCache Serverless with Valkey 9.0 or later.
Every connection must use IAM authentication and TLS 1.3, and the connection type can
only be chosen when the cache is created. For example (requires AWS CLI 2.37 or later):

```bash
aws elasticache create-serverless-cache \
  --serverless-cache-name my-rate-limit-cache \
  --engine valkey \
  --major-engine-version 9 \
  --connection-type public \
  --user-group-id default.iam-user-group \
  --region us-east-1
```

`default.iam-user-group` is the system-managed user group whose only user is
`default.iam-user`, which authenticates with IAM. With a regular VPC endpoint, run the
demo from a host inside the cache's VPC instead.

The IAM user or role that runs the demo needs `elasticache:DescribeServerlessCaches`
(to look up the endpoint) and `elasticache:Connect` on **both** the cache and the
ElastiCache user:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "elasticache:Connect",
            "Resource": [
                "arn:aws:elasticache:<region>:<account-id>:serverlesscache:<cache-name>",
                "arn:aws:elasticache:<region>:<account-id>:user:default.iam-user"
            ]
        },
        {
            "Effect": "Allow",
            "Action": "elasticache:DescribeServerlessCaches",
            "Resource": "*"
        }
    ]
}
```

### Run the demo

Set the cache name and region, then run the script:

```bash
cd rate-limiting

export CACHE_NAME=my-rate-limit-cache
export REGION=us-east-1          # optional, defaults to us-east-1

uv run rate_limit_demo.py
```

`uv run` installs the dependencies listed at the top of `rate_limit_demo.py`
automatically. To use plain `python` instead, install them into a virtual environment
first:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python rate_limit_demo.py
```

| Environment variable | Required | Default | Description |
|----------------------|----------|---------|-------------|
| `CACHE_NAME` | Yes | | Name of the ElastiCache Serverless cache |
| `REGION` | No | `us-east-1` | AWS Region of the cache |

The script looks up the cache endpoint with `DescribeServerlessCaches`, connects with a
cluster client over TLS using a short-lived IAM auth token from the
[Developer Toolkit for ElastiCache](https://github.com/aws/developer-toolkit-elasticache),
and then runs each algorithm with a limit of 5 requests per 10 seconds. Each run uses a
new random caller ID, so repeated runs don't share state. The output looks like this:

```
Connecting to my-rate-limit-cache-xxxxxx.public.serverless.use1.cache.amazonaws.com (public endpoint)
PING -> True

== Fixed window: 5 requests / 10 s
  request  1: ALLOW  remaining=4
  ...
  request  6: REJECT retry_after=7.00s
  ...
  -> 5/8 allowed
```

All demo keys have a TTL, so nothing is left behind once they expire.

### How the demo connects

Serverless caches always run in cluster mode, so the demo uses `ValkeyCluster`. The
credential provider is called for every new connection, so each connection gets a
fresh IAM token:

```python
class ElastiCacheCredentialProvider(CredentialProvider):
    def __init__(self, auth):
        self._auth = auth

    def get_credentials(self):
        return self._auth.user_id, self._auth.get_token()


auth = ElastiCacheIAMAuthTokenProvider(
    serverless_cache_name=CACHE_NAME, user_id=USER_ID, region=REGION
)
client = valkey.ValkeyCluster(
    host=endpoint,
    port=port,
    ssl=True,
    ssl_ca_certs=certifi.where(),
    credential_provider=ElastiCacheCredentialProvider(auth),
)
```

IAM tokens are valid for 15 minutes but are only checked when a connection is opened,
so existing connections aren't affected when a token expires. ElastiCache closes
connections after 12 hours and the client reconnects automatically.

> **macOS note:** Python from python.org doesn't use the macOS system certificate
> store, so the TLS handshake fails with `CERTIFICATE_VERIFY_FAILED`. The demo passes
> `ssl_ca_certs=certifi.where()` to avoid this. Don't turn off certificate verification.

### Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `Set CACHE_NAME ...` | The `CACHE_NAME` environment variable isn't set |
| `ServerlessCacheNotFoundFault` | Wrong `CACHE_NAME` or `REGION` |
| `WRONGPASS` | Missing `elasticache:Connect` on the cache **or** the user ARN, wrong username, or the cache doesn't use IAM authentication |
| `ERR service is not available` | Pre-auth failure, such as IAM authentication throttling. Retry with backoff |
| `CERTIFICATE_VERIFY_FAILED` | Your Python can't find a CA bundle; see the macOS note above |
| Connection timeout | The endpoint isn't reachable from your machine (VPC endpoint, or outbound port 6379 blocked) |

<a id="choosing-an-algorithm"></a>

## Choosing an algorithm

| Algorithm | Accuracy | Memory per caller | Bursts | Best for |
|-----------|----------|-------------------|--------|----------|
| [Fixed window](#fixed-window-counter) | Can allow up to 2× the limit at window edges | 1 integer | Allowed at window edges | Simple quotas ("1000 calls / hour") |
| [Sliding window log](#sliding-window-log) | Exact | 1 entry per request | No | Low limits where accuracy matters (login attempts) |
| [Sliding window counter](#sliding-window-counter) | Close approximation | 2 integers | Smoothed | High-volume API limits |
| [Token bucket](#token-bucket) | Exact average rate | 1 small hash | Allowed, up to bucket size | APIs that should allow short bursts |

If you're not sure, start with the **token bucket**. It's what most public APIs use,
including AWS API throttling.

Every algorithm below follows the same pattern: the Python function builds the key
names, then calls a Lua script that does the check and the update in one atomic step
on the server. The scripts are registered with `client.register_script(...)`, which
calls them with `EVALSHA` and only sends the script body if the server hasn't cached it
yet.

<a id="fixed-window-counter"></a>

## Fixed window counter

### How it works

Time is cut into fixed windows (for example, each calendar minute). Each caller has
one counter per window. Every request increments the counter. If the counter goes
over the limit, the request is rejected. When the window ends, the counter expires
and a new one starts.

```
limit = 5 per minute

12:00:00 ─────────────── 12:01:00 ─────────────── 12:02:00
 ✓ ✓ ✓ ✓ ✓ ✗ ✗            ✓ ✓ ✓ …
 counter: 1 2 3 4 5 6 7   counter resets to 1
```

### Key design

Include the window start in the key, so each window gets its own counter:

```
ratelimit:fixed:{<caller-id>}:<window-start-epoch-seconds>
```

For example, `ratelimit:fixed:{user:42}:1760371200`. The `{…}` is a cluster
**hash tag**; see [Cluster mode and hash tags](#cluster-mode-and-hash-tags).

### Commands

The whole algorithm is two commands, which must run together atomically:

```
INCR   ratelimit:fixed:{user:42}:1760371200
EXPIRE ratelimit:fixed:{user:42}:1760371200 60 NX
```

- `INCR` creates the key at `1` if it doesn't exist and returns the new value.
- `EXPIRE … NX` sets the TTL only if the key doesn't already have one, so the TTL is
  set once when the window starts and isn't pushed forward on every request.

The request is allowed if the value returned by `INCR` is ≤ the limit.

They must be atomic so the TTL is always set. A client that crashes between `INCR`
and `EXPIRE` would leave a key that never expires. You could use `MULTI`/`EXEC`, but
some cluster clients (including valkey-py's `ValkeyCluster`) don't support
transactions. A tiny Lua script works with every client:

```lua
-- KEYS[1] = counter key, ARGV[1] = window in seconds
local count = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1], 'NX')
return count
```

### Python

```python
def fixed_window(script, caller_id, limit, window_seconds):
    now = int(time.time())
    window_start = now - (now % window_seconds)
    key = f"ratelimit:fixed:{{{caller_id}}}:{window_start}"

    count = script(keys=[key], args=[window_seconds])

    allowed = count <= limit
    retry_after = 0 if allowed else window_start + window_seconds - now
    return allowed, max(limit - count, 0), retry_after
```

### Trade-offs

**Pros**
- Simplest algorithm, and one key with a single integer per caller.
- Each request is one small script call: an `INCR`, plus an `EXPIRE` that does nothing
  after the first request.
- The remaining count and reset time are easy to send back in HTTP headers
  (`X-RateLimit-Remaining`, `Retry-After`).

**Cons: bursts at window edges**

A caller can send the full limit at the end of one window and the full limit again at
the start of the next. That's up to **2× the limit** within a short span:

```
limit = 5 per minute
            12:00:59  12:01:00
              ✓✓✓✓✓ | ✓✓✓✓✓    ← 10 requests in ~1 second
```

If that matters, use the [sliding window counter](#sliding-window-counter) or the
[token bucket](#token-bucket).

### When to use it

Quotas where the edge burst doesn't matter: "1,000 API calls per hour per API key",
"10 password-reset emails per day".

<a id="sliding-window-log"></a>

## Sliding window log

### How it works

Store the timestamp of every accepted request in a **sorted set**, using the
timestamp as the score. On each new request:

1. Remove entries older than `now - window`.
2. Count what's left.
3. If the count is below the limit, add the new request and allow it. Otherwise reject.

Because the window slides with the current time, there is no edge burst. At any
instant, the last `window` seconds contain at most `limit` requests.

### Key design

```
ratelimit:log:{<caller-id>}
```

One sorted set per caller. Members are unique request IDs, and scores are timestamps
in milliseconds.

### Lua script

The check and the add must be **atomic**. If two requests run step 2 at the same time,
both could see `limit - 1` and both be allowed. A Lua script runs on the server as a
single atomic operation, and it saves several network round trips.

```lua
-- KEYS[1] = sorted set key
-- ARGV[1] = window in milliseconds
-- ARGV[2] = limit
-- ARGV[3] = unique member for this request
-- returns { allowed (1/0), current count, retry-after ms }

local key    = KEYS[1]
local window = tonumber(ARGV[1])
local limit  = tonumber(ARGV[2])
local member = ARGV[3]

-- Use the server clock so every app server agrees on "now"
local t   = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, window)
    return {1, count + 1, 0}
end

-- Rejected: tell the caller when the oldest entry ages out
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
return {0, count, tonumber(oldest[2]) + window - now}
```

Notes:

- **Server time (`TIME`)**: application servers' clocks drift. Using the Valkey
  server's clock gives every caller the same `now`.
- **Unique member**: two requests in the same millisecond would collide if the
  timestamp were the member, so the demo uses a UUID.
- **`PEXPIRE`**: the whole set expires once the caller goes idle for one window.
- Rejected requests are **not** recorded, so a caller that keeps retrying doesn't
  extend their own lockout.

### Python

```python
def sliding_log(script, caller_id, limit, window_seconds):
    key = f"ratelimit:log:{{{caller_id}}}"
    allowed, count, retry_ms = script(
        keys=[key], args=[window_seconds * 1000, limit, uuid.uuid4().hex]
    )
    return bool(allowed), limit - count, retry_ms / 1000
```

### Trade-offs

**Pros**
- Exact: no edge bursts, no approximation.
- Gives a precise `Retry-After`.

**Cons**
- Memory grows with the limit: one sorted-set entry per accepted request in the
  window. A limit of 10,000/hour means up to 10,000 entries per caller.
- `ZREMRANGEBYSCORE` gets more expensive as the set grows.

### When to use it

Low limits where precision matters: login attempts (5 per 15 minutes), OTP or SMS
sends, expensive operations such as report generation.

<a id="sliding-window-counter"></a>

## Sliding window counter

### How it works

This combines the low memory use of the [fixed window](#fixed-window-counter) with most
of the accuracy of the [sliding log](#sliding-window-log).

Keep a counter for the **current** fixed window and the **previous** one. Estimate the
number of requests in the last `window` seconds by weighting the previous window by
how much of it still overlaps the sliding window:

```
estimated = previous_count × (1 − elapsed / window) + current_count
```

```
limit = 100 / minute, now = 12:01:15 (25% into the current window)

   previous window (12:00)   current window (12:01)
   ├──────── 80 ───────────┤├── 30 ──┤
              ◄──── sliding 60 s ────►
                 75% of previous   + current

estimated = 80 × 0.75 + 30 = 90  → allowed (90 < 100)
```

The estimate assumes requests in the previous window were spread evenly. In practice
the error is small, and it removes the 2× edge burst.

### Key design

```
ratelimit:swc:{<caller-id>}:<window-start-epoch-seconds>
```

Each request touches **two** keys: the current window and the previous one. Both must
be in the same cluster slot to be used in one Lua script, which is why the caller ID is
wrapped in a hash tag; see [Cluster mode and hash tags](#cluster-mode-and-hash-tags).

### Lua script

```lua
-- KEYS[1] = current window key
-- KEYS[2] = previous window key
-- ARGV[1] = window in seconds
-- ARGV[2] = limit
-- ARGV[3] = elapsed seconds in current window (float)
-- returns { allowed (1/0), estimated count }

local window  = tonumber(ARGV[1])
local limit   = tonumber(ARGV[2])
local elapsed = tonumber(ARGV[3])

local current  = tonumber(redis.call('GET', KEYS[1]) or '0')
local previous = tonumber(redis.call('GET', KEYS[2]) or '0')

local estimated = previous * ((window - elapsed) / window) + current

if estimated < limit then
    redis.call('INCR', KEYS[1])
    -- Keep the key for two windows: it's read as the "previous" window next time
    redis.call('EXPIRE', KEYS[1], window * 2, 'NX')
    return {1, math.floor(estimated) + 1}
end

return {0, math.floor(estimated)}
```

### Python

```python
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
```

The window position is computed from the application clock here, because the keys
must be known before the script runs. Small clock drift between app servers only
slightly shifts the weighting.

### Trade-offs

**Pros**
- Two integers per caller, however high the limit.
- Smooths out the fixed window's edge burst.
- Constant cost per request.

**Cons**
- An approximation. It can be slightly off when traffic within a window is very uneven.
- No exact `Retry-After`.

### When to use it

High-volume API rate limits (thousands of requests per minute per key) where the
sliding log would use too much memory and the fixed window's edge burst is
unacceptable.

<a id="token-bucket"></a>

## Token bucket

### How it works

Each caller has a bucket that holds up to `capacity` tokens. Tokens refill at a steady
`refill_rate` per second. Each request takes one token. If the bucket is empty, the
request is rejected.

```
capacity = 10, refill_rate = 2/s

  idle for a while  →  bucket full (10)  →  burst of 10 requests all allowed
  then              →  ~2 requests/s sustained
```

Two parameters control two separate things:

- **`refill_rate`**: the long-term average rate.
- **`capacity`**: the largest burst allowed after a caller has been idle.

The demo uses a capacity of 5 and a refill rate of 2/s, and sends one request every
0.25 s (4/s). The first requests drain the bucket, after which about every other
request is allowed.

### Lazy refill

There is no background job adding tokens. The bucket stores only:

| Field | Meaning |
|-------|---------|
| `tokens` | Tokens left at the last update |
| `ts` | Time of the last update (ms) |

On each request, the script works out how many tokens have accumulated since `ts`,
adds them (up to `capacity`), and then tries to take one.

### Key design

```
ratelimit:bucket:{<caller-id>}
```

One small hash per caller.

### Lua script

```lua
-- KEYS[1] = bucket hash key
-- ARGV[1] = capacity (max tokens)
-- ARGV[2] = refill rate (tokens per second)
-- ARGV[3] = tokens requested (usually 1)
-- returns { allowed (1/0), tokens remaining (floor), retry-after ms }

local key       = KEYS[1]
local capacity  = tonumber(ARGV[1])
local rate      = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])

local t   = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(bucket[1]) or capacity
local ts     = tonumber(bucket[2]) or now

-- Refill based on time elapsed since the last update
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
-- Expire once the bucket would be full again; a missing key means "full"
redis.call('PEXPIRE', key, math.ceil(capacity * 1000 / rate))

return {allowed, math.floor(tokens), retry_after}
```

Notes:

- A missing key is treated as a **full bucket**, so new callers can burst right away,
  and the `PEXPIRE` lets idle buckets disappear without changing behavior.
- `tokens` is stored as a fraction, so slow refill rates such as 0.5/s work correctly.
- `requested` can be more than 1 for weighted requests, for example a bulk endpoint
  that costs 10 tokens.

### Python

```python
def token_bucket(script, caller_id, capacity, refill_per_second, cost=1):
    key = f"ratelimit:bucket:{{{caller_id}}}"
    allowed, remaining, retry_ms = script(
        keys=[key], args=[capacity, refill_per_second, cost]
    )
    return bool(allowed), remaining, retry_ms / 1000
```

### Trade-offs

**Pros**
- Allows short bursts while enforcing the average rate. This matches real traffic,
  such as a page load that fires 8 API calls at once.
- One small hash per caller, constant cost per request.
- Supports weighted requests.
- Gives an exact `Retry-After`.

**Cons**
- Two parameters to tune instead of one.
- "N requests per window" is less obvious to explain to API users.

### Variant: leaky bucket / GCRA

A *leaky bucket* processes requests at a fixed rate with no bursts. You get the same
effect from a token bucket with `capacity = 1`. GCRA (Generic Cell Rate Algorithm)
does this by storing a single timestamp, the "theoretical arrival time", which makes
it the cheapest exact limiter. It suits cases where you want smooth, evenly spaced
traffic, such as calling a fragile downstream system.

<a id="production-considerations"></a>

## Production considerations

<a id="cluster-mode-and-hash-tags"></a>

### Cluster mode and hash tags

ElastiCache Serverless always runs in **cluster mode**. Keys are spread across hash
slots, and a Lua script or `MULTI` transaction can only touch keys **in the same slot**.
Otherwise you get a `CROSSSLOT` error.

Put the part of the key that identifies the caller inside `{…}`. Only the text inside
the braces is hashed, so all of a caller's keys land in the same slot:

```
ratelimit:swc:{user:42}:1760371200    ┐
ratelimit:swc:{user:42}:1760371140    ┘ same slot → OK in one script
```

Different callers still spread across the cluster, because their tags differ. **Don't**
use one hash tag shared by everyone (for example `{ratelimit}`). That would put all
traffic on one shard.

### Key naming

```
ratelimit:<algorithm>:{<scope>:<id>}[:<window>]
```

| Scope | Example | Protects against |
|-------|---------|------------------|
| API key / tenant | `{tenant:acme}` | One customer using up shared capacity |
| User | `{user:42}` | Abuse by one account |
| IP address | `{ip:203.0.113.7}` | Unauthenticated abuse, credential stuffing |
| User + endpoint | `{user:42:POST /orders}` | Different limits per operation |

You can apply several limiters to one request (per-IP **and** per-user **and** global),
and reject the request if any of them says no.

### Always set a TTL

Every rate-limit key must expire. All the scripts in this example set `EXPIRE` or
`PEXPIRE`, so idle callers cost no memory. Without a TTL, keys build up for every
caller you have ever seen.

### Use the server clock

Clocks on application servers drift. Scripts that call `redis.call('TIME')` use the
Valkey server's clock, so every app server agrees on "now". This works because Valkey
replicates the *effects* of a script (the writes) rather than re-running the script on
replicas.

### Fail open or fail closed?

Decide what happens when the cache can't be reached (timeout, failover, throttled
IAM auth):

| Choice | Behavior | Use when |
|--------|----------|----------|
| **Fail open** | Allow the request, log, emit a metric | The limiter protects capacity and an outage of the limiter shouldn't become an outage of your API |
| **Fail closed** | Reject the request | The limiter is a security control (login attempts, OTP, fraud) |

Either way, set a **short client timeout** (for example 50–100 ms) on rate-limit calls,
so a slow cache can't slow down every request.

### Tell clients they are limited

Return HTTP `429 Too Many Requests` with headers built from the script results:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 3
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 0
```

### Reuse connections

With IAM authentication, every new connection authenticates with a fresh token.
Opening connections quickly, for example one per request in a serverless function,
can hit **IAM authentication throttling**, which shows up as `ERR service is not
available` before auth.

- Create the client once per process or container, not once per request.
- Use a client with connection pooling. [Valkey GLIDE](https://glide.valkey.io)
  multiplexes over a few connections and can generate IAM tokens itself.
- Back off exponentially on connection errors.

### Cost

ElastiCache Serverless is billed on data stored and **ElastiCache Processing Units
(ECPUs)**. Rate-limit keys are tiny, so cost is driven mostly by request volume:

- Each rate-limit check is roughly one script call per request. The fixed window and
  token bucket are the cheapest per call.
- The sliding log costs more per call as the limit grows, because its sorted set gets
  bigger.
- Set `--cache-usage-limits` on the cache to cap maximum ECPUs and storage, so a traffic
  spike or attack against your limiter can't drive unlimited cost.

### Monitoring

Useful CloudWatch metrics for the cache:

| Metric | Why |
|--------|-----|
| `ElastiCacheProcessingUnits` | Cost driver; grows with request rate |
| `SuccessfulReadRequestLatency` / `SuccessfulWriteRequestLatency` | Limiter latency is on your request path |
| `ThrottledCmds` | The cache itself is being throttled |
| `CurrConnections` / `NewConnections` | Spot connection churn (see above) |
| `EvalBasedCmds` | Volume of Lua-based rate-limit checks |

Also emit your **own** application metrics: allowed vs. rejected per limiter, and
fail-open events.

### Testing

- **Unit-test the logic** against a local Valkey server. The Lua scripts behave the
  same as on ElastiCache.
- **Test concurrency**: fire many parallel requests for one caller and check that exactly
  `limit` are allowed. This catches non-atomic implementations.
- **Test window edges**: send the full limit just before a boundary and again just after
  it, to confirm whether your algorithm allows the 2× burst.

## Further reading

- [Rate limiting strategies with Valkey and Redis](https://www.percona.com/blog/rate-limiting-strategies-with-valkey-redis/) (Percona blog)
- [Developer Toolkit for ElastiCache](https://github.com/aws/developer-toolkit-elasticache)
