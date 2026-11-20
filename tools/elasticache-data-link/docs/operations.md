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
| Dead letter | A change the target refused for itself on its first try and on all `MAX_DELIVERIES` (default 5) retries, 6 tries in all, or one the sink could not parse at all. It is moved to a `dlq:<stream>` stream and is **not** in the cache until replayed. |
| Poison | A dead letter whose entry cannot be parsed. Replaying it fails the same way; fix it at the source. |
| Tombstone | A short-lived marker (`TOMBSTONE_TTL_SEC`, default 1h) left in the cache when a row is deleted, so an older update that arrives late cannot bring the row back. |

---

## Alarms

Each CDC link creates these CloudWatch alarms on its SNS topic (the CDC stack's
`AlarmTopicArn` output; `advanced.alarms.email` in the link subscribes an
address). They fire on conditions that mean changes are **not reaching the
cache**, and each one's description says what to check first and links to a
section of this page.

The topic has no subscriber unless the link sets an email or you subscribe
something to `AlarmTopicArn`. The guided Build Pipeline form asks for the
email on its Review step (optional). The plan warns when no email is set, and the
Pipeline Monitor shows a banner while the topic has no confirmed
subscription. An email subscription counts only once it is confirmed from
the email.

| Alarm | Fires when | Usually means | First step |
|---|---|---|---|
| `…-capture-not-running` | Debezium has had no running task for 30 min (`advanced.alarms.capture_stopped_minutes`) while capture is not paused (`CapturePaused` 0 or not published) | Debezium is failing to start or restarting repeatedly. A pause for a bulk load does not count; `…-capture-paused` covers a pause that lasts too long | [Capture is paused or stopped](#capture-is-paused-or-stopped) |
| `…-capture-paused` | capture has been paused (Debezium's desired count 0, `CapturePaused` 1) for every hour of the last 7 (`CapturePausedAlarmHours`; the plan sets each table's load timeout times the tables, plus one hour, at most 24) | Longer than a load can hold it: nothing is going to resume it, for example the control plane stopped during a load, or someone scaled Debezium to 0. The source log keeps purging what Debezium has not read | [Capture is paused or stopped](#capture-is-paused-or-stopped): if no load is running, resume it |
| `…-capture-stale` | Debezium's newest heartbeat is older than 300s (`advanced.alarms.capture_stale_seconds`) for 3 minutes while capture is not paused, or the sink cannot read it | Debezium is running but not capturing: stuck, throttled, retrying or restarting. A pause for a bulk load does not count (`CapturePaused`), and after a resume the age counts from the resume | Debezium log group |
| `…-table-stale` | a table listed in `advanced.alarms.table_stale_seconds` (for example `{"orders": 3600}`) has had no change captured for longer than its threshold. Only exists when tables are listed | The table was renamed, is missing from Debezium's include list, or stopped changing at the source. The sink log line `table-stale` names it | Debezium log group; check the table name in the link |
| `…-sink-not-running` | no sink task for 5 min | Sink image missing, the task failing repeatedly at startup, or scaled to 0 | ECS service events; sink log group |
| `…-sink-metrics-missing` | the sink published no metrics (`TargetUp` samples) for 5 minutes | With `…-sink-not-running`: no task. Alone: the tasks run but cannot publish (AWS credentials, `PutMetricData` denied or throttled), so `…-sink-lag`, `…-dead-letters` and autoscaling cannot see anything | Sink log lines `ERROR metric-config` or `ERROR metric-put` |
| `…-sink-lag` | the oldest unapplied change in the broker is older than 300s (`sink_lag_seconds`), or the sink publishes no lag for 3 minutes | Target unreachable or slow, or the sink at its max task count | [Sink is behind](#sink-is-behind) |
| `…-target-down` | the sink could not write to the target for 3 minutes | Target unreachable, failing over, out of memory, or its user lost permissions. Changes are held on the broker, not lost | [Sink is behind](#sink-is-behind) |
| `…-dead-letters` | a change was dead-lettered in the last minute (`DeadLettered`). It notifies again for each later dead letter, and returns to OK a minute later without a notification: OK does not mean the dead letters were replayed | Changes that could not be applied are NOT in the cache. The sink log line `ERROR dead-letter: <stream> <id> reason=<r> deliveries=<n>: <error>` names each one | [Dead letters](#dead-letters) |
| `…-dlq-dropped` | dead letters were dropped at `DLQ_MAX_LEN` (`DLQDropped` > 0). Notifies for each minute with drops; no OK notification | The DLQ was full and its oldest dead letters were dropped to make room: those changes are not in the cache and no longer in the DLQ to replay. The sink log line `ERROR dlq-full` names the DLQ | Fix the cause of the dead letters, replay or purge the DLQ, then `ecdl signal snapshot --tables <t>` or reload the affected tables |
| `…-pending-vanished` | a change waiting to be applied was deleted from its broker stream | Changes were lost: not in the cache and not in the DLQ. The sink log line `pending-vanished` lists the ids | Reload the link, or `ecdl signal snapshot --tables <t>` for the affected tables |
| `…-target-evicted` | the target evicted keys (`TargetEvictedKeys` > 0) | The target was full and its `maxmemory-policy` deleted keys instead of refusing writes: cached rows under `allkeys-*`, delete markers under `volatile-*`. The sink log line `target-evicted` names the node | Set `maxmemory-policy noeviction` in the target's parameter group (or use a larger node), then reload the link |
| `…-broker-memory` | broker memory above 80% on any of `<group>-001`, `-002` or `-003`, or none of them reports for 3 minutes | The sink is behind; at 100% the broker refuses Debezium's writes and capture waits. No data means the broker was deleted or renamed. A broker with more than three members needs its own alarm | Treat as sink lag; use a larger broker node type if it recurs |
| `…-broker-replication-lag` | a broker replica (`<group>-001` to `-003`) is more than 5s (`BrokerReplicationLagAlarmSeconds`) behind its primary for 3 minutes | Capture slows (Debezium waits for the replica), and a failover now would lose what the replica lacks. Broker node too small, or a large backlog being written | Broker metrics; use a larger broker node type if it recurs |
| `…-loader-load-failed` (EventBridge rule on the loader stack, topic `LoadAlarmTopicArn`) | a load task stopped with a non-zero exit code, or never started | The load failed; the cache may be missing rows. Also fires when a deploy stops a load it gave up on | [A failed or interrupted bulk load](#a-failed-or-interrupted-bulk-load) |
| `…-broker-failover` | the broker's primary changed (`IsMaster` on any of `<group>-001` to `-003`) | An automatic or manual failover. Debezium restarts from the replicated offset; nothing is skipped | `aws elasticache describe-events --source-type replication-group --source-identifier ecdl-<link>-broker`; repeated failovers mean a failing node |
| `…-broker-writes-held` | Debezium's writes to the broker were held 3 or more times in 5 minutes: no replica acknowledged them within the WAIT timeout (log line `no replica acknowledged`), or the broker refused or could not take them (`broker memory is full`, `not written yet`) | The replica is missing, resyncing or lagging, or the broker is full, loading or unreachable. The offset does not move; after 30 WAIT timeouts in a row Debezium restarts. Nothing is lost | Debezium log lines `ECDL broker writer`; then `…-broker-replication-lag` or `…-broker-memory`. A broker with no replica needs `BrokerWait=false` |
| `…-debezium-errors` | ERROR or FATAL lines in 2 of the last 3 five-minute periods | Lost source connection, missing grants, purged log position, offset commits failing (`ECDL offset store: offset commit … failed`), or Debezium refusing to start. A purged binlog or WAL position means changes are lost: reload with a position reset | Debezium log group; [Capture is paused or stopped](#capture-is-paused-or-stopped), step 4 |
| `…-sink-errors` | ERROR or FATAL lines in 2 of the last 3 five-minute periods | The line's kind says what failed: broker reads, target writes, acks, metric publishing, dead letters, or a sink that refuses to start. Unapplied changes stay pending and are retried | Sink log group: lines read `ERROR <kind>: …` or `FATAL: …` |

The broker alarms that read ElastiCache metrics (`…-broker-memory`,
`…-broker-replication-lag`, `…-broker-failover`) need the broker's replication
group id, which the control plane always passes. They watch members `-001` to
`-003`; a broker of 4 to 6 nodes (`advanced.broker.replicas` 3 to 5) leaves the
others unwatched. The CDC stack also has an
EventBridge rule, `ecdl-<link>-cdc-broker-events`, that sends a message to the
same topic when someone reboots a broker node, runs a test failover, or removes
a node or the broker. It matches CloudTrail's record of the call, so it works
only in an account with a trail that logs management events. ElastiCache sends
neither its failover nor its node-replacement events to EventBridge, and cannot
publish its own notifications to an encrypted topic; `…-broker-failover`
covers failovers instead.

The target cache itself is yours and is not alarmed by ECDL; alarm on its own
`DatabaseMemoryUsagePercentage` and `EngineCPUUtilization`.

---

## Where to look

| What | Where |
|---|---|
| Debezium log | CloudWatch Logs `/ecs/ecdl-<link>-cdc/debezium` (healthy: `streaming`) |
| Sink log | `/ecs/ecdl-<link>-cdc/sink` (every failure is `ERROR <kind>: …`, at most one line per kind, stream and error per 10s with a count, and a closing `… more like …` line when a failure stops; a failed write that succeeded on an immediate re-send is `WARN target-write-retry: …`; changes the version guard skipped are `WARN stale-skipped: …`; each dead letter is `ERROR dead-letter: …`) |
| Loader log | `/ecs/ecdl-<link>-loader` (prints table, engine, target, and each retry) |
| Broker log | `/ecs/ecdl-<link>-broker/broker` (engine and slow log) |
| Sink metrics | CloudWatch `ECDL/CDCSink`, dimensions `ClusterName` and `ServiceName`. Levels: `TargetUp` (1 or 0), `BacklogPerTask`, `SinkBacklog`, `SinkRunningTasks`, `DLQDepth`, `OldestEntryAgeSeconds`. A level the sink could not read from the broker is left out for that interval, never sent as 0. Counts per publish interval (use Sum): `Writes`, `Deletes`, `StaleSkipped`, `WriteResends` (pipelines re-sent at once after a failure), `WritesRecovered` (changes a re-send confirmed), `WritesUnconfirmed` (changes left for the slower retry), `ReclaimRetried`, `DeadLettered`, `PendingVanished` (must stay 0), `TargetEvictedKeys` (keys the target evicted; must stay 0), `WaitShortfall` (changes applied on a target primary but left pending because its replica did not confirm them in time), `WritesRefused` (of `WritesUnconfirmed`, changes the target refused for themselves, such as `WRONGTYPE`), `DLQDropped` (dead letters dropped at `DLQ_MAX_LEN`; must stay 0), `Errors` with a `Kind` dimension (ERROR lines per kind; sent only for kinds that had one). Capture: `CaptureAgeSeconds` (age of Debezium's newest heartbeat), `CapturePaused` (1 while Debezium's desired count is 0), `CaptureStaleSeconds` (the `capture-stale` alarm input: 0 while paused). Freshness: `LastChangeAgeSeconds` with a `Table` dimension (time since the table's last captured change; about $0.30 a month per table), `StaleTables` (listed tables past their threshold), `CacheFreshnessSeconds` (how far the cache is behind: the oldest change still waiting, or Debezium's newest heartbeat when none is waiting; Debezium's own delay reading the source log is not included), `CatchingUpTables` (tables whose waiting changes were captured more than `CATCHING_UP_SECONDS`, default 60, after their commit: a replay, see [Replays](#replays-from-an-older-position)) |
| Live view | control plane → Pipeline Monitor |
| A deploy that failed | control plane → Deployments (records are kept across a restart), or the CLI output; `aws cloudformation describe-stack-events` |
| Who deployed, deleted or repaired a link | the control plane's log: one `[audit] {...}` JSON line per request (`accepted` or `refused`, with the reason) and per finished run (`succeeded` or `failed`), with the action, link, time and where the request came from (address, user agent, `X-Request-Id`). The API has one shared token, so a user name comes only from your front end's own log, matched by request id. |

The sink's `/health` (used by the ECS health check) returns 503 after 120s
(`HEALTH_STALL_SEC`) with no successful broker read. ECS then replaces the task.
Unapplied changes stay pending in the consumer group, so a replacement loses
nothing. A target outage does not fail `/health`: the sink holds its changes
and stays running, and `…-target-down` reports it.

**Pipeline Monitor.** The CDC lag figure always names its source: `in-flight`
(read from an entry still waiting in the broker, so only while the sink is
behind) or `probe` (the **Measure now** button times one marked row end to
end). The delivery-integrity verdict, worst table first:

| Verdict | Meaning | Action |
|---|---|---|
| `loss` | evicted, or dead-lettered in the last hour (`DEMO_DLQ_RECENT_SEC`) or since the last poll | [Dead letters](#dead-letters) |
| `stalled` | undelivered entries have not gone down for 60s (`DEMO_STALL_WINDOW_SEC`) and nothing drains | check the sink service |
| `unknown` | a stream (or the broker) could not be read, so loss cannot be ruled out; the page names the unread streams and shows their backlog as unknown | check the broker and that each table's stream exists |
| `dead_letters` | dead letters are present, but none new within that hour | [Dead letters](#dead-letters): review and replay them |
| `stuck` | a change has been pending for more than 60 s | reclaim retries it about every 30–45 s (`RETRY_IDLE_MS` 30 s, checked every 15 s, at most `RECLAIM_BATCH` 200 per stream each time). It clears when a retry applies it. A change the target keeps refusing is dead-lettered after 2.5–4 minutes; one held for a target outage stays until the target answers. If it lasts, read the sink log |
| `pressure` | broker memory high | treat as sink lag; use a larger broker node type |
| `retrying` | recovering on its own | watch the attempt count |
| `ok` | every stream was read and nothing was lost | — |

**What writes.** Only two actions write, both POSTs that must name their link
(`?link=` or `{"link": ...}`); with no link selected, or only
`DEMO_DEFAULT_LINK`, they are refused with 400. They are **Measure now** and
the demo load phases (`/run`, `/run/cancel`), which queue a job on that link's
broker. No GET writes; `/runs` keeps finished-run summaries in memory. The
page offers the load phases only on the configured pipeline, so its Run and
Stop buttons are disabled there, and it does not read `/runs` for a link.

**What a probe writes.** On a deployed link, **Measure now** writes a marker
value into one column of an existing cached row, waits for it in the cache, and
then writes the old value back. It only uses a `varchar`, `char` or `text`
column of at least 32 characters that is cached under its own name, is not in
the primary key, and is not used by a filter, computed field, rename or
denormalize of the link. If no column qualifies, the probe is refused and the
message lists why each column was rejected. Both writes are real `UPDATE`s:
`ON UPDATE CURRENT_TIMESTAMP` columns and `UPDATE` triggers on the table fire
for each, and the probe does not undo them. The result names the timestamp
columns it changed (`also_changed`).

**Reconcile exactly** counts the source table's rows and the cache keys under
the table's full key prefix, including the link's `KeyPrefix`. Other keys are
grouped by their first segment; the 20 largest groups are listed and the rest
are only counted.
The monitor reads, counts and probes each table in the schema the link names
(`Tables: Sales.orders` is read in `Sales`, whatever `SourceDBName` is).
The log error count on the Debezium and sink cards uses the same filter as the
`DebeziumErrors` and `SinkErrors` alarm metrics: lines containing `ERROR` or
`FATAL`.
When the source clock is ahead of the broker's, a change appears to arrive
before it was committed. The capture lag then reads 0 and the Debezium card
shows a `clock skew` note with the size of the difference (`clock_skew_ms`).
For MySQL the integrity panel compares the age of Debezium's committed position
with `binlog retention hours` (`retention_used_pct`, `retention_headroom_sec`),
and the banner warns once half the retention is used: changes still unread at
the end of it are purged and cannot be recovered. PostgreSQL has no retention
period. A replication slot keeps WAL until Debezium reads it, so a stopped
reader fills the disk instead: watch the slot's lag in `pg_replication_slots`
and the free storage.
Changes in flight (`writeflow`, also part of `/dashboard`) come with their row
values only when the link's source secret is in `MonitorSourceSecretArns`.
Otherwise each change keeps its operation, timing, column names and whether it
reached the cache (`values_withheld: true`).
**Background probe.** Off by default, because each probe writes to a row of
the source table. To turn it on for one link, set both `DEMO_PROBE_AUTO_SEC`
(seconds between probes) and `DEMO_PROBE_AUTO_LINK` (the link name) on the
control plane. It probes only that link, never "the newest link". Before each
probe it reads the link's stack and its secrets again. If the link is deleted
or a secret cannot be read, the probe stops, logs one `auto-probe: stopped`
line, and stays stopped until the control plane restarts.

CloudWatch figures on the page are 60-second averages published a minute or two
late, and each shows its age, so a high CPU next to a low live rate is usually a
stale average.
The monitor calls AWS in `DEMO_AWS_REGION` if set, else `AWS_REGION` or
`AWS_DEFAULT_REGION` (the control plane sets this one), else the region in the
AWS config.
It reuses a secret it has read for 5 minutes (`DEMO_SECRET_TTL_SEC`), so a
rotation or a revocation takes effect without a restart, and reads it again at
once when the database or Valkey rejects it. `DEMO_DB_PASSWORD`,
`DEMO_BROKER_PASSWORD` and `DEMO_CACHE_PASSWORD` are used only for the endpoints
configured on the control plane itself, never for a watched link.
It reads a link's stacks at most every 30 seconds. When CloudFormation cannot
be read (for example, throttling), the page reports the error and keeps the
selected link; it never shows another pipeline instead. The same applies to
`DEMO_DEFAULT_LINK`: if that link cannot be read, the default view shows the
error rather than the control plane's own `DEMO_*` endpoints.

---

## Capture is paused or stopped

Debezium is the only reader of the source log. While it is not running, changes
accumulate in the binlog (MySQL) or WAL (PostgreSQL write-ahead log), and they are only safe while the source still
**retains** them. Check the retention first: the deploy preflight warns when it
is under 24h (MySQL `binlog retention hours`, PostgreSQL slots, SQL Server
change-table cleanup), and a deploy that loads is blocked when its estimated
pause is longer than the MySQL binlog retention.

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
   `…-capture-paused` fires when capture has stayed paused longer than a load
   could hold it; `…-capture-not-running` does not fire for a pause.
3. If a control-plane restart interrupted a deploy, Deployments shows it as
   **interrupted**, with whether capture was left paused and these commands. A
   restart while the pause itself was running says capture **may** be paused;
   check the service's desired count. A link whose stacks are healthy but whose
   Debezium desired count is 0 is listed as **paused**.
4. If Debezium starts and stops again, read its log. A message about a binlog
   file or LSN (PostgreSQL log sequence number) that is not available means the source purged the log position
   it needed: those changes are gone. A plain `--reload` does not fix this,
   because Debezium keeps the stale position and fails again after the load.
   Reload with a position reset, from the control plane (it must read the
   broker): `ecdl apply --reload --reset-position`, or `"reload": true,
   "reset_position": true` on `/deploy`. The deployer pauses capture, deletes
   the stored offset and schema history, starts Debezium until it records the
   source's current position, pauses it again, and then loads. Deleting the key
   by hand after the load instead loses every change made during the load.
   PostgreSQL also needs a new replication slot, which the deployer cannot
   create for you: set the Debezium desired count to 0, run
   `SELECT pg_drop_replication_slot('ecdl_<link>');` on the source, then add
   `--pg-slot-dropped` (`"pg_slot_dropped": true`). The deploy prints these
   steps when the flag is missing.
5. `ECDL offset store: offset commit to '…' failed (…)` means Debezium cannot
   save its position in the broker: a full broker (`OOM`), a failover still in
   progress (`READONLY`, it reconnects on its own), or a lost connection. After
   10 failed commits in a row (`OFFSET_COMMIT_MAX_FAILURES`) it logs
   `FATAL ECDL offset store` and exits with status 70, and ECS starts a new
   task. That loses nothing: the new task resumes from the last committed
   position and writes the changes after it again. If it keeps restarting,
   fix the broker (treat `OOM` as sink lag).
6. `ECDL offset store: refusing to start` means Debezium found no stored
   position for a link that has stored one before. See
   [Debezium refuses to start: no stored offset](#debezium-refuses-to-start-no-stored-offset).
7. `FATAL ECDL schema history: a DDL record could not be stored durably`
   (MySQL, SQL Server, Oracle) means a schema change could not be written to
   the broker, or no broker replica acknowledged it within 30 WAITs
   (`SCHEMA_HISTORY_WAIT_ATTEMPTS`). The connector stops and ECS starts a new
   task, which reads that DDL from the source log again. Nothing is lost. If
   it repeats, check `…-broker-replication-lag` and `…-broker-memory`. A broker
   with no replica needs `BrokerWait=false`. `entry … cannot be read` at start
   means a damaged history entry: see [Schema history size](#schema-history-size).

A deploy that starts or resumes Debezium (a first deploy, the resume after a
load, the end of every update) succeeds only once Debezium writes a heartbeat
to the broker after the start. A running task can still be failing on grants,
a purged log position or a missing replication slot. If no heartbeat arrives
within 10 minutes the deploy fails with "Debezium is running but has written no
heartbeat", and capture is left running. When the deployer cannot read the
broker (the CLI on a laptop), the deploy succeeds and says capture is
unverified; watch the `capture-stale` alarm instead.

---

## Debezium refuses to start: no stored offset

Debezium keeps its log position in the broker (the CDC stack's `OffsetKey`).
If that key is missing, Debezium would start at the source's current log
position and skip every change since the last stored one. A first start looks
the same, so the deployer records the difference: once it has seen the link
store a position, it sets the CDC stack parameter `OffsetCommitted=true`
(Debezium's `EXPECT_OFFSET`), and every update keeps it. With it set, a start
with no stored position is refused. The log says
`ECDL offset store: refusing to start: there is no committed offset in broker
key '…'` and `FATAL ECDL offset store: not starting Debezium`, the task exits,
and the `…-debezium-errors` alarm fires.

The key goes missing when the broker was emptied or replaced (`FLUSHALL`, a
new or restored broker) or when `OffsetKey` changed. To recover:

1. If the old broker or the old key still holds the position, point the link
   back at it. Debezium then resumes from it and nothing is skipped.
2. Otherwise reload with a position reset: `ecdl apply --reload <link file>`
   (control plane: **Update and reload all tables**). The reload sends
   `OffsetCommitted=false`, re-reads every table and starts capture at the
   current position, so the cache is complete again. The deployer sets
   `OffsetCommitted=true` again once Debezium stores a new position. A
   cdc-only link has no load: the changes made while the position was missing
   are not in the cache, and have to be backfilled
   (`ecdl signal snapshot --tables <t>`).

Recording `OffsetCommitted` restarts the Debezium task once, at the end of the
first deploy (or of the first update after this release, unless the broker
already holds the position, in which case the update sets it). If the deployer
could not record it, it says so (`offset_record_failed`) and the deploy still
succeeds; deploy again to retry.

---

## Switchovers, blue/green and major upgrades

Debezium resumes from a position it stored: a binlog position or GTID set
(MySQL), or a replication slot (PostgreSQL). A change of writer that does not
carry that position over makes it meaningless, and without the checks below
capture would carry on from the wrong place and skip changes without an error.

| Event | MySQL with GTIDs | MySQL without GTIDs | PostgreSQL |
|---|---|---|---|
| Failover inside one cluster or Multi-AZ instance | resumes | can resume at the wrong position | resumes if the slot survived the failover; if not, **refused**, reload |
| RDS blue/green switchover | resumes on the new writer by GTID | wrong position | slot not on the new writer: **refused**, reload |
| Major version upgrade | resumes | wrong position | RDS requires dropping the slot first: **refused**, reload |
| Restore to a new instance or cluster | resumes only if the GTIDs it needs are still in the binlog | wrong position | no slot: **refused**, reload |

What the pipeline does about it:

- The deploy preflight **blocks** a MySQL source with `gtid_mode` or
  `enforce_gtid_consistency` not `ON` (when it can read them). An existing link
  without GTIDs is blocked at its next apply too: turn them on (see
  [prerequisites](prerequisites.md)), or deploy with `ignore_preflight` and
  plan to reload after any writer change.
- On PostgreSQL, once the link has stored a position (`OffsetCommitted=true`,
  see [Debezium refuses to start](#debezium-refuses-to-start-no-stored-offset)),
  Debezium checks that its slot exists before it starts. If it is missing, the
  log says `refusing to start: replication slot '…' does not exist` and the task
  exits, instead of creating a new slot at the current WAL position. If the
  check cannot run (no connection, no permission) it logs ERROR and starts.

Planned switchover or upgrade:

1. Check capture is caught up: no `…-capture-stale` or `…-sink-lag` alarm.
2. MySQL with GTIDs: switch over or upgrade. Debezium reconnects by itself.
   Watch the Debezium log for `streaming` and the alarms. If it stops on a
   purged GTID or binlog position, reload.
3. PostgreSQL: the slot cannot come along. For a major version upgrade RDS
   refuses to start while logical slots exist, so first stop capture
   (`aws ecs update-service … --desired-count 0`), drop the slot
   (`SELECT pg_drop_replication_slot('ecdl_<link>');`), then upgrade. For a
   blue/green switchover, switch over. Debezium then refuses to start, which
   is expected.
4. Reload with a position reset: `ecdl apply --reload <link file>` (control
   plane: **Update and reload all tables**). The cache keeps serving the old
   values until the load rewrites them. Make sure the publication and the
   signalling table exist on the new writer first (`ecdl signal ddl`), and that
   the new writer meets the [prerequisites](prerequisites.md).
5. On a blue/green deployment, drop the slot left on the old (blue) writer
   once it is no longer needed: an unread slot keeps WAL until the disk fills.

---

## Schema history size

MySQL, SQL Server and Oracle keep every DDL statement Debezium has seen in the
broker stream named by the CDC stack's `SchemaHistoryKey`
(`ecdl:<link>:schema_history`). PostgreSQL has none. Every Debezium start reads
the whole stream before capture resumes, and the stream only grows: it holds
the DDL of every table in the database, not only the captured ones, because
adding a table to a link later needs them. Online schema-change tools (gh-ost,
pt-online-schema-change) and partition maintenance add records quickly.

Debezium logs the size at each start: `ECDL schema history: reading N
record(s)`, or a WARN above 10,000 records (`SCHEMA_HISTORY_WARN_RECORDS`).
To check it by hand:

```bash
valkey-cli --tls -h <broker endpoint> -a "$TOKEN" XLEN ecdl:<link>:schema_history
```

A start that reads many records is slower, not wrong. To make the history small
again, rebuild it from the current table definitions:

1. Make sure no DDL runs on the source until step 4 is done.
2. Stop capture (`aws ecs update-service … --desired-count 0`).
3. Delete the history key (`DEL ecdl:<link>:schema_history`). Keep the offset.
4. Set `advanced.debezium.snapshot_mode: recovery` in the link and apply it.
   Debezium rebuilds the history from the tables as they are now and then
   streams from the stored offset. Once its log shows it streaming, remove the
   setting and apply again, so a later restart does not rebuild it again.
   PostgreSQL keeps no schema history and has no `recovery` mode.

`entry … of '…' cannot be read` at start means one history entry is damaged.
Debezium refuses to start rather than rebuild the schema from part of its
history, which would decode rows with the wrong columns. Recover the same way.

---

## Sink is behind

The sink lag alarm is the end-to-end "the cache is behind the source" signal.

- **Target unreachable or failing:** `…-target-down` alarm, `TargetUp` 0, sink
  log `ERROR target-down: … holding until the target recovers`. Each sink worker
  keeps the batch it has and retries it (back-off from 0.5s to 30s) instead of
  reading more, so at most `WORKERS x BATCH_SIZE` changes per task are pending
  and the rest wait in the stream. A deployed link runs 8 workers with batches
  of 2000 (`advanced.sink.workers` and `batch_size`); the sink binary run by
  hand defaults to 16 workers. The sink tasks stay healthy and running (a
  restart cannot fix the target), so the lag metric keeps reporting. This
  covers an unreachable node, a failover (`READONLY`, `CLUSTERDOWN`,
  `LOADING`, `MASTERDOWN`, `TRYAGAIN`), `OOM` and a permission change
  (`NOPERM`, `NOAUTH`, `WRONGPASS`). In cluster mode, writes to the healthy
  shards continue. A sink that starts while the target is down waits for it.
  An outage does not dead-letter a change: these errors hold it, and while any
  shard does not answer a `PING`, reclaim claims nothing. A retry claimed while
  every shard answered `PING` but writes failed with one of these errors still
  counts. So after such an outage a change may have fewer than 5 retries left,
  and a refusal of that change itself then dead-letters it sooner.
  Broker memory grows while the outage lasts; see `…-broker-memory`.
- **Broker full at startup:** sink log `ERROR broker-group: consumer group not
  ready … OOM`. The broker refuses to create a consumer group while it is over
  maxmemory. A task whose group already exists starts at once; a new one waits
  and retries (it stays healthy) until memory is freed. See `…-broker-memory`.
- **At the scaling ceiling:** `SinkRunningTasks` equals `advanced.sink.max_count`
  and `BacklogPerTask` stays high. Raise `max_count`, or see the benchmarks for
  the single-reader ceiling of each engine.
- **One table stuck:** Pipeline Monitor's per-table breakdown shows which stream.
- **A second consumer group on a stream** (a blue/green sink, or one added for
  debugging): sink log `WARN shared-stream`. Acknowledged entries are then kept
  until every group has acknowledged them, so broker memory and the lag
  metric follow the slowest group. Delete a group you no longer need
  (`XGROUP DESTROY`). The sink notices a new group within 15 seconds; until
  then it may delete entries the new group has not read, so create one before
  relying on it, not during a cut-over.
- **Target replica behind:** `WaitShortfall` above 0 and sink log
  `WARN target-wait`. Changes reach the target primary, but the sink waits for
  its replica before it acknowledges them (see
  [Delivery guarantees](#delivery-guarantees-and-the-_ecdl_v-field)). Check the
  target's `ReplicationLag` and whether a replica is down or being replaced.

---

## Truncated tables, dropped tables and partitions

A `TRUNCATE TABLE` at the source deletes the table's rows from the cache. The
sink logs one line per truncate:
`WARN truncate: <stream> <id>: table <t> truncated at the source; deleted N row(s) …`.
Only keys older than the truncate are deleted, so rows inserted after it stay,
and a change from before it that is retried later is skipped. Keys of other
tables and of other links' key prefixes are not touched. The target user needs
`SCAN` for this. With denormalize rules,
the children of a truncated parent lose its fields (as for a delete with
`on_parent_delete: null`), and a truncated child leaves its parents' indexes.

These send no change event, so the cache keeps the rows until you reload them:

- `DROP TABLE`, and dropping, detaching or exchanging a partition.
- MySQL `ALTER TABLE … TRUNCATE PARTITION`.
- PostgreSQL `TRUNCATE` of a single partition when the publication sends
  changes as the root table (`publish_via_partition_root`).
- PostgreSQL `TRUNCATE` when the publication does not publish `truncate` (see
  [prerequisites](prerequisites.md)).

No alarm fires for them: nothing reaches the broker. After one of these, reload
the link (`ecdl apply --reload`). `ecdl signal snapshot --tables <t>` rewrites
the rows that still exist but does not remove the ones that are gone.

---

## Dead letters

A change the target refused for itself (for example `WRONGTYPE`) on its first
try and on all `MAX_DELIVERIES` (5) retries, or that could not be parsed, is
moved to `dlq:<stream>` on the broker with the reason and the last error
(`error`). A change held because the target was unavailable is not
dead-lettered (see [Sink is behind](#sink-is-behind)). **It is not in the cache.** Fix the cause first, then
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

`list` and `replay` read at most `--limit` (1000) dead letters per stream. When
more are left, `replay` ends with `PARTIAL: N more were not read` and exits with
code 3; run it again until it does not.

Dead letters of a table that was removed from the link stay on the broker. They
are listed (marked "no longer in this link"), counted in `DLQDepth` (so the
dead-letter alarm stays on), and never replayed, since nothing consumes their
stream. Remove them with `purge` once the rows no longer matter, or after
adding the table back and snapshotting it.

`purge` deletes dead letters for good. Without `--yes` it only reports what it
would delete. `--stream <stream>` limits it to one table, and
`--older-than 72h` to older entries:

```bash
--overrides '{"containerOverrides":[{"name":"sink","command":["dlq","purge","--older-than","72h","--yes"]}]}'
```

Each dead-letter stream holds at most `DLQ_MAX_LEN` (10,000) entries, so a
flood of bad changes cannot fill the broker (it runs `noeviction`, and a full
broker stops capture for every table). At the cap the oldest dead letter is
dropped, the sink logs `ERROR dlq-full` and counts it in `DLQDropped`; snapshot
the table after fixing the cause. A dead letter is written and its original
acknowledged in one transaction, so a change is never dead-lettered twice.

`replay` skips a dead letter whose change is older than `TOMBSTONE_TTL_SEC`
(1h): a later delete's tombstone may have expired, and the replay could bring a
deleted row back. For those, snapshot the table instead
(`ecdl signal snapshot --tables <t>`), or replay with `--force` after checking
the rows.

---

## Delivery guarantees and the `_ecdl_v` field

The aim is that every change is applied, skipped because something newer is
already in the cache, or dead-lettered, and that nothing is dropped silently.
These are the known exceptions:
- A pending change deleted from its broker stream before it was applied is
  lost. This one is reported: `…-pending-vanished` fires, and the sink log line
  `pending-vanished` lists the ids.
- When the sink waits for no replica (the primary has none, or
  `TARGET_WAIT_REPLICAS=0`), a target failover or restart can lose writes the
  sink already acknowledged. With more than one replica per primary, the wait
  confirms one of them, and a failover to another can lose them too. Nothing
  reports this.
- `VERSION_GUARD=false` turns the version check off, and a retried older change
  can overwrite a newer one without a signal.
- Rows the target evicts are gone until they change again. Evictions are
  counted (`TargetEvictedKeys`, alarm `…-target-evicted`).
- Dead letters of a table removed from the link are no longer listed,
  replayed or counted in `DLQDepth`, so `…-dead-letters` returns to OK.
- A broker that is lost and not restored takes its dead letters with it (see
  [Restoring the broker from a snapshot](#restoring-the-broker-from-a-snapshot)).

How the rest is kept:

- Debezium writes changes to the broker in order and checks every reply. Its log
  position is committed only for changes a broker replica holds. A broker
  failover, or any refused write other than a full broker, stops the Debezium
  task; ECS restarts it from that position, so some changes are written twice and
  none are skipped. A full broker pauses capture (an ERROR line a minute) until
  the sink frees space.
- The sink acknowledges a change only after the target confirmed the write. An
  unconfirmed change stays pending in the consumer group and is retried.
- When the target has replicas, the sink also waits for one replica on each
  primary to hold the write (`WAIT`, up to `TARGET_WAIT_TIMEOUT_MS`, default
  1000) before it acknowledges it, so a failover to that replica cannot lose a
  change the broker has already dropped. A change its replica did not confirm in time
  stays pending and is applied again by the retry; `WaitShortfall` counts these
  and the sink logs `WARN target-wait`. `TARGET_WAIT_REPLICAS` sets how many
  replicas to wait for; unset, it is 1 on a primary that has a replica and 0 on
  one that has none (read at start and every 30 s). `TARGET_WAIT_REPLICAS=0`
  turns the wait off. A target user that may not run `WAIT` leaves every
  change pending; allow `WAIT` or set `TARGET_WAIT_REPLICAS=0`. A
  `WaitShortfall` that does not stop means a target
  replica is behind or unreachable: changes keep applying to the primary but
  pile up as pending, and the `…-sink-lag` alarm fires.
- A dead letter is written before the original is acknowledged, so a change is
  always in at least one of the two places.
- Each write is an atomic check that applies only if the change is newer than the
  version stored in the hash field `_ecdl_v` (the change's broker stream id). A
  retried or reordered older change therefore cannot overwrite a newer one. A
  delete leaves a tombstone so an older retry cannot bring the row back. It
  lives 1 hour (`TOMBSTONE_TTL_SEC`) plus the age of the oldest change still
  waiting in the broker, up to 7 days, so a backlog after a long outage is
  covered. Use `maxmemory-policy noeviction` on the target, because
  `volatile-*` evicts tombstones first and `allkeys-*` evicts cached rows. The
  deploy preflight reads the policy from the target's parameter group: it
  refuses `allkeys-*` and warns on `volatile-*` (`ignore_preflight`, or
  `ecdl apply --ignore-preflight`, deploys anyway). The sink also warns at start
  and counts evictions while it runs (`TargetEvictedKeys`, alarm
  `…-target-evicted`).
  Applications reading the hash should ignore `_ecdl_v`. `StaleSkipped` counts
  writes the check refused.
- Rows written by the bulk load carry the broker time at which capture was paused
  as their `_ecdl_v`. Changes captured before the pause are older than the loaded
  data and are skipped; changes after capture resumes apply. A load run by hand
  without `LOAD_VERSION` writes no version, and every later change applies.
- <a id="replays-from-an-older-position"></a>**Replays from an older position.**
  After a load, a Debezium restart from an older offset, or a broker restore,
  Debezium reads the source log again from an older position. The replayed
  changes get new stream ids, so the check applies them, and until the replay
  has drained a cached row can show an older value than it held (after a load,
  each row changed during the load goes back to its first change since the
  pause and then forward). The cache ends right. While a table's waiting
  changes were captured more than `CATCHING_UP_SECONDS` (default 60) after
  their commit, the sink logs `WARN catching-up:<table>`, counts the table in
  `CatchingUpTables`, and logs `<table>: caught up` when it is done; the
  monitor shows "catching up". Readers that must not see older values should
  wait until `CatchingUpTables` is 0. A dead letter being replayed keeps its
  original version and is not counted. With `EVENT_FORMAT=flat` the events
  carry no commit time, so nothing is shown. Ordering changes by source log
  position instead of stream id (decision D4) would remove the window.
- A load that pauses capture first confirms Debezium has committed a log
  position; without one, the deploy stops before pausing. From a machine that
  cannot read the broker, `ecdl apply` refuses unless `--offset-checked` is given.

- Denormalized tables get the same check. A child row also stores, per rule,
  which parent it points at (`_ecdl_fk:<parent>:<join_field>`) and the version of
  the parent change its embedded fields came from (`_ecdl_pv:<parent>:<join_field>`).
  A parent change reaches a child only if the child still points at that parent
  and the parent change is newer, so retried or reordered parent and child
  changes cannot leave old parent values on a child. Applications should ignore
  these fields too. Tables that are in no rule keep the faster batched path.
  A child's own write and a parent's fan-out both take the parent fields from
  the parent row after the parent's transform, so a parent with its own
  `rename`, `columns` or computed field gives a child the same values
  whichever side wrote last.
  A row that moves to another parent leaves the old parent's reverse index,
  found from the row's stored `_ecdl_fk` field or the before-image. A retried
  change that has neither logs `WARN denorm-index:<table>`: the old index may
  still list the row. That entry does no harm, because a fan-out checks
  `_ecdl_fk`, but it is not removed.
  Denormalization needs `EVENT_FORMAT=envelope` and the version guard; the sink
  refuses to start without them.
- A field Debezium could not read is not written. PostgreSQL leaves an unchanged
  TOASTed value out of an UPDATE, and Oracle an unchanged LOB, and Debezium sends
  `__debezium_unavailable_value` for it. The sink leaves that field out of the
  write, on the plain and the denormalized path, so the cached value stays. In an
  insert or a snapshot read it logs `WARN unavailable-value:<table>` naming the
  fields. Debezium and the sink both read `UNAVAILABLE_VALUE_PLACEHOLDER`
  (default `__debezium_unavailable_value`); set it on both tasks or on neither.

Limit: changes are only safe while the source still retains its log (see
[Capture is paused or stopped](#capture-is-paused-or-stopped)).

Upgrading a link with denormalize rules from 0.2.16 or earlier: reload it
(`ecdl apply --reload`) if it uses `advanced.key_prefix` or a schema-qualified
`parent_table`. Its reverse indexes were written under keys the sink no longer
reads, so parent changes would not reach existing children until each child
changes.

Upgrading from 0.2.28 or earlier:
- A computed expression outside the shared grammar (see
  [value-formats.md](value-formats.md#computed-fields-and-filters)), a filter
  with an unknown operator, or two columns renamed to one name now fails
  `ecdl validate`, and the sink and the loader refuse to start with it. Fix
  the link before upgrading. Such a link used to compute different values in
  the sink and the loader.
- A computed value that is NaN, an infinity or -0 is now written `NaN`,
  `Infinity`, `-Infinity` or `0` (it was `nan`/`inf` from the loader and
  `NaN`/`+Inf` from the sink). Rows keep the old text until they change or
  the link is reloaded.
- A denormalize rule whose parent table has its own `rename`, `columns` or
  computed field: children keep what they embedded until the parent changes
  or the link is reloaded (`ecdl apply --reload`).

---

## Changing a running link

A change can leave cached rows wrong or missing without any error. Every way of
changing a deployed link (`ecdl apply`, the Deploy button, and Edit → Apply on
the Deployments page) first compares the update with the link's **deployed
stacks**, not with another file, and classifies each change by what it leaves
behind:

| Severity | Meaning | What happens |
|---|---|---|
| `deploy` | a setting the tasks read; no data implication | applied |
| `backfill` | cached rows are now wrong, or a new table's existing rows are absent | applied after you acknowledge it; then run the printed snapshot `INSERT` |
| `cleanup` | keys nothing maintains any more | applied after you acknowledge it; then delete the printed key patterns |
| `recreate` | cannot be done in place: a new source, database or target, a renamed link, or a mode without CDC | refused: deploy a new link, then delete the old one |

```bash
ecdl apply --dry-run desired.yaml   # shows the change to the deployed link; changes nothing
ecdl apply desired.yaml             # asks before a backfill or cleanup change
ecdl apply --yes --acknowledge desired.yaml   # unattended: --yes alone refuses those
ecdl signal snapshot --tables orders desired.yaml
```

The comparison is made on the stack parameters, so a setting the file leaves out
is reported (for example `advanced.key_prefix (KeyPrefix 'b' -> '')`) instead of
silently reset to its default. New template defaults in a release upgrade are
not reported. The Edit page loads every deployed setting into the file, so an
edit changes only what you change.

- **Add a table:** capture starts from the update, so its existing rows need the
  snapshot.
- **Change a transform or add a filter:** cached rows hold values from the old
  rules until the snapshot rewrites them. Apply first, then snapshot; a snapshot
  before the new rules are live rewrites every row under the old ones.
- **Remove a table:** its keys stay in the cache; delete them if nothing reads
  them. The printed pattern includes the link's key prefix.
- **Change `advanced.key_prefix`:** every key moves. Apply, snapshot every table,
  then delete the keys under the old prefix.
- **Read a table from another schema** (`source.schema`, or a qualifier such as
  `sales.orders`): the keys stay the same but hold the old table's rows. Apply,
  delete the printed key pattern, then snapshot. A new source host, port, engine
  or kind is refused like a new ARN; a new secret or username is applied, and
  the tasks restart with it.
- **Remove capture (mode `initial_load`):** refused. Destroy the link and deploy
  it again in the new mode.

An update never moves capture: Debezium's task count is kept, and at the end the
update checks that capture is running. A capture left paused (a failed resume,
a control plane restarted during a load, a pause by hand) is resumed, unless a
load of the link is still running. While a load runs, every update of the link
is refused, from the CLI and from the control plane.

`signal snapshot` prints an `INSERT` for the link's signalling table; run it on
the source (the control plane holds no database credential). Debezium then
re-reads the table in chunks while streaming continues.

`ecdl signal snapshot --wait 300 ...` also reports whether Debezium read the
signal. It needs a network path to the broker. Run the `INSERT`, press Enter,
and it watches the broker for up to 300 seconds:

- `finished`: Debezium's committed offset listed the snapshot and then
  dropped it.
- `consumed`: the snapshot started and was still running when the wait ended.
- `unverified`: no sign that Debezium read the signal. An empty table gives no
  rows to see. Otherwise check the Debezium log and, on PostgreSQL, that the
  signalling table is in the publication. The command exits 2.

**PostgreSQL: adding a table.** Add it to the link's publication before the
snapshot, or PostgreSQL never sends its changes. The update review lists the
statement as the first step, for example
`ALTER PUBLICATION ecdl_shop_pub ADD TABLE public.items;` (`dbz_pub` for a link
deployed before per-link publications). An update never changes the publication
a running link reads.

**Expected on every CDC stack update (MySQL):** as the old Debezium task stops it
logs `ERROR ... Exception while closing JDBC connection ... Communications link
failure during rollback()`. Nothing is lost: the position lives in the broker,
and the new task logs `Requesting streaming from position ...` and `Skip N events
on streaming start`. It is a problem only if the new task never reaches
`Starting streaming`.

---

## Rotating credentials
Each task reads its secrets once, when it starts. So the CDC stack restarts a
service when one of its secrets gets a new current value: an EventBridge rule
(`…-SecretChangedRule-…`) on Secrets Manager's `Secret Label Updated` event for
`AWSCURRENT` runs a small function (`…-RestartFunction-…`, log group
`/ecs/ecdl-<link>-cdc/restart`) that forces a new deployment of the Debezium
service when the source secret changes, and of the sink service when the
target secret changes. The event is sent at the end of every rotation and
when anyone stores a new value, and needs no CloudTrail trail. The function's
role may only do that, on those two services. Nothing is lost: Debezium
resumes from its stored offset (capture stops for about 2 minutes while the new
task starts), and the sink rolls with new tasks starting before the old ones
stop. A service paused at 0 tasks for a load stays at 0, and starts with the
new value when it is resumed. The log group shows `restarted debezium for
<secret ARN>` for each restart. The broker token is not handled this way; see
below.
- **Source database password:** an RDS-managed secret, which RDS rotates on its
  schedule, needs nothing from you. For a secret you manage, store the new
  password in the secret after the database accepts it; the restart follows.
  Without the restart (a stack deployed before it existed) the running task
  keeps its open connection and fails to log in at its next reconnect (a
  failover, a network interruption). On PostgreSQL the task then stops and ECS
  starts a new one, which reads the rotated password. On MySQL it does not
  stop: the binlog client tries the old password again every minute, logging
  WARN `keepalive: Failed to restore connection`, the health check stays green,
  and capture stops until `…-capture-stale` fires. To restart it by
  hand:
  `aws ecs update-service --cluster ecdl-<link> --service ecdl-<link>-cdc-debezium --force-new-deployment`.
  The bulk loader also reads the password when it starts, so a load started
  after the rotation uses the new one.
- **Target cache credential** (the sink's `TargetSecretArn`): the sink is
  restarted when the secret changes, but the target itself is yours, so the
  order matters. The new credential must work on the target before the
  secret holds it, and the old one must keep working until the new sink tasks
  run:
  1. For an auth token, add the new token with `ROTATE`
     (`aws elasticache modify-replication-group --replication-group-id <target>
     --auth-token "$NEW" --auth-token-update-strategy ROTATE --apply-immediately`),
     so the target accepts both. For an RBAC user, give the user the new
     password as a second one (a user can have two).
  2. Store the new value in the secret. The sink restarts on its own.
  3. Once the sink service is stable (`aws ecs wait services-stable --cluster
     ecdl-<link> --services ecdl-<link>-cdc-sink`) and `…-target-down` is OK,
     remove the old credential (`SET` with the new token, or drop the old
     password).
  Changing the credential on the target in one step makes every sink write
  fail until the sink restarts; the changes wait on the broker
  (`…-target-down`) and are not lost. A Secrets Manager rotation function for
  the target must follow the same order.
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
An update of a running link keeps the sink's current task count, so a sink
scaled out to drain a backlog stays scaled out. Set `advanced.sink.desired_count`
only to force a count.
The broker's size is `advanced.broker.node_type` (default `cache.t4g.small`,
about a million queued events). Changing it on a deployed link resizes the
broker in place.

Debezium's Java heap is 75% of its task memory (`advanced.debezium.memory`,
default 4096 MB), and its queue holds at most 32,768 changes or 256 MB,
whichever comes first. If it runs out of heap anyway, the log says
`java.lang.OutOfMemoryError`, the process exits, and ECS starts a new task,
which resumes from the stored offset. If that repeats on the same change (one
very large transaction), raise `advanced.debezium.memory`.

The broker buffers changes while the sink is behind or the target is down. A
new link gets a `cache.m7g.large` broker with three nodes (a primary and two
replicas): about 4.8 GB for queued changes, roughly 70 minutes of backlog at
1,000 changes/s. `ecdl plan` prints the estimate for the link's broker. Set
`advanced.broker.node_type` and `advanced.broker.replicas` to change them; the
broker is scaled in place, not replaced. A broker deployed before these
settings existed (`cache.t4g.small`, two nodes, about 15 minutes at 1,000
changes/s) keeps its size until you set them. Burstable `cache.t4g` nodes run
out of CPU credits under a sustained backlog, so the plan warns about them.

---

## A failed or interrupted bulk load

While it runs, the loader logs `progress: N rows written, k/n chunks done`
every minute (`LOAD_LOG_EVERY_SEC`), and `WARNING: the load has written no rows
for 10 min` when nothing moves (`LOAD_STALL_SEC`). The dashboard shows the load
as `stalled` when its progress hash has not changed for 10 minutes and the run
did not complete. `ecdl apply` prints the load task's status when it changes
and every few minutes.

The loader retries a failed chunk with backoff. It gives up after 5 failures in
a row without progress (`LOAD_RETRIES`); an attempt that wrote rows before it
failed starts that count again. A chunk stops after 50 failures in all
(`LOAD_RETRIES_TOTAL`).
Before each retry it reads back the last 5 batches it wrote
(`LOAD_VERIFY_BATCHES`) and resumes from the first one the target no longer has,
because a primary that fails over can lose writes it already confirmed. If the
loss goes back further than those batches, the chunk starts again.

After every chunk is done, the loader reads back each chunk's last batches and
an evenly spread sample of 200 keys per chunk (`LOAD_VERIFY_SAMPLE`). A key
counts as written when its `_ecdl_v` is this load's version or newer; a load
without a version only checks that the key exists. A chunk with a missing key
is loaded again and checked again, up to 2 times (`LOAD_VERIFY_REPAIRS`). A loss
that remains fails the load with `FATAL: the target does not have rows this
load wrote`. The log line `check: N written keys read back` shows a passed
check.

One table's load may run for 6 hours before the deploy stops it and fails. Set
`advanced.loader.timeout_minutes` in the link for a larger table, or
`ecdl apply --load-timeout MINUTES` for one run; the source must keep its log
for longer than the load.
If a chunk still fails, the deploy fails,
capture is resumed, and the error says the cache may be missing rows. The
error gives the loader's exit code and what it means (137: killed, usually out
of memory), its last `FATAL` or `ERROR` line, and its log stream
(`loader/loader/<task id>` in `/ecs/ecdl-<link>-loader`). Run the
load again with `ecdl apply --reload` (or **Update and reload all tables**): the
table whose load failed resumes, skipping chunks that finished; tables that
had finished load again in full. `LOAD_RESUME=false` on the loader task starts
from the first row.

Each link and table keeps its load progress in its own hash on the target,
`ecdl:<link>:bulkload:<table>` (with `advanced.key_prefix`,
`ecdl:<link>:bulkload:<prefix>:<table>`). A run resumes only an incomplete run
with the same table, key prefix, transforms, replace mode, split and key
columns, `--min-pk`/`--max-pk`, and loader stack (a link deleted and created
again under the same name does not resume the old link's load). When any of
these differ, the log says which, and the load starts from the first row. A
resumed run reuses the earlier run's chunks, so rows inserted at the source in
the meantime do not prevent it: keys above (or below) the stored chunks get a
chunk of their own.

The key range is split into one chunk per worker, of equal width. When the
range is more than twice as wide as the table's row count (sparse or clustered
keys), the boundaries are read from the index instead, so each chunk holds
about the same number of rows (log line `split by quantiles`).

The loader reads each table by the name the source knows it by: `schema.table`
for PostgreSQL, SQL Server and Oracle, `database.table` for MySQL. A table
outside the default schema is loaded from its own schema. Cache keys use the
bare table name, as the sink does. A loader stack deployed by an earlier
release reads the bare name until the link is updated.

Loaded rows are keyed by the table's whole primary key, exactly as the sink
keys them: one column is `<table>:<value>`; a composite key joins the values
with `:` in the sorted order of the column names (`order_lines:<line_no>:<order_id>`).
The load is split into integer ranges of one column: the primary key's first
column, or the column `advanced.loader.primary_keys` names for the table. That
column must be an indexed integer column and does not have to be unique; it
does not change any key. A table loaded by an earlier release with
`primary_keys` naming a column that is not its whole primary key (the old advice
for a composite key) holds keys the sink never writes, one per value of that
column: reload it (`ecdl apply --reload`) and delete the old keys.

**A load replaces the table's rows and sweeps the rest.** Every load the
deployer runs rebuilds each row (fields a transform no longer produces go) and
deletes rows the filter now drops (`REPLACE_EXISTING=true`). After a complete
load of the whole key range it deletes every hash under the table's key prefix
whose `_ecdl_v` is older than the load: rows the source no longer has (log line
`sweep: N key(s) ... deleted`). Keys without `_ecdl_v` are left alone and
counted. There is no sweep for a load without a version (capture not paused),
with `--min-pk`/`--max-pk`, or with `LOAD_SWEEP=false`.

**The load reads the source in short statements.** Each chunk is read in pages
of up to 50,000 rows (`FETCH_BATCH`), one statement and transaction per page,
continuing from the last key read. So no read holds a MySQL metadata lock or
read view, or a PostgreSQL snapshot that holds back VACUUM, for longer than one
page. A read that waits more than 10 s for a lock (behind a DDL,
`LOAD_LOCK_WAIT_SEC`) fails and is retried. Paging needs an index on the column
the load is split by; for a column without one, `LOAD_PAGED=false` reads each
chunk with one statement as before.

**An unfinished load is remembered.** A deploy that loads tags the loader stack
`ecdl:initial-load=incomplete` before the first table and `complete` once every
table has loaded. While it says `incomplete`, a plain deploy of the running link
is refused ("the initial load ... did not complete"), because an update loads
nothing; deploy with a reload instead. Deployments shows **initial load
incomplete** for the link. Stacks deployed before the tag existed count as
complete. If every table loaded but the tag could not be updated, the deploy
still succeeds and says so, and the next plain deploy asks for a reload.

**Capture is resumed only after the load task has stopped.** When a deploy fails,
times out or is interrupted (Ctrl-C) during the load, it stops the load task and
waits until ECS reports it `STOPPED` (up to 5 minutes), then resumes capture. A
load that kept writing under live capture would overwrite newer changes with
older rows and bring deleted rows back. If the stop cannot be confirmed (the
task does not stop, or AWS cannot be read, for example after the session
expired), capture is **left paused** and the error says so. Then check that no
load task runs (`aws ecs list-tasks --cluster ecdl-<link> --family
ecdl-<link>-loader --desired-status RUNNING`), stop any that do, and set the
Debezium service's desired count to 1. The loader stops all its workers at once
on SIGTERM. Before each table the deploy also checks that capture is still
paused, and stops if something resumed it. Run the loader task by hand only
with capture paused. If the resume itself is sent but no Debezium task starts,
the deploy fails without sending it again: capture is not paused (desired count
1), and the service's events and the Debezium log say why the task does not
start. The loader stack is created before capture is paused, so the pause
covers only the load.

**"will not fit in the target cache".** Before writing anything, the loader
estimates what the table needs (catalogue row count, or `COUNT(*)` for a table
with no statistics, x the size of 200 rows sampled across the key range as it
would write them, with the version field, embedded parent fields and
reverse-index entries, plus per-key overhead, excluding rows the filter drops
and keys already cached) and compares it with the free memory the target's
primaries report. If it needs more than 90% of that, it stops with both numbers
and nothing written. Use a larger node type or more shards. The estimate runs
about 20-40% above Valkey's own `MEMORY USAGE`, on purpose;
`LOAD_SKIP_MEMORY_CHECK=true` on the loader task loads anyway. Plan also warns
when the target is already 80% full.

**A load that filled the target.** If the target still fills up (`OOM command
not allowed`), the load stops and its rows so far stay in the cache. While the
cache is full it refuses the sink's writes for every link on it, and those
changes go to the dead-letter queue. Delete the partial table's keys before
loading again, on each primary of the target (`<prefix>` is the table's key
prefix from the loader log, for example `orders:`):

```
valkey-cli --tls -h <primary> --scan --pattern '<prefix>*' | xargs -n 500 valkey-cli --tls -h <primary> UNLINK
```

Then make room (a larger node type, more shards) and reload. Replay the
dead letters afterwards ([Dead letters](#dead-letters)).

**"a loader worker process was killed".** A worker ran the task out of memory.
Each worker holds one page of rows; the page is 50,000 rows, lowered for wide
rows so all workers fit in half the task's memory (log line `fetch batch`).
Lower `advanced.loader.workers` or `advanced.loader.fetch_batch`, or raise
`advanced.loader.memory`, and reload: the load resumes where it stopped.

## Two links, one cache

Keys are `<table>:<primary key>`, so two links that copy a table of the same
name into one cache would overwrite each other. Plan and deploy refuse that,
naming the other link. Use another target, or set `advanced.key_prefix` on one
link: its keys become `<prefix>:<table>:<primary key>`. `ignore_preflight`
(`--ignore-preflight`) does not override this refusal. If the other link is gone
but its stacks are not, delete them.

The same applies inside one link: two tables with the same name in different
schemas (`sales.users` and `hr.users`) would write the same keys, so the link is
refused at validation and the sink refuses to start with such a `STREAMS` list.
Put one of them in its own link with a different `advanced.key_prefix`.

---

## Restoring the broker from a snapshot

The broker holds four things nothing else has: Debezium's committed log
position (`ecdl:<link>:offsets`), the captured schema history, the changes the
sink has not applied yet, and the dead letters. What protects them:

- A primary and a replica in two Availability Zones, with automatic failover.
  Debezium commits a position only after the replica holds the writes.
- A daily automatic snapshot in `SnapshotWindow` (03:00-04:00 UTC), kept for
  `SnapshotRetentionLimit` days (3). Automatic snapshots are deleted with the
  broker.
- A final snapshot whenever the broker is deleted or replaced: deleting the
  link, or a stack change that replaces the broker (DeletionPolicy and
  UpdateReplacePolicy `Snapshot`). ElastiCache keeps it, and charges for it,
  until you delete it. The control plane never deletes snapshots.

**Never reboot the primary** of the broker, and never reboot a single-node
broker. The broker keeps no data on disk, so a rebooted primary restarts empty
and the replica then copies the empty primary: offsets, schema history, backlog
and dead letters are gone. To move the primary, run
`aws elasticache test-failover --replication-group-id ecdl-<link>-broker
--node-group-id 0001`, which promotes the replica with its data. Rebooting the
replica alone is safe. A broker created outside ECDL with a single node
(`BrokerWait=false`) loses everything on any reboot or node failure.

To restore:

1. Find the snapshot. The teardown prints the final snapshot's name; otherwise
   `aws elasticache describe-snapshots --replication-group-id ecdl-<link>-broker
   --query "Snapshots[].[SnapshotName,NodeSnapshots[0].SnapshotCreateTime]"`.
2. The deploy role restores only snapshots whose name starts with `ecdl-`. For
   any other name, copy it with your own credentials:
   `aws elasticache copy-snapshot --source-snapshot-name <name>
   --target-snapshot-name ecdl-<link>-restore`. Copy an automatic snapshot the
   same way before you delete its broker.
3. A broker is restored only when it is created. If the link is still deployed,
   delete it first (this takes another final snapshot). Setting
   `advanced.broker.restore_snapshot` on a deployed link is refused.
4. Set `advanced.broker.restore_snapshot: <name>` in the link and deploy it.
   Use a broker node type at least as large as the one the snapshot came from.
   With `initial_load_and_cdc` the deploy also loads every table again, which is
   correct but slower than `cdc`.

What a restore brings back:

- Debezium resumes from the position in the snapshot, which is older than the
  moment the broker was lost. MySQL, SQL Server and Oracle replay the changes
  since then from the source log, if it still retains them; the sink applies
  them again and nothing is skipped. Until they have drained, rows can show
  older values ([Replays](#replays-from-an-older-position)).
- **PostgreSQL needs a reload.** Debezium confirms each committed position to
  the replication slot, so the slot has moved past the restored position, and
  PostgreSQL does not resend the changes in between. Run `ecdl apply --reload`
  (or `ecdl signal snapshot` for every table) after the restore. If the slot
  was dropped, a new one starts at the current position, which also needs the
  reload.
- The unapplied changes are applied, and the dead letters are back in
  `dlq:<stream>` (`DLQDepth` and the Pipeline Monitor show them). Replay them.
  `…-dead-letters` counts new dead letters only, so it does not fire for
  restored ones. A broker that was lost and not restored takes its dead
  letters with it; the sink log lines `ERROR dead-letter` still name them.
- The auth token is new; the broker stack generates it, and the tasks read it.

Delete a snapshot you no longer need:
`aws elasticache delete-snapshot --snapshot-name <name>`.

---

## The control plane itself

The pipelines do not depend on the control plane: capture, the sink and their
alarms keep running when it is down. It matters during a deploy or teardown
and for the Pipeline Monitor.

- **Logs.** The API and UI containers log to CloudWatch Logs,
  `/ecdl/control-plane/<stack>` (streams `backend` and `frontend`, 30 days).
  When the instance cannot reach CloudWatch Logs at boot, the containers keep
  Docker's default log driver; the boot log
  (`/var/log/cloud-init-output.log`) says `CloudWatch Logs not reachable`,
  and `docker logs ecdl-backend` on the instance shows the output.
- **Hardware failure.** The alarm `ECDLControlPlane-<stack>-system-check`
  fires when the instance fails its EC2 system status check, and EC2 recovers
  the instance onto other hardware with the same instance id, addresses and
  disk. It has no notification; add one to the alarm if you want it. An
  instance that fails its own status check (the operating system) is not
  recovered automatically: reboot it.
- **An update with a broken image fails.** Updating the stack to a new
  release restarts the instance with the new images, and the stack waits up
  to 20 minutes for the API to answer (a wait condition named for the
  release, `ReadyV<version>`). If it does not, the update fails and rolls
  back to the previous images. Changing only `BackendImage` or
  `FrontendImage` on the same release is not checked this way; check the API
  yourself after such a change.

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
- The broker's **final snapshot** (offsets, schema history, unapplied changes and
  dead letters). Both print its name and the command that deletes it.
- The **last error lines of the link's logs.** Deleting the stacks deletes
  their log groups, so before each stack goes the teardown copies the
  `ERROR`, `FATAL`, `WARN` and `Exception` lines of the last 24 hours (the
  newest 50 per log group; the last hour if the day is too long to read) into
  its result: the deployment record on the control plane (`follow_up.kept_log_lines`),
  or `ecdl-<link>-teardown-logs.txt` in the current directory from the CLI.
  The log groups themselves are not kept: each is encrypted with its stack's
  KMS key, which is deleted too, and a kept group would make the next deploy
  of the same link fail.
  ElastiCache charges for it until it is deleted; keep it to deploy the link
  again from it ([Restoring the broker from a snapshot](#restoring-the-broker-from-a-snapshot)).
  For a PostgreSQL link you may restore, decide that before dropping the slot.

The delete is refused while a deploy or update of the link is changing a stack,
and a running load task is stopped (and waited for) before anything is deleted.
Both say first how many changes the broker holds that are not yet in the cache,
and how many dead letters. If there are any, the delete is refused until you
confirm: answer the CLI prompt, or use `ecdl destroy --delete-backlog` with
`--yes`, or send `"delete_backlog": true` to `/api/links/destroy`. They are kept
only in the broker's final snapshot. **Delete pipeline** in the UI lists them
per stream and offers **Delete the backlog and dead letters too**, unticked;
only a ticked box sends `delete_backlog`. When
the broker cannot be read (the CLI on a laptop), that is said and the delete
goes ahead.

---

## The pages superseded by links

The **Superseded by Links** pages (Data Sources, Connectors, Brokers, Targets,
Pipelines) are kept for reference and deploy nothing.

- **Source passwords are not stored.** A data source names a Secrets Manager
  secret in **Password secret ARN**, the same kind of secret a link uses (JSON
  with `username` and `password`, or the password alone). The control plane
  reads it when it lists the source's tables. A request that carries a
  `password` is refused. On start the control plane blanks any password an older
  version stored in its database, so those sources need a secret ARN before
  they can connect. The control plane can read the secret only if it is tagged
  `ecdl:link` or listed in `PipelineSecretArns`. Older versions also created
  secrets named `ecdl/sources/<name>`; nothing uses them, so delete them.
- **The Randomizer is off.** It writes generated rows into the source database
  and is not ready, so the UI hides it and its API (`/api/randomizer/*`, and
  `/api/sources/<id>/tables` and `/columns`) answers 404 unless the backend is
  started with `ECDL_ENABLE_RANDOMIZER=true`. When on, each run stops after
  `max_rows` insert attempts (default 1,000, at most 100,000) or `max_seconds`
  (default 300, at most 3,600), whichever comes first.
- **Backup and restore** (Admin → Maintenance). A backup carries no stored
  credentials. A restore accepts only an intact ECDL control-plane database of
  at most 32 MiB with no triggers or views, and then brings it up to the
  current schema. Any other API request body over 1 MiB is refused with 413,
  and the link editor refuses YAML aliases (`*name`).
