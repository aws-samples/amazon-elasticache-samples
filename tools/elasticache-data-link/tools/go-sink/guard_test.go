package main

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func TestVersionNewerOrdersStreamIDs(t *testing.T) {
	cases := []struct {
		a, b string
		want bool
	}{
		{"1700000000001-0", "1700000000000-9", true},
		{"1700000000000-2", "1700000000000-1", true},
		{"1700000000000-1", "1700000000000-1", false}, // the same change again
		{"1700000000000-1", "1700000000000-2", false},
		{"999-0", "1000-0", false},           // numeric, not lexicographic
		{"1700000000000-0", "invalid", true}, // a bad stored value never blocks updates to a key
		{"invalid", "1700000000000-0", false},
	}
	for _, c := range cases {
		if got := versionNewer(c.a, c.b); got != c.want {
			t.Errorf("versionNewer(%q, %q) = %v, want %v", c.a, c.b, got, c.want)
		}
	}
}

func TestMsgVersionPrefersTheOriginalOfAReplay(t *testing.T) {
	live := redis.XMessage{ID: "200-0", Values: map[string]interface{}{"value": "{}"}}
	if v := msgVersion(live); v != "200-0" {
		t.Fatalf("live message version = %q", v)
	}
	replayed := redis.XMessage{ID: "900-0", Values: map[string]interface{}{"ecdl_v": "200-0"}}
	if v := msgVersion(replayed); v != "200-0" {
		t.Fatalf("a replayed dead letter must keep its original version, got %q", v)
	}
}

func TestTombstoneSharesTheRowsSlot(t *testing.T) {
	if got := tombstoneKey("orders:42"); got != "{orders:42}:ecdl-del" {
		t.Fatalf("tombstone key = %q", got)
	}
	// A key with its own hash tag cannot be reproduced; no separate tombstone.
	if got := tombstoneKey("{tenant}:orders:42"); got != "{tenant}:orders:42" {
		t.Fatalf("braced key tombstone = %q", got)
	}
}

func TestUpsertArgsNeverLetAColumnOverwriteTheVersion(t *testing.T) {
	args := upsertArgs("5-0", true, map[string]interface{}{"a": "1", versionField: "evil"})
	if args[0] != "5-0" || args[1] != versionField || args[2] != "1" {
		t.Fatalf("header args = %v", args[:3])
	}
	for i := 3; i < len(args); i += 2 {
		if args[i] == versionField {
			t.Fatalf("a source column named %s was passed through", versionField)
		}
	}
	if len(args) != 5 {
		t.Fatalf("expected one field pair, got %v", args[3:])
	}
}

func TestEvalHealth(t *testing.T) {
	const stall = int64(120000)
	start := int64(1_000_000)
	// Fresh start: healthy before anything has happened.
	if ok, _ := evalHealth(start+1000, start, 0, false, stall); !ok {
		t.Fatal("a just-started sink must be healthy")
	}
	// No successful broker read for longer than the stall window.
	if ok, why := evalHealth(start+stall+5000, start, 0, false, stall); ok ||
		!strings.Contains(why, "broker read") {
		t.Fatalf("expected broker-read failure, got ok=%v %q", ok, why)
	}
	// Idle but reading: healthy.
	now := start + 10*stall
	if ok, _ := evalHealth(now, start, now-1000, false, stall); !ok {
		t.Fatal("an idle sink that is reading must be healthy")
	}
	// Holding for the target: no reads are expected, and a restart would not
	// help, so it stays healthy however long the outage lasts.
	if ok, why := evalHealth(now, start, start, true, stall); !ok || !strings.Contains(why, "holding") {
		t.Fatalf("a holding sink must stay healthy, got ok=%v %q", ok, why)
	}
	// The stall is counted again once the hold ends.
	if ok, _ := evalHealth(now, start, start, false, stall); ok {
		t.Fatal("no reads and no hold must fail health")
	}
}

func TestErrLoggerRateLimitsPerKind(t *testing.T) {
	var lines []string
	clock := time.Unix(0, 0)
	l := newErrLogger(10 * time.Second)
	l.now = func() time.Time { return clock }
	l.out = func(s string) { lines = append(lines, s) }

	l.log("target-write", errors.New("boom"))
	l.log("target-write", errors.New("boom"))
	l.log("target-write", errors.New("boom"))
	l.log("broker-read", errors.New("other"))
	if len(lines) != 2 {
		t.Fatalf("want one line per kind, got %v", lines)
	}
	clock = clock.Add(11 * time.Second)
	l.log("target-write", errors.New("boom"))
	if len(lines) != 3 || !strings.Contains(lines[2], "(2 similar") {
		t.Fatalf("want the suppressed count reported, got %v", lines)
	}
	if !strings.HasPrefix(lines[0], "ERROR target-write: ") {
		t.Fatalf("lines must start with ERROR <kind> for the alarm filter: %q", lines[0])
	}
	l.log("x", nil)
	if len(lines) != 3 {
		t.Fatal("a nil error must not log")
	}
}

func TestConsumersToDeleteKeepsAnythingPending(t *testing.T) {
	cons := []redis.XInfoConsumer{
		{Name: "old-task-w1", Pending: 0, Idle: 2 * time.Hour}, // stale, empty: delete
		{Name: "old-task-w2", Pending: 3, Idle: 2 * time.Hour}, // stale but owns work: keep
		{Name: "busy-task-w1", Pending: 0, Idle: time.Second},  // active: keep
		{Name: "me-w1", Pending: 0, Idle: 2 * time.Hour},       // ours: keep
	}
	got := consumersToDelete(cons, time.Hour, "me")
	if len(got) != 1 || got[0] != "old-task-w1" {
		t.Fatalf("consumersToDelete = %v", got)
	}
}

func TestEntryAgeMs(t *testing.T) {
	if got := entryAgeMs("1000-5", 4000); got != 3000 {
		t.Fatalf("age = %d", got)
	}
	if got := entryAgeMs("5000-0", 4000); got != 0 {
		t.Fatalf("a future id must read 0, got %d", got)
	}
	if got := entryAgeMs("nope", 4000); got != 0 {
		t.Fatalf("bad id = %d", got)
	}
}

func TestReplayValuesCarryTheOriginalVersion(t *testing.T) {
	dl := redis.XMessage{ID: "9-0", Values: map[string]interface{}{
		"value": `{"op":"u"}`, "key": `{"id":1}`, "orig_id": "3-0", "ecdl_v": "3-0"}}
	v, err := replayValues(dl)
	if err != nil {
		t.Fatal(err)
	}
	if v["ecdl_v"] != "3-0" || v["value"] != `{"op":"u"}` || v["key"] != `{"id":1}` {
		t.Fatalf("replay values = %v", v)
	}
	// Older dead letters have only orig_id.
	old := redis.XMessage{ID: "9-1", Values: map[string]interface{}{"value": "x", "orig_id": "4-0"}}
	if v, _ := replayValues(old); v["ecdl_v"] != "4-0" {
		t.Fatalf("fallback version = %v", v["ecdl_v"])
	}
	if _, err := replayValues(redis.XMessage{ID: "1-0", Values: map[string]interface{}{}}); err == nil {
		t.Fatal("a dead letter with no payload cannot be replayed")
	}
}
