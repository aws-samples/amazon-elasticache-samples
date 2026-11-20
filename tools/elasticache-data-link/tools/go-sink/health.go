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
//   - /health returns 503 when the sink has made no successful broker read for
//     HEALTH_STALL_SEC (default 120s) while nothing is held for the target. ECS
//     then replaces the task. Unapplied entries stay pending in the consumer
//     group, so a replacement loses nothing.
//   - A target outage does NOT fail /health. It used to (after 120s of failed
//     writes), which replaced a task that a restart could not fix, crash-looped
//     the service on the startup dial, and stopped the metrics that report the
//     outage. Workers hold their batches instead (hold.go) and TargetUp is 0.
//   - The heartbeat is written only while healthy and nothing is held.

import (
	"context"
	"fmt"
	"os"
	"sort"
	"strings"
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
func markWriteOK()  { atomic.StoreInt64(&lastWriteOKMs, nowMs()); setTargetUp(true) }
func markWriteErr() { atomic.StoreInt64(&lastWriteErrMs, nowMs()) }

// evalHealth is the health rule, as a pure function of the timestamps so it can
// be tested without a clock or a broker.
//
//   - unhealthy if no broker read has succeeded for `stall` (counting from start,
//     so a task is not failed before it has had time to start), unless a worker
//     or the startup is holding for the target: then no read is expected
//
// An idle sink is healthy: a blocking read that times out with nothing to read
// still counts as a successful read.
func evalHealth(now, started, readOK int64, holding bool, stall int64) (bool, string) {
	if holding {
		return true, "ok (holding changes until the target recovers)"
	}
	if readOK < started {
		readOK = started
	}
	if d := now - readOK; d > stall {
		return false, fmt.Sprintf("no successful broker read for %ds", d/1000)
	}
	return true, "ok"
}

func healthy() (bool, string) {
	return evalHealth(nowMs(), atomic.LoadInt64(&startedAtMs),
		atomic.LoadInt64(&lastReadOKMs), isHolding() || atomic.LoadInt64(&groupWaiting) > 0,
		healthStall.Milliseconds())
}

// errLogger prints at most one line per kind AND error class per interval,
// carrying a count of the lines it suppressed, so a sustained failure is
// visible without flooding the log group.
//
// It used to rate-limit by kind only (Jira story F096), so table A's WRONGTYPE hid table
// B's NOPERM for as long as A kept failing, and the count of suppressed lines
// was printed only if the kind failed again after the interval. Now the key
// is the kind plus the message with its numbers removed (errClass), which
// keeps the stream and the error text apart; flush prints what is still
// suppressed once its interval is over; and errorsTotal counts every ERROR by
// kind, published as Errors{Kind}.
type errLogger struct {
	mu         sync.Mutex
	interval   time.Duration
	now        func() time.Time
	last       map[string]time.Time
	suppressed map[string]int
	pending    map[string]string // key -> "<level> <kind>" of the suppressed lines
	classes    map[string]int    // kind -> distinct classes seen
	totals     map[string]int64  // kind -> ERROR lines, suppressed or not
	out        func(string)
}

func newErrLogger(interval time.Duration) *errLogger {
	return &errLogger{
		interval: interval, now: time.Now,
		last: map[string]time.Time{}, suppressed: map[string]int{},
		pending: map[string]string{}, classes: map[string]int{}, totals: map[string]int64{},
		out: func(s string) { fmt.Fprintln(os.Stderr, s) },
	}
}

// maxClassesPerKind bounds the logger's memory: past it, a kind's further
// classes share one key. Error texts that embed keys or values are the case.
const maxClassesPerKind = 50

// errClass is the message with digit runs replaced and cut to 120 bytes:
// stream ids, counts and attempts vary between lines of one failure, the
// stream name and the error text do not.
func errClass(msg string) string {
	var b strings.Builder
	digit := false
	for _, r := range msg {
		if r >= '0' && r <= '9' {
			if !digit {
				b.WriteByte('#')
			}
			digit = true
			continue
		}
		digit = false
		b.WriteRune(r)
		if b.Len() >= 120 {
			break
		}
	}
	return b.String()
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
	if level == "ERROR" {
		l.totals[kind]++
	}
	key := level + " " + kind + "|" + errClass(err.Error())
	if _, seen := l.last[key]; !seen {
		if l.classes[level+" "+kind] >= maxClassesPerKind {
			key = level + " " + kind + "|(other)"
		} else {
			l.classes[level+" "+kind]++
		}
	}
	t := l.now()
	if last, ok := l.last[key]; ok && t.Sub(last) < l.interval {
		l.suppressed[key]++
		l.pending[key] = level + " " + kind
		return
	}
	line := fmt.Sprintf("%s %s: %v", level, kind, err)
	if n := l.suppressed[key]; n > 0 {
		line += fmt.Sprintf(" (%d similar in the last %v)", n, l.interval)
	}
	l.last[key] = t
	l.suppressed[key] = 0
	delete(l.pending, key)
	l.out(line)
}

// flush prints one line for every class whose suppressed lines would
// otherwise be lost because the failure stopped. The line keeps the level and
// kind, so the SinkErrors filter still counts a trailing ERROR.
// With force (shutdown) it does not wait for the interval.
func (l *errLogger) flush(force bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	t := l.now()
	keys := make([]string, 0, len(l.pending))
	for key := range l.pending {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		if !force && t.Sub(l.last[key]) < l.interval {
			continue
		}
		_, class, _ := strings.Cut(key, "|")
		l.out(fmt.Sprintf("%s: %d more like %q in the last %v", l.pending[key], l.suppressed[key], class, l.interval))
		l.last[key] = t
		l.suppressed[key] = 0
		delete(l.pending, key)
	}
}

// flushLoop calls flush every interval until ctx ends.
func (l *errLogger) flushLoop(ctx context.Context) {
	if l.interval <= 0 {
		return
	}
	tick := time.NewTicker(l.interval)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			l.flush(true)
			return
		case <-tick.C:
			l.flush(false)
		}
	}
}

