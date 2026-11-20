package main

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func TestEvictionTrackerCountsOnlyIncreases(t *testing.T) { // D6
	tr := &evictionTracker{}
	steps := []struct {
		node string
		v    int64
		want int64
	}{
		{"a", 50, 0}, // first reading is the baseline, not 50 evictions
		{"a", 50, 0},
		{"a", 53, 3},
		{"b", 7, 0}, // a second node has its own baseline
		{"b", 9, 2},
		{"a", 1, 0}, // counter went down: restart or failover, new baseline
		{"a", 4, 3},
		{"b", 9, 0},
	}
	for i, s := range steps {
		if got := tr.observe(s.node, s.v); got != s.want {
			t.Errorf("step %d observe(%s, %d) = %d, want %d", i, s.node, s.v, got, s.want)
		}
	}
}

type fakeInfo struct {
	body string
	err  error
}

func (f fakeInfo) Info(ctx context.Context, sections ...string) *redis.StringCmd {
	cmd := redis.NewStringCmd(ctx, "info")
	if f.err != nil {
		cmd.SetErr(f.err)
	} else {
		cmd.SetVal(f.body)
	}
	return cmd
}

func TestEvictedKeysParsing(t *testing.T) {
	ctx := context.Background()
	n, err := evictedKeys(ctx, fakeInfo{body: "# Stats\r\nexpired_keys:4\r\nevicted_keys:12\r\nkeyspace_hits:1\r\n"})
	if err != nil || n != 12 {
		t.Fatalf("got %d, %v", n, err)
	}
	if _, err := evictedKeys(ctx, fakeInfo{body: "# Stats\r\nexpired_keys:4\r\n"}); err == nil {
		t.Fatal("missing field must be an error, not 0")
	}
	if _, err := evictedKeys(ctx, fakeInfo{body: "evicted_keys:x\r\n"}); err == nil {
		t.Fatal("unparsable value must be an error")
	}
	if _, err := evictedKeys(ctx, fakeInfo{err: fmt.Errorf("NOPERM")}); err == nil {
		t.Fatal("INFO error must be returned")
	}
}

func TestEvictedKeysPublished(t *testing.T) {
	c := deliveryCounts{evicted: 9}.minus(deliveryCounts{evicted: 4})
	found := false
	for _, d := range c.datums(nil) {
		if *d.MetricName == "TargetEvictedKeys" {
			found = true
			if *d.Value != 5 {
				t.Errorf("TargetEvictedKeys = %v, want 5", *d.Value)
			}
		}
	}
	if !found {
		t.Fatal("TargetEvictedKeys not published")
	}
}

// forceEviction fills a node past a tiny maxmemory under allkeys-random and
// returns a restore function. The node's own settings are put back.
func forceEviction(t *testing.T, ctx context.Context, c *redis.Client, tag string) func() {
	cfg := c.ConfigGet(ctx, "maxmemory*").Val()
	restore := func() {
		for i := 0; i < 2000; i++ {
			c.Del(ctx, fmt.Sprintf("{%s}evict:%d", tag, i))
		}
		c.ConfigSet(ctx, "maxmemory-policy", cfg["maxmemory-policy"])
		c.ConfigSet(ctx, "maxmemory", cfg["maxmemory"])
	}
	used := int64(0)
	for _, line := range strings.Split(c.Info(ctx, "memory").Val(), "\n") {
		if v, ok := strings.CutPrefix(strings.TrimSpace(line), "used_memory:"); ok {
			used, _ = strconv.ParseInt(v, 10, 64)
		}
	}
	if err := c.ConfigSet(ctx, "maxmemory-policy", "allkeys-random").Err(); err != nil {
		t.Fatalf("config set: %v", err)
	}
	c.ConfigSet(ctx, "maxmemory", strconv.FormatInt(used+2<<20, 10))
	val := strings.Repeat("x", 4096)
	for i := 0; i < 2000; i++ {
		c.Set(ctx, fmt.Sprintf("{%s}evict:%d", tag, i), val, time.Minute)
	}
	return restore
}

func TestIntegrationTargetEvictionLoopCounts(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	node := c.(*redis.Client)
	atomic.StoreInt64(&targetEvicted, 0)
	lctx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() { targetEvictionLoop(lctx, c, 10*time.Millisecond); close(done) }()
	time.Sleep(50 * time.Millisecond) // baseline taken
	if n := atomic.LoadInt64(&targetEvicted); n != 0 {
		t.Fatalf("evictions before any pressure: %d", n)
	}
	restore := forceEviction(t, ctx, node, "a")
	defer restore()
	deadline := time.Now().Add(3 * time.Second)
	for atomic.LoadInt64(&targetEvicted) == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	<-done
	if atomic.LoadInt64(&targetEvicted) == 0 {
		t.Fatal("eviction on the target was not counted")
	}
}

func TestIntegrationClusterTargetEvictionLoopCountsEveryPrimary(t *testing.T) {
	addr := os.Getenv("ECDL_TEST_VALKEY_CLUSTER")
	if addr == "" {
		t.Skip("ECDL_TEST_VALKEY_CLUSTER not set")
	}
	ctx := context.Background()
	cc := redis.NewClusterClient(&redis.ClusterOptions{Addrs: []string{addr}})
	defer cc.Close()
	atomic.StoreInt64(&targetEvicted, 0)
	lctx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() { targetEvictionLoop(lctx, cc, 10*time.Millisecond); close(done) }()
	time.Sleep(100 * time.Millisecond)
	// Squeeze ONE primary (the one that owns tag "b"); the loop must still see it.
	shard, err := cc.MasterForKey(ctx, "{b}x")
	if err != nil {
		t.Fatal(err)
	}
	restore := forceEviction(t, ctx, shard, "b")
	defer restore()
	deadline := time.Now().Add(3 * time.Second)
	for atomic.LoadInt64(&targetEvicted) == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	<-done
	if atomic.LoadInt64(&targetEvicted) == 0 {
		t.Fatal("eviction on one cluster primary was not counted")
	}
}
