package main

// Reliability layer: retry + dead-letter for the CDC sink.
//
// Problem it solves: the fast path in worker() writes a batch to the target,
// then ACKs it. If a single change can't be written (target OOM, value too big,
// a transient network error) or can't even be parsed ("poison"), the old code
// dropped it silently — the cache was left stale for that key with no retry and
// no visibility.
//
// How it works now:
//   - worker() ACKs only the entries whose write SUCCEEDED. A failed write is
//     left in the consumer group's Pending Entries List (PEL), unacked.
//   - reclaimLoop() periodically claims entries that have been pending longer
//     than RETRY_IDLE_MS and reprocesses them (automatic retry with backoff via
//     the idle threshold). Each claim increases the entry's delivery count by one.
//   - Once an entry's delivery count passes MAX_DELIVERIES, it is written to a
//     dead-letter stream (DLQ_STREAM_PREFIX + source stream) with failure
//     metadata, then acked so it stops looping.
//   - "Poison" messages (unparseable / structurally invalid) skip retries and
//     go straight to the DLQ — retrying can't help them.
//
// Nothing is silently dropped: a change is either written to the cache or written to
// the DLQ where it is visible and replayable.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
)

// errPoison marks a message that can never succeed as-is (bad JSON, no value,
// structurally invalid). Such messages are dead-lettered immediately.
var errPoison = errors.New("poison: unprocessable message")

var (
	dlqPrefix       = envOr("DLQ_STREAM_PREFIX", "dlq:")
	maxDeliveries   = int64(atoiOr("MAX_DELIVERIES", 5))
	retryIdle       = time.Duration(atoiOr("RETRY_IDLE_MS", 30000)) * time.Millisecond
	reclaimInterval = time.Duration(atoiOr("RECLAIM_INTERVAL_MS", 15000)) * time.Millisecond
	reclaimBatch    = int64(atoiOr("RECLAIM_BATCH", 200))
	// About 10-20 MB per table at 1-2 KB a dead letter.
	dlqMaxLen = int64(atoiOr("DLQ_MAX_LEN", 10000))

	dlqSent int64 // messages routed to the DLQ
	retried int64 // messages that succeeded on a retry
	// dead letters dropped because their DLQ was at DLQ_MAX_LEN
	dlqDropped int64
)

