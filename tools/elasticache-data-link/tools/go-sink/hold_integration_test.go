package main

// Target outages against a real Valkey (ECDL_TEST_VALKEY; see
// guard_integration_test.go). The target is the same server, behind a hook that
// fails every command with io.EOF while `down` is set -- what a dropped or
// refused connection looks like to the sink.

import (
	"context"
	"fmt"
	"io"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

type outage struct {
	down  atomic.Bool
	tries atomic.Int64 // commands or pipelines refused
}

func (o *outage) DialHook(next redis.DialHook) redis.DialHook { return next }
func (o *outage) ProcessHook(next redis.ProcessHook) redis.ProcessHook {
	return func(ctx context.Context, cmd redis.Cmder) error {
		if o.down.Load() {
			o.tries.Add(1)
			cmd.SetErr(io.EOF)
			return io.EOF
		}
		return next(ctx, cmd)
	}
}
func (o *outage) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return func(ctx context.Context, cmds []redis.Cmder) error {
		if o.down.Load() {
			o.tries.Add(1)
			for _, c := range cmds {
				c.SetErr(io.EOF)
			}
			return io.EOF
		}
		return next(ctx, cmds)
	}
}

// outageSetup: a broker client, a target client behind an outage hook, one
// stream of n changes to table "items", and fast back-off.
func outageSetup(t *testing.T, n int) (broker, target redis.UniversalClient, o *outage, sp streamSpec, ctx context.Context) {
	t.Helper()
	broker, ctx = testValkey(t), context.Background()
	broker.FlushDB(ctx)
	addr := broker.(*redis.Client).Options().Addr
	tc := redis.NewClient(&redis.Options{Addr: addr, MaxRetries: -1})
	o = &outage{}
	tc.AddHook(o)
	target = tc
	sb, sm, wb, rb := holdBase, holdMax, writeRetryBase, writeRetries
	holdBase, holdMax, writeRetryBase, writeRetries = 2*time.Millisecond, 20*time.Millisecond, time.Millisecond, 1
	t.Cleanup(func() {
		holdBase, holdMax, writeRetryBase, writeRetries = sb, sm, wb, rb
		stoppingFlag.Store(false)
		setTargetUp(true)
		tc.Close()
	})
	if err := loadScripts(ctx, broker); err != nil {
		t.Fatal(err)
	}
	sp = streamSpec{key: "data:t.items", table: "items"}
	for i := 1; i <= n; i++ {
		broker.XAdd(ctx, &redis.XAddArgs{Stream: sp.key, Values: map[string]interface{}{
			"key":   fmt.Sprintf(`{"id":%d}`, i),
			"value": fmt.Sprintf(`{"op":"c","source":{"table":"items"},"after":{"id":%d,"v":"x%d"}}`, i, i),
		}})
	}
	if err := broker.XGroupCreateMkStream(ctx, sp.key, "g", "0").Err(); err != nil {
		t.Fatal(err)
	}
	return
}

func waitFor(t *testing.T, what string, d time.Duration, f func() bool) {
	t.Helper()
	deadline := time.Now().Add(d)
	for !f() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestIntegrationOutageHoldsTheBatchAndBoundsPending(t *testing.T) { // Y10
	broker, target, o, sp, ctx := outageSetup(t, 60)
	o.down.Store(true)
	wctx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() { worker(wctx, broker, target, []streamSpec{sp}, "g", "w1", 10, "envelope"); close(done) }()
	waitFor(t, "the worker to retry", 2*time.Second, func() bool { return o.tries.Load() > 20 })
	// During the outage: one batch of 10 is pending, the other 50 were never read.
	if p := broker.XPending(ctx, sp.key, "g").Val(); p.Count != 10 {
		t.Fatalf("pending during the outage = %d, want the one held batch of 10", p.Count)
	}
	if h, why := healthy(); !h {
		t.Fatalf("unhealthy during a target outage: %s", why)
	}
	if atomic.LoadInt64(&metricTargetUp) != 0 {
		t.Fatal("TargetUp not 0 during the outage")
	}
	o.down.Store(false)
	waitFor(t, "every change to apply", 5*time.Second, func() bool {
		return broker.XLen(ctx, sp.key).Val() == 0 && broker.Exists(ctx, "items:60").Val() == 1
	})
	cancel()
	<-done
	for _, k := range []string{"items:1", "items:30", "items:60"} {
		if broker.HGet(ctx, k, "v").Val() == "" {
			t.Fatalf("%s not applied", k)
		}
	}
	if p := broker.XPending(ctx, sp.key, "g").Val(); p.Count != 0 {
		t.Fatalf("pending after recovery = %d", p.Count)
	}
	if n := broker.XLen(ctx, dlqPrefix+sp.key).Val(); n != 0 {
		t.Fatalf("%d change(s) dead-lettered by an outage", n)
	}
	if atomic.LoadInt64(&metricTargetUp) != 1 {
		t.Fatal("TargetUp not back to 1")
	}
}

func TestIntegrationShutdownEndsAHoldAndLeavesItPending(t *testing.T) {
	broker, target, o, sp, ctx := outageSetup(t, 5)
	o.down.Store(true)
	msgs := broker.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "w1",
		Streams: []string{sp.key, ">"}, Count: 5}).Val()[0].Messages
	go func() {
		waitFor(t, "a few tries", 2*time.Second, func() bool { return o.tries.Load() > 5 })
		stoppingFlag.Store(true)
	}()
	applyBatch(ctx, broker, target, sp.key, "g", msgs, "envelope", "items")
	if p := broker.XPending(ctx, sp.key, "g").Val(); p.Count != 5 {
		t.Fatalf("pending after a shutdown during a hold = %d, want 5", p.Count)
	}
	if isHolding() {
		t.Fatal("still marked holding")
	}
}

