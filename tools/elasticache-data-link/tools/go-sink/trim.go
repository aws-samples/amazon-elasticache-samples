package main

// Trimming streams that nothing consumes.
//
// Debezium's heartbeat reaches the broker on purpose: Debezium Server commits a
// source offset only for records that reach the sink, so with the heartbeat
// filtered out an idle source never wrote one, and the first deploy of a link
// failed before its bulk load ("Debezium has not committed a log position").
// Nothing reads the heartbeat stream, so the sink caps it instead of letting it
// grow by one entry every heartbeat interval.

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

// Entries kept per trimmed stream. Small, but more than zero so the most recent
// heartbeat stays readable when diagnosing "is Debezium alive".
const trimMaxLen = 100

// splitList parses a comma-separated env value, dropping blanks.
func splitList(raw string) []string {
	var out []string
	for _, item := range strings.Split(raw, ",") {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}

// trimOnce caps each stream at roughly maxLen entries. Approximate trimming
// (MAXLEN ~) lets Valkey drop whole radix-tree nodes, which is far cheaper than
// an exact trim. A missing stream is not an error: Debezium may not have
// written its first heartbeat yet.
func trimOnce(ctx context.Context, broker redis.UniversalClient, streams []string, maxLen int64) {
	for _, s := range streams {
		if err := broker.XTrimMaxLenApprox(ctx, s, maxLen, 0).Err(); err != nil && err != redis.Nil {
			// ERROR, so the SinkErrors filter counts it (Jira story F115): a stream that
			// is never trimmed grows until the broker is full.
			logErr("broker-trim", fmt.Errorf("%s: %w", s, err))
		}
	}
}

func trimLoop(ctx context.Context, broker redis.UniversalClient, streams []string, maxLen int64, every time.Duration) {
	if len(streams) == 0 {
		return
	}
	trimOnce(ctx, broker, streams, maxLen)
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			trimOnce(ctx, broker, streams, maxLen)
		}
	}
}