func atoiOr(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

// planMessage parses + transforms one broker message into an intended action:
//   - poison=true               -> unprocessable, send to DLQ
//   - isDelete=true             -> DEL key on the target
//   - otherwise                 -> HSET key = fields on the target
//
// This is the single source of truth for how a CDC event maps to a target
// command, shared by the fast path (worker) and the retry path (reclaimLoop).
// `replace` is true for a snapshot READ (op "r"): the row is the authoritative
// full record, so the key is written by DEL+HSET (a full replace) rather than a
// merge. That drops any field the transform no longer produces -- a removed or
// renamed column left over from a previous shape. A backfill after a transform
// change emits exactly op "r", so this is what cleans a table under new rules.
func planMessage(msg redis.XMessage, eventFormat, streamTable string) (key string, fields map[string]interface{}, isDelete bool, poison bool, replace bool) {
	val, ok := msg.Values["value"].(string)
	if !ok {
		return "", nil, false, true, false
	}

	if eventFormat == "flat" {
		var row map[string]interface{}
		if err := decodeJSON([]byte(val), &row); err != nil || row == nil {
			return "", nil, false, true, false
		}
		pk := pkFromMsg(msg, row)
		if pk == "" {
			return "", nil, false, true, false
		}
		key = rowKey(streamTable, pk)
		// No op in a flat event, so a placeholder is dropped without a warning
		// (see unavailable.go); a flat write is always a merge.
		row, _ = dropUnavailable(row)
		out, keep := transformSpec.Apply(streamTable, stringifyRow(row))
		if !keep {
			return key, nil, true, false, false // filtered out -> delete
		}
		f := make(map[string]interface{}, len(out))
		for k, v := range out {
			f[k] = v
		}
		return key, f, false, false, false
	}

	var evt CDCEvent
	if err := decodeJSON([]byte(val), &evt); err != nil {
		return "", nil, false, true, false
	}
	if evt.Op == "d" {
		pk := pkFromMsg(msg, evt.Before)
		if pk == "" {
			return "", nil, false, true, false
		}
		return rowKey(evt.Source.Table, pk), nil, true, false, false
	}
	if evt.After == nil {
		return "", nil, false, true, false
	}
	pk := pkFromMsg(msg, evt.After)
	if pk == "" {
		return "", nil, false, true, false
	}
	key = rowKey(evt.Source.Table, pk)
	// A column Debezium could not read (an unchanged TOAST or LOB value) is
	// left out, so the cached value stays; see unavailable.go.
	after, dropped := dropUnavailable(evt.After)
	warnUnavailable(evt.Source.Table, evt.Op, dropped)
	out, keep := transformSpec.Apply(evt.Source.Table, stringifyRow(after))
	if !keep {
		return key, nil, true, false, false // filtered out / pushed out of scope -> delete
	}
	f := make(map[string]interface{}, len(out))
	for k, v := range out {
		f[k] = v
	}
	// A replace would delete the value a dropped field leaves in the cache.
	return key, f, false, false, evt.Op == "r" && len(dropped) == 0
}

// processOne applies a single message to the target. Returns:
//   - nil        on success
//   - errPoison  if the message is unprocessable (dead-letter it)
//   - other err  on a transient write failure (leave for another retry)
func processOne(ctx context.Context, target redis.UniversalClient, msg redis.XMessage, eventFormat, streamTable string) error {
	if t, table := isTruncate(msg, eventFormat); t {
		return truncateOne(ctx, target, streamTable, streamTable, msg, table)
	}
	key, fields, isDelete, poison, replace := planMessage(msg, eventFormat, streamTable)
	if poison {
		return errPoison
	}
	if old, err := beforeTruncate(ctx, target, streamTable, msgVersion(msg)); err != nil {
		return err
	} else if old {
		atomic.AddInt64(&staleSkipped, 1) // removed by a later truncate (truncate.go)
		return nil
	}
	if versionGuard {
		// The retry path is exactly where ordering goes wrong: this change
		// failed ~30s ago, and a newer one to the same row may have been applied
		// since. The guard skips it in that case instead of writing over it.
		var ok bool
		var err error
		if isDelete {
			ok, err = runDelete(ctx, target, key, msgVersion(msg))
		} else {
			ok, err = runUpsert(ctx, target, key, msgVersion(msg), replace, fields)
		}
		if err != nil {
			return err
		}
		switch {
		case !ok:
			atomic.AddInt64(&staleSkipped, 1)
		case isDelete:
			atomic.AddInt64(&deletes, 1)
		default:
			atomic.AddInt64(&writes, 1)
		}
		return nil
	}
	if isDelete {
		if err := target.Del(ctx, key).Err(); err != nil {
			return err
		}
		atomic.AddInt64(&deletes, 1)
		return nil
	}
	if replace {
		// Snapshot read: replace the key (DEL then HSET) so stale fields from an
		// old shape are dropped. One pipeline keeps it to a single round trip.
		pipe := target.Pipeline()
		pipe.Del(ctx, key)
		hs := pipe.HSet(ctx, key, fields)
		if _, err := pipe.Exec(ctx); err != nil {
			return err
		}
		if err := hs.Err(); err != nil {
			return err
		}
		atomic.AddInt64(&writes, 1)
		return nil
	}
	if err := target.HSet(ctx, key, fields).Err(); err != nil {
		return err
	}
	atomic.AddInt64(&writes, 1)
	return nil
}

// deadLetter moves a failed message to the dead-letter stream for its source
// stream, preserving the original payload and adding failure metadata so it can
// be inspected and replayed, and acks and deletes the original IN THE SAME
// MULTI (Jira story F045).
//
// The DLQ is the only durable record of a message that could not be applied,
// so the original must never be acked unless the dead letter was written. This
// used to be an XADD followed by a separate ackAndTrim: when the ack failed
// (or the task stopped in between) the entry stayed pending, reclaim
// dead-lettered it again, and the DLQ held the change twice. Now the dead
// letter and the ack happen together or not at all. A full broker refuses the
// XADD (OOM) before EXEC, so nothing is applied and the entry stays pending.
//
// The dead-letter stream is capped at DLQ_MAX_LEN entries (Jira story F025). The broker
// runs noeviction, so an uncapped DLQ filled by a flood of unparseable
// changes would fill the broker and stop capture for every table. At the cap
// the oldest dead letter is dropped, counted in DLQDropped and logged as an
// ERROR: those rows need a snapshot. The dead-letter alarm (DLQDepth > 0)
// fires long before the cap.
func deadLetter(ctx context.Context, broker redis.UniversalClient, m redis.XMessage, stream, group, reason string,
	retries int64, cause error) error {
	val, _ := m.Values["value"].(string)
	// Why it failed, so the dead letter says what to fix before a replay.
	why := ""
	if cause != nil {
		why = cause.Error()
		if len(why) > 500 {
			why = why[:500]
		}
	}
	key, _ := m.Values["key"].(string)
	dlq := dlqPrefix + stream
	pipe := broker.TxPipeline()
	before := pipe.XLen(ctx, dlq)
	pipe.XAdd(ctx, &redis.XAddArgs{
		Stream: dlq,
		MaxLen: dlqMaxLen, // exact, so the number dropped is known
		Values: map[string]interface{}{
			"orig_id": m.ID,
			// The change's version (guard.go). A replay carries it back so the
			// replayed change cannot overwrite anything newer.
			"ecdl_v":        msgVersion(m),
			"source_stream": stream,
			"reason":        reason,
			"retries":       retries,
			"failed_at":     time.Now().UTC().Format(time.RFC3339),
			"error":         why,
			"key":           key,
			"value":         val,
		},
	})
	pipe.XAck(ctx, stream, group, m.ID)
	if !streamShared(stream) {
		pipe.XDel(ctx, stream, m.ID)
	}
	if _, err := pipe.Exec(ctx); err != nil {
		return err
	}
	atomic.AddInt64(&dlqSent, 1)
	logDeadLetter(stream, m.ID, reason, retries, why)
	if dropped := before.Val() + 1 - dlqMaxLen; dropped > 0 {
		atomic.AddInt64(&dlqDropped, dropped)
		logErr("dlq-full", fmt.Errorf("%s is at DLQ_MAX_LEN (%d): its oldest dead letter was dropped to make "+
			"room. Fix the cause, replay or purge the DLQ, and snapshot the table (ecdl signal snapshot)",
			dlq, dlqMaxLen))
	}
	return nil
}

// logDeadLetter is the log record of one dead letter (Jira story F094). Without it the
// log could not say which table, when or why, and `dlq list` loses that
// history once the dead letters are replayed. ERROR, so the sink-errors alarm
// counts it. Ids, reason and cause only: never the key or the row values.
func logDeadLetter(stream, id, reason string, deliveries int64, cause string) {
	if len(cause) > 200 {
		cause = cause[:200]
	}
	logErr("dead-letter", fmt.Errorf("%s %s reason=%s deliveries=%d: %s",
		stream, id, reason, deliveries, cause))
}

// ackAndTrim removes entries from the group's Pending Entries List (XACK) and
// from the stream (XDEL) in ONE transaction (MULTI/EXEC), so the two can never
// happen apart.
//
// The old code called XAck then XDel as two separate, error-ignored commands.
// Two failure modes resulted from that:
//   - XAck succeeds, XDel fails (or the task is killed in between): the entry is
//     acked (gone from the PEL) but stays RESIDENT in the stream forever. Nothing
//     re-reads it (XREADGROUP ">") and reclaim ignores it (not pending), so it is
//     a permanent orphan. This is the leak that accumulated 155 -> 296 entries
//     across restarts and caused the stale capture-lag reading.
//   - XDel succeeds, XAck fails: a phantom PEL entry for a vanished message.
//
// With MULTI/EXEC either both apply or neither does. On failure nothing changed,
// so the entry stays pending+resident and reclaimLoop reprocesses it; writes are
// idempotent (keyed by table:pk), so the replay is a harmless no-op. No loss, no
// orphan, and no crash window between the two commands.
func ackAndTrim(ctx context.Context, broker redis.UniversalClient, stream, group string, ids ...string) error {
	if len(ids) == 0 {
		return nil
	}
	pipe := broker.TxPipeline()
	pipe.XAck(ctx, stream, group, ids...)
	if !streamShared(stream) {
		pipe.XDel(ctx, stream, ids...)
	}
	_, err := pipe.Exec(ctx)
	return err
}

// sharedStreams marks streams whose XINFO GROUPS lists more than one consumer
// group (Jira story F026): a blue/green sink, or a group someone added for debugging.
// XDEL removes an entry for every group, so on such a stream an ack must not
// delete: the other group may not have read it yet. Acked entries are then
// removed by trimStream once EVERY group has acked them. Refreshed each
// reclaim cycle and at startup, so a group added later is noticed within
// RECLAIM_INTERVAL_MS (15s).
var sharedStreams sync.Map

func streamShared(stream string) bool {
	v, ok := sharedStreams.Load(stream)
	return ok && v.(bool)
}

func noteGroups(stream string, groups []redis.XInfoGroup) {
	shared := len(groups) > 1
	was := streamShared(stream)
	sharedStreams.Store(stream, shared)
	if shared != was {
		names := make([]string, 0, len(groups))
		for _, g := range groups {
			names = append(names, g.Name)
		}
		if shared {
			logWarn("shared-stream", fmt.Errorf("%s has %d consumer groups (%s): acked entries are kept until "+
				"every group has acked them, so broker memory and the lag metric follow the slowest group",
				stream, len(groups), strings.Join(names, ", ")))
		} else {
			logWarn("shared-stream", fmt.Errorf("%s has one consumer group again: acked entries are deleted at once", stream))
		}
	}
}

// safeTrimMinID computes the smallest stream ID that MUST be kept, so a periodic
// XTRIM MINID can evict fully-processed leftover entries (delivered AND acked but
// never XDEL'd) without ever touching an entry that is still pending or not yet
// delivered.
//
// Why this cannot lose data (the invariant):
//   - an entry with id  >  lastDelivered is UNDELIVERED            -> must keep
//   - an entry with id <= lastDelivered that is in the PEL is UNACKED -> must keep
//   - anything else (id <= lastDelivered, not pending) is delivered AND acked -> safe to drop
//
// The kept-minimum is therefore the oldest pending id when anything is pending,
// otherwise lastDelivered (keep it and everything newer). BOTH are <= lastDelivered,
// so the trim boundary can never reach an undelivered entry, and it can never
// pass the oldest pending entry. XTRIM MINID evicts ids strictly LOWER than the
// boundary and keeps the boundary itself, so a returned id is always retained.
//
// Returns "" (skip the trim) when nothing is safely known yet: no group position
// at all, i.e. nothing has ever been delivered.
func safeTrimMinID(oldestPendingID, lastDeliveredID string) string {
	if lastDeliveredID == "" || lastDeliveredID == "0-0" {
		if oldestPendingID != "" {
			return oldestPendingID
		}
		return ""
	}
	if oldestPendingID != "" && !versionNewer(oldestPendingID, lastDeliveredID) {
		return oldestPendingID
	}
	return lastDeliveredID
}

// trimStream removes delivered-and-acked leftover entries from one stream. It is
// the cleanup pass that makes the pipeline self-healing: even if an ackAndTrim ever
// failed to delete (it should not, being transactional), or entries were
// orphaned by an older build, this reclaims them on the next pass -- provably
// without deleting anything still outstanding (see safeTrimMinID).
//
// READ ORDER MATTERS. The group's last-delivered id is read FIRST and the
// pending list SECOND. It used to be the other way round: with nothing pending
// at the first read, a worker's XREADGROUP landing before the second read moved
// last-delivered past a batch that was delivered but not yet acked, and the
// trim deleted all of it but the last entry. Reclaim then found those entries
// gone and XCLAIM dropped them from the pending list with no error (Y5).
// Read in this order, every entry at or below the last-delivered id was
// delivered before the pending list was read, so it is either still listed
// there or acked; and the boundary is the lower of the two ids.
func trimStream(ctx context.Context, broker redis.UniversalClient, stream, group string) {
	lastDelivered := ""
	groups, err := broker.XInfoGroups(ctx, stream).Result()
	if err != nil {
		logErr("broker-trim", fmt.Errorf("%s: XINFO GROUPS: %w", stream, err))
		return
	}
	noteGroups(stream, groups)
	if len(groups) > 1 {
		trimShared(ctx, broker, stream, groups)
		return
	}
	for _, g := range groups {
		if g.Name == group {
			lastDelivered = g.LastDeliveredID
			break
		}
	}
	if betweenTrimReads != nil {
		betweenTrimReads()
	}
	sum, err := broker.XPending(ctx, stream, group).Result()
	if err != nil {
		logErr("broker-trim", fmt.Errorf("%s: XPENDING: %w", stream, err))
		return
	}
	oldestPending := ""
	if sum != nil && sum.Count > 0 {
		oldestPending = sum.Lower
	}
	minID := safeTrimMinID(oldestPending, lastDelivered)
	if minID == "" {
		return
	}
	if err := broker.XTrimMinID(ctx, stream, minID).Err(); err != nil {
		logErr("broker-trim", fmt.Errorf("%s: %w", stream, err))
	}
}

// trimShared is trimStream for a stream with several groups: the boundary is
// the lowest of each group's safe boundary, so nothing a group still needs is
// removed. A group that has delivered nothing yet keeps the whole stream.
// Groups were read first (above), pending lists second, as in trimStream.
func trimShared(ctx context.Context, broker redis.UniversalClient, stream string, groups []redis.XInfoGroup) {
	if betweenTrimReads != nil {
		betweenTrimReads()
	}
	minID := ""
	for _, g := range groups {
		sum, err := broker.XPending(ctx, stream, g.Name).Result()
		if err != nil {
			logErr("broker-trim", fmt.Errorf("%s: XPENDING %s: %w", stream, g.Name, err))
			return
		}
		oldest := ""
		if sum != nil && sum.Count > 0 {
			oldest = sum.Lower
		}
		id := safeTrimMinID(oldest, g.LastDeliveredID)
		if id == "" {
			return
		}
		if minID == "" || versionNewer(minID, id) {
			minID = id
		}
	}
	if err := broker.XTrimMinID(ctx, stream, minID).Err(); err != nil {
		logErr("broker-trim", fmt.Errorf("%s: %w", stream, err))
	}
}

// betweenTrimReads is nil except in tests, which deliver entries between the
// two reads of trimStream.
var betweenTrimReads func()

// applyFunc applies one message to the target. It exists so reclaimLoop can
// retry using THE SAME logic the live worker used for that message.
//
// This matters: the plain path maps an event with planMessage/processOne, while
// a table in a denormalize rule goes through processDenorm (parent embedding,
// reverse indexes, child updates). Retrying a denormalized message with the
// plain mapping would write the row without its embedded parent fields and skip
// the index maintenance, quietly producing a cache entry that differs from the
// one the fast path would have written.
type applyFunc func(ctx context.Context, target redis.UniversalClient, msg redis.XMessage, table string) error

// sinkApply picks the path per table, the same choice worker() makes.
func sinkApply(eventFormat string) applyFunc {
	return func(ctx context.Context, target redis.UniversalClient, msg redis.XMessage, table string) error {
		if usesDenorm(transformSpec, table) {
			return processDenorm(ctx, target, msg, table)
		}
		return processOne(ctx, target, msg, eventFormat, table)
	}
}

// reclaimLoop retries entries that were left unacked (failed writes) and
// dead-letters the ones that keep being refused. It runs on its own consumer
// name so it never competes with the live workers for new messages.
//
// It covers EVERY stream in the link.
//
// TARGET OUTAGES DO NOT DEAD-LETTER. The target is pinged (every shard, for a
// cluster) once per cycle; while it does not answer, nothing is claimed, so no
// delivery is added. Every claimed change is then APPLIED, including on its
// final delivery, and is dead-lettered only when the target refused that change
// itself (targetWide is false) after MAX_DELIVERIES. It used to be
// dead-lettered on the last delivery without being tried, after a PING that
// went to one random node -- so OOM, NOPERM or one shard failing over moved
// every affected change to the DLQ, and changes that piled up deliveries during
// an outage were dead-lettered right after it, against a healthy target.
//
// Plain tables are re-sent as one pipeline per stream (writeOps), not one
// round trip per change, so reclaim drains at fast-path speed.
//
// Retries are guarded by the same version check as the live path (guard.go), so
// a retry that runs after a newer change to the same row is skipped, not
// applied over it.
func reclaimLoop(ctx context.Context, broker, target redis.UniversalClient, specs []streamSpec, group, consumerPrefix, eventFormat string) {
	consumer := consumerPrefix + "-reclaim"
	gcIdle := time.Duration(atoiOr("CONSUMER_GC_IDLE_MS", 3600000)) * time.Millisecond
	tick := time.NewTicker(reclaimInterval)
	defer tick.Stop()
	cycle := 0
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		cycle++
		up := pingTarget(ctx, target)
		if up == nil {
			setTargetUp(true)
		} else {
			setTargetUp(false)
			logErr("target-down", fmt.Errorf("target does not answer; reclaim waits, nothing is dead-lettered: %w", up))
		}
		// Tombstones written from now on outlive the oldest waiting change. If
		// any stream could not be read the backlog is unknown, and the last
		// value is kept: shortening it then is exactly the wrong guess.
		var oldest int64
		var ageErr error
		for _, spec := range specs {
			age, err := oldestEntryAgeMs(ctx, broker, spec.key)
			if err != nil {
				ageErr = err
				break
			}
			if age > oldest {
				oldest = age
			}
		}
		if ageErr == nil {
			atomic.StoreInt64(&tombstoneTTLNow, tombstoneTTLFor(oldest))
		} else if ctx.Err() == nil {
			logErr("tombstone-ttl", fmt.Errorf("backlog age not read; keeping the tombstone TTL at %ds: %w",
				currentTombstoneTTL(), ageErr))
		}
		for _, spec := range specs {
			s := spec.key
			// Cleanup first: evict delivered-and-acked leftovers so the stream
			// stays trimmed to only outstanding work. Provably cannot touch
			// pending/undelivered entries.
			trimStream(ctx, broker, s, group)
			// Every ~5 minutes, drop consumers left behind by stopped tasks. Only
			// ones with NOTHING pending: deleting a consumer deletes its pending
			// entries, which would lose them.
			if cycle%20 == 1 {
				gcConsumers(ctx, broker, s, group, consumerPrefix, gcIdle)
			}
			if up != nil {
				continue
			}
			msgs, deliveries, err := claimIdle(ctx, broker, s, group, consumer)
			if err != nil {
				logErr("reclaim-claim", fmt.Errorf("%s: %w", s, err))
				continue
			}
			if len(msgs) > 0 {
				applyReclaimed(ctx, broker, target, s, group, spec.table, eventFormat, msgs, deliveries)
			}
		}
	}
}