func TestIntegrationRefusedChangeIsNotHeld(t *testing.T) {
	broker, target, _, sp, ctx := outageSetup(t, 3)
	broker.Set(ctx, "items:2", "not a hash", 0) // WRONGTYPE for change 2
	msgs := broker.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "w1",
		Streams: []string{sp.key, ">"}, Count: 3}).Val()[0].Messages
	start := time.Now()
	applyBatch(ctx, broker, target, sp.key, "g", msgs, "envelope", "items")
	if time.Since(start) > time.Second {
		t.Fatal("a refusal of one change was held like an outage")
	}
	p := broker.XPendingExt(ctx, &redis.XPendingExtArgs{Stream: sp.key, Group: "g", Start: "-", End: "+", Count: 10}).Val()
	if len(p) != 1 || p[0].ID != msgs[1].ID {
		t.Fatalf("pending = %v, want only the refused change", p)
	}
	if broker.HGet(ctx, "items:3", "v").Val() != "x3" {
		t.Fatal("the other changes were not applied")
	}
}

// deliverN gives an entry n deliveries in all.
func deliverN(t *testing.T, c redis.UniversalClient, ctx context.Context, stream, id string, n int) {
	for i := 1; i < n; i++ {
		c.XClaim(ctx, &redis.XClaimArgs{Stream: stream, Group: "g", Consumer: "other", Messages: []string{id}})
	}
}

func TestIntegrationReclaimAppliesTheFinalDelivery(t *testing.T) { // M1
	broker, target, _, sp, ctx := outageSetup(t, 1)
	m := broker.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "w1",
		Streams: []string{sp.key, ">"}, Count: 1}).Val()[0].Messages[0]
	deliverN(t, broker, ctx, sp.key, m.ID, int(maxDeliveries)+3)
	applyReclaimed(ctx, broker, target, sp.key, "g", "items", "envelope",
		[]redis.XMessage{m}, map[string]int64{m.ID: maxDeliveries + 3})
	if broker.HGet(ctx, "items:1", "v").Val() != "x1" {
		t.Fatal("the final delivery was not applied")
	}
	if n := broker.XLen(ctx, dlqPrefix+sp.key).Val(); n != 0 {
		t.Fatal("a change the healthy target accepted was dead-lettered")
	}
	if p := broker.XPending(ctx, sp.key, "g").Val(); p.Count != 0 {
		t.Fatal("not acked")
	}
}

func TestIntegrationReclaimNeverDeadLettersForAnOutage(t *testing.T) {
	broker, target, o, sp, ctx := outageSetup(t, 1)
	m := broker.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "w1",
		Streams: []string{sp.key, ">"}, Count: 1}).Val()[0].Messages[0]
	o.down.Store(true)
	if err := pingTarget(ctx, target); err == nil {
		t.Fatal("ping succeeded against a target that is down")
	}
	applyReclaimed(ctx, broker, target, sp.key, "g", "items", "envelope",
		[]redis.XMessage{m}, map[string]int64{m.ID: maxDeliveries + 10})
	if n := broker.XLen(ctx, dlqPrefix+sp.key).Val(); n != 0 {
		t.Fatal("dead-lettered because the target was down")
	}
	if p := broker.XPending(ctx, sp.key, "g").Val(); p.Count != 1 {
		t.Fatal("the change is no longer pending")
	}
}

