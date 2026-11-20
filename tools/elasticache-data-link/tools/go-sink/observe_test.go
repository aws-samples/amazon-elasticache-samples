package main

// Error logging and the signals added for Jira stories F040, F096, F099 and F115.

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	json "github.com/goccy/go-json"
	"github.com/redis/go-redis/v9"
)

func clockLogger(interval time.Duration) (*errLogger, *time.Time, *[]string) {
	var lines []string
	clock := time.Unix(1000, 0)
	l := newErrLogger(interval)
	l.now = func() time.Time { return clock }
	l.out = func(s string) { lines = append(lines, s) }
	return l, &clock, &lines
}

func TestErrLoggerKeysOnStreamAndErrorClass(t *testing.T) { // Jira story F096
	l, _, lines := clockLogger(10 * time.Second)
	l.log("target-write", errors.New("data:p.s.orders: 1 of 10 change(s) not confirmed: WRONGTYPE Operation"))
	l.log("target-write", errors.New("data:p.s.users: 3 of 10 change(s) not confirmed: NOPERM user"))
	l.log("target-write", errors.New("data:p.s.orders: 1 of 10 change(s) not confirmed: NOPERM user"))
	// Same stream and class, different counts and ids: suppressed.
	l.log("target-write", errors.New("data:p.s.orders: 7 of 12 change(s) not confirmed: WRONGTYPE Operation"))
	if len(*lines) != 3 {
		t.Fatalf("want one line per stream and error class, got %q", *lines)
	}
	if !strings.Contains((*lines)[1], "users") || !strings.Contains((*lines)[2], "orders") ||
		!strings.Contains((*lines)[2], "NOPERM") {
		t.Fatalf("lines = %q", *lines)
	}
}

func TestErrLoggerFlushesTrailingSuppressedCount(t *testing.T) { // Jira story F096
	l, clock, lines := clockLogger(10 * time.Second)
	for i := 0; i < 4; i++ {
		l.log("broker-read", fmt.Errorf("attempt %d: connection refused", i))
	}
	l.flush(false) // the interval is not over: nothing yet
	if len(*lines) != 1 {
		t.Fatalf("lines = %q", *lines)
	}
	*clock = clock.Add(11 * time.Second)
	l.flush(false)
	if len(*lines) != 2 || !strings.HasPrefix((*lines)[1], "ERROR broker-read: 3 more like") {
		t.Fatalf("trailing count not flushed: %q", *lines)
	}
	l.flush(false) // printed once only
	*clock = clock.Add(11 * time.Second)
	l.flush(false)
	if len(*lines) != 2 {
		t.Fatalf("flushed twice: %q", *lines)
	}
	// WARN keeps its level; a forced flush (shutdown) does not wait.
	l.logLevel("WARN", "stale-skipped", errors.New("s: 1 change(s) skipped"))
	l.logLevel("WARN", "stale-skipped", errors.New("s: 2 change(s) skipped"))
	l.flush(true)
	if got := (*lines)[len(*lines)-1]; !strings.HasPrefix(got, "WARN stale-skipped: 1 more like") {
		t.Fatalf("forced flush: %q", *lines)
	}
}

func TestErrLoggerBoundsClassesAndCountsErrors(t *testing.T) { // Jira story F096
	l, _, lines := clockLogger(time.Hour)
	for i := 0; i < maxClassesPerKind+30; i++ {
		l.log("reclaim-write", fmt.Errorf("key %s: script error", strings.Repeat("x", i)))
	}
	if len(*lines) != maxClassesPerKind+1 {
		t.Fatalf("lines = %d, want %d (the rest share one key)", len(*lines), maxClassesPerKind+1)
	}
	if len(l.last) > maxClassesPerKind+1 {
		t.Fatalf("logger holds %d keys", len(l.last))
	}
	l.logLevel("WARN", "reclaim-write", errors.New("handled"))
	l.log("broker-read", io.EOF)
	got := l.errorTotals()
	if got["reclaim-write"] != int64(maxClassesPerKind+30) || got["broker-read"] != 1 {
		t.Fatalf("totals = %v (WARN lines must not count)", got)
	}
}

