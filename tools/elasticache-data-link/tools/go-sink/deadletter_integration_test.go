package main

// Dead-lettering against a real Valkey (ECDL_TEST_VALKEY): the dead letter and
// the ack are one MULTI (Jira story F045), and each DLQ is capped (Jira story F025).

import (
	"context"
	"fmt"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func dlqSetup(t *testing.T, poison int) (redis.UniversalClient, context.Context, string) {
	t.Helper()
	c, ctx := testValkey(t), context.Background()
	stream := "itest:dlq:" + t.Name()
	c.Del(ctx, stream, dlqPrefix+stream)
	t.Cleanup(func() { c.Del(ctx, stream, dlqPrefix+stream) })
	for i := 0; i < poison; i++ {
		c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: fmt.Sprintf("%d-0", 100+i),
			Values: map[string]interface{}{"key": fmt.Sprintf(`{"id":%d}`, i), "value": "not json"}})
	}
	if err := c.XGroupCreate(ctx, stream, "g", "0").Err(); err != nil {
		t.Fatal(err)
	}
	return c, ctx, stream
}

func readAll(t *testing.T, c redis.UniversalClient, stream string) []redis.XMessage {
	t.Helper()
	res, err := c.XReadGroup(context.Background(), &redis.XReadGroupArgs{Group: "g", Consumer: "w1",
		Streams: []string{stream, ">"}, Count: 1000, Block: -1}).Result()
	if err != nil {
		t.Fatal(err)
	}
	return res[0].Messages
}

func TestIntegrationPoisonIsDeadLetteredAndAckedTogether(t *testing.T) { // Jira story F045
	c, ctx, stream := dlqSetup(t, 3)
	msgs := readAll(t, c, stream)
	before := atomic.LoadInt64(&dlqSent)
	applyPlainBatch(ctx, c, c, stream, "g", msgs, "envelope", "t")
	if n := c.XLen(ctx, dlqPrefix+stream).Val(); n != 3 {
		t.Fatalf("dead letters = %d, want 3", n)
	}
	if p := c.XPending(ctx, stream, "g").Val(); p.Count != 0 {
		t.Fatalf("pending = %d after dead-lettering", p.Count)
	}
	if n := c.XLen(ctx, stream).Val(); n != 0 {
		t.Fatalf("stream still holds %d", n)
	}
	if got := atomic.LoadInt64(&dlqSent) - before; got != 3 {
		t.Fatalf("dlqSent += %d", got)
	}
	e := c.XRange(ctx, dlqPrefix+stream, "-", "+").Val()[0]
	if e.Values["orig_id"] != "100-0" || e.Values["ecdl_v"] != "100-0" || e.Values["reason"] != "poison" ||
		e.Values["key"] != `{"id":0}` || e.Values["value"] != "not json" || e.Values["source_stream"] != stream {
		t.Fatalf("dead letter = %v", e.Values)
	}
}

func TestIntegrationRefusedDeadLetterLeavesTheEntryPendingOnce(t *testing.T) { // Jira story F045
	c, ctx := ownedValkey(t), context.Background()
	stream := "itest:dlq:" + t.Name()
	c.Del(ctx, stream, dlqPrefix+stream)
	t.Cleanup(func() { c.Del(ctx, stream, dlqPrefix+stream) })
	c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: "100-0", Values: []string{"value", "not json"}})
	c.XGroupCreate(ctx, stream, "g", "0")
	msgs := readAll(t, c, stream)
	restore := withFullBroker(t, c)
	err := deadLetter(ctx, c, msgs[0], stream, "g", "poison", 0, errPoison)
	if err == nil {
		t.Fatal("dead letter written on a full broker")
	}
	restore()
	// Nothing happened: no dead letter, and the entry is still pending and
	// resident, so reclaim dead-letters it exactly once later.
	if n := c.XLen(ctx, dlqPrefix+stream).Val(); n != 0 {
		t.Fatalf("dead letters = %d after a refused MULTI", n)
	}
	if p := c.XPending(ctx, stream, "g").Val(); p.Count != 1 {
		t.Fatalf("pending = %d, want 1", p.Count)
	}
	oldIdle := retryIdle
	retryIdle = time.Millisecond
	defer func() { retryIdle = oldIdle }()
	time.Sleep(5 * time.Millisecond)
	got, deliveries, err := claimIdle(ctx, c, stream, "g", "x-reclaim")
	if err != nil || len(got) != 1 {
		t.Fatalf("claim: %v %v", got, err)
	}
	applyReclaimed(ctx, c, c, stream, "g", "t", "envelope", got, deliveries)
	if n := c.XLen(ctx, dlqPrefix+stream).Val(); n != 1 {
		t.Fatalf("dead letters = %d, want exactly 1", n)
	}
	if p := c.XPending(ctx, stream, "g").Val(); p.Count != 0 {
		t.Fatalf("pending = %d", p.Count)
	}
}