// pingTarget asks every shard of a cluster, not one random node.
func pingTarget(ctx context.Context, target redis.UniversalClient) error {
	pctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if cc, ok := target.(*redis.ClusterClient); ok {
		return cc.ForEachShard(pctx, func(ctx context.Context, c *redis.Client) error {
			return c.Ping(ctx).Err()
		})
	}
	return target.Ping(pctx).Err()
}

// applyReclaimed applies claimed entries of one stream and acks what is done.
func applyReclaimed(ctx context.Context, broker, target redis.UniversalClient, stream, group, table, eventFormat string,
	msgs []redis.XMessage, deliveries map[string]int64) {
	var ackIDs []string
	var nRetried int64
	// failed decides what happens to a change that was not applied.
	failed := func(m redis.XMessage, err error) {
		retries := deliveries[m.ID]
		switch {
		case errors.Is(err, errPoison):
			if e := deadLetter(ctx, broker, m, stream, group, "poison", retries, err); e != nil {
				logErr("dlq-write", fmt.Errorf("%s %s left pending: %w", stream, m.ID, e))
			}
		case errors.Is(err, errWaitShortfall):
			// Applied, but its replicas have not confirmed it (replwait.go):
			// keep it pending for the next cycle; never dead-letter. Counted and
			// logged where the wait fell short.
		case targetWide(err):
			// The target, not the change: keep it pending; never dead-letter.
			markWriteErr()
			logErr("reclaim-write", fmt.Errorf("%s %s held (target unavailable): %w", stream, m.ID, err))
		case retries >= maxDeliveries:
			if e := deadLetter(ctx, broker, m, stream, group, "max_retries", retries, err); e != nil {
				logErr("dlq-write", fmt.Errorf("%s %s left pending: %w", stream, m.ID, e))
			}
		default:
			markWriteErr()
			logErr("reclaim-write", fmt.Errorf("%s %s (delivery %d of %d): %w",
				stream, m.ID, retries+1, maxDeliveries+1, err))
		}
	}
	if usesDenorm(transformSpec, table) {
		var applied []string
		for _, m := range msgs {
			if err := processDenormRetry(ctx, target, m, table); err != nil {
				failed(m, err)
				continue
			}
			markWriteOK()
			applied = append(applied, m.ID)
		}
		// One replica wait for everything applied above (replwait.go).
		if len(applied) > 0 {
			if err := confirmReplicated(ctx, target); err != nil {
				noteShortfall(len(applied), err)
				applied = nil
			}
		}
		ackIDs = append(ackIDs, applied...)
		nRetried += int64(len(applied))
	} else {
		ops := make([]targetOp, 0, len(msgs))
		opMsg := make([]redis.XMessage, 0, len(msgs))
		// Truncates first: the version guard and the truncate marker keep the
		// other changes right whichever runs first (truncate.go).
		var truncated []string
		for _, m := range msgs {
			t, ttable := isTruncate(m, eventFormat)
			if !t {
				continue
			}
			if err := truncateOne(ctx, target, stream, table, m, ttable); err != nil {
				failed(m, err)
				continue
			}
			truncated = append(truncated, m.ID)
		}
		if len(truncated) > 0 {
			if err := confirmReplicated(ctx, target); err != nil {
				noteShortfall(len(truncated), err)
			} else {
				ackIDs = append(ackIDs, truncated...)
				nRetried += int64(len(truncated))
			}
		}
		for _, m := range msgs {
			if t, _ := isTruncate(m, eventFormat); t {
				continue
			}
			key, fields, isDel, poison, replace := planMessage(m, eventFormat, table)
			if poison {
				failed(m, errPoison)
				continue
			}
			ops = append(ops, targetOp{id: m.ID, version: msgVersion(m), key: key,
				table: table, fields: fields, replace: replace, del: isDel})
			opMsg = append(opMsg, m)
		}
		if len(ops) > 0 {
			stale := 0
			res := writeOps(ctx, target, ops)
			for i, c := range res.cmds {
				if c == nil {
					err := res.errs[i]
					if err == nil {
						err = res.lastErr
					}
					failed(opMsg[i], err)
					continue
				}
				if applied(c) {
					if ops[i].del {
						atomic.AddInt64(&deletes, 1)
					} else {
						atomic.AddInt64(&writes, 1)
					}
				} else {
					stale++
				}
				ackIDs = append(ackIDs, opMsg[i].ID)
				nRetried++
			}
			warnStale(stream, stale)
			atomic.AddInt64(&writesRefused, int64(res.refusedCount()))
			if res.unconfirmed() < len(ops) {
				markWriteOK()
			}
		}
	}
	if len(ackIDs) == 0 {
		return
	}
	if err := ackAndTrim(ctx, broker, stream, group, ackIDs...); err != nil {
		logErr("broker-ack", fmt.Errorf("%s: %d reclaimed entr(ies) stay pending: %w", stream, len(ackIDs), err))
		return
	}
	atomic.AddInt64(&retried, nRetried)
}

