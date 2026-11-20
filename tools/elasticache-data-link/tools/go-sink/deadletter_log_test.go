package main

// Jira stories F093 and F094: the failures that used to be silent in the log.
//
//   - A dead letter is logged as an ERROR line naming the stream, the entry id,
//     the reason and the delivery count, and never the row values.
//   - A metrics loop that cannot load its AWS configuration says so as an
//     ERROR line (it used to print an info line), so sink-errors counts it.

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/redis/go-redis/v9"
)

func TestIntegrationDeadLetterIsLoggedWithoutValues(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	lines := captureLog(t)
	stream := "itest:dl-log:orders"
	c.Del(ctx, stream, dlqPrefix+stream)
	t.Cleanup(func() { c.Del(ctx, stream, dlqPrefix+stream) })
	msg := redis.XMessage{ID: "1700000000000-3", Values: map[string]interface{}{
		"key": "orders:42", "value": `{"after":{"card":"4111-SECRET"}}`}}

	c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: msg.ID, Values: msg.Values})
	c.XGroupCreate(ctx, stream, "g", "0")
	if err := deadLetter(ctx, c, msg, stream, "g", "max_retries", 5, errors.New("WRONGTYPE Operation against a key")); err != nil {
		t.Fatalf("deadLetter: %v", err)
	}
	got := lines()
	if len(got) != 1 {
		t.Fatalf("want one log line, got %q", got)
	}
	line := got[0]
	for _, want := range []string{"ERROR dead-letter: ", stream, msg.ID, "reason=max_retries",
		"deliveries=5", "WRONGTYPE"} {
		if !strings.Contains(line, want) {
			t.Errorf("log line %q lacks %q", line, want)
		}
	}
	for _, secret := range []string{"4111-SECRET", "orders:42"} {
		if strings.Contains(line, secret) {
			t.Errorf("log line %q carries the change's key or values (%q)", line, secret)
		}
	}
	// The dead letter itself keeps the cause for a replay decision.
	entries, err := c.XRange(ctx, dlqPrefix+stream, "-", "+").Result()
	if err != nil || len(entries) != 1 {
		t.Fatalf("dlq entries: %v %v", entries, err)
	}
	if e := entries[0].Values["error"]; e != "WRONGTYPE Operation against a key" {
		t.Fatalf("dead letter error field = %v", e)
	}
}

func TestIntegrationAFailedDeadLetterWriteIsNotLoggedAsOne(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	lines := captureLog(t)
	stream := "itest:dl-log:wrongtype"
	// The DLQ key holds a string, so XADD fails: the entry is not dead-lettered
	// and must not be reported as if it were.
	c.Set(ctx, dlqPrefix+stream, "not a stream", 0)
	t.Cleanup(func() { c.Del(ctx, dlqPrefix+stream) })
	msg := redis.XMessage{ID: "1-1", Values: map[string]interface{}{"value": "{}"}}
	if err := deadLetter(ctx, c, msg, stream, "g", "poison", 0, errPoison); err == nil {
		t.Fatal("deadLetter succeeded against a non-stream key")
	}
	for _, l := range lines() {
		if strings.Contains(l, "dead-letter") {
			t.Fatalf("a failed dead-letter write was logged as a dead letter: %q", l)
		}
	}
}

func TestDeadLetterLogLineTruncatesTheCause(t *testing.T) {
	lines := captureLog(t)
	logDeadLetter("s", "1-0", "poison", 0, strings.Repeat("x", 1000))
	got := lines()
	if len(got) != 1 || len(got[0]) > 300 {
		t.Fatalf("want one short line, got %d lines, %d bytes", len(got), len(got[0]))
	}
}

func TestMetricsConfigErrorIsAnErrorLine(t *testing.T) {
	lines := captureLog(t)
	t.Setenv("CLUSTER_NAME", "ecdl-shop")
	t.Setenv("SERVICE_NAME", "ecdl-shop-cdc-sink")
	t.Setenv("BACKLOG_METRIC_ENABLED", "")
	old := loadAWSConfig
	loadAWSConfig = func(context.Context, ...func(*awsconfig.LoadOptions) error) (aws.Config, error) {
		return aws.Config{}, errors.New("failed to load shared config profile")
	}
	t.Cleanup(func() { loadAWSConfig = old })

	// Returns at once: nothing to publish with.
	backlogMetricsLoop(context.Background(), nil, nil, "ecdl-sink")

	got := lines()
	if len(got) != 1 || !strings.HasPrefix(got[0], "ERROR metric-config: ") ||
		!strings.Contains(got[0], "NOT published") || !strings.Contains(got[0], "shared config profile") {
		t.Fatalf("want one ERROR metric-config line, got %q", got)
	}
}
