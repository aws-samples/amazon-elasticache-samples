package main

import (
	"context"
	"fmt"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func TestTombstoneTTLFollowsTheBacklog(t *testing.T) { // Y11
	base, max := int64(tombstoneTTL), int64(tombstoneTTLMax)
	for _, c := range []struct{ ageMs, want int64 }{
		{0, base},
		{-5, base},
		{1, base + 1},
		{2 * 3600 * 1000, base + 7200},
		{30 * 24 * 3600 * 1000, max},
	} {
		if got := tombstoneTTLFor(c.ageMs); got != c.want {
			t.Errorf("tombstoneTTLFor(%d) = %d, want %d", c.ageMs, got, c.want)
		}
	}
}

func TestReplayAgeCheck(t *testing.T) {
	now := int64(10_000_000_000)
	limit := int64(tombstoneTTL) * 1000
	if err := replayAgeCheck(fmt.Sprintf("%d-0", now-limit+1000), now); err != nil {
		t.Fatalf("recent dead letter refused: %v", err)
	}
	err := replayAgeCheck(fmt.Sprintf("%d-3", now-limit-1000), now)
	if err == nil || !strings.Contains(err.Error(), "snapshot") || !strings.Contains(err.Error(), "--force") {
		t.Fatalf("old dead letter: %v", err)
	}
	if err := replayAgeCheck("", now); err != nil {
		t.Fatalf("no version: %v", err)
	}
}

func TestEvictionWarning(t *testing.T) {
	for p, warn := range map[string]bool{"": false, "noeviction": false, "volatile-lru": true,
		"volatile-ttl": true, "allkeys-lru": true, "allkeys-random": true} {
		w := evictionWarning(p)
		if (w != "") != warn {
			t.Errorf("%q: warning %q", p, w)
		}
		if warn && !strings.Contains(w, "noeviction") {
			t.Errorf("%q: warning does not name the fix", p)
		}
	}
	if w := evictionWarning("volatile-lru"); !strings.Contains(w, "with a TTL first") {
		t.Errorf("volatile warning does not explain why tombstones go first: %q", w)
	}
}

func TestIntegrationEvictionPolicyRead(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	want := c.ConfigGet(ctx, "maxmemory-policy").Val()["maxmemory-policy"]
	got, err := evictionPolicy(ctx, c)
	if err != nil || got != want || got == "" {
		t.Fatalf("policy %q err=%v, want %q", got, err, want)
	}
}

func TestIntegrationReclaimCycleLengthensTombstones(t *testing.T) {
	broker, target, _, sp, ctx := outageSetup(t, 0)
	defer atomic.StoreInt64(&tombstoneTTLNow, int64(tombstoneTTL))
	si := reclaimInterval
	reclaimInterval = 5 * time.Millisecond
	defer func() { reclaimInterval = si }()
	// A change two hours old is still waiting.
	old := time.Now().Add(-2 * time.Hour).UnixMilli()
	broker.XAdd(ctx, &redis.XAddArgs{Stream: sp.key, ID: fmt.Sprintf("%d-0", old), Values: []string{"value", "x"}})
	rctx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() { reclaimLoop(rctx, broker, target, []streamSpec{sp}, "g", "task", "envelope"); close(done) }()
	waitFor(t, "the TTL to follow the backlog", 2*time.Second, func() bool {
		return currentTombstoneTTL() >= int64(tombstoneTTL)+7200
	})
	cancel()
	<-done
	key := "itest:ts:1"
	broker.Del(ctx, key, tombstoneKey(key))
	if _, err := runDelete(ctx, broker, key, fmt.Sprintf("%d-0", time.Now().UnixMilli())); err != nil {
		t.Fatal(err)
	}
	if ttl := broker.TTL(ctx, tombstoneKey(key)).Val(); ttl < time.Duration(tombstoneTTL+7000)*time.Second {
		t.Fatalf("tombstone TTL %v does not cover the 2h backlog", ttl)
	}
	// The two-hour-old update to that row is skipped.
	if ok, _ := runUpsert(ctx, broker, key, fmt.Sprintf("%d-0", old), false, map[string]interface{}{"v": "1"}); ok {
		t.Fatal("an older change recreated the deleted row")
	}
}

func TestReclaimKeepsTheTombstoneTTLWhenTheBrokerCannotBeRead(t *testing.T) {
	defer atomic.StoreInt64(&tombstoneTTLNow, int64(tombstoneTTL))
	atomic.StoreInt64(&tombstoneTTLNow, 9999)
	si := reclaimInterval
	reclaimInterval = 5 * time.Millisecond
	defer func() { reclaimInterval = si }()
	dead := redis.NewClient(&redis.Options{Addr: "127.0.0.1:1", MaxRetries: -1, DialTimeout: 20 * time.Millisecond})
	defer dead.Close()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		reclaimLoop(ctx, dead, dead, []streamSpec{{key: "data:x.t", table: "t"}}, "g", "task", "envelope")
		close(done)
	}()
	time.Sleep(100 * time.Millisecond)
	cancel()
	<-done
	if got := currentTombstoneTTL(); got != 9999 {
		t.Fatalf("TTL = %d after unreadable cycles, want it kept at 9999", got)
	}
}