var pendingVanished int64 // pending entries found deleted from the stream (lost changes)

// claimIdle claims up to RECLAIM_BATCH entries idle for RETRY_IDLE and returns
// them with the number of earlier deliveries of each.
//
// XAUTOCLAIM is sent raw because its third reply element, the ids that were
// pending but no longer exist in the stream, is discarded by go-redis. Such an
// entry is a change that was never applied and can no longer be read; XCLAIM
// used to drop it from the pending list without a word. Now it is an ERROR
// line and the PendingVanished metric.
func claimIdle(ctx context.Context, broker redis.UniversalClient, stream, group, consumer string) ([]redis.XMessage, map[string]int64, error) {
	raw, err := broker.Do(ctx, "XAUTOCLAIM", stream, group, consumer,
		retryIdle.Milliseconds(), "0-0", "COUNT", reclaimBatch).Result()
	if err != nil {
		return nil, nil, err
	}
	msgs, deleted, err := parseAutoClaim(raw)
	if err != nil {
		return nil, nil, err
	}
	if len(deleted) > 0 {
		atomic.AddInt64(&pendingVanished, int64(len(deleted)))
		logErr("pending-vanished", fmt.Errorf("%s: %d pending entr(ies) were deleted from the stream before "+
			"they were applied; these changes are lost and the affected rows need a reload or snapshot: %s",
			stream, len(deleted), strings.Join(deleted, ",")))
	}
	if len(msgs) == 0 {
		return nil, nil, nil
	}
	// Delivery counts, read after the claim (which added one).
	pend, err := broker.XPendingExt(ctx, &redis.XPendingExtArgs{
		Stream: stream, Group: group, Consumer: consumer,
		Start: msgs[0].ID, End: msgs[len(msgs)-1].ID, Count: int64(len(msgs)) + reclaimBatch,
	}).Result()
	if err != nil {
		return nil, nil, err
	}
	deliveries := make(map[string]int64, len(pend))
	for _, p := range pend {
		deliveries[p.ID] = p.RetryCount - 1
	}
	return msgs, deliveries, nil
}

