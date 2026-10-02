# Valkey GLIDE Test Tools

Two standalone scripts for load-testing and seeding ElastiCache / Valkey
clusters using [valkey-glide](https://github.com/valkey-io/valkey-glide).

Both connect over TLS using the cluster configuration endpoint, and both
work against Redis clusters as old as 5.0.6 as well as newer Valkey
clusters, cluster-mode enabled with any number of shards.

---

## DO NOT RUN AGAINST PRODUCTION

**These are sandbox / test-environment tools only.** Both scripts write
real data to whatever target you point them at.

- `seed_data.py` writes millions of keys and consumes GiB of cluster
  memory. On a production cluster this can trigger evictions of real
  application data, or hit the maxmemory limit and cause write failures.
- `glide_load_generator.py` drives sustained write and read load continuously
  until stopped, competing with real application traffic.
- Keys are namespaced under a single prefix (`glidetest:...`), but if your
  application happens to use that prefix, **existing keys will be
  silently overwritten**. Change the `KEY_PREFIX` constant at the top of
  each script to something unique to your environment if this applies.
- Neither script has an undo. There is no dry-run mode.

Use these only against a dedicated test, sandbox, or throwaway cluster
that you are willing to have arbitrary data written to. Double-check the
`--host` value before every run -- a copy-pasted production endpoint is
the most likely way this causes damage.

---

## Requirements

- Python 3.9+
- Network access to the target cluster on port 6379 (or your configured port)
- TLS enabled on the target cluster (both scripts default to `--tls`)

```bash
pip3 install -r requirements.txt
```

## Compatibility note: RESP2 vs RESP3

`valkey-glide` prefers RESP3, which requires the `HELLO` command
(Redis 6.0+). Redis versions before 6.0 (e.g. 5.0.6) do not support
`HELLO` and will fail to connect under RESP3 with an error like:

```
Redis Server doesn't support HELLO command therefore resp3 cannot be
used - RESP3NotSupported
```

- `seed_data.py` always connects using RESP2, so it works unmodified
  against Redis 5.0.6 through current Valkey releases.
- `glide_load_generator.py` auto-detects: it tries RESP3 first, and if the
  server rejects it for this reason, it automatically retries with RESP2
  and logs a warning. No flag needed on either engine.

---

## Authentication (optional)

Both scripts support optional AUTH. Omit these flags entirely for
clusters with no auth configured.

```bash
# password only (requirepass / AUTH token)
--password <password>

# ACL / RBAC user
--username <user> --password <password>
```

`--username` requires `--password`; the scripts reject the combination
otherwise. Without credentials against an auth-protected target you will
get a clear `NOAUTH: Authentication required` error rather than a hang.

Note that passing a password on the command line makes it visible in
shell history and to other users via `ps`. For anything sensitive,
prefer piping from a secrets manager or using a shell that suppresses
history for the command.

---

## seed_data.py

Seeds a cluster with a target volume of mixed data types (strings,
hashes, sets), spread evenly across all 16384 hash slots so every shard
gets a proportional share. Useful for generating a realistic dataset to
migrate, benchmark, or demo against.

### What it writes

| Type    | Size per key                  |
|---------|--------------------------------|
| String  | 1 KiB random value             |
| Hash    | 20 fields x 100 bytes each     |
| Set     | 20 members x 100 bytes each    |

The `--gb` argument is the target **logical payload** size, split evenly
across the three types by byte count. Actual memory usage on the server
will be higher due to per-key and per-field overhead (observed ~1.6x in
testing — e.g. a 5 GiB logical target used ~8.25 GiB of server memory).

### Usage

```bash
python3 seed_data.py --host <cluster-config-endpoint> --gb 5
python3 seed_data.py --host <cluster-config-endpoint> --gb 5 --port 6380
python3 seed_data.py --host <cluster-config-endpoint> --gb 5 --no-tls
```

### Output

Logs progress every 10 seconds to both the console and a timestamped
file under `./logs/`:

```
2026-09-16 19:50:15 INFO progress 45.2% ops=1600000/3537194 rate=4523/s eta=428s str=800000 hash=400000 set=400000 err=0
```

Final line reports total counts per type and any errors:

```
2026-09-16 19:50:48 INFO done in 735.3s (4810 ops/s) | strings=1747626 hashes=894784 sets=894784 errors=0
```

---

## glide_load_generator.py

Continuously writes and reads a 1KB random value to every one of the
16384 hash slots, reporting per-shard success/failure/retry counts.
Useful for generating sustained read/write load during a live migration
or replication test, and for exercising every shard directly.

### Usage

```bash
# Run for a fixed duration (seconds)
python3 glide_load_generator.py --host <cluster-config-endpoint> --duration 1800

# Run a fixed number of sweeps
python3 glide_load_generator.py --host <cluster-config-endpoint> --iterations 5

# Run indefinitely until Ctrl+C
python3 glide_load_generator.py --host <cluster-config-endpoint>

# Prefer same-AZ replicas/primary for reads (recommended on AWS)
python3 glide_load_generator.py --host <cluster-config-endpoint> --az us-east-1a
```

Each "iteration" is one full sweep: a SET followed by a GET for every
slot (16384 x 2 = 32768 operations), using a key pattern of
`glidetest:slot:{N}` so each key deterministically lands on slot N.
Because each sweep overwrites the same 16384 keys with new random
values, the total data footprint on the cluster stays constant (~16 MB)
regardless of how long the script runs.

### Read strategy

Defaults to `ReadFrom.AZ_AFFINITY_REPLICAS_AND_PRIMARY` when `--az` is
given (prefers replicas or primary in the client's own AZ, falling back
to any other node if none exist there), or `PREFER_REPLICA` otherwise.
See `--help` for other options.

### Retries

Each individual SET/GET retries up to 10 times on failure with a short
backoff before being counted as a failure. Retry counts are reported
per-shard in the iteration summary.

### Output

Logs to console and a timestamped file under `./logs/`, one summary
block per iteration:

```
2026-09-16 19:50:15 INFO ▶ Iteration #1 done | SET ok=16384 fail=0 retries=0 | GET ok=16384 fail=0 retries=0
2026-09-16 19:50:15 INFO   Slots [0-3276] | SET ok=3277 fail=0 retries=0 | GET ok=3277 fail=0 retries=0
2026-09-16 19:50:15 INFO   Slots [3277-6553] | SET ok=3277 fail=0 retries=0 | GET ok=3277 fail=0 retries=0
```

---

## Cleaning up

Both scripts namespace every key they write under `glidetest:` (see
`KEY_PREFIX` in each script). To remove everything they have written,
scan for that prefix and delete the matches on each node:

```bash
# standalone target
valkey-cli --tls -h <endpoint> -p 6379 \
  --scan --pattern "glidetest:*" | \
  xargs -r -n 100 valkey-cli --tls -h <endpoint> -p 6379 DEL

# cluster target -- SCAN/DEL are per-node, run against each shard primary
for primary in <shard1-endpoint> <shard2-endpoint> <shard3-endpoint>; do
  valkey-cli --tls -h "$primary" -p 6379 \
    --scan --pattern "glidetest:*" | \
    xargs -r -n 100 valkey-cli --tls -h "$primary" -p 6379 DEL
done
```

Drop `--tls` if the target does not use TLS. `xargs -r` avoids invoking
`DEL` with no arguments if the scan finds nothing.

---

## Notes

- Both scripts auto-detect whether the target is cluster-mode enabled or
  disabled and use the matching glide client (`GlideClusterClient` or
  `GlideClient`) automatically -- no flag needed. On a standalone target,
  `glide_load_generator.py` reports all 16384 slots as a single shard, since
  there is only one node to write to.
- Log files accumulate under `./logs/` on every run and are not
  automatically cleaned up.
- Both scripts write real data to the target. Do not point them at a
  production cluster unless that is intentional.
