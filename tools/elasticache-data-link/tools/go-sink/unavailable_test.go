package main

// Jira story F035: Debezium's unavailable-value placeholder (an unchanged PostgreSQL TOAST
// column, an unchanged Oracle LOB) must never overwrite a cached value.
//
// The unit tests need no infrastructure. The integration tests run against a
// real Valkey (ECDL_TEST_VALKEY, see guard_integration_test.go): the plain
// path through both the batched fast path (applyBatch) and the retry path
// (processOne), with the version guard, and the denormalization path.

import (
	"context"
	"encoding/base64"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

const ph = defaultUnavailablePlaceholder

var phBase64 = base64.StdEncoding.EncodeToString([]byte(ph))

func TestUnavailableFormsDefaultAndOverride(t *testing.T) {
	text, bin := unavailableForms("")
	if text != ph || bin != phBase64 {
		t.Fatalf("empty -> %q %q, want Debezium's default", text, bin)
	}
	text, bin = unavailableForms("ZZZ")
	if text != "ZZZ" || bin != base64.StdEncoding.EncodeToString([]byte("ZZZ")) {
		t.Fatalf("override -> %q %q", text, bin)
	}
	// Debezium reads "hex:" as hex-encoded bytes for binary columns.
	text, bin = unavailableForms("hex:0102ff")
	if text != "hex:0102ff" || bin != base64.StdEncoding.EncodeToString([]byte{1, 2, 0xff}) {
		t.Fatalf("hex -> %q %q", text, bin)
	}
}

func TestIsUnavailableShapes(t *testing.T) {
	yes := []interface{}{ph, phBase64, []interface{}{ph}, []interface{}{ph, ph}}
	no := []interface{}{"", "x", ph + " ", "prefix" + ph, nil, 5, []interface{}{},
		[]interface{}{ph, "x"}, map[string]interface{}{"a": ph}, true}
	for _, v := range yes {
		if !isUnavailable(v) {
			t.Errorf("isUnavailable(%#v) = false", v)
		}
	}
	for _, v := range no {
		if isUnavailable(v) {
			t.Errorf("isUnavailable(%#v) = true", v)
		}
	}
}

func TestDropUnavailable(t *testing.T) {
	in := map[string]interface{}{"id": "1", "doc": ph, "blob": phBase64, "qty": "2"}
	out, dropped := dropUnavailable(in)
	if len(out) != 2 || out["id"] != "1" || out["qty"] != "2" {
		t.Fatalf("kept %v", out)
	}
	if strings.Join(dropped, ",") != "blob,doc" {
		t.Fatalf("dropped %v, want sorted blob,doc", dropped)
	}
	if len(in) != 4 {
		t.Fatal("the input row was modified")
	}
	clean := map[string]interface{}{"id": "1"}
	if out, dropped := dropUnavailable(clean); dropped != nil || len(out) != 1 {
		t.Fatalf("clean row: %v %v", out, dropped)
	}
}

func phEvent(op string) string {
	return fmt.Sprintf(`{"op":%q,"source":{"table":"docs"},"after":{"id":1,"title":"t2","body":%q}}`, op, ph)
}

func TestPlanMessageDropsThePlaceholder(t *testing.T) {
	transformSpec = nil
	t.Cleanup(func() { transformSpec = nil })
	for _, op := range []string{"u", "c"} {
		key, f, del, poison, replace := planMessage(msgWithValue(phEvent(op)), "envelope", "docs")
		if poison || del || replace || key != "docs:1" {
			t.Fatalf("%s: key=%q del=%v poison=%v replace=%v", op, key, del, poison, replace)
		}
		if _, ok := f["body"]; ok {
			t.Fatalf("%s: the placeholder field was planned for writing: %v", op, f)
		}
		if f["title"] != "t2" || f["id"] != "1" {
			t.Fatalf("%s: the other fields were lost: %v", op, f)
		}
	}
	// A snapshot read with a placeholder merges instead of replacing: a
	// replace would delete the cached value of the field it cannot write.
	_, _, _, _, replace := planMessage(msgWithValue(phEvent("r")), "envelope", "docs")
	if replace {
		t.Fatal("snapshot read with a placeholder must not replace the key")
	}
	_, _, _, _, replace = planMessage(msgWithValue(
		`{"op":"r","source":{"table":"docs"},"after":{"id":1,"body":"full"}}`), "envelope", "docs")
	if !replace {
		t.Fatal("a complete snapshot read must still replace the key")
	}
	// Flat events: dropped too.
	_, f, _, _, _ := planMessage(redis.XMessage{ID: "1-0", Values: map[string]interface{}{
		"value": fmt.Sprintf(`{"id":1,"body":%q,"n":3}`, ph)}}, "flat", "docs")
	if _, ok := f["body"]; ok || f["n"] != "3" {
		t.Fatalf("flat: %v", f)
	}
}

// A placeholder field renamed by a transform is still left out: it is dropped
// before the transform runs.
func TestPlanMessageDropsAPlaceholderBeforeRename(t *testing.T) {
	s, err := LoadTransformSpec([]byte(`{"tables":{"docs":{"rename":{"body":"text"}}}}`))
	if err != nil {
		t.Fatal(err)
	}
	transformSpec = s
	t.Cleanup(func() { transformSpec = nil })
	_, f, _, _, _ := planMessage(msgWithValue(phEvent("u")), "envelope", "docs")
	if _, ok := f["text"]; ok {
		t.Fatalf("renamed placeholder written: %v", f)
	}
	if f["title"] != "t2" {
		t.Fatalf("fields: %v", f)
	}
}

// captureLog swaps the error logger's output for the test.
func captureLog(t *testing.T) func() []string {
	var mu sync.Mutex
	var lines []string
	old := errLog
	errLog = newErrLogger(time.Hour)
	errLog.out = func(s string) { mu.Lock(); lines = append(lines, s); mu.Unlock() }
	t.Cleanup(func() { errLog = old })
	return func() []string { mu.Lock(); defer mu.Unlock(); return append([]string(nil), lines...) }
}

func TestWarnOnlyForCreateAndSnapshot(t *testing.T) {
	transformSpec = nil
	logs := captureLog(t)
	planMessage(msgWithValue(phEvent("u")), "envelope", "docs")
	if got := logs(); len(got) != 0 {
		t.Fatalf("an update logged: %v", got)
	}
	planMessage(msgWithValue(phEvent("c")), "envelope", "docs")
	got := logs()
	if len(got) != 1 || !strings.HasPrefix(got[0], "WARN ") ||
		!strings.Contains(got[0], "docs") || !strings.Contains(got[0], "body") {
		t.Fatalf("create: %v, want one WARN naming docs and body", got)
	}
	// Per table: a second table is not suppressed by the first.
	planMessage(msgWithValue(strings.Replace(phEvent("r"), `"docs"`, `"notes"`, 1)), "envelope", "notes")
	if got := logs(); len(got) != 2 || !strings.Contains(got[1], "notes") ||
		!strings.Contains(got[1], "snapshot read") {
		t.Fatalf("snapshot: %v", got)
	}
}

// ── real Valkey ────────────────────────────────────────────────────────────

func docMsg(id, op string, after map[string]interface{}) redis.XMessage {
	return evMsg(id, op, "docs", 1, nil, after)
}

func TestIntegrationPlaceholderKeepsCachedValueRetryPath(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	c.FlushDB(ctx)
	transformSpec = nil
	if err := loadScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	for _, guard := range []bool{true, false} {
		t.Run(fmt.Sprintf("guard=%v", guard), func(t *testing.T) {
			old := versionGuard
			versionGuard = guard
			t.Cleanup(func() { versionGuard = old })
			c.FlushDB(ctx)
			key := rowKey("docs", "1")
			apply := func(m redis.XMessage) {
				t.Helper()
				if err := processOne(ctx, c, m, "envelope", "docs"); err != nil {
					t.Fatalf("processOne %s: %v", m.ID, err)
				}
			}
			apply(docMsg("100-0", "r", row("id", 1, "title", "t1", "body", "LARGE")))
			apply(docMsg("200-0", "u", row("id", 1, "title", "t2", "body", ph, "img", phBase64)))
			if got := c.HGet(ctx, key, "body").Val(); got != "LARGE" {
				t.Fatalf("body = %q: the placeholder overwrote the cached value", got)
			}
			if c.HExists(ctx, key, "img").Val() {
				t.Fatal("binary placeholder written")
			}
			if got := c.HGet(ctx, key, "title").Val(); got != "t2" {
				t.Fatalf("title = %q: the other fields were not applied", got)
			}
			if guard {
				if got := c.HGet(ctx, key, versionField).Val(); got != "200-0" {
					t.Fatalf("version = %q", got)
				}
				// The guard still works: an older retry is skipped.
				apply(docMsg("150-0", "u", row("id", 1, "title", "old", "body", "OLD")))
				if c.HGet(ctx, key, "title").Val() != "t2" || c.HGet(ctx, key, "body").Val() != "LARGE" {
					t.Fatal("an older change overwrote a newer one")
				}
			}
			// The column itself changes: the new value is written.
			apply(docMsg("300-0", "u", row("id", 1, "title", "t3", "body", "NEW")))
			if got := c.HGet(ctx, key, "body").Val(); got != "NEW" {
				t.Fatalf("body = %q after a real change", got)
			}
		})
	}
}

func TestIntegrationPlaceholderKeepsCachedValueFastPath(t *testing.T) {
	broker, ctx := testValkey(t), context.Background()
	broker.FlushDB(ctx)
	transformSpec = nil
	if err := loadScripts(ctx, broker); err != nil {
		t.Fatal(err)
	}
	stream := "data:t.public.docs"
	broker.HSet(ctx, rowKey("docs", "1"), "id", "1", "title", "t1", "body", "LARGE", versionField, "1-0")
	for id, v := range map[int]string{
		1: fmt.Sprintf(`{"op":"u","source":{"table":"docs"},"after":{"id":1,"title":"t2","body":%q}}`, ph),
		2: fmt.Sprintf(`{"op":"r","source":{"table":"docs"},"after":{"id":2,"title":"n","body":%q}}`, ph),
	} {
		broker.XAdd(ctx, &redis.XAddArgs{Stream: stream, Values: map[string]interface{}{
			"key": fmt.Sprintf(`{"id":%d}`, id), "value": v}})
	}
	if err := broker.XGroupCreateMkStream(ctx, stream, "g", "0").Err(); err != nil {
		t.Fatal(err)
	}
	res, err := broker.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "w",
		Streams: []string{stream, ">"}, Count: 10}).Result()
	if err != nil {
		t.Fatal(err)
	}
	applyBatch(ctx, broker, broker, stream, "g", res[0].Messages, "envelope", "docs")
	if got := broker.HGet(ctx, rowKey("docs", "1"), "body").Val(); got != "LARGE" {
		t.Fatalf("body = %q: the placeholder overwrote the cached value", got)
	}
	if got := broker.HGet(ctx, rowKey("docs", "1"), "title").Val(); got != "t2" {
		t.Fatalf("title = %q", got)
	}
	// A new row's placeholder field is simply absent, never the placeholder.
	if broker.HExists(ctx, rowKey("docs", "2"), "body").Val() ||
		broker.HGet(ctx, rowKey("docs", "2"), "title").Val() != "n" {
		t.Fatalf("new row: %v", broker.HGetAll(ctx, rowKey("docs", "2")).Val())
	}
	if n := broker.XPending(ctx, stream, "g").Val().Count; n != 0 {
		t.Fatalf("%d entries left pending: a placeholder is not a failure", n)
	}
}