func TestErrClass(t *testing.T) {
	if a, b := errClass("s 1700-12 (delivery 2 of 6): X"), errClass("s 1701-3 (delivery 5 of 6): X"); a != b {
		t.Fatalf("%q != %q", a, b)
	}
	if errClass("a: X") == errClass("b: X") {
		t.Fatal("streams not kept apart")
	}
	if n := len(errClass(strings.Repeat("y", 500))); n > 124 {
		t.Fatalf("class is %d bytes", n)
	}
}

func TestErrorDatumsAreDeltasPerKind(t *testing.T) { // Jira story F096
	ds := errorDatums(nil, map[string]int64{"a": 5, "b": 2, "c": 1}, map[string]int64{"a": 3, "b": 2})
	if len(ds) != 2 {
		t.Fatalf("datums = %d", len(ds))
	}
	if *ds[0].MetricName != "Errors" || *ds[0].Dimensions[0].Value != "a" || *ds[0].Value != 2 ||
		*ds[1].Dimensions[0].Value != "c" || *ds[1].Value != 1 {
		t.Fatalf("datums = %+v", ds)
	}
}

func TestNewCountersArePublishedAndServed(t *testing.T) { // Jira story F040 Jira story F025
	c := deliveryCounts{refused: 5, dlqDropped: 3}.minus(deliveryCounts{refused: 1, dlqDropped: 1})
	got := datumMap(c.datums(nil))
	if got["WritesRefused"] != 4 || got["DLQDropped"] != 2 {
		t.Fatalf("datums = %v", got)
	}
	old := errLog
	errLog = newErrLogger(time.Hour)
	errLog.out = func(string) {}
	defer func() { errLog = old }()
	logErr("broker-read", io.EOF)
	rec := httptest.NewRecorder()
	healthMux().ServeHTTP(rec, httptest.NewRequest("GET", "/metrics", nil))
	var m map[string]interface{}
	if err := json.Unmarshal(rec.Body.Bytes(), &m); err != nil {
		t.Fatalf("/metrics is not JSON: %v\n%s", err, rec.Body.String())
	}
	if _, ok := m["writes_refused"]; !ok {
		t.Fatal("no writes_refused")
	}
	if _, ok := m["dlq_dropped"]; !ok {
		t.Fatal("no dlq_dropped")
	}
	if e, _ := m["errors_total"].(map[string]interface{}); e["broker-read"] != float64(1) {
		t.Fatalf("errors_total = %v", m["errors_total"])
	}
}

func TestIntegrationOneRefusedChangeDoesNotFailTheBatch(t *testing.T) { // Jira story F040 Jira story F099
	c, ctx := testValkey(t), context.Background()
	if err := loadScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	stream := "itest:refuse:" + t.Name()
	keys := []string{"f40:1", "f40:2", "f40:3"}
	c.Del(ctx, append(keys, stream)...)
	t.Cleanup(func() { c.Del(ctx, append(keys, stream)...); setTargetUp(true) })
	c.Set(ctx, "f40:2", "a string: the upsert gets WRONGTYPE", 0)
	for i := 1; i <= 3; i++ {
		c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: fmt.Sprintf("%d-0", 100+i), Values: map[string]interface{}{
			"key": fmt.Sprintf(`{"id":%d}`, i), "value": fmt.Sprintf(`{"op":"c","source":{"table":"f40"},"after":{"id":%d}}`, i)}})
	}
	c.XGroupCreate(ctx, stream, "g", "0")
	var lines []string
	old := errLog
	errLog = newErrLogger(0)
	errLog.out = func(s string) { lines = append(lines, s) }
	defer func() { errLog = old }()
	setTargetUp(false)
	markStarted()
	markReadOK() // health depends on reads only; the batch must not change that
	refused := atomic.LoadInt64(&writesRefused)
	unconf := atomic.LoadInt64(&writesUnconfirmed)
	applyPlainBatch(ctx, c, c, stream, "g", readN(t, c, stream, "g", 10), "envelope", "f40")
	if c.Exists(ctx, "f40:1", "f40:3").Val() != 2 {
		t.Fatal("the other changes were not applied")
	}
	pend := c.XPendingExt(ctx, &redis.XPendingExtArgs{Stream: stream, Group: "g", Start: "-", End: "+", Count: 10}).Val()
	if len(pend) != 1 || pend[0].ID != "102-0" {
		t.Fatalf("pending = %v, want only the refused change", pend)
	}
	if atomic.LoadInt64(&metricTargetUp) != 1 {
		t.Fatal("TargetUp not set: one refusal marked the whole batch failed")
	}
	if ok, why := healthy(); !ok {
		t.Fatalf("unhealthy after one refusal: %s", why)
	}
	if d := atomic.LoadInt64(&writesRefused) - refused; d != 1 {
		t.Fatalf("WritesRefused += %d", d)
	}
	if d := atomic.LoadInt64(&writesUnconfirmed) - unconf; d != 1 {
		t.Fatalf("WritesUnconfirmed += %d", d)
	}
	if len(lines) != 1 || !strings.Contains(lines[0], "1 of 3 change(s) not confirmed") ||
		!strings.Contains(lines[0], "1 of them refused") {
		t.Fatalf("lines = %q", lines)
	}

	// Jira story F099: replaying an older change logs a sampled WARN.
	lines = nil
	c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: "200-0", Values: map[string]interface{}{
		"key": `{"id":1}`, "ecdl_v": "50-0", "value": `{"op":"u","source":{"table":"f40"},"after":{"id":1}}`}})
	stale := atomic.LoadInt64(&staleSkipped)
	applyPlainBatch(ctx, c, c, stream, "g", readN(t, c, stream, "g", 10), "envelope", "f40")
	if atomic.LoadInt64(&staleSkipped)-stale != 1 {
		t.Fatal("not counted as stale")
	}
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "WARN stale-skipped: "+stream+": 1 change(s) skipped") {
		t.Fatalf("lines = %q", lines)
	}
}