func TestIntegrationReclaimDeadLettersARefusedChangeWithItsError(t *testing.T) {
	broker, target, _, sp, ctx := outageSetup(t, 1)
	broker.Set(ctx, "items:1", "not a hash", 0)
	m := broker.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "w1",
		Streams: []string{sp.key, ">"}, Count: 1}).Val()[0].Messages[0]
	// Below the limit: kept for another try.
	applyReclaimed(ctx, broker, target, sp.key, "g", "items", "envelope",
		[]redis.XMessage{m}, map[string]int64{m.ID: 1})
	if broker.XLen(ctx, dlqPrefix+sp.key).Val() != 0 {
		t.Fatal("dead-lettered before MAX_DELIVERIES")
	}
	applyReclaimed(ctx, broker, target, sp.key, "g", "items", "envelope",
		[]redis.XMessage{m}, map[string]int64{m.ID: maxDeliveries})
	dl := broker.XRange(ctx, dlqPrefix+sp.key, "-", "+").Val()
	if len(dl) != 1 || !strings.Contains(fmt.Sprint(dl[0].Values["error"]), "WRONGTYPE") ||
		dl[0].Values["reason"] != "max_retries" {
		t.Fatalf("dead letter = %v", dl)
	}
	if p := broker.XPending(ctx, sp.key, "g").Val(); p.Count != 0 {
		t.Fatal("dead-lettered change still pending")
	}
}

func TestIntegrationDenormChangeIsHeldInOrder(t *testing.T) {
	broker, target, o, _, ctx := outageSetup(t, 0)
	oldSpec := transformSpec
	defer func() { transformSpec = oldSpec }()
	transformSpec, _ = LoadTransformSpec([]byte(`{"tables":{"orders":{"denormalize":[{"parent_table":"customers",
	 "join_field":"cid","fields":["name"],"prefix":"c_"}]},"customers":{}}}`))
	if err := loadDenormScripts(ctx, broker); err != nil {
		t.Fatal(err)
	}
	stream := "data:t.orders"
	broker.XGroupCreateMkStream(ctx, stream, "g", "0")
	for i, q := range []int{1, 2, 3} {
		broker.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: fmt.Sprintf("%d-0", 100+i), Values: map[string]interface{}{
			"key": `{"id":1}`, "value": fmt.Sprintf(`{"op":"u","source":{"table":"orders"},"after":{"id":1,"cid":7,"qty":%d}}`, q)}})
	}
	msgs := broker.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "w1",
		Streams: []string{stream, ">"}, Count: 3}).Val()[0].Messages
	o.down.Store(true)
	go func() {
		waitFor(t, "a few tries", 2*time.Second, func() bool { return o.tries.Load() > 10 })
		o.down.Store(false)
	}()
	applyDenormBatch(ctx, broker, target, stream, "g", "orders", msgs)
	if got := broker.HGet(ctx, "orders:1", "qty").Val(); got != "3" {
		t.Fatalf("qty = %q after the outage, want the last change", got)
	}
	if p := broker.XPending(ctx, stream, "g").Val(); p.Count != 0 || broker.XLen(ctx, dlqPrefix+stream).Val() != 0 {
		t.Fatalf("pending=%d dlq=%d", p.Count, broker.XLen(ctx, dlqPrefix+stream).Val())
	}
}

func TestIntegrationReclaimClaimsNothingWhileTheTargetIsDown(t *testing.T) {
	broker, target, o, sp, ctx := outageSetup(t, 1)
	si, ri := reclaimInterval, retryIdle
	reclaimInterval, retryIdle = 5*time.Millisecond, time.Millisecond
	defer func() { reclaimInterval, retryIdle = si, ri }()
	broker.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "w1", Streams: []string{sp.key, ">"}, Count: 1})
	o.down.Store(true)
	rctx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() { reclaimLoop(rctx, broker, target, []streamSpec{sp}, "g", "task", "envelope"); close(done) }()
	time.Sleep(150 * time.Millisecond)
	cancel()
	<-done
	p := broker.XPendingExt(ctx, &redis.XPendingExtArgs{Stream: sp.key, Group: "g", Start: "-", End: "+", Count: 10}).Val()
	if len(p) != 1 || p[0].Consumer != "w1" || p[0].RetryCount != 1 {
		t.Fatalf("pending = %+v; an outage must not add deliveries", p)
	}
	if atomic.LoadInt64(&metricTargetUp) != 0 {
		t.Fatal("TargetUp not 0 while the ping fails")
	}
	// Target back: the next cycles claim and apply it.
	o.down.Store(false)
	rctx, cancel = context.WithCancel(ctx)
	done = make(chan struct{})
	go func() { reclaimLoop(rctx, broker, target, []streamSpec{sp}, "g", "task", "envelope"); close(done) }()
	waitFor(t, "reclaim to apply it", 3*time.Second, func() bool { return broker.HGet(ctx, "items:1", "v").Val() == "x1" })
	cancel()
	<-done
}
