package main

// Jira story F043: a replay from an older position is reported as "catching up".

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/redis/go-redis/v9"
)

func envelope(tsMs int64, payload bool) string {
	src := map[string]interface{}{"table": "orders", "ts_ms": tsMs}
	e := map[string]interface{}{"op": "u", "source": src, "after": map[string]interface{}{"id": 1}}
	if payload {
		e = map[string]interface{}{"payload": e}
	}
	b, _ := json.Marshal(e)
	return string(b)
}

func TestReplayAgeMs(t *testing.T) {
	const at = int64(1_700_000_600_000)
	id := fmt.Sprintf("%d-0", at)
	for _, c := range []struct {
		name string
		m    *redis.XMessage
		want int64
	}{
		{"live change", &redis.XMessage{ID: id, Values: map[string]interface{}{"value": envelope(at-800, false)}}, 800},
		{"replayed change", &redis.XMessage{ID: id, Values: map[string]interface{}{"value": envelope(at-3_600_000, false)}}, 3_600_000},
		{"wrapped in payload", &redis.XMessage{ID: id, Values: map[string]interface{}{"value": envelope(at-5000, true)}}, 5000},
		{"dead letter replayed with its version",
			&redis.XMessage{ID: id, Values: map[string]interface{}{"value": envelope(at-3_600_000, false),
				"ecdl_v": fmt.Sprintf("%d-3", at-3_599_000)}}, 1000},
		{"source clock ahead", &redis.XMessage{ID: id, Values: map[string]interface{}{"value": envelope(at+500, false)}}, 0},
		{"no source time (flat)", &redis.XMessage{ID: id, Values: map[string]interface{}{"value": `{"id":1}`}}, 0},
		{"zero source time", &redis.XMessage{ID: id, Values: map[string]interface{}{"value": envelope(0, false)}}, 0},
		{"not json", &redis.XMessage{ID: id, Values: map[string]interface{}{"value": "{"}}, 0},
		{"no value", &redis.XMessage{ID: id, Values: map[string]interface{}{}}, 0},
		{"no entry", &redis.XMessage{}, 0},
		{"nil", nil, 0},
	} {
		if got := replayAgeMs(c.m); got != c.want {
			t.Errorf("%s: %d, want %d", c.name, got, c.want)
		}
	}
}

func TestCatchingUpLines(t *testing.T) {
	got := catchingUp([]tableReading{{table: "orders", replayMs: 7_260_000}, {table: "items", replayMs: 60_000},
		{table: "users", replayMs: 61_000}, {table: "empty"}}, 60)
	if len(got) != 2 || !strings.HasPrefix(got[0], "orders: applying changes committed up to 2h1m0s") ||
		!strings.HasPrefix(got[1], "users:") || !strings.Contains(got[0], "can show older values") {
		t.Fatalf("got %q", got)
	}
}

func TestIntegrationCatchingUpFromTheBroker(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	warns := captureWarnings(t)
	specs := []streamSpec{{key: "itest:cu:orders", table: "orders"}, {key: "itest:cu:items", table: "items"}}
	c.Del(ctx, specs[0].key, specs[1].key)
	defer c.Del(ctx, specs[0].key, specs[1].key)
	now := c.Time(ctx).Val().UnixMilli()
	add := func(stream string, at, committed int64, seq int) string {
		id := fmt.Sprintf("%d-%d", at, seq)
		if err := c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: id,
			Values: []string{"value", envelope(committed, false)}}).Err(); err != nil {
			t.Fatal(err)
		}
		return id
	}
	m := &freshnessMonitor{specs: specs}
	// A live table: captured within a second of the commit.
	add(specs[1].key, now-2000, now-2600, 0)
	// orders replays the load window: captured now, committed 2 h ago.
	first := add(specs[0].key, now-1000, now-7_200_000, 0)
	last := add(specs[0].key, now-500, now-7_100_000, 0)
	plain, _ := datumsByTable(m.datums(ctx, c, nil))
	if plain["CatchingUpTables"] != 1 {
		t.Fatalf("CatchingUpTables %v, want 1", plain["CatchingUpTables"])
	}
	if w := strings.Join(warns(), "\n"); !strings.Contains(w, "WARN catching-up:orders: applying changes committed up to 1h59m59s before") ||
		strings.Contains(w, "items") {
		t.Fatalf("warnings: %q", w)
	}
	// The oldest replayed change applied; the newest waiting one still counts.
	c.XDel(ctx, specs[0].key, first)
	if plain, _ = datumsByTable(m.datums(ctx, c, nil)); plain["CatchingUpTables"] != 1 {
		t.Fatalf("CatchingUpTables %v with one replayed change left, want 1", plain["CatchingUpTables"])
	}
	// Drained and back to live changes: caught up.
	c.XDel(ctx, specs[0].key, last)
	add(specs[0].key, now, now-300, 0)
	if plain, _ = datumsByTable(m.datums(ctx, c, nil)); plain["CatchingUpTables"] != 0 {
		t.Fatalf("CatchingUpTables %v after the replay, want 0", plain["CatchingUpTables"])
	}
	if len(m.catching) != 0 {
		t.Fatalf("still marked: %v", m.catching)
	}
	// Only the newest waiting change is a replay (the oldest is live): counts.
	add(specs[1].key, now+1, now-3_600_000, 0)
	if plain, _ = datumsByTable(m.datums(ctx, c, nil)); plain["CatchingUpTables"] != 1 {
		t.Fatalf("CatchingUpTables %v, want 1 (items' newest change is a replay)", plain["CatchingUpTables"])
	}
}