// parseAutoClaim reads [cursor, entries, deleted-ids] (deleted-ids from 7.0).
func parseAutoClaim(raw interface{}) ([]redis.XMessage, []string, error) {
	top, ok := raw.([]interface{})
	if !ok || len(top) < 2 {
		return nil, nil, fmt.Errorf("unexpected XAUTOCLAIM reply %T", raw)
	}
	entries, ok := top[1].([]interface{})
	if !ok {
		return nil, nil, fmt.Errorf("unexpected XAUTOCLAIM entries %T", top[1])
	}
	msgs := make([]redis.XMessage, 0, len(entries))
	for _, e := range entries {
		pair, ok := e.([]interface{})
		if !ok || len(pair) != 2 {
			continue // a nil entry (Redis 6) is a deleted id; 7.0+ lists those separately
		}
		id, _ := pair[0].(string)
		vals := map[string]interface{}{}
		switch fv := pair[1].(type) {
		case []interface{}:
			for i := 0; i+1 < len(fv); i += 2 {
				k, _ := fv[i].(string)
				vals[k] = fv[i+1]
			}
		case map[interface{}]interface{}:
			for k, v := range fv {
				ks, _ := k.(string)
				vals[ks] = v
			}
		}
		msgs = append(msgs, redis.XMessage{ID: id, Values: vals})
	}
	var deleted []string
	if len(top) >= 3 {
		if ids, ok := top[2].([]interface{}); ok {
			for _, x := range ids {
				if s, ok := x.(string); ok {
					deleted = append(deleted, s)
				}
			}
		}
	}
	return msgs, deleted, nil
}