func TestIntegrationAllRefusedLeavesTargetUpAlone(t *testing.T) { // Jira story F040
	c, ctx := testValkey(t), context.Background()
	loadScripts(ctx, c)
	stream := "itest:refuse:" + t.Name()
	c.Del(ctx, stream, "f40b:1")
	t.Cleanup(func() { c.Del(ctx, stream, "f40b:1"); setTargetUp(true) })
	c.Set(ctx, "f40b:1", "string", 0)
	c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: "100-0", Values: map[string]interface{}{
		"key": `{"id":1}`, "value": `{"op":"c","source":{"table":"f40b"},"after":{"id":1}}`}})
	c.XGroupCreate(ctx, stream, "g", "0")
	old := errLog
	errLog = newErrLogger(0)
	errLog.out = func(string) {}
	defer func() { errLog = old }()
	setTargetUp(false)
	applyPlainBatch(ctx, c, c, stream, "g", readN(t, c, stream, "g", 10), "envelope", "f40b")
	// Nothing was confirmed, so nothing here says the target is up.
	if atomic.LoadInt64(&metricTargetUp) != 0 {
		t.Fatal("TargetUp set with nothing confirmed")
	}
}

func TestIntegrationTrimFailureIsAnError(t *testing.T) { // Jira story F115
	c, ctx := testValkey(t), context.Background()
	key := "itest:trim-wrongtype:" + t.Name()
	c.Set(ctx, key, "not a stream", 0)
	defer c.Del(ctx, key)
	var lines []string
	old := errLog
	errLog = newErrLogger(0)
	errLog.out = func(s string) { lines = append(lines, s) }
	defer func() { errLog = old }()
	trimOnce(ctx, c, []string{key, "itest:absent:" + t.Name()}, 10)
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "ERROR broker-trim: "+key) {
		t.Fatalf("lines = %q", lines)
	}
}

type replyErr string

func (e replyErr) Error() string { return string(e) }
func (replyErr) RedisError()     {}

func TestRefusedCountIsOnlyDefiniteRefusals(t *testing.T) { // Jira story F040
	ok := redis.NewStatusCmd(context.Background())
	r := writeResult{
		cmds: []redis.Cmder{nil, nil, nil, nil, nil, ok},
		errs: []error{replyErr("WRONGTYPE Operation"), io.EOF, replyErr("OOM command not allowed"),
			replyErr("LOADING"), errWaitShortfall, nil},
	}
	if n := r.refusedCount(); n != 1 {
		t.Fatalf("refusedCount = %d, want 1 (only the WRONGTYPE)", n)
	}
}
