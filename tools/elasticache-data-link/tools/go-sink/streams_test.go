package main

import (
	"strings"
	"testing"
)

// env builds a getenv function from a map, so parseStreams can be tested without
// touching the process environment.
func env(kv map[string]string) func(string) string {
	return func(k string) string { return kv[k] }
}

// ── tableFromStream ────────────────────────────────────────────────────────
// One case per engine, because the segment COUNT differs between them and that
// is exactly why the table is taken from the end rather than by position.

func TestTableFromStreamPerEngine(t *testing.T) {
	cases := []struct{ engine, stream, want string }{
		{"mysql", "data:checkout.Inventory.orders", "orders"},
		{"postgresql", "data:checkout.public.orders", "orders"},
		{"sqlserver", "data:checkout.Inventory.dbo.orders", "orders"},
		{"oracle", "data:checkout.ADMIN.ORDERS3W", "ORDERS3W"},
	}
	for _, c := range cases {
		if got := tableFromStream(c.stream); got != c.want {
			t.Errorf("%s: tableFromStream(%q) = %q, want %q", c.engine, c.stream, got, c.want)
		}
	}
}

func TestTableFromStreamNoDots(t *testing.T) {
	// The benchmark harnesses use bare names like data:orders.
	if got := tableFromStream("data:orders"); got != "orders" {
		t.Errorf("got %q, want orders", got)
	}
}

func TestTableFromStreamBare(t *testing.T) {
	if got := tableFromStream("orders"); got != "orders" {
		t.Errorf("got %q, want orders", got)
	}
}

func TestTableFromStreamEmpty(t *testing.T) {
	// A trailing dot leaves nothing to take. Must return "" so the caller can
	// refuse, rather than producing keys named ":123".
	for _, s := range []string{"data:checkout.Inventory.", "", "   "} {
		if got := tableFromStream(s); got != "" {
			t.Errorf("tableFromStream(%q) = %q, want empty", s, got)
		}
	}
}

// ── parseStreams: the shipped form ─────────────────────────────────────────