// consumersToDelete picks the consumers that are safe to remove: idle longer
// than `idle`, with nothing pending, and not one of this task's own.
func consumersToDelete(consumers []redis.XInfoConsumer, idle time.Duration, ownPrefix string) []string {
	var out []string
	for _, c := range consumers {
		if c.Pending > 0 || c.Idle < idle {
			continue
		}
		if ownPrefix != "" && strings.HasPrefix(c.Name, ownPrefix+"-") {
			continue
		}
		out = append(out, c.Name)
	}
	return out
}

// gcConsumers removes consumers left by stopped tasks. Each task registers
// WORKERS+1 names that were never deleted, so a group accumulated hundreds and
// the dashboard's "consumers" count was meaningless.
//
// The check and the delete are one script (Jira story F044). They used to be XINFO
// CONSUMERS here and XGROUP DELCONSUMER later: a live task's reclaim
// consumer that claimed entries in between was deleted with them, and those
// changes were no longer pending anywhere.
func gcConsumers(ctx context.Context, broker redis.UniversalClient, stream, group, ownPrefix string, idle time.Duration) {
	cons, err := broker.XInfoConsumers(ctx, stream, group).Result()
	if err != nil {
		logErr("consumer-gc", err)
		return
	}
	names := consumersToDelete(cons, idle, ownPrefix)
	if len(names) > 0 && betweenGCReads != nil {
		betweenGCReads()
	}
	for _, name := range names {
		if err := gcConsumerScript.Run(ctx, broker, []string{stream}, group, name, idle.Milliseconds()).Err(); err != nil {
			logErr("consumer-gc", fmt.Errorf("%s: %s: %w", stream, name, err))
		}
	}
}

// gcConsumerScript deletes one consumer only if, at that moment, it has
// nothing pending and has been idle at least ARGV[3] ms. Returns 1 when it
// deleted it, 0 when it kept it.
var gcConsumerScript = redis.NewScript(`
local info = redis.call('XINFO', 'CONSUMERS', KEYS[1], ARGV[1])
for _, c in ipairs(info) do
  local f = {}
  for i = 1, #c, 2 do f[c[i]] = c[i + 1] end
  if f['name'] == ARGV[2] then
    if tonumber(f['pending']) == 0 and tonumber(f['idle']) >= tonumber(ARGV[3]) then
      redis.call('XGROUP', 'DELCONSUMER', KEYS[1], ARGV[1], ARGV[2])
      return 1
    end
    return 0
  end
end
return 0
`)

// betweenGCReads is nil except in tests, which claim entries between the
// consumer listing and the delete.
var betweenGCReads func()
