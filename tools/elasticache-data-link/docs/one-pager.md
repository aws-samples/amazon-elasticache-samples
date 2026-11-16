# ElastiCache Data Link (ECDL)

Keeps ElastiCache for Valkey in sync with a relational database, using change
data capture (CDC: reading each committed change from the database's own log),
for read-heavy workloads.

## Scope

ECDL is meant for tables that are read far more often than they are written, and
that are mostly read by key. It is not a write path, and it does not suit tables
that need strictly consistent reads (see "Limits" below).

Keeping a cache in sync with a database means capturing every change, applying
changes in order, recovering from failures, following schema changes, and loading
the existing data first. ECDL provides those parts as one deployed pipeline.

## What ECDL does

- **Initial load.** A parallel bulk loader copies the enrolled tables into Valkey.
- **Ongoing sync.** Each committed insert, update, or delete on the source is
  applied to Valkey. Measured time from commit to the key appearing in the cache
  is about 12 ms for single-row transactions and 195–250 ms at p90 (90% of changes arrive within that time) for the benchmark
  workload. It grows with transaction size (about 1–1.6 s for 2,000-row
  transactions). Method and figures: [benchmark-results.md](benchmark-results.md).
- **Transforms.** Row filters, column selection, renames, computed fields, and
  denormalization (embedding parent fields into child rows).
- **Schema changes.** Detected from Debezium's schema history; new columns appear
  in the cached rows.
- **Recovery.** Undelivered changes stay in the broker and are retried. Changes
  that still fail are kept in a dead-letter stream (a separate list of changes that could not be
  applied) so they can be replayed. See
  [operations.md](operations.md).

The application reads enrolled tables from Valkey. Writes still go to the
database, which stays the system of record.

## How It Works

**Write path + CDC:**

```
Application          MySQL / Aurora / RDS        ECDL                 ElastiCache / Valkey
writes normally  ──▶  source of truth       ──▶  CDC + transforms ──▶  updated from CDC
```

**Read path (enrolled tables only):**

```
Application                    ElastiCache / Valkey
HGETALL table:pk         ──▶   one key read (pk = primary key)
```

**Components:** Debezium, an open-source change-capture tool, reads the
source's change log. Valkey Streams (an ordered log data type in Valkey) on a
separate broker cache hold the events until they are applied. A Go sink worker applies
transforms and writes to the target. The control plane or the `ecdl` CLI deploys
a pipeline from one link file.

## Example Workloads

Illustrative examples of tables that fit the scope above. They are not
measurements.

| Workload | Tables read by key | What ECDL keeps in the cache | What stays on the database |
|---|---|---|---|
| Payments | BIN (bank identification number) routing, merchant codes, fee schedules, limits, sanctions lists | One hash per reference row | The ledger write and time-windowed queries |
| Healthcare | CPT/ICD-10 procedure and diagnosis codes, drug formulary, provider NPI (National Provider Identifier), eligibility | Reference rows, loaded once and then updated from CDC; batch refreshes on the source arrive as ordinary changes | Claims adjudication and updates to member records |
| E-commerce | SKU (stock-keeping unit) attributes, price, inventory status, category | One pre-joined object per SKU (denormalization), so a product read is one key instead of a multi-table JOIN | Orders, checkout writes |
| AI agents | Customer, account state, policy, entitlements, configuration | Context rows updated from CDC rather than expired by a TTL (time-to-live) | Any write an agent makes |
| Database migration to Aurora | Read-heavy lookup tables of the migrated application | The same tables, fed from Aurora | Writes and complex queries, so Aurora does not have to be sized for peak read load |

## Measured Results

Four source engines on matched hardware (8 vCPU / 64 GiB source, ElastiCache for
Valkey `cache.m7g.large` target). Full setup: [benchmark results](benchmark-results.md).

| Source | Initial load (3-shard target) | Live CDC | Freshness p90 |
|---|---|---|---|
| Aurora MySQL | 225K rows/s | 23K events/s | 250 ms |
| Aurora PostgreSQL | 254K rows/s | 51K events/s | 210 ms |
| SQL Server | 229K rows/s | 21K events/s | 195 ms |
| Oracle (RDS) | 259K rows/s | 43K events/s | 220 ms |

Initial load scales with target shards and loader parallelism (up to 610K rows/s
measured). Live CDC is bounded by the source's single ordered log: ~37K events/s
per source on Fargate, ~66K/s on fast dedicated cores.

## Limits

- Reads from the cache are eventually consistent: they lag behind the source by the
  freshness figures above. Reads that must see the application's own write
  should go to the database.
- Tables that change very often relative to how often they are read gain little.
- Ad-hoc analytical queries and joins are not served from the cache; only the
  key-based shapes the link defines are.
