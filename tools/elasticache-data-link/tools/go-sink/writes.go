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
	"io"
	"net"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"
)

var (
	writeRetries   = atoiOr("WRITE_RETRIES", 3)
	writeRetryBase = time.Duration(atoiOr("WRITE_RETRY_BASE_MS", 100)) * time.Millisecond

	writeResends      int64 // pipelines re-sent after a failure
	writesRecovered   int64 // changes confirmed by a re-send
	writesUnconfirmed int64 // changes left pending after every immediate attempt
	writesRefused     int64 // of those, changes the target refused for themselves
)

// targetOp is one change to apply: an upsert, or a delete when del is set.
type targetOp struct {
	id, version, key string
	table            string // the stream's table: its truncate marker (truncate.go)
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

// targetWide reports whether err is a condition of the target as a whole, not
// of one change: no connection, a node loading or failing over, the cluster
// down or resharding, out of memory, a permission or credential change. Such a
// change is not bad; it is held and retried until the target recovers, never
// dead-lettered. A refusal of the change itself (WRONGTYPE, a script error on
// its data) is not target-wide, and goes to reclaim and then the DLQ. A
// replica wait that fell short is neither: the write was applied, and the
// change waits in the pending list for reclaim (replwait.go).
func targetWide(err error) bool {
	if err == nil || errors.Is(err, errWaitShortfall) {
		return false
	}
	if !isServerReply(err) {
		return isTransport(err)
	}
	msg := err.Error()
	for _, p := range []string{"LOADING", "TRYAGAIN", "CLUSTERDOWN", "MASTERDOWN",
		"READONLY", "BUSY", "NOSCRIPT", "OOM", "NOPERM", "NOAUTH", "WRONGPASS",
		"MOVED", "ASK"} {
		if strings.HasPrefix(msg, p) {
			return true
		}
	}
	return false
}

// isTransport reports a failure to talk to the server: EOF, reset, refused,
// timeout, a closed or exhausted pool. Anything else that is not a server reply
// (a poison message, a malformed script reply) is a problem of the change.
func isTransport(err error) bool {
	if err == nil || errors.Is(err, errPoison) {
		return false
	}
	var ne net.Error
	if errors.As(err, &ne) || errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) ||
		errors.Is(err, context.DeadlineExceeded) || errors.Is(err, syscall.ECONNRESET) ||
		errors.Is(err, syscall.ECONNREFUSED) || errors.Is(err, syscall.EPIPE) ||
		errors.Is(err, redis.ErrClosed) {
		return true
	}
	msg := err.Error()
	for _, p := range []string{"connection reset", "connection refused", "broken pipe",
		"use of closed network connection", "i/o timeout", "no such host", "EOF",
		"connection pool timeout"} {
		if strings.Contains(msg, p) {
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
	errs     []error       // per op: why it is unconfirmed (nil when confirmed)
	// waitShort counts ops the primary applied but too few replicas confirmed
	// (replwait.go). They are unconfirmed, but not a failed write.
	waitShort int
}

// held reports the ops left unconfirmed by a condition of the whole target
// (unreachable, failing over, out of memory, permissions), as opposed to a
// refusal of that one change. See targetWide.
func (r writeResult) held() []int {
	var out []int
	for i, c := range r.cmds {
		if c == nil && targetWide(r.errs[i]) {
			out = append(out, i)
		}
	}
	return out
}

// refusedCount is the ops the target refused for themselves (WRONGTYPE, a
// script error on the row's data): left pending for reclaim and the DLQ, but
// not a sign that the target is down (Jira story F040).
func (r writeResult) refusedCount() int {
	n := 0
	for i, c := range r.cmds {
		if e := r.errs[i]; c == nil && isServerReply(e) && !retryableReply(e) && !targetWide(e) {
			n++
		}
	}
	return n
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
// With a replica wait configured (replwait.go) an op counts as confirmed only
// once its primary's replicas hold it.
//
// An op older than its table's last truncate is not sent: it is reported as a
// stale skip (truncate.go).
func writeOps(ctx context.Context, target redis.UniversalClient, ops []targetOp) writeResult {
	res := writeResult{cmds: make([]redis.Cmder, len(ops)), errs: make([]error, len(ops))}
	live := truncateFilter(ctx, target, ops, &res)
	if len(live) == 0 {
		return res
	}
	sub := ops
	if len(live) < len(ops) {
		sub = make([]targetOp, len(live))
		for j, i := range live {
			sub[j] = ops[i]
		}
	}
	r := replication.request()
	got := writeOpsOnce(r.context(ctx), target, sub)
	r.apply(&got)
	if len(live) == len(ops) && res.lastErr == nil {
		return got
	}
	for j, i := range live {
		res.cmds[i], res.errs[i] = got.cmds[j], got.errs[j]
	}
	res.sends, res.firstErr, res.refused, res.waitShort = got.sends, got.firstErr, got.refused, got.waitShort
	if got.lastErr != nil {
		res.lastErr = got.lastErr
	}
	return res
}

func writeOpsOnce(ctx context.Context, target redis.UniversalClient, ops []targetOp) writeResult {
	res := writeResult{cmds: make([]redis.Cmder, len(ops)), errs: make([]error, len(ops))}
	// A cluster pipeline marks every command of a node that failed, so on the
	// other nodes a command without an error was answered, even when Exec
	// reports a transport error. Commands used to be confirmed only when the
	// error was a server reply, so one unreachable shard left the writes for
	// every shard unconfirmed. A single-node client can leave unsent commands
	// unmarked (a failed dial), so there it is still all or nothing.
	_, cluster := target.(*redis.ClusterClient)
	pending := make([]int, len(ops))
	for i := range ops {
		pending[i] = i
	}
	scriptsLoaded := false
	start := time.Now()
	for attempt := 0; len(pending) > 0; attempt++ {
		if attempt > 0 {
			if attempt > writeRetries {
				break
			}
			// Re-send only while the whole call stays under half of
			// RETRY_IDLE_MS (Jira story F046). Beyond that, reclaim on another task can
			// claim this batch while it is still being written, which adds a
			// delivery to every entry and moves them toward the DLQ early.
			// What is left unconfirmed waits for reclaim instead.
			backoff := writeRetryBase << (attempt - 1)
			if time.Since(start)+backoff >= retryIdle/2 {
				break
			}
			atomic.AddInt64(&writeResends, 1)
			sleep(backoff)
		}
		res.sends++
		pipe := target.Pipeline()
		cmds := make([]redis.Cmder, len(pending))
		for j, i := range pending {
			cmds[j] = queueOp(ctx, pipe, ops[i])
		}
		// Do NOT ignore the aggregate error. When a single-node pipeline failed
		// (connection drop, failed dial) a command's Err() can be nil although
		// its write never reached the target, so there per-command results are
		// trusted only when the error came back from the server. A cluster
		// pipeline marks every command it could not confirm (see above).
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
				for _, i := range pending {
					res.errs[i] = execErr
				}
				break
			}
		}
		trusted := isServerReply(execErr) || cluster
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
				res.errs[i] = err
				// A definite refusal. Left pending: reclaimLoop retries it and,
				// if it keeps failing against a target that is up, dead-letters it.
			default:
				res.errs[i] = err
				if err == nil {
					res.errs[i] = execErr
				}
				next = append(next, i)
			}
		}
		pending = next
	}
	return res
}
