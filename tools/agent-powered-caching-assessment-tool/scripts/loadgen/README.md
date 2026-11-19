# Load Generator

Generates a mixed read/write workload against a PostgreSQL database so the
caching assessment has realistic `pg_stat_statements` data to score.

Uses `pgbench` with custom weighted scripts. pgbench handles concurrency,
duration, and throughput reporting, so the scripts only need to describe the
work.

## Workload shape

70% reads, 30% writes by pgbench script weight.

| Script | Weight | Class | What it exercises |
|---|---|---|---|
| `r_simple.sql` | 35 | Simple read | Indexed point lookups on primary keys |
| `r_medium.sql` | 25 | Medium read | Two to three table joins, filtered ranges, ORDER BY + LIMIT |
| `r_complex.sql` | 10 | Complex read | Multi-table aggregation, GROUP BY, HAVING, window function, view |
| `w_simple.sql` | 18 | Simple write | Single-row INSERT and indexed UPDATE |
| `w_medium.sql` | 12 | Medium write | Multi-statement transaction, INSERT plus dependent UPDATE |

Read weight: 35 + 25 + 10 = 70. Write weight: 18 + 12 = 30.

## Why this mix matters for the assessment

The point is not raw throughput. It is to produce a workload where different
tables have genuinely different caching profiles, so the scoring has something
to discriminate:

- `product` and `category` are small, read-heavily, and never written. They
  should look like strong candidates on access shape, but may still be excluded
  as already well served if PostgreSQL keeps them in memory.
- `order_item` and `order` are large and receive both heavy reads and writes.
  Their read/write ratio decides whether invalidation churn disqualifies them.
- `inventory` is small but written on almost every write transaction. It should
  be write-disqualified despite being tiny and hot.

A uniform workload would score every table the same and prove nothing.

## Running

```bash
python scripts/loadgen/run_load.py --container retail-order-store-pg \
    --user retail_admin --database retail_order_store \
    --clients 16 --jobs 4 --duration 120
```

The runner resets `pg_stat_statements` before starting so the measurement window
covers only the generated load.

## Caveats

- **This writes data.** Rows are inserted into `order`, `order_item`, and
  `payment`, and `inventory` is updated. Run it against a disposable database.
- Queries are written against the `retail` schema of the bundled sample
  database. They will not run elsewhere without edits.
- pgbench reports client-side latency. The assessment reads server-side
  statistics from `pg_stat_statements`, so the two will not match exactly.