func TestIntegrationMaxRetriesDeadLetterIsAcked(t *testing.T) { // Jira story F045
	c, ctx := testValkey(t), context.Background()
	stream := "itest:dlq:" + t.Name()
	key := "dlqt:1"
	c.Del(ctx, stream, dlqPrefix+stream, key)
	t.Cleanup(func() { c.Del(ctx, stream, dlqPrefix+stream, key) })
	c.Set(ctx, key, "a string, so HSET answers WRONGTYPE", 0)
	if err := loadScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: "100-0", Values: map[string]interface{}{
		"key": `{"id":1}`, "value": `{"op":"c","source":{"table":"dlqt"},"after":{"id":1,"v":"x"}}`}})
	c.XGroupCreate(ctx, stream, "g", "0")
	msgs := readAll(t, c, stream)
	applyReclaimed(ctx, c, c, stream, "g", "dlqt", "envelope", msgs, map[string]int64{"100-0": maxDeliveries})
	d := c.XRange(ctx, dlqPrefix+stream, "-", "+").Val()
	if len(d) != 1 || d[0].Values["reason"] != "max_retries" || !strings.Contains(fmt.Sprint(d[0].Values["error"]), "WRONGTYPE") {
		t.Fatalf("dead letters = %v", d)
	}
	if p := c.XPending(ctx, stream, "g").Val(); p.Count != 0 || c.XLen(ctx, stream).Val() != 0 {
		t.Fatalf("pending = %d, stream = %d", p.Count, c.XLen(ctx, stream).Val())
	}
	// One more delivery short of the limit: kept pending, not dead-lettered.
	c.Del(ctx, dlqPrefix+stream)
	c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: "101-0", Values: map[string]interface{}{
		"key": `{"id":1}`, "value": `{"op":"c","source":{"table":"dlqt"},"after":{"id":1,"v":"y"}}`}})
	msgs = readAll(t, c, stream)
	applyReclaimed(ctx, c, c, stream, "g", "dlqt", "envelope", msgs, map[string]int64{"101-0": maxDeliveries - 1})
	if n := c.XLen(ctx, dlqPrefix+stream).Val(); n != 0 {
		t.Fatalf("dead-lettered before MAX_DELIVERIES: %d", n)
	}
	if p := c.XPending(ctx, stream, "g").Val(); p.Count != 1 {
		t.Fatalf("pending = %d, want 1", p.Count)
	}
}

func TestIntegrationDeadLetterStreamIsCapped(t *testing.T) { // Jira story F025
	c, ctx, stream := dlqSetup(t, 5)
	oldMax := dlqMaxLen
	dlqMaxLen = 3
	defer func() { dlqMaxLen = oldMax }()
	var lines []string
	old := errLog
	errLog = newErrLogger(0)
	errLog.out = func(s string) { lines = append(lines, s) }
	defer func() { errLog = old }()
	dropped := atomic.LoadInt64(&dlqDropped)
	msgs := readAll(t, c, stream)
	applyPlainBatch(ctx, c, c, stream, "g", msgs, "envelope", "t")
	d := c.XRange(ctx, dlqPrefix+stream, "-", "+").Val()
	if len(d) != 3 {
		t.Fatalf("dead letters = %d, want the cap of 3", len(d))
	}
	// The newest are kept.
	if d[0].Values["orig_id"] != "102-0" || d[2].Values["orig_id"] != "104-0" {
		t.Fatalf("kept %v .. %v", d[0].Values["orig_id"], d[2].Values["orig_id"])
	}
	if got := atomic.LoadInt64(&dlqDropped) - dropped; got != 2 {
		t.Fatalf("dlqDropped += %d, want 2", got)
	}
	n := 0
	for _, l := range lines {
		if strings.HasPrefix(l, "ERROR dlq-full: ") && strings.Contains(l, "DLQ_MAX_LEN (3)") {
			n++
		}
	}
	if n != 2 {
		t.Fatalf("dlq-full lines = %d: %q", n, lines)
	}
	// Under the cap nothing is dropped and nothing is logged.
	if p := c.XPending(ctx, stream, "g").Val(); p.Count != 0 {
		t.Fatalf("pending = %d", p.Count)
	}
}

func TestIntegrationDeadLetterUnderTheCapDropsNothing(t *testing.T) { // Jira story F025
	c, ctx, stream := dlqSetup(t, 2)
	dropped := atomic.LoadInt64(&dlqDropped)
	applyPlainBatch(ctx, c, c, stream, "g", readAll(t, c, stream), "envelope", "t")
	if n := c.XLen(ctx, dlqPrefix+stream).Val(); n != 2 || atomic.LoadInt64(&dlqDropped) != dropped {
		t.Fatalf("dead letters %d, dropped %d", n, atomic.LoadInt64(&dlqDropped)-dropped)
	}
}

func TestIntegrationDenormPoisonIsDeadLetteredAndAcked(t *testing.T) { // Jira story F045
	d := setupDenorm(t, "", "customers", "null")
	c, ctx := d.c, d.ctx
	stream := "itest:dlq:" + t.Name()
	c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: "100-0", Values: []string{"value", "not json"}})
	good := evMsg("", "c", "orders", 1, nil, row("id", 1, "cid", 7, "qty", 2))
	c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: "101-0", Values: good.Values})
	c.XGroupCreate(ctx, stream, "g", "0")
	applyDenormBatch(ctx, c, c, stream, "g", "orders", readAll(t, c, stream))
	if n := c.XLen(ctx, dlqPrefix+stream).Val(); n != 1 {
		t.Fatalf("dead letters = %d", n)
	}
	if p := c.XPending(ctx, stream, "g").Val(); p.Count != 0 || c.XLen(ctx, stream).Val() != 0 {
		t.Fatalf("pending = %d, stream = %d", p.Count, c.XLen(ctx, stream).Val())
	}
	if !d.exists("orders", 1) {
		t.Fatal("the valid change was not applied")
	}
}