func TestParseStreamsMultiTableDerivesTables(t *testing.T) {
	specs, err := parseStreams(env(map[string]string{
		"STREAMS": "data:ck.Inventory.orders,data:ck.Inventory.customers,data:ck.Inventory.products",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(specs) != 3 {
		t.Fatalf("got %d specs, want 3", len(specs))
	}
	want := []string{"orders", "customers", "products"}
	for i, w := range want {
		if specs[i].table != w {
			t.Errorf("spec %d table = %q, want %q", i, specs[i].table, w)
		}
	}
}

func TestParseStreamsExplicitTableWins(t *testing.T) {
	// An explicit table must override derivation: a topic whose last segment is
	// not the table name is the reason the override exists.
	specs, err := parseStreams(env(map[string]string{
		"STREAMS": "data:ck.Inventory.order_items=orders,data:ck.Inventory.customers",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if specs[0].table != "orders" {
		t.Errorf("explicit table not honoured: got %q", specs[0].table)
	}
	if specs[1].table != "customers" {
		t.Errorf("derived table wrong: got %q", specs[1].table)
	}
}

func TestParseStreamsToleratesWhitespaceAndTrailingComma(t *testing.T) {
	specs, err := parseStreams(env(map[string]string{
		"STREAMS": " data:ck.db.a , data:ck.db.b ,",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(specs) != 2 || specs[0].key != "data:ck.db.a" || specs[1].key != "data:ck.db.b" {
		t.Errorf("got %+v", specs)
	}
}

// ── parseStreams: backward compatibility ───────────────────────────────────

func TestParseStreamsLegacySinglePair(t *testing.T) {
	specs, err := parseStreams(env(map[string]string{
		"STREAM_KEY": "data:ck.Inventory.orders", "STREAM_TABLE": "orders",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(specs) != 1 || specs[0].key != "data:ck.Inventory.orders" || specs[0].table != "orders" {
		t.Errorf("got %+v", specs)
	}
}

func TestParseStreamsLegacyStreamKeyDerivesTable(t *testing.T) {
	// STREAM_KEY without STREAM_TABLE used to default the table to "customers",
	// which silently wrote every row under the wrong key prefix.
	specs, err := parseStreams(env(map[string]string{"STREAM_KEY": "data:ck.Inventory.orders"}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if specs[0].table != "orders" {
		t.Errorf("table = %q, want orders", specs[0].table)
	}
}

func TestParseStreamsLegacyStreamKeysList(t *testing.T) {
	specs, err := parseStreams(env(map[string]string{"STREAM_KEYS": "data:ck.db.a,data:ck.db.b"}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(specs) != 2 {
		t.Fatalf("got %d, want 2", len(specs))
	}
}

func TestParseStreamsPrecedence(t *testing.T) {
	// STREAMS takes precedence over STREAM_KEYS, then STREAM_KEY. The most
	// specific one is used, and the others must not contribute extra streams.
	specs, err := parseStreams(env(map[string]string{
		"STREAMS":     "data:ck.db.fromstreams",
		"STREAM_KEYS": "data:ck.db.fromkeys",
		"STREAM_KEY":  "data:ck.db.fromkey",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(specs) != 1 || specs[0].table != "fromstreams" {
		t.Errorf("got %+v, want only fromstreams", specs)
	}

	specs, err = parseStreams(env(map[string]string{
		"STREAM_KEYS": "data:ck.db.fromkeys",
		"STREAM_KEY":  "data:ck.db.fromkey",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(specs) != 1 || specs[0].table != "fromkeys" {
		t.Errorf("got %+v, want only fromkeys", specs)
	}
}

// ── parseStreams: refusals ─────────────────────────────────────────────────

func TestParseStreamsNothingConfigured(t *testing.T) {
	// No stream default. Consuming a stream that was not configured is worse than
	// refusing to start, and the old default was data:ht.Inventory.customers.
	_, err := parseStreams(env(map[string]string{}))
	if err == nil {
		t.Fatal("expected an error when no stream is configured")
	}
	if !strings.Contains(err.Error(), "STREAMS") {
		t.Errorf("error should name the variable to set: %v", err)
	}
}

func TestParseStreamsDuplicateStreamRefused(t *testing.T) {
	// Deduplicating would hide a real misconfiguration (two tables whose topics
	// collide), so this is an error.
	_, err := parseStreams(env(map[string]string{
		"STREAMS": "data:ck.db.a,data:ck.db.a",
	}))
	if err == nil {
		t.Fatal("expected an error for a duplicated stream")
	}
	if !strings.Contains(err.Error(), "twice") {
		t.Errorf("error should say the stream is listed twice: %v", err)
	}
}

func TestParseStreamsUnderivableTableRefused(t *testing.T) {
	_, err := parseStreams(env(map[string]string{"STREAMS": "data:ck.db."}))
	if err == nil {
		t.Fatal("expected an error when the table cannot be derived")
	}
	if !strings.Contains(err.Error(), "=<table>") {
		t.Errorf("error should show how to name the table explicitly: %v", err)
	}
}

func TestParseStreamsEmptyListRefused(t *testing.T) {
	_, err := parseStreams(env(map[string]string{"STREAMS": " , , "}))
	if err == nil {
		t.Fatal("expected an error for a list with no usable entries")
	}
}

// ── XREADGROUP argument shape ──────────────────────────────────────────────

func TestReadArgsShape(t *testing.T) {
	// Redis wants every stream name first, then one id per stream. Interleaving
	// them is accepted syntactically and then reads the wrong thing.
	specs := []streamSpec{
		{key: "s1", table: "a"}, {key: "s2", table: "b"}, {key: "s3", table: "c"},
	}
	got := readArgs(specs)
	want := []string{"s1", "s2", "s3", ">", ">", ">"}
	if len(got) != len(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("got %v, want %v", got, want)
		}
	}
}

func TestReadArgsSingleStream(t *testing.T) {
	got := readArgs([]streamSpec{{key: "s1", table: "a"}})
	if len(got) != 2 || got[0] != "s1" || got[1] != ">" {
		t.Errorf("got %v, want [s1 >]", got)
	}
}

// ── lookup helpers ─────────────────────────────────────────────────────────

func TestTableForKnownAndUnknown(t *testing.T) {
	specs := []streamSpec{{key: "s1", table: "orders"}, {key: "s2", table: "customers"}}
	if tbl, ok := tableFor(specs, "s2"); !ok || tbl != "customers" {
		t.Errorf("s2 -> %q, %v", tbl, ok)
	}
	// An unknown stream must report NOT ok. worker() refuses to apply such a
	// batch, because attributing rows to the wrong table corrupts the cache.
	if _, ok := tableFor(specs, "s9"); ok {
		t.Error("unknown stream reported as known")
	}
}

func TestStreamKeysOfPreservesOrder(t *testing.T) {
	specs := []streamSpec{{key: "s1", table: "a"}, {key: "s2", table: "b"}}
	got := streamKeysOf(specs)
	if len(got) != 2 || got[0] != "s1" || got[1] != "s2" {
		t.Errorf("got %v", got)
	}
}

func TestDescribeStreamsNamesTargetKeyPattern(t *testing.T) {
	// The startup line should let an operator see which cache keys each stream
	// will produce, since that is the thing they will look for in the target.
	got := describeStreams([]streamSpec{{key: "data:ck.db.orders", table: "orders"}})
	if !strings.Contains(got, "data:ck.db.orders") || !strings.Contains(got, "orders:*") {
		t.Errorf("got %q", got)
	}
}
