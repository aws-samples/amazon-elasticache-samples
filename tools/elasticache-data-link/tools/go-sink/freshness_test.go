package main

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	cwtypes "github.com/aws/aws-sdk-go-v2/service/cloudwatch/types"
	"github.com/redis/go-redis/v9"
)

var twoTables = []streamSpec{{key: "data:x.db.orders", table: "orders"}, {key: "data:x.db.Customers", table: "Customers"}}

func TestStreamIDMs(t *testing.T) {
	for id, want := range map[string]int64{"1700000000123-7": 1700000000123, "0-0": 0, "": 0, "x-1": 0, "-5-0": 0} {
		if got := streamIDMs(id); got != want {
			t.Errorf("%q: %d, want %d", id, got, want)
		}
	}
}

func TestParseStaleThresholds(t *testing.T) {
	got, err := parseStaleThresholds(" orders=3600, customers = 600 ", twoTables)
	if err != nil || got["orders"] != 3600 || got["Customers"] != 600 || len(got) != 2 {
		t.Fatalf("got %v, %v", got, err)
	}
	if got, err := parseStaleThresholds("", twoTables); err != nil || len(got) != 0 {
		t.Fatalf("empty: %v %v", got, err)
	}
	for _, bad := range []string{"orders", "orders=abc", "orders=59", "=600", "invoices=600", "orders=600,,"} {
		if _, err := parseStaleThresholds(bad, twoTables); err == nil {
			t.Errorf("%q must be refused", bad)
		}
	}
}

func TestStaleTables(t *testing.T) {
	now := int64(10_000_000)
	readings := []tableReading{
		{table: "orders", lastChangeMs: now - 3_700_000}, // 61 min
		{table: "Customers", lastChangeMs: now - 30_000}, // 30 s
		{table: "items", lastChangeMs: now - 9_000_000},  // old but not listed
		{table: "never"}, // no change ever
	}
	th := map[string]int64{"orders": 3600, "Customers": 600, "never": 120}
	got := staleTables(readings, th, now, now-60_000) // sink started 60 s ago
	if len(got) != 1 || !strings.HasPrefix(got[0], "orders: no change captured for 1h1m40s") {
		t.Fatalf("got %v", got)
	}
	// The never-changed table counts from when the sink first looked.
	got = staleTables(readings, th, now, now-121_000)
	if len(got) != 2 || !strings.HasPrefix(got[0], "never:") {
		t.Fatalf("got %v", got)
	}
	if got := staleTables(readings, nil, now, now); len(got) != 0 {
		t.Fatalf("nothing listed, nothing stale: %v", got)
	}
}

func TestFreshAsOf(t *testing.T) {
	if got := freshAsOfMs(tableReading{oldestMs: 5000}, 9000); got != 4999 {
		t.Fatalf("waiting change: %d", got)
	}
	if got := freshAsOfMs(tableReading{}, 9000); got != 9000 {
		t.Fatalf("nothing waiting: %d", got)
	}
	s := freshnessSnapshot{nowMs: 20000, heartbeatMs: 19000, readings: []tableReading{
		{table: "a"}, {table: "b", oldestMs: 12001}}}
	if f, ok := s.cacheFreshnessMs(); !ok || f != 8000 {
		t.Fatalf("freshness %d %v, want 8000 (table b's oldest waiting change)", f, ok)
	}
	s.readings = []tableReading{{table: "a"}}
	s.heartbeatMs = 0
	if _, ok := s.cacheFreshnessMs(); ok {
		t.Fatal("no heartbeat and nothing waiting: unknown, not fresh")
	}
	if got := (freshnessSnapshot{heartbeatMs: 0, readings: []tableReading{{table: "a"}, {table: "b", oldestMs: 7}}}).heartbeatValues(); len(got) != 1 || got["b"] != 6 {
		t.Fatalf("heartbeat values %v", got)
	}
}

func datumsByTable(ds []cwtypes.MetricDatum) (map[string]float64, map[string]float64) {
	plain, tables := map[string]float64{}, map[string]float64{}
	for _, d := range ds {
		table := ""
		for _, dim := range d.Dimensions {
			if *dim.Name == "Table" {
				table = *dim.Value
			}
		}
		if table != "" {
			tables[table] = *d.Value
		} else {
			plain[*d.MetricName] = *d.Value
		}
	}
	return plain, tables
}

