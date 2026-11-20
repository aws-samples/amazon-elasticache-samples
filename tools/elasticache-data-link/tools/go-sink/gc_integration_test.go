package main

// Consumer GC against a real Valkey (ECDL_TEST_VALKEY): the pending check and
// the delete are one script, so a consumer that claims entries in between is
// kept (Jira story F044).

import (
	"context"
	"sort"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func gcSetup(t *testing.T) (redis.UniversalClient, context.Context, string) {
	t.Helper()
	c, ctx, stream := dlqSetup(t, 3)
	t.Cleanup(func() { betweenGCReads = nil })
	return c, ctx, stream
}

func consumerNames(t *testing.T, c redis.UniversalClient, stream string) []string {
	t.Helper()
	var out []string
	for _, x := range c.XInfoConsumers(context.Background(), stream, "g").Val() {
		out = append(out, x.Name)
	}
	sort.Strings(out)
	return out
}

func TestIntegrationGCDeletesOnlyIdleEmptyConsumers(t *testing.T) { // Jira story F044
	c, ctx, stream := gcSetup(t)
	c.XGroupCreateConsumer(ctx, stream, "g", "dead-w0")
	c.XGroupCreateConsumer(ctx, stream, "g", "dead-reclaim")
	c.XGroupCreateConsumer(ctx, stream, "g", "me-w0")
	// busy-w0 holds an entry: never deleted.
	c.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "busy-w0", Streams: []string{stream, ">"}, Count: 1})
	time.Sleep(20 * time.Millisecond)
	gcConsumers(ctx, c, stream, "g", "me", 10*time.Millisecond)
	got := consumerNames(t, c, stream)
	if len(got) != 2 || got[0] != "busy-w0" || got[1] != "me-w0" {
		t.Fatalf("consumers after GC = %v", got)
	}
	// Not idle long enough: kept.
	c.XGroupCreateConsumer(ctx, stream, "g", "fresh-w0")
	gcConsumers(ctx, c, stream, "g", "me", time.Hour)
	if n := len(consumerNames(t, c, stream)); n != 3 {
		t.Fatalf("a fresh consumer was deleted: %v", consumerNames(t, c, stream))
	}
}

func TestIntegrationGCKeepsAConsumerThatClaimedMeanwhile(t *testing.T) { // Jira story F044
	c, ctx, stream := gcSetup(t)
	// Task A's reclaim consumer exists and is idle with nothing pending.
	c.XGroupCreateConsumer(ctx, stream, "g", "taskA-reclaim")
	msgs := readAll(t, c, stream) // w1 holds three entries
	time.Sleep(20 * time.Millisecond)
	// Between task B's listing and its delete, task A claims the entries.
	betweenGCReads = func() {
		ids := []string{}
		for _, m := range msgs {
			ids = append(ids, m.ID)
		}
		if err := c.XClaimJustID(ctx, &redis.XClaimArgs{Stream: stream, Group: "g", Consumer: "taskA-reclaim",
			MinIdle: 0, Messages: ids}).Err(); err != nil {
			t.Fatal(err)
		}
	}
	gcConsumers(ctx, c, stream, "g", "taskB", 10*time.Millisecond)
	if p := c.XPending(ctx, stream, "g").Val(); p.Count != 3 {
		t.Fatalf("pending = %d after GC, want 3: entries were deleted with their consumer", p.Count)
	}
	found := false
	for _, n := range consumerNames(t, c, stream) {
		found = found || n == "taskA-reclaim"
	}
	if !found {
		t.Fatal("the reclaim consumer that holds entries was deleted")
	}
}

func TestIntegrationGCPendingCheckIsInTheScript(t *testing.T) { // Jira story F044
	c, ctx, stream := gcSetup(t)
	c.XGroupCreateConsumer(ctx, stream, "g", "taskA-reclaim")
	msgs := readAll(t, c, stream)
	betweenGCReads = func() {
		c.XClaimJustID(ctx, &redis.XClaimArgs{Stream: stream, Group: "g", Consumer: "taskA-reclaim",
			Messages: []string{msgs[0].ID}})
	}
	// Idle threshold 0: only the pending check can keep it.
	gcConsumers(ctx, c, stream, "g", "taskB", 0)
	if p := c.XPending(ctx, stream, "g").Val(); p.Count != 3 {
		t.Fatalf("pending = %d, want 3", p.Count)
	}
}

func TestIntegrationGCIdleCheckIsInTheScript(t *testing.T) { // Jira story F044
	c, ctx, stream := gcSetup(t)
	c.XGroupCreateConsumer(ctx, stream, "g", "taskA-reclaim")
	readAll(t, c, stream)
	time.Sleep(20 * time.Millisecond)
	// Active again (a read that found nothing) between listing and delete.
	betweenGCReads = func() {
		c.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "taskA-reclaim",
			Streams: []string{stream, ">"}, Count: 1, Block: -1})
	}
	gcConsumers(ctx, c, stream, "g", "taskB", 15*time.Millisecond)
	found := false
	for _, n := range consumerNames(t, c, stream) {
		found = found || n == "taskA-reclaim"
	}
	if !found {
		t.Fatal("a consumer active at the moment of the delete was deleted")
	}
}
