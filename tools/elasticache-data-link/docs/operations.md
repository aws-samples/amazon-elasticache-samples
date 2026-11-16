# Operations: alarms, troubleshooting, recovery

Running a link after it is deployed: changing it, reading its alarms and
dashboard, recovering from failures, rotating credentials, and taking it down.

Names below use `<link>` for the link name; its stacks are `ecdl-<link>-broker`,
`ecdl-<link>-cdc` and `ecdl-<link>-loader`, and its ECS cluster is `ecdl-<link>`.

## Terms

| Term | Meaning |
| --- | --- |
| Stream | The Valkey stream on the broker that holds captured changes until the sink applies them. |
| Consumer group | The sink's shared read position on a stream. Each change is handed to one sink task, and stays *pending* in the group until that task confirms it was written, so a task that stops does not lose it. |
| Dead letter | A change the sink stopped retrying after `MAX_DELIVERIES` (default 5) attempts, or could not parse at all. It is moved to a `dlq:<stream>` stream and is **not** in the cache until replayed. |
| Poison | A dead letter whose entry cannot be parsed. Replaying it fails the same way; fix it at the source. |
| Tombstone | A short-lived marker (`TOMBSTONE_TTL_SEC`, default 1h) left in the cache when a row is deleted, so an older update that arrives late cannot bring the row back. |

---

## Alarms

Each CDC link creates these CloudWatch alarms on its SNS topic (the CDC stack's
`AlarmTopicArn` output; `advanced.alarms.email` in the link subscribes an
address). They fire on conditions that mean changes are **not reaching the
cache**, and each one's description says what to check first.

