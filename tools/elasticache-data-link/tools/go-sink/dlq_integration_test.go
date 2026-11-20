package main

// The dlq subcommand against a real Valkey (ECDL_TEST_VALKEY): dead letters of
// removed tables are found (Jira story F132), a replay that stops at --limit says so
// (Jira story F133), and purge deletes only with --yes (Jira story F025).

import (
	"bytes"
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

// dlqFixture is a link with one table (in) whose DLQ holds nIn dead letters,
// and a removed table (gone) whose DLQ holds nGone.
func dlqFixture(t *testing.T, nIn, nGone int) (redis.UniversalClient, context.Context, []streamSpec, string, string) {
	t.Helper()
	c, ctx := testValkey(t), context.Background()
	in := "itest:dlqtool:" + t.Name() + ".in"
	gone := "itest:dlqtool:" + t.Name() + ".gone"
	c.Del(ctx, in, gone, dlqPrefix+in, dlqPrefix+gone)
	t.Cleanup(func() { c.Del(ctx, in, gone, dlqPrefix+in, dlqPrefix+gone) })
	now := time.Now().UnixMilli()
	add := func(stream string, n int) {
		for i := 0; i < n; i++ {
			v := fmt.Sprintf("%d-%d", now, i)
			c.XAdd(ctx, &redis.XAddArgs{Stream: dlqPrefix + stream, Values: map[string]interface{}{
				"orig_id": v, "ecdl_v": v, "reason": "max_retries", "key": fmt.Sprintf(`{"id":%d}`, i),
				"value": `{"op":"c","source":{"table":"x"},"after":{"id":1}}`}})
		}
	}
	add(in, nIn)
	add(gone, nGone)
	return c, ctx, []streamSpec{{key: in, table: "in"}}, in, gone
}

func runDLQOn(c redis.UniversalClient, specs []streamSpec, action string, o dlqOptions) (int, string, string) {
	var out, errOut bytes.Buffer
	if o.limit == 0 {
		o.limit = 1000
	}
	code := dlqRun(context.Background(), c, specs, action, o, &out, &errOut)
	return code, out.String(), errOut.String()
}

func TestIntegrationDLQListFindsRemovedTables(t *testing.T) { // Jira story F132
	c, ctx, specs, in, gone := dlqFixture(t, 2, 3)
	code, out, _ := runDLQOn(c, specs, "list", dlqOptions{})
	if code != 0 || !strings.Contains(out, dlqPrefix+in+": 2 dead letter(s)") ||
		!strings.Contains(out, dlqPrefix+gone+": 3 dead letter(s) (its table is no longer in this link") {
		t.Fatalf("exit %d:\n%s", code, out)
	}
	if n, err := dlqDepthOf(ctx, c, specs); err != nil || n < 5 {
		t.Fatalf("DLQDepth = %d (%v), want the removed table's 3 included", n, err)
	}
	// A key that is not a stream, or another prefix, is not a DLQ.
	c.Set(ctx, dlqPrefix+"itest:notastream:"+t.Name(), "x", 0)
	defer c.Del(ctx, dlqPrefix+"itest:notastream:"+t.Name())
	dl, _ := findDLQs(ctx, c, specs)
	for _, d := range dl {
		if strings.Contains(d.dlq, "notastream") {
			t.Fatal("a string key was listed as a DLQ")
		}
	}
}

func TestIntegrationDLQReplayRefusesRemovedTables(t *testing.T) { // Jira story F132
	c, ctx, specs, in, gone := dlqFixture(t, 1, 2)
	code, out, errOut := runDLQOn(c, specs, "replay", dlqOptions{})
	if code != 0 || !strings.Contains(out, "replayed 1 dead letter(s), skipped 2") {
		t.Fatalf("exit %d:\n%s\n%s", code, out, errOut)
	}
	if !strings.Contains(errOut, "is not consumed by this link") {
		t.Fatalf("stderr:\n%s", errOut)
	}
	if c.XLen(ctx, in).Val() != 1 || c.XLen(ctx, dlqPrefix+in).Val() != 0 {
		t.Fatal("the link's dead letter was not replayed")
	}
	if c.Exists(ctx, gone).Val() != 0 || c.XLen(ctx, dlqPrefix+gone).Val() != 2 {
		t.Fatal("a removed table's dead letters were moved")
	}
}

func TestIntegrationDLQPartialReplaySaysSo(t *testing.T) { // Jira story F133
	c, ctx, specs, in, _ := dlqFixture(t, 5, 0)
	code, out, _ := runDLQOn(c, specs, "replay", dlqOptions{limit: 2})
	if code != 3 || !strings.Contains(out, "replayed 2 dead letter(s)") ||
		!strings.Contains(out, "PARTIAL: 3 more were not read because of --limit 2") ||
		!strings.Contains(out, "... 3 more not shown") {
		t.Fatalf("exit %d:\n%s", code, out)
	}
	if c.XLen(ctx, dlqPrefix+in).Val() != 3 {
		t.Fatal("wrong number left")
	}
	// Run again until done: 2, then the last 1 completes with exit 0.
	runDLQOn(c, specs, "replay", dlqOptions{limit: 2})
	code, out, _ = runDLQOn(c, specs, "replay", dlqOptions{limit: 2})
	if code != 0 || strings.Contains(out, "PARTIAL") || c.XLen(ctx, in).Val() != 5 {
		t.Fatalf("exit %d, stream %d:\n%s", code, c.XLen(ctx, in).Val(), out)
	}
	// A dry run that hits the limit is partial too, and changes nothing.
	c2, ctx2, specs2, in2, _ := dlqFixture(t, 3, 0)
	_ = ctx2
	code, out, _ = runDLQOn(c2, specs2, "replay", dlqOptions{limit: 1, dryRun: true})
	if code != 3 || !strings.Contains(out, "would replay 1") || c2.XLen(ctx, dlqPrefix+in2).Val() != 3 {
		t.Fatalf("dry run: exit %d:\n%s", code, out)
	}
}

func TestIntegrationDLQPurgeNeedsYes(t *testing.T) { // Jira story F025
	c, ctx, specs, in, gone := dlqFixture(t, 2, 3)
	code, out, _ := runDLQOn(c, specs, "purge", dlqOptions{})
	if code != 0 || !strings.Contains(out, "would delete 5 dead letter(s); run again with --yes") {
		t.Fatalf("exit %d:\n%s", code, out)
	}
	if c.XLen(ctx, dlqPrefix+in).Val() != 2 || c.XLen(ctx, dlqPrefix+gone).Val() != 3 {
		t.Fatal("purge without --yes deleted")
	}
	// One stream only.
	code, out, _ = runDLQOn(c, specs, "purge", dlqOptions{stream: gone, yes: true})
	if code != 0 || c.Exists(ctx, dlqPrefix+gone).Val() != 0 || c.XLen(ctx, dlqPrefix+in).Val() != 2 {
		t.Fatalf("exit %d:\n%s", code, out)
	}
	if code, _, errOut := runDLQOn(c, specs, "purge", dlqOptions{stream: "data:nope", yes: true}); code != 1 ||
		!strings.Contains(errOut, "no dead-letter stream for data:nope") {
		t.Fatalf("unknown stream: exit %d %s", code, errOut)
	}
	code, out, _ = runDLQOn(c, specs, "purge", dlqOptions{yes: true})
	if code != 0 || c.Exists(ctx, dlqPrefix+in).Val() != 0 || !strings.Contains(out, "deleted 2 dead letter(s)") {
		t.Fatalf("exit %d:\n%s", code, out)
	}
}

func TestIntegrationDLQPurgeOlderThan(t *testing.T) { // Jira story F025
	c, ctx, specs, in, _ := dlqFixture(t, 0, 0)
	old := time.Now().Add(-48 * time.Hour).UnixMilli()
	for i := 0; i < 2; i++ {
		c.XAdd(ctx, &redis.XAddArgs{Stream: dlqPrefix + in, ID: fmt.Sprintf("%d-%d", old, i), Values: []string{"value", "x"}})
	}
	c.XAdd(ctx, &redis.XAddArgs{Stream: dlqPrefix + in, Values: []string{"value", "new"}})
	code, out, _ := runDLQOn(c, specs, "purge", dlqOptions{olderThan: 24 * time.Hour})
	if code != 0 || !strings.Contains(out, "would delete 2") {
		t.Fatalf("exit %d:\n%s", code, out)
	}
	code, out, _ = runDLQOn(c, specs, "purge", dlqOptions{olderThan: 24 * time.Hour, yes: true})
	left := c.XRange(ctx, dlqPrefix+in, "-", "+").Val()
	if code != 0 || len(left) != 1 || left[0].Values["value"] != "new" {
		t.Fatalf("exit %d, left %v:\n%s", code, left, out)
	}
}

func TestDLQUsage(t *testing.T) {
	if code := runDLQ(nil); code != 2 {
		t.Fatalf("no action: %d", code)
	}
	if code := runDLQ([]string{"drop"}); code != 2 {
		t.Fatalf("unknown action: %d", code)
	}
}

func TestGlobEscape(t *testing.T) {
	if got := globEscape(`dl*q?[x]\`); got != `dl\*q\?\[x\]\\` {
		t.Fatalf("%s", got)
	}
}
