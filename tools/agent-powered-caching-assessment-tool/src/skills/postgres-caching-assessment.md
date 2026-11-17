# Skill: PostgreSQL Caching Assessment

Guidance for reasoning about whether a self-managed PostgreSQL workload should have a cache added in front of it.

Adapted from the upstream `elasticache-data-modeling` skill, which addressed a different question: how to model Redis as a *migration target* replacing the source database. Here PostgreSQL remains the system of record.

---

## The question being answered

**Not** "can this be cached?" — almost anything can be.

**But** "should it be, and what is the evidence?"

Those differ, and the difference is the whole value of the assessment. A table can be perfectly shaped for caching — read-only, frequent, small, predictably keyed — and still be a bad investment because PostgreSQL already serves it from memory in under a millisecond. Adding a cache there buys nothing and costs operational complexity plus an invalidation surface.

---

## Hard rules

1. **Never recommend caching without pressure evidence.** Access shape alone is insufficient. Cite a buffer hit ratio, disk-read share, temp spill, or host saturation figure. If none is available, say so rather than implying the recommendation is evidence-backed.

2. **Never present a score as a measurement.** Every threshold is an uncalibrated default. Say "scored 82 against the default thresholds," not "is an 82% match."

3. **Never present sizing as a capacity plan.** Sizing is not built yet. True working-set size cannot be derived from statistics — it needs a pilot. If a size is ever offered, state the assumption behind it.

4. **Never present cost as a quote.** There is no cost output today. Any future one is an estimate, not a price.

5. **Never claim a degraded assessment is conclusive.** Without `pg_stat_statements`, query-level signals are absent. Confidence is capped at 60 and the workload verdict is `INSUFFICIENT_DATA`. Report the restart prerequisite plainly.

6. **Distinguish I/O-bound from lock-bound slowness.** A cache fixes the former, not the latter. If wait events are dominated by `Lock`, say caching will not help and explain why.

7. **Do not penalize query complexity.** A complex join's result caches as well as a simple lookup's. Joins raise *invalidation* complexity, not candidacy — that belongs in key design.

---

## How to read the four dimensions

| Dimension | Reading it |
|---|---|
| **Access Suitability** | High = right shape. Necessary, not sufficient. High here with low Pressure means "could, but no need." |
| **Pressure Evidence** | High = PostgreSQL is struggling. The *why now*. Low here is usually a decline regardless of other scores. |
| **Cache Efficiency** | High = caches cheaply. Modulates cost-effectiveness, not need. |
| **Disqualifiers** | Non-zero = reasons against. Hard exclusion overrides everything. |

The instructive pattern is `100/0/90` — perfect shape, zero pressure, cache-friendly shape. That is a **decline**, and explaining why is more useful to the customer than the score.

---

## Interpreting specific signals

**Buffer hit ratio** — healthy OLTP PostgreSQL sustains ~99%. Below 95% means real disk traffic. Always caveat with `shared_buffers`: 90% against 128 MB means something very different from 90% against 32 GB.

**Disk-read share** — must compare *cumulative* `blk_read_time` to *cumulative* `total_exec_time`. Comparing cumulative I/O to per-call mean execution time is a unit error that yields shares above 100%.

**Temp blocks written** — sorts or hashes spilling to disk. Indicates `work_mem` pressure. A cache relieves this indirectly by freeing buffer pool.

**Host CPU** — high with high query volume argues for capacity relief, not just latency. Comfortable headroom *weakens* the case: tuning may be the cheaper answer.

**Swap** — any sustained swapping on a database host is significant. Do not wave it away as small.

**Sequential scans** — only meaningful when scanning is expensive. Thousands of scans of a 45-row lookup table cost nothing.

**Dead tuple percentage** — conflates genuine volatility with autovacuum lag. Check `last_autovacuum` before calling a table volatile.

---

## What statistics cannot tell you

Raise these with the customer rather than guessing:

- **Staleness tolerance** — a business decision per data domain.
- **True working-set size** — needs a pilot or instrumentation.
- **Application cache-aside readiness** — do reads funnel through a path that can consult a cache?
- **Peak versus average load** — `pg_stat_statements` gives cumulative averages, so real peaks are understated.
- **Consistency requirements** — some data cannot tolerate any staleness at any TTL.

---

## Reporting style

Lead with the verdict and the single strongest piece of evidence. Then the caveat that most affects the decision. Then detail.

Good:

> Recommended for `product_catalog`. 225 calls/sec at 100% reads, with a 71% buffer hit ratio and 43% of execution time spent on disk reads — PostgreSQL is not holding this in memory. Whether it pays off depends on the hit rate, which should be validated in a pilot before provisioning.

Poor:

> `product_catalog` scored 94/100 and is an excellent caching candidate. Recommended node: cache.r7g.medium at $165/month.

The second omits the evidence, presents a default-threshold score as a measurement, and presents an unsupported figure as a price.
