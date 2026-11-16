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

	dlqSent int64 // messages routed to the DLQ
	retried int64 // messages that succeeded on a retry
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
	out, keep := transformSpec.Apply(evt.Source.Table, stringifyRow(evt.After))
	if !keep {
		return key, nil, true, false, false // filtered out / pushed out of scope -> delete
	}
	f := make(map[string]interface{}, len(out))
	for k, v := range out {
		f[k] = v
	}
	return key, f, false, false, evt.Op == "r"
}

// processOne applies a single message to the target. Returns:
//   - nil        on success
//   - errPoison  if the message is unprocessable (dead-letter it)
//   - other err  on a transient write failure (leave for another retry)
func processOne(ctx context.Context, target redis.UniversalClient, msg redis.XMessage, eventFormat, streamTable string) error {
	key, fields, isDelete, poison, replace := planMessage(msg, eventFormat, streamTable)
	if poison {
		return errPoison
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

// toDLQ writes a failed message to the dead-letter stream for its source stream,
// preserving the original payload and adding failure metadata so it can be
// inspected and replayed.
//
// It RETURNS THE ERROR and the caller MUST NOT ack/trim the original until this
// has succeeded. The DLQ is the only durable record of a message that could not
// be applied; if the XADD fails and we ack+delete the original anyway, that
// change is gone from both the stream and the DLQ -- a silent loss. On failure
// the caller leaves the entry pending so it is retried (and dead-lettered again)
// on the next cycle.
func toDLQ(ctx context.Context, broker redis.UniversalClient, m redis.XMessage, sourceStream, reason string, retries int64) error {
	val, _ := m.Values["value"].(string)
	key, _ := m.Values["key"].(string)
	return broker.XAdd(ctx, &redis.XAddArgs{
		Stream: dlqPrefix + sourceStream,
		Values: map[string]interface{}{
			"orig_id": m.ID,
			// The change's version (guard.go). A replay carries it back so the
			// replayed change cannot overwrite anything newer.
			"ecdl_v":        msgVersion(m),
			"source_stream": sourceStream,
			"reason":        reason,
			"retries":       retries,
			"failed_at":     time.Now().UTC().Format(time.RFC3339),
			"key":           key,
			"value":         val,
		},
	}).Err()
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
	pipe.XDel(ctx, stream, ids...)
	_, err := pipe.Exec(ctx)
	return err
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
	if oldestPendingID != "" {
		return oldestPendingID
	}
	if lastDeliveredID == "" || lastDeliveredID == "0-0" {
		return ""
	}
	return lastDeliveredID
}

// trimStream removes delivered-and-acked leftover entries from one stream. It is
// the cleanup pass that makes the pipeline self-healing: even if an ackAndTrim ever
// failed to delete (it should not, being transactional), or entries were
// orphaned by an older build, this reclaims them on the next pass -- provably
// without deleting anything still outstanding (see safeTrimMinID).
func trimStream(ctx context.Context, broker redis.UniversalClient, stream, group string) {
	sum, err := broker.XPending(ctx, stream, group).Result()
	if err != nil {
		return
	}
	oldestPending := ""
	if sum != nil && sum.Count > 0 {
		oldestPending = sum.Lower
	}
	lastDelivered := ""
	groups, err := broker.XInfoGroups(ctx, stream).Result()
	if err != nil {
		return
	}
	for _, g := range groups {
		if g.Name == group {
			lastDelivered = g.LastDeliveredID
			break
		}
	}
	minID := safeTrimMinID(oldestPending, lastDelivered)
	if minID == "" {
		return
	}
	if err := broker.XTrimMinID(ctx, stream, minID).Err(); err != nil {
		logErr("broker-trim", err)
	}
}

// applyFunc applies one message to the target. It exists so reclaimLoop can
// retry using THE SAME logic the live worker used for that message.
//
// This matters: the plain path maps an event with planMessage/processOne, while
// the denormalization path fans out through processMsg (parent embedding, reverse
// indexes, child updates). Retrying a denormalized message with the plain mapping
// would write the row without its embedded parent fields and skip the index
// maintenance, quietly producing a cache entry that differs from the one the
// fast path would have written.
type applyFunc func(ctx context.Context, target redis.UniversalClient, msg redis.XMessage, table string) error

// plainApply is the retry path for the ordinary (non-denormalizing) sink.
func plainApply(eventFormat string) applyFunc {
	return func(ctx context.Context, target redis.UniversalClient, msg redis.XMessage, table string) error {
		return processOne(ctx, target, msg, eventFormat, table)
	}
}

// denormApply is the retry path when the spec has denormalization rules. The
// table is ignored because processMsg reads it from the event envelope, which
// denormalization requires anyway.
func denormApply() applyFunc {
	return func(ctx context.Context, target redis.UniversalClient, msg redis.XMessage, _ string) error {
		return processMsg(ctx, target, msg)
	}
}

// reclaimLoop retries entries that were left unacked (failed writes) and
// dead-letters the ones that exhaust MAX_DELIVERIES. It runs on its own
// consumer name so it never competes with the live workers for new messages.
//
// It covers EVERY stream in the link. It used to be started with
// []string{streamKey} -- the first stream only -- and was skipped entirely when
// denormalization was on.
//
// TARGET OUTAGES DO NOT DEAD-LETTER. MAX_DELIVERIES x RETRY_IDLE is about three
// minutes, so a target outage longer than that used to move every pending
// change into the DLQ, from where nothing replayed it. A change that failed
// because the target was DOWN is not a bad change, so before dead-lettering
// for max retries the target is pinged; if it does not answer, the entries are
// HELD (left pending, retried next cycle) and only a change that keeps failing
// against a target that IS up is dead-lettered.
//
// Retries are guarded by the same version check as the live path (guard.go), so
// a retry that runs after a newer change to the same row is skipped, not
// applied over it.
func reclaimLoop(ctx context.Context, broker, target redis.UniversalClient, specs []streamSpec, group, consumerPrefix string, apply applyFunc) {
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
		targetUp := true
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
			pend, err := broker.XPendingExt(ctx, &redis.XPendingExtArgs{
				Stream: s, Group: group, Idle: retryIdle,
				Start: "-", End: "+", Count: reclaimBatch,
			}).Result()
			if err != nil {
				logErr("reclaim-pending", fmt.Errorf("%s: %w", s, err))
				continue
			}
			if len(pend) == 0 {
				continue
			}
			// One XCLAIM for the whole batch, rather than one round trip per
			// entry: reclaim used to manage ~13 entries/s per stream per task.
			ids := make([]string, len(pend))
			deliveries := make(map[string]int64, len(pend))
			for i, p := range pend {
				ids[i] = p.ID
				deliveries[p.ID] = p.RetryCount
			}
			msgs, err := broker.XClaim(ctx, &redis.XClaimArgs{
				Stream: s, Group: group, Consumer: consumer,
				MinIdle: retryIdle, Messages: ids,
			}).Result()
			if err != nil {
				logErr("reclaim-claim", fmt.Errorf("%s: %w", s, err))
				continue
			}
			// Is the target answering? Asked once per stream per cycle, and only
			// when something is about to be dead-lettered for max retries.
			checkedTarget := false
			for _, m := range msgs {
				retries := deliveries[m.ID]
				if retries > maxDeliveries {
					if !checkedTarget {
						checkedTarget = true
						if err := target.Ping(ctx).Err(); err != nil {
							targetUp = false
							logErr("dlq-hold", fmt.Errorf("target unreachable, holding %s entries "+
								"pending instead of dead-lettering: %w", s, err))
						}
					}
					if !targetUp {
						continue
					}
					// Exhausted retries against a target that is up: dead-letter.
					// Only ack+trim once the DLQ write is DURABLE.
					if err := toDLQ(ctx, broker, m, s, "max_retries", retries); err != nil {
						logErr("dlq-write", err)
						continue
					}
					if err := ackAndTrim(ctx, broker, s, group, m.ID); err == nil {
						atomic.AddInt64(&dlqSent, 1)
					} else {
						logErr("broker-ack", err)
					}
					continue
				}

				switch err := apply(ctx, target, m, spec.table); {
				case err == nil:
					markWriteOK()
					if err := ackAndTrim(ctx, broker, s, group, m.ID); err == nil {
						atomic.AddInt64(&retried, 1)
					} else {
						logErr("broker-ack", err)
					}
				case errors.Is(err, errPoison):
					if err := toDLQ(ctx, broker, m, s, "poison", retries); err != nil {
						logErr("dlq-write", err)
						continue
					}
					if err := ackAndTrim(ctx, broker, s, group, m.ID); err == nil {
						atomic.AddInt64(&dlqSent, 1)
					} else {
						logErr("broker-ack", err)
					}
				default:
					// Transient: leave it pending; the next cycle claims it again.
					markWriteErr()
					logErr("reclaim-write", fmt.Errorf("%s %s (delivery %d of %d): %w",
						s, m.ID, retries, maxDeliveries, err))
				}
			}
		}
	}
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
func gcConsumers(ctx context.Context, broker redis.UniversalClient, stream, group, ownPrefix string, idle time.Duration) {
	cons, err := broker.XInfoConsumers(ctx, stream, group).Result()
	if err != nil {
		logErr("consumer-gc", err)
		return
	}
	for _, name := range consumersToDelete(cons, idle, ownPrefix) {
		if err := broker.XGroupDelConsumer(ctx, stream, group, name).Err(); err != nil {
			logErr("consumer-gc", err)
		}
	}
}