| Alarm | Fires when | Usually means | First step |
|---|---|---|---|
| `…-capture-not-running` | Debezium has had no running task for 30 min (`advanced.alarms.capture_stopped_minutes`) | Capture is paused (expected only during a bulk load) or restarting repeatedly | [Capture is paused or stopped](#capture-is-paused-or-stopped) |
| `…-sink-not-running` | no sink task for 5 min | Sink image missing, the task failing repeatedly at startup, or scaled to 0 | ECS service events; sink log group |
| `…-sink-lag` | the oldest unapplied change in the broker is older than 300s (`sink_lag_seconds`) | Target unreachable or slow, or the sink at its max task count | [Sink is behind](#sink-is-behind) |
| `…-dead-letters` | any entry in a `dlq:<stream>` | Changes that could not be applied are NOT in the cache | [Dead letters](#dead-letters) |
| `…-broker-memory` | broker memory above 80% | The sink is behind; at 100% the broker refuses Debezium's writes and capture stops | Treat as sink lag; use a larger broker node type if it recurs |
| `…-debezium-errors` | ERROR lines in 2 of the last 3 five-minute periods | Lost source connection, missing grants, purged log position | Debezium log group |
| `…-sink-errors` | ERROR lines in each of the last 3 five-minute periods | Persistent broker or target errors | Sink log group: lines read `ERROR <kind>: …` |

The target cache itself is yours and is not alarmed by ECDL; alarm on its own
`DatabaseMemoryUsagePercentage` and `EngineCPUUtilization`.

---

## Where to look

| What | Where |
|---|---|
| Debezium log | CloudWatch Logs `/ecs/ecdl-<link>-cdc/debezium` (healthy: `streaming`) |
| Sink log | `/ecs/ecdl-<link>-cdc/sink` (every failure is `ERROR <kind>: …`, at most one line per kind per 10s with a count; a failed write that succeeded on an immediate re-send is `WARN target-write-retry: …`) |
| Loader log | `/ecs/ecdl-<link>-loader` (prints table, engine, target, and each retry) |
| Broker log | `/ecs/ecdl-<link>-broker/broker` (engine and slow log) |
| Sink metrics | CloudWatch `ECDL/CDCSink`, dimensions `ClusterName` and `ServiceName`. Levels: `BacklogPerTask`, `SinkBacklog`, `SinkRunningTasks`, `DLQDepth`, `OldestEntryAgeSeconds`. Counts per publish interval (use Sum): `Writes`, `Deletes`, `StaleSkipped`, `WriteResends` (pipelines re-sent at once after a failure), `WritesRecovered` (changes a re-send confirmed), `WritesUnconfirmed` (changes left for the slower retry), `ReclaimRetried`, `DeadLettered` |
| Live view | control plane → Pipeline Monitor |
| A deploy that failed | control plane → Deployments (records are kept across a restart), or the CLI output; `aws cloudformation describe-stack-events` |

The sink's `/health` (used by the ECS health check) returns 503 after 120s
(`HEALTH_STALL_SEC`) with no successful broker read, or with target writes
failing and none succeeding. ECS then replaces the task. Unapplied changes stay
pending in the consumer group, so a replacement loses nothing.

**Pipeline Monitor.** The CDC lag figure always names its source: `in-flight`
(read from an entry still waiting in the broker, so only while the sink is
behind) or `probe` (the **Measure now** button times one heartbeat row end to
end). The delivery-integrity verdict, worst table first:

| Verdict | Meaning | Action |
|---|---|---|
| `loss` | dead-lettered or evicted | [Dead letters](#dead-letters) |
| `stalled` | backlog not draining, no consumers | check the sink service |
| `stuck` | delivered but unacknowledged past the retry window | usually clears in 45–60s; otherwise read the sink log |
| `pressure` | broker memory high | treat as sink lag; use a larger broker node type |
| `retrying` | recovering on its own | watch the attempt count |
| `ok` | nothing lost | — |

CloudWatch figures on the page are 60-second averages published a minute or two
late, and each shows its age, so a high CPU next to a low live rate is usually a
stale average.

---

## Capture is paused or stopped

Debezium is the only reader of the source log. While it is not running, changes
accumulate in the binlog (MySQL) or WAL (PostgreSQL write-ahead log), and they are only safe while the source still
**retains** them. Check the retention first: the deploy preflight warns when it
is under 24h (MySQL `binlog retention hours`, PostgreSQL slots, SQL Server
change-table cleanup).

1. Is a load running? A first deploy (or `--reload`) pauses capture on purpose
   for the bulk load:
   `aws ecs list-tasks --cluster ecdl-<link> --family ecdl-<link>-loader`.
   If one is running, let it finish; the deployer resumes capture afterwards,
   including when the load fails.
2. Otherwise resume it:
   ```bash
   aws ecs update-service --cluster ecdl-<link> \
     --service ecdl-<link>-cdc-debezium --desired-count 1
   ```
   It resumes from the offset it last committed, so nothing is skipped.
3. If a control-plane restart interrupted a deploy, Deployments shows it as
   **interrupted**, with whether capture was left paused and these commands.
4. If Debezium starts and stops again, read its log. A message about a binlog
   file or LSN (PostgreSQL log sequence number) that is not available means the source purged the log position
   it needed: those changes are gone, and the link needs a reload
   (`ecdl apply --reload`).

---

## Sink is behind

The sink lag alarm is the end-to-end "the cache is behind the source" signal.

- **Target unreachable or failing:** sink log `ERROR target-write`. Changes stay
  pending and are retried every 15s. They are **not** dead-lettered while the
  target does not answer a PING; dead-lettering only applies to a change that
  keeps failing against a target that is up.
- **At the scaling ceiling:** `SinkRunningTasks` equals `advanced.sink.max_count`
  and `BacklogPerTask` stays high. Raise `max_count`, or see the benchmarks for
  the single-reader ceiling of each engine.
- **One table stuck:** Pipeline Monitor's per-table breakdown shows which stream.

---

## Dead letters

A change the sink could not apply after `MAX_DELIVERIES` (5) attempts against a
reachable target, or that could not be parsed, is moved to `dlq:<stream>` on the
broker with the reason. **It is not in the cache.** Fix the cause first, then
replay. The sink binary has both commands; run it as a one-off task on the
sink's own task definition, so it has the broker address and credentials:

```bash
CLUSTER=ecdl-<link>; SERVICE=ecdl-<link>-cdc-sink
NET=$(aws ecs describe-services --cluster $CLUSTER --services $SERVICE \
      --query 'services[0].networkConfiguration' --output json)
TD=$(aws ecs describe-services --cluster $CLUSTER --services $SERVICE \
      --query 'services[0].taskDefinition' --output text)

# 1. look
aws ecs run-task --cluster $CLUSTER --task-definition $TD --launch-type FARGATE \
  --network-configuration "$NET" \
  --overrides '{"containerOverrides":[{"name":"sink","command":["dlq","list"]}]}'

# 2. replay (add "--dry-run" first if you want)
aws ecs run-task --cluster $CLUSTER --task-definition $TD --launch-type FARGATE \
  --network-configuration "$NET" \
  --overrides '{"containerOverrides":[{"name":"sink","command":["dlq","replay"]}]}'
```

The output is in the sink log group. `replay` moves each dead letter back onto
its stream and removes it from the dead-letter stream (DLQ) in one transaction; the running sink then
applies it. A replayed change keeps its **original version**, so if the row has
changed since, the replay is skipped rather than overwriting the newer value.
Entries with reason `poison` cannot be parsed and will return to the DLQ; inspect
them with `list` and fix them at the source.

Replay within an hour of the failure where you can: a delete leaves a tombstone
for `TOMBSTONE_TTL_SEC` (1h) that stops an older insert or update from recreating the row,
and a replay after the tombstone has expired could bring back a deleted row.

---

## Delivery guarantees and the `_ecdl_v` field

Every change is applied, skipped because something newer is already in the
cache, or dead-lettered. Nothing is dropped silently:

- The sink acknowledges a change only after the target confirmed the write. An
  unconfirmed change stays pending in the consumer group and is retried.
- A dead letter is written before the original is acknowledged, so a change is
  always in at least one of the two places.
- Each write is an atomic check that applies only if the change is newer than the
  version stored in the hash field `_ecdl_v` (the change's broker stream id). A
  retried or reordered older change therefore cannot overwrite a newer one. A
  delete leaves a 1-hour tombstone so an older retry cannot bring the row back.
  Applications reading the hash should ignore `_ecdl_v`. `StaleSkipped` counts
  writes the check refused.

Limits: the version check covers ordinary tables, not denormalization fan-out;
and changes are only safe while the source still retains its log (see
[Capture is paused or stopped](#capture-is-paused-or-stopped)).

---

## Changing a running link

A change can leave cached rows wrong or missing without any error, so always run
`ecdl diff` first. It classifies each change by what it leaves behind:

| Severity | Meaning | Procedure |
|---|---|---|
| `deploy` | a setting the tasks read; no data implication | `ecdl apply` |
| `backfill` | cached rows are now wrong, or a new table's existing rows are absent | `ecdl apply`, then `ecdl signal snapshot --tables <t>` |
| `cleanup` | keys nothing maintains any more | `ecdl apply`, then delete `<table>:*` yourself |
| `recreate` | cannot be applied in place (new target, renamed link) | create a new link, then delete the old one |

```bash
ecdl diff deployed.yaml desired.yaml          # changes nothing
ecdl apply desired.yaml                       # capture keeps running; no bulk reload
ecdl signal snapshot --tables orders desired.yaml
```

- **Add a table:** capture starts from the update, so its existing rows need the
  snapshot.
- **Change a transform or add a filter:** cached rows hold values from the old
  rules until the snapshot rewrites them. Apply first, then snapshot; a snapshot
  before the new rules are live rewrites every row under the old ones.
- **Remove a table:** its keys stay in the cache; delete them if nothing reads
  them.

`signal snapshot` prints an `INSERT` for the link's signalling table; run it on
the source (the control plane holds no database credential). Debezium then
re-reads the table in chunks while streaming continues.

**Expected on every CDC stack update (MySQL):** as the old Debezium task stops it
logs `ERROR ... Exception while closing JDBC connection ... Communications link
failure during rollback()`. Nothing is lost: the position lives in the broker,
and the new task logs `Requesting streaming from position ...` and `Skip N events
on streaming start`. It is a problem only if the new task never reaches
`Starting streaming`.

---

## Rotating credentials

- **Source database password:** update its secret, then force a new deployment
  of the Debezium service (and run the loader again only if a load is due).
- **Broker auth token:** the token lives in two places, the broker and its secret
  (the broker stack's `AuthSecretArn` output), so changing only the secret locks
  every task out. ElastiCache can accept two tokens at once, which makes this a
  rotation with no outage:

  ```bash
  BROKER=ecdl-<link>-broker
  SECRET=<AuthSecretArn>
  NEW=$(aws secretsmanager get-random-password --exclude-punctuation \
          --password-length 64 --query RandomPassword --output text)
  # 1. The broker accepts the new token as well as the old one.
  aws elasticache modify-replication-group --replication-group-id "$BROKER" \
    --auth-token "$NEW" --auth-token-update-strategy ROTATE --apply-immediately
  aws elasticache wait replication-group-available --replication-group-id "$BROKER"
  # 2. The secret holds the new token; restart the tasks so they read it.
  aws secretsmanager put-secret-value --secret-id "$SECRET" --secret-string "$NEW"
  aws ecs update-service --cluster ecdl-<link> --service ecdl-<link>-cdc-debezium --force-new-deployment
  aws ecs update-service --cluster ecdl-<link> --service ecdl-<link>-cdc-sink --force-new-deployment
  aws ecs wait services-stable --cluster ecdl-<link> \
    --services ecdl-<link>-cdc-debezium ecdl-<link>-cdc-sink
  # 3. Only after the Pipeline Monitor shows both healthy: drop the old token.
  aws elasticache modify-replication-group --replication-group-id "$BROKER" \
    --auth-token "$NEW" --auth-token-update-strategy SET --apply-immediately
  unset NEW
  ```

  Automatic rotation (a Secrets Manager rotation function doing the same three
  steps) is not built yet; it is planned with the phase-1 work.

---

## Scaling

The sink autoscales on backlog per task (and CPU) between `advanced.sink.min_count`
and `max_count`; one task drains roughly 18,500 events/s. Above about 40,000
events/s the limit is Debezium, which reads one log with one reader, so more sink
tasks do not help. Debezium is always 1 task (0 while paused), never more.

---

## A failed or interrupted bulk load

The loader retries a failed chunk up to 5 times (`LOAD_RETRIES`) with backoff,
from the last key the target confirmed. If it still fails, the deploy fails,
capture is resumed, and the error says the cache may be missing rows. Run the
load again with `ecdl apply --reload` (or **Update and reload all tables**): it
resumes the incomplete run, skipping chunks that finished. `LOAD_RESUME=false`
on the loader task starts from the first row.

**"will not fit in the target cache".** Before writing anything, the loader
estimates what the table needs (catalogue row count x the size of 200 sampled
rows as it would write them, plus per-key overhead, excluding rows the filter
drops and keys already cached) and compares it with the free memory the target's
primaries report. If it needs more than 90% of that, it stops with both numbers
and nothing written. Use a larger node type or more shards. The estimate runs
about 20-40% above Valkey's own `MEMORY USAGE`, on purpose;
`LOAD_SKIP_MEMORY_CHECK=true` on the loader task loads anyway. Plan also warns
when the target is already 80% full.

## Two links, one cache

Keys are `<table>:<primary key>`, so two links that copy a table of the same
name into one cache would overwrite each other. Plan and deploy refuse that,
naming the other link. Use another target, or set `advanced.key_prefix` on one
link: its keys become `<prefix>:<table>:<primary key>`.

---

## Tearing a link down

**Deployments → Delete pipeline** in the UI (type the name to confirm), or
`ecdl destroy --link <link>`, deletes the link's three stacks and its ECS
cluster if empty, and keeps the source and target. Both show what is left to you:

- **PostgreSQL:** the replication slot. Drop it
  (`SELECT pg_drop_replication_slot('ecdl_<link>');`, or `ecdl_slot` for a link
  created before slots were per link), or the source keeps WAL until its disk
  fills.
- The signalling table on the source.
- The link's keys in the target cache, which nothing maintains any more.