func TestIntegrationDenormPlaceholderKeepsOwnAndEmbeddedValues(t *testing.T) {
	eachPrefix(t, func(t *testing.T, p string) {
		d := setupDenorm(t, p, "customers", "null")
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		d.order("200-0", "c", 1, 7, 5, nil)
		d.want("orders", 1, "c_tier", "gold")
		// Child: an update with a placeholder keeps the cached own field.
		d.apply(evMsg("210-0", "r", "orders", 1, nil, row("id", 1, "cid", 7, "qty", 5, "note", "N1")), "orders")
		d.apply(evMsg("220-0", "u", "orders", 1, nil, row("id", 1, "cid", 7, "qty", 6, "note", ph)), "orders")
		d.want("orders", 1, "note", "N1")
		d.want("orders", 1, "qty", "6")
		d.want("orders", 1, "c_name", "Ann")
		d.want("orders", 1, versionField, "220-0")
		// The guard still applies on this path: an older change is skipped.
		d.apply(evMsg("215-0", "u", "orders", 1, nil, row("id", 1, "cid", 7, "qty", 1, "note", "old")), "orders")
		d.want("orders", 1, "qty", "6")
		d.want("orders", 1, "note", "N1")
		// Parent: an embedded field that is unavailable is not blanked on the
		// child, and the available one fans out.
		d.apply(evMsg("300-0", "u", "customers", 7, nil, row("id", 7, "name", "Anna", "tier", ph)), "customers")
		d.want("customers", 7, "tier", "gold")
		d.want("customers", 7, "name", "Anna")
		d.want("orders", 1, "c_tier", "gold")
		d.want("orders", 1, "c_name", "Anna")
		// A snapshot read of the child with a placeholder merges (no replace).
		d.apply(evMsg("400-0", "r", "orders", 1, nil, row("id", 1, "cid", 7, "qty", 8, "note", ph)), "orders")
		d.want("orders", 1, "note", "N1")
		d.want("orders", 1, "qty", "8")
	})
}
