# ECDL benchmark results

Measured throughput and freshness for the two ECDL paths. The **bulk load** copies
tables straight from the source into the cache (no Debezium, no broker). **Live
CDC** (change data capture) streams every committed change: source log → Debezium → broker (Valkey
Streams) → sink → cache.

> **When these were measured.** These runs used ECDL 0.1.0 (October 2026), with
> Debezium, the broker and the sink on one EC2 `c5.4xlarge` rather than on
> Fargate, where a deployed link runs. They have not been repeated on 0.2.29.
> Two changes since then make each write wait for a replica, which costs
> throughput:
> - Debezium waits for a broker replica to confirm each batch before its source
>   position moves on (0.2.17).
> - The sink waits for a target replica to confirm each batch before it
>   acknowledges it (0.2.26). Measured locally: about 25–30% fewer writes per
>   second on a single-shard target with a replica, no measurable change on a
>   cluster. `TARGET_WAIT_REPLICAS=0` turns it off.

## Four source engines, matched hardware

One controlled run: the source engine was the only thing that changed.

| Source | Bulk load, 1-shard target | Bulk load, 3-shard target | Live CDC, 2M events end to end | Freshness p90 (90th percentile) |
|---|---|---|---|---|
| Aurora MySQL 8.0 | 179K rows/s | 225K rows/s | 23K events/s | 250 ms |
| Aurora PostgreSQL 16 | 214K rows/s | 254K rows/s | 51K events/s | 210 ms |
| SQL Server 2022 | 187K rows/s | 229K rows/s | 21K events/s | 195 ms |
| Oracle 19c (RDS) | 194K rows/s | 259K rows/s | 43K events/s | 220 ms |

Every run delivered every row: 5,000,000 keys per bulk load and 2,000,000 events
per CDC run.


**What freshness means here.** Time from a commit on the source to the key
appearing in the cache. It grows with transaction size, because Debezium emits a
transaction's rows only after it commits. Measured on MySQL at the same throughput:

| Rows per transaction | Freshness |
|---|---|
| 1 | ~12 ms |
| 100 | ~200–700 ms |
| 2,000 | ~1.0–1.6 s |

## Scaling limits

| What | Result | What limits it |
|---|---|---|
| Bulk load, 1-shard t3.medium target, Python loader | 143K rows/s (5M rows in 35 s) | the single target primary |
| Bulk load, 3-shard t3.medium target, Go loader, 32–48 workers | 610K rows/s (5M rows in 8 s) | not reached |
| Live CDC, MySQL, Debezium on Fargate: 0.5 / 2 / 4 / 8 vCPU | 11K / 22K / 33K / 32K events/s | one Debezium thread reads the log in order: ~37K/s on Fargate, ~66K/s on fast dedicated cores |
| Sink drain from a backlog, 3 / 6-shard m6g.large target | 194K / 222K events/s | target writes |
| Row filter + computed field + rename + column select | 194K vs 200K events/s plain (−3%) | target writes |
| Denormalization: embedding parent fields into each child | ~8K events/s | three sequential round trips per event |
| Denormalization: a parent change fanned out to its children | ~71K child writes/s | pipelined writes |

Live CDC is bounded by the source's single ordered log, not by ECDL: going faster
needs a faster core for Debezium, or splitting the source into several databases.
The sink drains about 4–9x faster than one log produces, so after an outage the
backlog clears faster than it built up.

## Test setup

**Four-engine run (the table above)**

| Component | Configuration |
|---|---|
| Sources | Aurora MySQL 8.0.39, Aurora PostgreSQL 16.11 and RDS Oracle 19c EE on `db.r6i.2xlarge`; SQL Server 2022 CU26 Developer on EC2 `r6i.2xlarge` with 100 GiB gp3 (6,000 IOPS). All 8 vCPU / 64 GiB. |
| Table | `orders3w (id BIGINT PK, category VARCHAR(50), amount DECIMAL(18,2), customer_id INT)`, exactly 5,000,000 rows, contiguous ids |
| Target | ElastiCache for Valkey 9.0, `cache.m7g.large`, cluster mode on, TLS, 1 replica per shard; 1-shard and 3-shard |
| Pipeline | Debezium Server 3.0.6, broker (Valkey 8) and the Go sink on one EC2 `c5.4xlarge` (16 vCPU) |
| Bulk load | Python loader, 16 workers, 50,000-row fetches; target flushed before each run, key count verified after |
| Live CDC | 2,000,000 inserts from 24 writers, 1,000 rows per statement, commit every 2,000 rows, 3-shard target |
| Region | us-east-2; Oracle in us-east-1, on a test environment rebuilt to the same specification |

**Scaling runs**

| Run | Configuration |
|---|---|
| Bulk load scaling | Aurora MySQL source; loader on EC2 `c5.4xlarge`; target `cache.t3.medium`, 1 shard or 3 shards × (1 primary + 2 replicas), cluster mode, TLS |
| Live CDC scaling | Aurora MySQL Serverless v2; 24 writers × 2,000-row batches, 2M rows; Debezium on ECS Fargate; broker 1-shard Valkey; sink 2 Fargate tasks × 1 vCPU |
| Sink drain and transforms | Broker pre-loaded with 1–5M events; Go sink with 16 workers, 20,000-entry batches; target `cache.m6g.large` per shard, cluster mode, TLS |
| Denormalization | Same as sink drain, 3-shard target; `customers.{name, tier}` embedded into `orders` |

## Caveats

- Bulk load plateaus near 250K rows/s in the four-engine test environment at any worker count
  from 8 to 48, and a third shard adds only ~20%. The cause is not established;
  do not read that column as an engine ranking.
- The pipeline host was shared with other containers, so absolute CDC rates are
  below a dedicated host; all four engines ran on the same host.
- Oracle ran in a different account and region: same specification, different
  physical hardware and network path.
- SQL Server's continuous capture (`pollinginterval = 0`) roughly halved its source
  insert rate under sustained load in these runs (102K → 51K rows/s). This was not a
  same-session A/B comparison.
- On SQL Server, CDC also costs 1.34x the base table's storage (change tables) and
  peak transaction-log headroom. MySQL and PostgreSQL write their logs regardless.
