package main

// Holding changes while the target is down (Y10, Y12).
//
// WHAT WAS WRONG
//   - A worker whose batch failed against an unreachable target read the next
//     batch at once. Within seconds the whole stream sat in the pending list,
//     and the only way out was reclaim at about 13 entries/s per stream per
//     task: a one-minute outage at 500 changes/s took ~19 minutes to drain.
//   - Every claim added a delivery, so after a long outage the changes that had
//     piled up deliveries were dead-lettered on the first cycle after recovery,
//     against a healthy target, without a single retry.
//   - /health failed after 120 s of failed writes, so ECS replaced the task; the
//     replacement could not reach the target and exited FATAL, and the service
//     crash-looped. No task was left to publish the lag metric, which the alarm
//     treated as OK.
//
// NOW
//   - A batch left unconfirmed by a condition of the whole target (targetWide)
//     is HELD: the worker retries it with back-off from HOLD_BASE_MS to
//     HOLD_MAX_MS and reads nothing more until it is applied. The pending list
//     stays at most WORKERS x BATCH_SIZE per task; the rest waits in the stream.
//   - A refusal of one change (WRONGTYPE, ...) is not held; it goes to reclaim,
//     and to the DLQ after MAX_DELIVERIES, as before.
//   - While anything is held the task stays healthy: replacing it cannot fix
//     the target. TargetUp is 0, an ERROR line is logged every 10 s, the
//     freshness heartbeat stops, and the lag metric keeps growing.
//   - On shutdown a hold stops; its changes stay pending for another task.

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

var (
	holdBase = time.Duration(atoiOr("HOLD_BASE_MS", 500)) * time.Millisecond
	holdMax  = time.Duration(atoiOr("HOLD_MAX_MS", 30000)) * time.Millisecond

	holding        int64     // workers (and startup) currently waiting for the target
	metricTargetUp int64 = 1 // 1 while the target answers, published as TargetUp
	stoppingFlag   atomic.Bool
	heldTotal      int64 // changes that had to be held at least once
)

func sinkStopping() bool { return stoppingFlag.Load() }

func setTargetUp(up bool) {
	if up {
		atomic.StoreInt64(&metricTargetUp, 1)
	} else {
		atomic.StoreInt64(&metricTargetUp, 0)
	}
}

func isHolding() bool { return atomic.LoadInt64(&holding) > 0 }

// holdSleep waits d, waking early on shutdown. It sleeps through `sleep` so
// tests can replace the clock.
func holdSleep(d time.Duration) {
	const step = 100 * time.Millisecond
	for d > 0 && !sinkStopping() {
		s := step
		if d < s {
			s = d
		}
		sleep(s)
		d -= s
	}
}

func nextBackoff(d time.Duration) time.Duration {
	d *= 2
	if d > holdMax {
		return holdMax
	}
	return d
}

// holdLoop calls try with back-off while it reports a target-wide failure.
// try returns (done, err): done when nothing more is held; err is the reason
// when it is not done. It returns false if shutdown cut the hold short.
func holdLoop(what string, try func() (bool, error), first error) bool {
	atomic.AddInt64(&holding, 1)
	defer atomic.AddInt64(&holding, -1)
	setTargetUp(false)
	wait, err := holdBase, first
	for {
		if sinkStopping() {
			return false
		}
		logErr("target-down", fmt.Errorf("%s: holding until the target recovers (next try in %v): %w", what, wait, err))
		holdSleep(wait)
		if sinkStopping() {
			return false
		}
		done, e := try()
		if done {
			setTargetUp(true)
			return true
		}
		err = e
		wait = nextBackoff(wait)
	}
}

// holdBatch re-sends the target-wide failures of a writeOps result until they
// are applied or the sink stops. Changes refused for themselves are left as
// they are, for reclaim.
func holdBatch(ctx context.Context, target redis.UniversalClient, stream string, ops []targetOp, res writeResult) writeResult {
	idx := res.held()
	if len(idx) == 0 {
		return res
	}
	atomic.AddInt64(&heldTotal, int64(len(idx)))
	holdLoop(stream+": "+strconv.Itoa(len(idx))+" change(s)", func() (bool, error) {
		idx = res.held()
		sub := make([]targetOp, len(idx))
		for j, i := range idx {
			sub[j] = ops[i]
		}
		r := writeOps(ctx, target, sub)
		res.sends += r.sends
		for j, i := range idx {
			res.cmds[i], res.errs[i] = r.cmds[j], r.errs[j]
		}
		res.lastErr = r.lastErr
		if r.refused != nil {
			res.refused = r.refused
		}
		left := res.held()
		if len(left) == 0 {
			return true, nil
		}
		return false, res.errs[left[0]]
	}, res.errs[idx[0]])
	return res
}

// dialRetryable reports a startup connection failure that time can fix: the
// target unreachable, loading or failing over. A rejected credential, a TLS
// mismatch or a topology mismatch is a configuration error and stays fatal.
func dialRetryable(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	for _, p := range []string{"tls:", "certificate", "x509", "handshake",
		"cluster-mode", "NOAUTH", "WRONGPASS", "NOPERM", "Authentication required",
		"invalid username-password", "invalid password", "user is disabled", "no password is set",
		"without any password configured"} {
		if strings.Contains(msg, p) {
			return false
		}
	}
	var re redis.Error
	if errors.As(err, &re) {
		return retryableReply(re)
	}
	return isTransport(err)
}

// dialTarget connects to the target, waiting out an outage instead of exiting.
// It used to exit FATAL, and a target outage at startup turned the service into
// a crash loop that published no metrics. It returns (nil, nil) on shutdown.
var dialFn = dial

func dialTarget(ctx context.Context, e endpoint, stop <-chan os.Signal) (redis.UniversalClient, error) {
	wait := holdBase
	waiting := false
	defer func() {
		if waiting {
			atomic.AddInt64(&holding, -1)
		}
	}()
	for {
		c, err := dialFn(ctx, e)
		if err == nil {
			setTargetUp(true)
			return c, nil
		}
		if !dialRetryable(err) {
			return nil, err
		}
		if !waiting {
			waiting = true
			atomic.AddInt64(&holding, 1) // health stays OK: a restart cannot fix it
		}
		setTargetUp(false)
		logErr("target-connect", fmt.Errorf("retrying in %v: %w", wait, err))
		select {
		case <-stop:
			return nil, nil
		case <-time.After(wait):
		}
		wait = nextBackoff(wait)
	}
}
