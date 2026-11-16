package main

// Writing one batch to the target, with an immediate retry.
//
// Before this, a batch whose pipeline failed was left entirely in the pending
// list for reclaimLoop, which picks entries up only after RETRY_IDLE_MS (30 s)
// on a RECLAIM_INTERVAL_MS (15 s) tick. Two things made that worse than it
// needed to be:
//
//   - A dropped connection is usually fixed by sending again on a fresh one,
//     milliseconds later. The end-to-end test saw a single "EOF" after an idle
//     period delay a change by 30-45 s.
//   - Exec returns the first error of any command, so one refused command (a
//     WRONGTYPE on one key) held back every other change in the batch, although
//     the server had applied them.
//
// Now: a transport failure (EOF, reset, timeout) re-sends the unconfirmed
// commands up to WRITE_RETRIES times with doubling backoff from
// WRITE_RETRY_BASE_MS. When the server answered, each command's own reply
// decides: applied commands are confirmed, a retryable refusal (LOADING,
// TRYAGAIN, CLUSTERDOWN, NOSCRIPT ...) is re-sent, and a definite refusal is
// left pending for reclaimLoop, which retries it MAX_DELIVERIES times and then
// dead-letters it. Re-sending is safe: writes are keyed by table:pk and the
// version guard skips a change that is not newer than the stored one.

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
)

var (
	writeRetries   = atoiOr("WRITE_RETRIES", 3)
	writeRetryBase = time.Duration(atoiOr("WRITE_RETRY_BASE_MS", 100)) * time.Millisecond

	writeResends      int64 // pipelines re-sent after a failure
	writesRecovered   int64 // changes confirmed by a re-send
	writesUnconfirmed int64 // changes left pending after every immediate attempt
)

// targetOp is one change to apply: an upsert, or a delete when del is set.
type targetOp struct {
	id, version, key string
	fields           map[string]interface{}
	replace, del     bool
}

func queueOp(ctx context.Context, pipe redis.Pipeliner, op targetOp) redis.Cmder {
	if op.del {
		if versionGuard {
			return queueDelete(ctx, pipe, op.key, op.version)
		}
		return pipe.Del(ctx, op.key)
	}
	if versionGuard {
		return queueUpsert(ctx, pipe, op.key, op.version, op.replace, op.fields)
	}
	// Unguarded: a snapshot read replaces the key so stale fields from an old
	// shape are dropped; the DEL is sent in the same pipeline.
	if op.replace {
		pipe.Del(ctx, op.key)
	}
	return pipe.HSet(ctx, op.key, op.fields)
}

// isServerReply reports whether err is an error reply from the server, as
// opposed to a failure to talk to it. Only after a server reply can the other
// commands' replies in the same pipeline be trusted.
func isServerReply(err error) bool {
	var re redis.Error
	return err != nil && errors.As(err, &re)
}

// retryableReply is a refusal that a re-send moments later can succeed: the node
// is loading, failing over, resharding, or lost its script cache.
func retryableReply(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	for _, p := range []string{"LOADING", "TRYAGAIN", "CLUSTERDOWN", "MASTERDOWN",
		"READONLY", "BUSY", "NOSCRIPT"} {
		if strings.HasPrefix(msg, p) {
			return true
		}
	}
	return false
}

// writeResult is the outcome of writeOps.
type writeResult struct {
	cmds     []redis.Cmder // per op: its reply if confirmed, nil if not
	sends    int           // pipelines sent, 1 when nothing failed
	firstErr error         // the error that triggered the first re-send
	lastErr  error         // why something is unconfirmed; nil when everything was confirmed
	refused  error         // the last definite refusal, if any command got one
}

func (r writeResult) unconfirmed() int {
	n := 0
	for _, c := range r.cmds {
		if c == nil {
			n++
		}
	}
	return n
}

var sleep = time.Sleep // replaced in tests

// writeOps applies ops in order and returns which of them the target confirmed.
func writeOps(ctx context.Context, target redis.UniversalClient, ops []targetOp) writeResult {
	res := writeResult{cmds: make([]redis.Cmder, len(ops))}
	pending := make([]int, len(ops))
	for i := range ops {
		pending[i] = i
	}
	scriptsLoaded := false
	for attempt := 0; len(pending) > 0; attempt++ {
		if attempt > 0 {
			if attempt > writeRetries {
				break
			}
			atomic.AddInt64(&writeResends, 1)
			sleep(writeRetryBase << (attempt - 1))
		}
		res.sends++
		pipe := target.Pipeline()
		cmds := make([]redis.Cmder, len(pending))
		for j, i := range pending {
			cmds[j] = queueOp(ctx, pipe, ops[i])
		}
		// Do NOT ignore the aggregate error. When the pipeline itself failed
		// (connection drop, partial cluster failure) a command's Err() can be nil
		// although its write never reached the target, so per-command results
		// are trusted only when the error came back from the server.
		_, execErr := pipe.Exec(ctx)
		if execErr == nil {
			for j, i := range pending {
				res.cmds[i] = cmds[j]
			}
			if attempt > 0 {
				atomic.AddInt64(&writesRecovered, int64(len(pending)))
			}
			res.lastErr = res.refused
			return res
		}
		if res.firstErr == nil {
			res.firstErr = execErr
		}
		res.lastErr = execErr
		// A node that lost its script cache (failover, restart, new shard)
		// answers NOSCRIPT. Load the scripts once; the re-send runs them.
		if versionGuard && isNoScript(execErr) && !scriptsLoaded {
			scriptsLoaded = true
			if err := loadScripts(ctx, target); err != nil {
				logErr("target-script-load", err)
				break
			}
		}
		trusted := isServerReply(execErr)
		next := make([]int, 0, len(pending))
		for j, i := range pending {
			err := cmds[j].Err()
			switch {
			case trusted && err == nil:
				res.cmds[i] = cmds[j]
				if attempt > 0 {
					atomic.AddInt64(&writesRecovered, 1)
				}
			case trusted && isServerReply(err) && !retryableReply(err):
				res.refused = err
				// A definite refusal. Left pending: reclaimLoop retries it and,
				// if it keeps failing against a target that is up, dead-letters it.
			default:
				next = append(next, i)
			}
		}
		pending = next
	}
	return res
}
