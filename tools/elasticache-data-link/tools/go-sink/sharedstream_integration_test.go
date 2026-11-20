package main

// A stream with a second consumer group (Jira story F026), against a real Valkey
// (ECDL_TEST_VALKEY): an ack by one group must not delete entries the other
// has not read.

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/redis/go-redis/v9"
)

func sharedSetup(t *testing.T, n int) (redis.UniversalClient, context.Context, string) {
	t.Helper()
	c, ctx := testValkey(t), context.Background()
	stream := "itest:shared:" + t.Name()
	c.Del(ctx, stream, dlqPrefix+stream)
	t.Cleanup(func() { c.Del(ctx, stream, dlqPrefix+stream); sharedStreams.Delete(stream) })
	for i := 0; i < n; i++ {
		c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: fmt.Sprintf("%d-0", 100+i), Values: []string{"value", "x"}})
	}
	c.XGroupCreate(ctx, stream, "g", "0")
	return c, ctx, stream
}

func ids(c redis.UniversalClient, stream string) string {
	var out []string
	for _, m := range c.XRange(context.Background(), stream, "-", "+").Val() {
		out = append(out, m.ID)
	}
	return strings.Join(out, ",")
}

func readN(t *testing.T, c redis.UniversalClient, stream, group string, n int64) []redis.XMessage {
	t.Helper()
	res, err := c.XReadGroup(context.Background(), &redis.XReadGroupArgs{Group: group, Consumer: "w",
		Streams: []string{stream, ">"}, Count: n, Block: -1}).Result()
	if err != nil {
		t.Fatal(err)
	}
	return res[0].Messages
}

func TestIntegrationSecondGroupKeepsEntriesUntilBothAck(t *testing.T) { // Jira story F026
	c, ctx, stream := sharedSetup(t, 4)
	c.XGroupCreate(ctx, stream, "debug", "0")
	if _, err := ensureGroup(ctx, c, stream, "g"); err != nil { // startup notices the second group
		t.Fatal(err)
	}
	if !streamShared(stream) {
		t.Fatal("two groups not noticed")
	}
	msgs := readN(t, c, stream, "g", 4)
	if err := ackAndTrim(ctx, c, stream, "g", msgs[0].ID, msgs[1].ID, msgs[2].ID, msgs[3].ID); err != nil {
		t.Fatal(err)
	}
	if got := ids(c, stream); got != "100-0,101-0,102-0,103-0" {
		t.Fatalf("after g acked: %s (deleted entries debug has not read)", got)
	}
	if p := c.XPending(ctx, stream, "g").Val(); p.Count != 0 {
		t.Fatalf("g pending = %d", p.Count)
	}
	// debug has read nothing: the trim keeps everything.
	trimStream(ctx, c, stream, "g")
	if got := ids(c, stream); got != "100-0,101-0,102-0,103-0" {
		t.Fatalf("trim before debug read: %s", got)
	}
	// debug reads two and acks one: 100 is safe to drop, 101 is pending.
	d := readN(t, c, stream, "debug", 2)
	c.XAck(ctx, stream, "debug", d[0].ID)
	trimStream(ctx, c, stream, "g")
	if got := ids(c, stream); got != "101-0,102-0,103-0" {
		t.Fatalf("trim with debug at 101 pending: %s", got)
	}
	// Once debug is gone the stream is single-group again and acks delete.
	c.XGroupDestroy(ctx, stream, "debug")
	trimStream(ctx, c, stream, "g")
	if streamShared(stream) {
		t.Fatal("still marked shared after the second group was removed")
	}
	c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: "200-0", Values: []string{"value", "x"}})
	m := readN(t, c, stream, "g", 1)
	ackAndTrim(ctx, c, stream, "g", m[0].ID)
	if strings.Contains(ids(c, stream), "200-0") {
		t.Fatal("single group: ack did not delete")
	}
}

func TestIntegrationSecondGroupDeadLetterDoesNotDelete(t *testing.T) { // Jira story F026
	c, ctx, stream := sharedSetup(t, 1)
	c.XGroupCreate(ctx, stream, "debug", "0")
	ensureGroup(ctx, c, stream, "g")
	m := readN(t, c, stream, "g", 1)
	if err := deadLetter(ctx, c, m[0], stream, "g", "poison", 0, errPoison); err != nil {
		t.Fatal(err)
	}
	if c.XLen(ctx, stream).Val() != 1 || c.XLen(ctx, dlqPrefix+stream).Val() != 1 {
		t.Fatalf("stream %d, dlq %d", c.XLen(ctx, stream).Val(), c.XLen(ctx, dlqPrefix+stream).Val())
	}
	if p := c.XPending(ctx, stream, "g").Val(); p.Count != 0 {
		t.Fatalf("pending = %d", p.Count)
	}
}