func TestIntegrationFreshnessFromTheBroker(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	specs := []streamSpec{{key: "itest:fr:orders", table: "orders"}, {key: "itest:fr:items", table: "items"},
		{key: "itest:fr:empty", table: "empty"}}
	hb := "itest:fr:heartbeat"
	keys := []string{specs[0].key, specs[1].key, specs[2].key, hb}
	c.Del(ctx, keys...)
	defer c.Del(ctx, keys...)
	now := c.Time(ctx).Val().UnixMilli()
	id := func(agoMs int64, seq int) string { return fmt.Sprintf("%d-%d", now-agoMs, seq) }
	add := func(stream, id string) {
		if err := c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: id, Values: []string{"k", "v"}}).Err(); err != nil {
			t.Fatal(err)
		}
	}
	// orders: two changes 2 h and 1 h ago, both applied (deleted by the sink).
	add(specs[0].key, id(7_200_000, 0))
	add(specs[0].key, id(3_600_000, 0))
	c.XDel(ctx, specs[0].key, id(7_200_000, 0), id(3_600_000, 0))
	// items: a change 90 s ago still waiting, and one 30 s ago.
	add(specs[1].key, id(90_000, 0))
	add(specs[1].key, id(30_000, 0))
	add(hb, id(5_000, 0))

	m := &freshnessMonitor{specs: specs, heartbeat: hb,
		thresholds: map[string]int64{"orders": 1800, "items": 600, "empty": 60}}
	plain, tables := datumsByTable(m.datums(ctx, c, nil))
	if a := tables["orders"]; a < 3599 || a > 3610 {
		t.Fatalf("orders: last change age %v, want about 3600 (kept after the entries were deleted)", a)
	}
	if a := tables["items"]; a < 29 || a > 40 {
		t.Fatalf("items: %v", a)
	}
	if _, ok := tables["empty"]; ok {
		t.Fatal("a table with no change ever has no age")
	}
	if plain["StaleTables"] != 1 {
		t.Fatalf("StaleTables %v, want 1 (orders; empty only counts after its threshold)", plain["StaleTables"])
	}
	if f := plain["CacheFreshnessSeconds"]; f < 89 || f > 100 {
		t.Fatalf("CacheFreshnessSeconds %v, want about 90 (items' oldest waiting change)", f)
	}

	// Once the never-changed table has been watched past its threshold, it counts.
	m.startMs = now - 61_000
	plain, _ = datumsByTable(m.datums(ctx, c, nil))
	if plain["StaleTables"] != 2 {
		t.Fatalf("StaleTables %v, want 2", plain["StaleTables"])
	}

	// With items applied, freshness falls back to the heartbeat.
	c.XDel(ctx, specs[1].key, id(90_000, 0), id(30_000, 0))
	plain, _ = datumsByTable(m.datums(ctx, c, nil))
	if f := plain["CacheFreshnessSeconds"]; f < 4 || f > 15 {
		t.Fatalf("CacheFreshnessSeconds %v, want about 5 (the heartbeat)", f)
	}

	// No thresholds: no StaleTables at all.
	m.thresholds = nil
	plain, _ = datumsByTable(m.datums(ctx, c, nil))
	if _, ok := plain["StaleTables"]; ok {
		t.Fatal("StaleTables sent with nothing listed")
	}
}

func TestIntegrationFreshnessBrokerDownSendsNothing(t *testing.T) {
	c := testValkey(t).(*redis.Client)
	dead := redis.NewClient(&redis.Options{Addr: c.Options().Addr, DialTimeout: time.Second})
	dead.Close()
	m := &freshnessMonitor{specs: twoTables, thresholds: map[string]int64{"orders": 600}}
	if got := m.datums(context.Background(), dead, nil); len(got) != 0 {
		t.Fatalf("unreadable broker must send nothing (not 0 stale tables): %v", got)
	}
}

func TestIntegrationHeartbeatKeysHoldFreshAsOf(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	specs := []streamSpec{{key: "itest:frh:orders", table: "orders"}}
	c.Del(ctx, specs[0].key)
	defer c.Del(ctx, specs[0].key)
	now := c.Time(ctx).Val().UnixMilli()
	waiting := fmt.Sprintf("%d-0", now-120_000)
	c.XAdd(ctx, &redis.XAddArgs{Stream: specs[0].key, ID: waiting, Values: []string{"k", "v"}})
	m := &freshnessMonitor{specs: specs}
	snap, err := m.read(ctx, c)
	if err != nil {
		t.Fatal(err)
	}
	if got := snap.heartbeatValues()["orders"]; got != now-120_000-1 {
		t.Fatalf("orders fresh as of %d, want %d: a stalled table must not look fresh", got, now-120_000-1)
	}
}

func TestIntegrationWriteHeartbeats(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	specs := []streamSpec{{key: "itest:frw:orders", table: "orders"}, {key: "itest:frw:items", table: "items"}}
	hb := "itest:frw:hb"
	keys := []string{specs[0].key, specs[1].key, hb, "itest:hbk:orders", "itest:hbk:items"}
	c.Del(ctx, keys...)
	defer c.Del(ctx, keys...)
	now := c.Time(ctx).Val().UnixMilli()
	c.XAdd(ctx, &redis.XAddArgs{Stream: specs[0].key, ID: fmt.Sprintf("%d-0", now-300_000), Values: []string{"k", "v"}})
	c.XAdd(ctx, &redis.XAddArgs{Stream: hb, ID: fmt.Sprintf("%d-0", now-2_000), Values: []string{"k", "v"}})
	m := &freshnessMonitor{specs: specs, heartbeat: hb}
	if err := writeHeartbeats(ctx, m, c, c, "itest:hbk"); err != nil {
		t.Fatal(err)
	}
	if got, _ := c.Get(ctx, "itest:hbk:orders").Int64(); got != now-300_000-1 {
		t.Fatalf("orders key %d, want %d (its change waiting 5 min)", got, now-300_000-1)
	}
	if got, _ := c.Get(ctx, "itest:hbk:items").Int64(); got != now-2_000 {
		t.Fatalf("items key %d, want %d (nothing waiting: the heartbeat)", got, now-2_000)
	}
}
