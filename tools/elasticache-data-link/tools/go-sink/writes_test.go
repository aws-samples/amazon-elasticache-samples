package main

import (
	"context"
	"errors"
	"io"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func TestRetryableReply(t *testing.T) {
	for _, c := range []struct {
		err  error
		want bool
	}{
		{errors.New("LOADING Valkey is loading the dataset in memory"), true},
		{errors.New("NOSCRIPT No matching script"), true},
		{errors.New("CLUSTERDOWN The cluster is down"), true},
		{errors.New("WRONGTYPE Operation against a key holding the wrong kind of value"), false},
		{errors.New("OOM command not allowed"), false},
		{nil, false},
	} {
		if got := retryableReply(c.err); got != c.want {
			t.Errorf("retryableReply(%v) = %v, want %v", c.err, got, c.want)
		}
	}
	if isServerReply(io.EOF) {
		t.Error("io.EOF is a transport failure, not a server reply")
	}
}

func TestNestedValuesAreJSON(t *testing.T) {
	var row map[string]interface{}
	if err := decodeJSON([]byte(`{"tags":[1,2.50,"a<b"],"geo":{"wkb":"AQ==","srid":null},"n":5}`), &row); err != nil {
		t.Fatal(err)
	}
	got := stringifyRow(row)
	if got["tags"] != `[1,2.50,"a<b"]` || got["geo"] != `{"srid":null,"wkb":"AQ=="}` || got["n"] != "5" {
		t.Fatalf("stringifyRow = %#v", got)
	}
}

// failFirstPipeline makes the first pipeline fail the way a dropped connection
// does (every command gets io.EOF, nothing reaches the server), then lets the
// rest through.
type failFirstPipeline struct{ calls int32 }

func (h *failFirstPipeline) DialHook(next redis.DialHook) redis.DialHook { return next }
func (h *failFirstPipeline) ProcessHook(next redis.ProcessHook) redis.ProcessHook {
	return next
}
func (h *failFirstPipeline) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return func(ctx context.Context, cmds []redis.Cmder) error {
		if atomic.AddInt32(&h.calls, 1) == 1 {
			for _, c := range cmds {
				c.SetErr(io.EOF)
			}
			return io.EOF
		}
		return next(ctx, cmds)
	}
}

func noSleep(t *testing.T) {
	saved := sleep
	sleep = func(time.Duration) {}
	t.Cleanup(func() { sleep = saved })
}

// The end-to-end defect: one EOF used to leave the batch for reclaim (30-45 s).
func TestIntegrationWriteRecoversFromDroppedConnection(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	noSleep(t)
	if err := loadScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	keys := []string{"itest:w:1", "itest:w:2"}
	c.Del(ctx, keys[0], keys[1], tombstoneKey(keys[0]), tombstoneKey(keys[1]))
	h := &failFirstPipeline{}
	c.AddHook(h)
	before := atomic.LoadInt64(&writesRecovered)
	res := writeOps(ctx, c, []targetOp{
		{id: "10-0", version: "10-0", key: keys[0], fields: map[string]interface{}{"qty": "1"}},
		{id: "11-0", version: "11-0", key: keys[1], fields: map[string]interface{}{"qty": "2"}},
	})
	if res.sends != 2 || res.unconfirmed() != 0 || res.lastErr != nil || !errors.Is(res.firstErr, io.EOF) {
		t.Fatalf("sends=%d unconfirmed=%d last=%v first=%v", res.sends, res.unconfirmed(), res.lastErr, res.firstErr)
	}
	if got := atomic.LoadInt64(&writesRecovered) - before; got != 2 {
		t.Fatalf("writesRecovered +%d, want +2", got)
	}
	for i, want := range []string{"1", "2"} {
		if v := c.HGet(ctx, keys[i], "qty").Val(); v != want {
			t.Fatalf("%s qty=%q, want %q", keys[i], v, want)
		}
	}
}

// One refused command no longer holds back the rest of the batch.
func TestIntegrationRefusedCommandDoesNotHoldTheBatch(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	noSleep(t)
	if err := loadScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	bad, good := "itest:w:string", "itest:w:3"
	c.Del(ctx, bad, good, tombstoneKey(bad), tombstoneKey(good))
	c.Set(ctx, bad, "not a hash", 0) // HSET on it is WRONGTYPE
	res := writeOps(ctx, c, []targetOp{
		{id: "20-0", version: "20-0", key: bad, fields: map[string]interface{}{"qty": "1"}},
		{id: "21-0", version: "21-0", key: good, fields: map[string]interface{}{"qty": "3"}},
	})
	if res.cmds[0] != nil || res.cmds[1] == nil || res.sends != 1 || res.lastErr == nil {
		t.Fatalf("cmds=%v sends=%d last=%v; want only the good write confirmed, one send",
			res.cmds, res.sends, res.lastErr)
	}
	if v := c.HGet(ctx, good, "qty").Val(); v != "3" {
		t.Fatalf("good write not applied: %q", v)
	}
}

// A target that stays unreachable is not retried indefinitely: WRITE_RETRIES re-sends,
// then the changes are left for reclaim.
func TestIntegrationWriteGivesUpAfterRetries(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	noSleep(t)
	c.AddHook(&alwaysFail{})
	res := writeOps(ctx, c, []targetOp{{id: "30-0", version: "30-0", key: "itest:w:4",
		fields: map[string]interface{}{"qty": "4"}}})
	if res.sends != writeRetries+1 || res.unconfirmed() != 1 || !errors.Is(res.lastErr, io.EOF) {
		t.Fatalf("sends=%d unconfirmed=%d last=%v", res.sends, res.unconfirmed(), res.lastErr)
	}
}

type alwaysFail struct{ failFirstPipeline }

func (h *alwaysFail) ProcessPipelineHook(redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return func(ctx context.Context, cmds []redis.Cmder) error {
		for _, c := range cmds {
			c.SetErr(io.EOF)
		}
		return io.EOF
	}
}

func TestWriteResendsStopBeforeReclaimCouldClaim(t *testing.T) { // Jira story F046
	ri, wr, wb := retryIdle, writeRetries, writeRetryBase
	retryIdle, writeRetries, writeRetryBase = 300*time.Millisecond, 10, 60*time.Millisecond
	defer func() { retryIdle, writeRetries, writeRetryBase = ri, wr, wb }()
	c := redis.NewClient(&redis.Options{Addr: "127.0.0.1:1", MaxRetries: -1, DialTimeout: 50 * time.Millisecond})
	defer c.Close()
	ops := []targetOp{{id: "1-0", version: "1-0", key: "k", fields: map[string]interface{}{"a": "1"}}}
	start := time.Now()
	res := writeOpsOnce(context.Background(), c, ops)
	took := time.Since(start)
	// Sends at 0 and 60ms; the next back-off (120ms) would end past half of
	// RETRY_IDLE_MS (150ms), so it stops there.
	if res.sends != 2 || res.unconfirmed() != 1 {
		t.Fatalf("sends = %d, unconfirmed = %d", res.sends, res.unconfirmed())
	}
	if took >= retryIdle/2+50*time.Millisecond {
		t.Fatalf("writeOps took %v, over half of RETRY_IDLE_MS", took)
	}
	// With a long RETRY_IDLE_MS every configured re-send is used.
	retryIdle, writeRetries, writeRetryBase = time.Minute, 3, time.Millisecond
	if res := writeOpsOnce(context.Background(), c, ops); res.sends != 4 {
		t.Fatalf("sends = %d, want 1 + WRITE_RETRIES", res.sends)
	}
}
