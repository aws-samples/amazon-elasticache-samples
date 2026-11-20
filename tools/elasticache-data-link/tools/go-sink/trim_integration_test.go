package main

// Trim and reclaim against a real Valkey (ECDL_TEST_VALKEY; see
// guard_integration_test.go).

import (
	"context"
	"fmt"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func trimSetup(t *testing.T, n int) (redis.UniversalClient, context.Context, string, string) {
	t.Helper()
	c, ctx := testValkey(t), context.Background()
	stream, group := "itest:trim:"+t.Name(), "g"
	c.Del(ctx, stream)
	for i := 0; i < n; i++ {
		c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: fmt.Sprintf("%d-0", 100+i), Values: []string{"value", "x"}})
	}
	if err := c.XGroupCreate(ctx, stream, group, "0").Err(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { betweenTrimReads = nil; c.Del(ctx, stream) })
	return c, ctx, stream, group
}

func TestIntegrationTrimRaceKeepsEntriesDeliveredDuringTheCheck(t *testing.T) { // Y5
	c, ctx, stream, group := trimSetup(t, 5)
	betweenTrimReads = func() {
		// A worker reads the whole batch between the two reads, not yet acked.
		if err := c.XReadGroup(ctx, &redis.XReadGroupArgs{Group: group, Consumer: "w1",
			Streams: []string{stream, ">"}, Count: 5}).Err(); err != nil {
			t.Fatal(err)
		}
	}
	trimStream(ctx, c, stream, group)
	if n := c.XLen(ctx, stream).Val(); n != 5 {
		t.Fatalf("stream has %d entries after the trim; delivered-but-unacked entries were deleted", n)
	}
	if p := c.XPending(ctx, stream, group).Val(); p.Count != 5 {
		t.Fatalf("pending = %d, want 5", p.Count)
	}
}

func TestIntegrationTrimRemovesOnlyAckedEntries(t *testing.T) {
	c, ctx, stream, group := trimSetup(t, 6)
	c.XReadGroup(ctx, &redis.XReadGroupArgs{Group: group, Consumer: "w1", Streams: []string{stream, ">"}, Count: 4})
	c.XAck(ctx, stream, group, "100-0", "101-0", "103-0") // 102 stays pending; 104, 105 undelivered
	trimStream(ctx, c, stream, group)
	ids := []string{}
	for _, m := range c.XRange(ctx, stream, "-", "+").Val() {
		ids = append(ids, m.ID)
	}
	if got := strings.Join(ids, ","); got != "102-0,103-0,104-0,105-0" {
		t.Fatalf("after trim: %s", got)
	}
	c.XAck(ctx, stream, group, "102-0")
	trimStream(ctx, c, stream, group)
	if n := c.XLen(ctx, stream).Val(); n != 3 { // 103 is last delivered and kept; 104, 105 undelivered
		t.Fatalf("after acking everything delivered: %d entries", n)
	}
}

func TestIntegrationClaimIdleReportsVanishedPending(t *testing.T) {
	c, ctx, stream, group := trimSetup(t, 3)
	oldIdle := retryIdle
	retryIdle = time.Millisecond
	defer func() { retryIdle = oldIdle }()
	c.XReadGroup(ctx, &redis.XReadGroupArgs{Group: group, Consumer: "w1", Streams: []string{stream, ">"}, Count: 3})
	c.XDel(ctx, stream, "101-0") // deleted while pending: a lost change
	time.Sleep(5 * time.Millisecond)
	before := atomic.LoadInt64(&pendingVanished)
	msgs, deliveries, err := claimIdle(ctx, c, stream, group, "reclaim")
	if err != nil {
		t.Fatal(err)
	}
	if len(msgs) != 2 || msgs[0].ID != "100-0" || msgs[1].ID != "102-0" {
		t.Fatalf("claimed %v", msgs)
	}
	if msgs[0].Values["value"] != "x" {
		t.Fatalf("values not parsed: %v", msgs[0].Values)
	}
	if got := atomic.LoadInt64(&pendingVanished) - before; got != 1 {
		t.Fatalf("PendingVanished += %d, want 1", got)
	}
	if deliveries["100-0"] != 1 {
		t.Fatalf("earlier deliveries = %d, want 1 (the worker's read)", deliveries["100-0"])
	}
	// A second claim counts another delivery.
	time.Sleep(5 * time.Millisecond)
	_, deliveries, _ = claimIdle(ctx, c, stream, group, "reclaim")
	if deliveries["100-0"] != 2 {
		t.Fatalf("earlier deliveries after a second claim = %d, want 2", deliveries["100-0"])
	}
	// An entry acked meanwhile is not a loss.
	c.XAck(ctx, stream, group, "100-0", "102-0")
	before = atomic.LoadInt64(&pendingVanished)
	time.Sleep(5 * time.Millisecond)
	msgs, _, err = claimIdle(ctx, c, stream, group, "reclaim")
	if err != nil || len(msgs) != 0 || atomic.LoadInt64(&pendingVanished) != before {
		t.Fatalf("after ack: msgs=%v err=%v vanished+=%d", msgs, err, atomic.LoadInt64(&pendingVanished)-before)
	}
}

func TestIntegrationClaimIdleSkipsEntriesNotYetIdle(t *testing.T) {
	c, ctx, stream, group := trimSetup(t, 2)
	c.XReadGroup(ctx, &redis.XReadGroupArgs{Group: group, Consumer: "w1", Streams: []string{stream, ">"}, Count: 2})
	msgs, _, err := claimIdle(ctx, c, stream, group, "reclaim") // RETRY_IDLE is 30 s
	if err != nil || len(msgs) != 0 {
		t.Fatalf("claimed a busy entry: %v %v", msgs, err)
	}
}
