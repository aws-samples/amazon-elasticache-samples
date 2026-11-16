package main

// Health and error reporting.
//
// WHAT WAS WRONG
//   - /health always answered 200, so ECS kept a sink that could not read the
//     broker or write the target forever, and the dashboard showed "1/1 tasks".
//   - Errors were discarded: XREADGROUP errors went straight to `continue`, ackAndTrim and
//     XGroupCreateMkStream results were discarded with `_ =`, target write
//     errors were never printed, and reclaim errors went to `_ = fmt.Sprint(err)`.
//     A sink that was failing every write produced no log line at all.
//   - The freshness heartbeat was written on a timer whether or not anything was
//     being applied, so a stuck sink still looked fresh to a reader of it.
//
// NOW
//   - Every failure is logged as "ERROR <kind>: <error>", rate-limited per kind
//     so a target outage produces one line every 10s (with a suppressed count),
//     not one per batch. The CDC stack turns those lines into an alarm metric.
//   - /health returns 503 when the sink has made no successful broker read, or
//     every target write has failed, for HEALTH_STALL_SEC (default 120s). ECS
//     then replaces the task. Unapplied entries stay pending in the consumer
//     group, so a replacement loses nothing.
//   - The heartbeat is written only while healthy.

import (
	"fmt"
	"os"
	"sync"
	"sync/atomic"
	"time"
)

var (
	startedAtMs    int64
	lastReadOKMs   int64 // a broker read that returned (data or a block timeout)
	lastWriteOKMs  int64 // a target write that was confirmed
	lastWriteErrMs int64 // a target write that failed
	staleSkipped   int64 // writes the version guard refused as older than the cache
	healthStall    = time.Duration(atoiOr("HEALTH_STALL_SEC", 120)) * time.Second
)

func nowMs() int64 { return time.Now().UnixMilli() }

func markStarted()  { atomic.StoreInt64(&startedAtMs, nowMs()) }
func markReadOK()   { atomic.StoreInt64(&lastReadOKMs, nowMs()) }
func markWriteOK()  { atomic.StoreInt64(&lastWriteOKMs, nowMs()) }
func markWriteErr() { atomic.StoreInt64(&lastWriteErrMs, nowMs()) }

// evalHealth is the health rule, as a pure function of the timestamps so it can
// be tested without a clock or a broker.
//
//   - unhealthy if no broker read has succeeded for `stall` (counting from start,
//     so a task is not failed before it has had time to start)
//   - unhealthy if target writes are failing and none has succeeded for `stall`
//
// An idle sink is healthy: a blocking read that times out with nothing to read
// still counts as a successful read.
func evalHealth(now, started, readOK, writeOK, writeErr, stall int64) (bool, string) {
	since := func(t int64) int64 {
		if t < started {
			t = started
		}
		return now - t
	}
	if d := since(readOK); d > stall {
		return false, fmt.Sprintf("no successful broker read for %ds", d/1000)
	}
	if writeErr > writeOK {
		if d := since(writeOK); d > stall {
			return false, fmt.Sprintf("target writes failing for %ds", d/1000)
		}
	}
	return true, "ok"
}

func healthy() (bool, string) {
	return evalHealth(nowMs(), atomic.LoadInt64(&startedAtMs),
		atomic.LoadInt64(&lastReadOKMs), atomic.LoadInt64(&lastWriteOKMs),
		atomic.LoadInt64(&lastWriteErrMs), healthStall.Milliseconds())
}

// errLogger prints at most one line per kind per interval, carrying a count of
// the lines it suppressed, so a sustained failure is visible without flooding
// the log group.
type errLogger struct {
	mu         sync.Mutex
	interval   time.Duration
	now        func() time.Time
	last       map[string]time.Time
	suppressed map[string]int
	out        func(string)
}

func newErrLogger(interval time.Duration) *errLogger {
	return &errLogger{
		interval: interval, now: time.Now,
		last: map[string]time.Time{}, suppressed: map[string]int{},
		out: func(s string) { fmt.Fprintln(os.Stderr, s) },
	}
}

func (l *errLogger) log(kind string, err error) { l.logLevel("ERROR", kind, err) }

// logLevel is log with the line's level. WARN is for a problem that was handled
// (a write that failed and then succeeded on a re-send): visible in the log,
// but not counted by the SinkErrors metric filter, which matches "ERROR".
func (l *errLogger) logLevel(level, kind string, err error) {
	if err == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	t := l.now()
	if last, ok := l.last[kind]; ok && t.Sub(last) < l.interval {
		l.suppressed[kind]++
		return
	}
	line := fmt.Sprintf("%s %s: %v", level, kind, err)
	if n := l.suppressed[kind]; n > 0 {
		line += fmt.Sprintf(" (%d similar in the last %v)", n, l.interval)
	}
	l.last[kind] = t
	l.suppressed[kind] = 0
	l.out(line)
}

var errLog = newErrLogger(10 * time.Second)

// logErr records a failure. Every call site that used to discard an error
// goes through here.
func logErr(kind string, err error) { errLog.log(kind, err) }

// logWarn records a failure that was recovered from.
func logWarn(kind string, err error) { errLog.logLevel("WARN", kind, err) }