// errorTotals is a copy of the ERROR count per kind.
func (l *errLogger) errorTotals() map[string]int64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make(map[string]int64, len(l.totals))
	for k, v := range l.totals {
		out[k] = v
	}
	return out
}

var errLog = newErrLogger(10 * time.Second)

// logErr records a failure. Every call site that used to discard an error
// goes through here.
func logErr(kind string, err error) { errLog.log(kind, err) }

// logWarn records a failure that was recovered from.
func logWarn(kind string, err error) { errLog.logLevel("WARN", kind, err) }

// warnStale logs, sampled by errLog, that the version guard skipped changes
// (Jira story F099). StaleSkipped was only a counter: a sink skipping most of what it
// read (a wrong LOAD_VERSION, a clock problem in a replay) looked healthy.
// A few after a retry, a replay or a bulk load are normal.
func warnStale(stream string, n int) {
	if n > 0 {
		logWarn("stale-skipped", fmt.Errorf("%s: %d change(s) skipped as older than the version already "+
			"cached (normal for a retry, a replay or changes from before a bulk load; StaleSkipped counts them)",
			stream, n))
	}
}

// errorTotalsJSON is errorsTotal as a JSON object for /metrics.
func errorTotalsJSON() string {
	totals := errLog.errorTotals()
	kinds := make([]string, 0, len(totals))
	for k := range totals {
		kinds = append(kinds, k)
	}
	sort.Strings(kinds)
	var b strings.Builder
	b.WriteByte('{')
	for i, k := range kinds {
		if i > 0 {
			b.WriteByte(',')
		}
		fmt.Fprintf(&b, "%q:%d", k, totals[k])
	}
	b.WriteByte('}')
	return b.String()
}
