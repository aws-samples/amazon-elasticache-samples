package main

// TRUNCATE against a real Valkey (Jira story F006). ECDL_TEST_VALKEY for a single node,
// ECDL_TEST_VALKEY_CLUSTER for a cluster.

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

// truncSetup resets the marker cache and the resweep for a test.
func truncSetup(t *testing.T, c redis.UniversalClient, prefix string) context.Context {
	t.Helper()
	ctx := context.Background()
	savedPrefix, savedResweep, savedGuard := keyPrefix, truncateResweep, versionGuard
	keyPrefix, truncateResweep, versionGuard = normalizeKeyPrefix(prefix), 0, true
	markers.reset()
	t.Cleanup(func() {
		keyPrefix, truncateResweep, versionGuard = savedPrefix, savedResweep, savedGuard
		markers.reset()
	})
	if err := loadScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	return ctx
}

func mustUpsert(t *testing.T, c redis.UniversalClient, key, version string) {
	t.Helper()
	if ok, err := runUpsert(context.Background(), c, key, version, false, map[string]interface{}{"v": version}); err != nil || !ok {
		t.Fatalf("upsert %s %s: ok=%v err=%v", key, version, ok, err)
	}
}

func exists(c redis.UniversalClient, key string) bool {
	return c.Exists(context.Background(), key).Val() == 1
}

// One table's rows and older delete markers go; newer rows, other tables,
// keys of other links' prefixes and keys that are not rows stay.
func TestIntegrationTruncateDeletesOnlyThatTable(t *testing.T) { // Jira story F006
	eachPrefix(t, func(t *testing.T, prefix string) {
		c := testValkey(t)
		c.FlushDB(context.Background())
		ctx := truncSetup(t, c, prefix)
		other := "other:orders:1" // another link, prefix "other"
		if prefix != "" {
			other = "orders:1" // another link without a prefix
		}
		for i := 1; i <= 50; i++ {
			mustUpsert(t, c, rowKey("orders", fmt.Sprint(i)), "100-0")
		}
		mustUpsert(t, c, rowKey("orders", "51"), "300-0") // inserted after the truncate
		mustUpsert(t, c, rowKey("customers", "1"), "100-0")
		mustUpsert(t, c, rowKey("orders_archive", "1"), "100-0")
		mustUpsert(t, c, other, "100-0")
		c.Set(ctx, rowKey("orders", "note"), "not a row", 0)
		if _, err := runDelete(ctx, c, rowKey("orders", "60"), "150-0"); err != nil { // older delete marker
			t.Fatal(err)
		}
		if _, err := runDelete(ctx, c, rowKey("orders", "61"), "250-0"); err != nil { // newer delete marker
			t.Fatal(err)
		}

		res, err := runTruncate(ctx, c, "orders", "orders", "200-0")
		if err != nil {
			t.Fatal(err)
		}
		if res.rows != 50 || res.tombstones != 1 {
			t.Fatalf("deleted %d rows and %d markers, want 50 and 1", res.rows, res.tombstones)
		}
		for i := 1; i <= 50; i++ {
			if exists(c, rowKey("orders", fmt.Sprint(i))) {
				t.Fatalf("%s survived the truncate", rowKey("orders", fmt.Sprint(i)))
			}
		}
		for _, k := range []string{rowKey("orders", "51"), rowKey("customers", "1"), rowKey("orders_archive", "1"),
			other, rowKey("orders", "note"), tombstoneKey(rowKey("orders", "61"))} {
			if !exists(c, k) {
				t.Fatalf("%s was deleted", k)
			}
		}
		if exists(c, tombstoneKey(rowKey("orders", "60"))) {
			t.Fatal("the older delete marker was kept")
		}
		// Applying it again deletes nothing more.
		if res, err := runTruncate(ctx, c, "orders", "orders", "200-0"); err != nil || res.rows != 0 {
			t.Fatalf("second run: %+v %v", res, err)
		}
	})
}

// A change from before the truncate, retried or reclaimed after it, does not
// bring its row back; a change after it applies.
func TestIntegrationTruncateBlocksOlderChanges(t *testing.T) { // Jira story F006
	c := testValkey(t)
	c.FlushDB(context.Background())
	ctx := truncSetup(t, c, "")
	if _, err := runTruncate(ctx, c, "orders", "orders", "200-0"); err != nil {
		t.Fatal(err)
	}
	stale0 := staleSkipped
	res := writeOps(ctx, c, []targetOp{
		{id: "1", version: "150-0", key: "orders:1", table: "orders", fields: map[string]interface{}{"v": "old"}},
		{id: "2", version: "250-0", key: "orders:2", table: "orders", fields: map[string]interface{}{"v": "new"}},
		{id: "3", version: "150-0", key: "orders:3", table: "orders", del: true},
		{id: "4", version: "150-0", key: "customers:1", table: "customers", fields: map[string]interface{}{"v": "c"}},
	})
	if res.unconfirmed() != 0 {
		t.Fatalf("unconfirmed: %v", res.lastErr)
	}
	if applied(res.cmds[0]) || applied(res.cmds[2]) {
		t.Fatal("a change older than the truncate counted as applied")
	}
	if staleSkipped-stale0 != 2 {
		t.Fatalf("StaleSkipped +%d, want 2", staleSkipped-stale0)
	}
	if exists(c, "orders:1") || exists(c, tombstoneKey("orders:3")) {
		t.Fatal("a change older than the truncate was written")
	}
	if c.HGet(ctx, "orders:2", "v").Val() != "new" || !exists(c, "customers:1") {
		t.Fatal("a change after the truncate, or of another table, was not written")
	}
	// The single-change path (sinkApply) is filtered too.
	m := redis.XMessage{ID: "150-1", Values: map[string]interface{}{"key": `{"id":9}`,
		"value": `{"op":"c","source":{"table":"orders"},"after":{"id":9}}`}}
	if err := processOne(ctx, c, m, "envelope", "orders"); err != nil || exists(c, "orders:9") {
		t.Fatalf("processOne wrote a change older than the truncate (err %v)", err)
	}
}

// A write that read the marker before it was set and landed during the first
// scan is removed by the second.
func TestIntegrationTruncateResweepCatchesARacingWrite(t *testing.T) { // Jira story F006
	c := testValkey(t)
	c.FlushDB(context.Background())
	ctx := truncSetup(t, c, "")
	truncateResweep = time.Millisecond
	saved := sleep
	t.Cleanup(func() { sleep = saved })
	sleep = func(time.Duration) {
		// What another task's in-flight batch does between the two scans.
		c.HSet(ctx, "orders:7", "v", "late", versionField, "150-0")
		c.HSet(ctx, "orders:8", "v", "after", versionField, "250-0")
	}
	res, err := runTruncate(ctx, c, "orders", "orders", "200-0")
	if err != nil {
		t.Fatal(err)
	}
	if exists(c, "orders:7") || res.rows != 1 {
		t.Fatalf("the racing older write survived (rows %d)", res.rows)
	}
	if !exists(c, "orders:8") {
		t.Fatal("a newer write was deleted")
	}
}

func truncStream(t *testing.T, c redis.UniversalClient, stream string, events []string) []redis.XMessage {
	t.Helper()
	ctx := context.Background()
	c.Del(ctx, stream, dlqPrefix+stream)
	for i, e := range events {
		key := "default" // Debezium's null key
		if e != "t" {
			key = fmt.Sprintf(`{"id":%d}`, i+1)
		}
		val := `{"before":null,"after":null,"source":{"table":"orders"},"op":"t"}`
		if e != "t" {
			val = fmt.Sprintf(`{"op":"%s","source":{"table":"orders"},"after":{"id":%d,"v":"x"}}`, e, i+1)
		}
		c.XAdd(ctx, &redis.XAddArgs{Stream: stream, Values: map[string]interface{}{"key": key, "value": val}})
	}
	c.XGroupCreateMkStream(ctx, stream, "g", "0")
	res, err := c.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "w", Streams: []string{stream, ">"},
		Count: int64(len(events))}).Result()
	if err != nil {
		t.Fatal(err)
	}
	return res[0].Messages
}

// Through the live path: the truncate splits the batch, rows before it go,
// rows after it stay, and every entry is acked (nothing dead-lettered).
func TestIntegrationTruncateInABatch(t *testing.T) { // Jira story F006
	for _, guard := range []bool{true, false} {
		t.Run(fmt.Sprintf("guard=%v", guard), func(t *testing.T) {
			c := testValkey(t)
			c.FlushDB(context.Background())
			ctx := truncSetup(t, c, "")
			versionGuard = guard
			const stream = "data:t.orders"
			msgs := truncStream(t, c, stream, []string{"c", "c", "t", "c"})
			applyBatch(ctx, c, c, stream, "g", msgs, "envelope", "orders")
			if exists(c, "orders:1") || exists(c, "orders:2") {
				t.Fatal("rows before the truncate survived")
			}
			if !exists(c, "orders:4") {
				t.Fatal("the row inserted after the truncate was deleted")
			}
			if n := c.XPending(ctx, stream, "g").Val().Count; n != 0 {
				t.Fatalf("%d entries left pending", n)
			}
			if n := c.XLen(ctx, dlqPrefix+stream).Val(); n != 0 {
				t.Fatalf("%d entries dead-lettered", n)
			}
			if c.Get(ctx, truncateMarkerKey("orders")).Val() != msgs[2].ID {
				t.Fatal("no truncate marker at the truncate's version")
			}
		})
	}
}

// A change from before the truncate that was left pending is reclaimed after
// it: skipped and acked, not written. A truncate that is itself reclaimed is
// applied.
func TestIntegrationTruncateReclaim(t *testing.T) { // Jira story F006
	c := testValkey(t)
	c.FlushDB(context.Background())
	ctx := truncSetup(t, c, "")
	savedIdle := retryIdle
	retryIdle = 0
	t.Cleanup(func() { retryIdle = savedIdle })
	const stream = "data:t.orders"
	msgs := truncStream(t, c, stream, []string{"c", "t", "c"})
	// The truncate and the newer change apply; the first change "failed".
	applyBatch(ctx, c, c, stream, "g", msgs[1:], "envelope", "orders")
	claimed, deliveries, err := claimIdle(ctx, c, stream, "g", "r")
	if err != nil || len(claimed) != 1 {
		t.Fatalf("claim: %d %v", len(claimed), err)
	}
	applyReclaimed(ctx, c, c, stream, "g", "orders", "envelope", claimed, deliveries)
	if exists(c, "orders:1") {
		t.Fatal("a reclaimed change from before the truncate brought its row back")
	}
	if !exists(c, "orders:3") || c.XPending(ctx, stream, "g").Val().Count != 0 {
		t.Fatal("not all applied and acked")
	}

	// A truncate that failed on the live path is applied by reclaim.
	c.FlushDB(ctx)
	markers.reset()
	msgs = truncStream(t, c, stream, []string{"c", "t"})
	applyBatch(ctx, c, c, stream, "g", msgs[:1], "envelope", "orders")
	claimed, deliveries, _ = claimIdle(ctx, c, stream, "g", "r")
	applyReclaimed(ctx, c, c, stream, "g", "orders", "envelope", claimed, deliveries)
	if exists(c, "orders:1") || c.XPending(ctx, stream, "g").Val().Count != 0 {
		t.Fatal("a reclaimed truncate was not applied and acked")
	}
}

// Denormalized tables: a truncated child leaves its parents' indexes; a
// truncated parent's children are blanked, except children of a parent
// inserted again after the truncate.
func TestIntegrationTruncateDenorm(t *testing.T) { // Jira story F006
	eachPrefix(t, func(t *testing.T, prefix string) {
		d := setupDenorm(t, prefix, "customers", "null")
		truncSetup(t, d.c, prefix)
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		d.customer("c", "100-1", "c", 8, "Bob", "blue")
		d.order("110-0", "c", 1, 7, 5, nil)
		d.order("110-1", "c", 2, 7, 5, nil)
		d.order("110-2", "c", 3, 8, 5, nil)

		// Child truncate at 200-0, then a new order 4 for customer 7.
		d.apply(evMsg("200-0", "t", "orders", 0, nil, nil), "orders")
		d.order("210-0", "c", 4, 7, 1, nil)
		if d.exists("orders", 1) || d.exists("orders", 3) {
			t.Fatal("child rows survived their table's truncate")
		}
		if d.inIdx(7, 1) || d.inIdx(7, 2) || d.inIdx(8, 3) {
			t.Fatal("index entries of truncated children were kept")
		}
		if !d.inIdx(7, 4) {
			t.Fatal("the child added after the truncate left the index")
		}
		// An older child change replayed now does nothing.
		d.order("150-0", "u", 2, 7, 9, nil)
		if d.exists("orders", 2) || d.inIdx(7, 2) {
			t.Fatal("a child change older than the truncate came back")
		}

		// Parent truncate at 300-0. Customer 8 was inserted again at 310-0 and
		// applied first (another worker): it and its child's embed stay.
		d.order("220-0", "c", 6, 8, 1, nil)
		d.customer("c", "310-0", "c", 8, "Bea", "red")
		d.want("orders", 6, "c_name", "Bea")
		d.apply(evMsg("300-0", "t", "customers", 0, nil, nil), "customers")
		if d.exists("customers", 7) {
			t.Fatal("a parent row survived its table's truncate")
		}
		if !d.exists("customers", 8) {
			t.Fatal("the parent inserted after the truncate was deleted")
		}
		d.want("orders", 4, "c_name", "") // parent 7 is gone: blanked
		d.want("orders", 6, "c_name", "Bea")
		// An older parent change replayed now does not fan out.
		d.customer("c", "250-0", "u", 7, "Old", "x")
		d.want("orders", 4, "c_name", "")
		if d.exists("customers", 7) {
			t.Fatal("a parent change older than the truncate came back")
		}
	})
}

// Cluster: every primary is swept.
func TestIntegrationTruncateCluster(t *testing.T) { // Jira story F006
	addr := os.Getenv("ECDL_TEST_VALKEY_CLUSTER")
	if addr == "" {
		t.Skip("ECDL_TEST_VALKEY_CLUSTER not set")
	}
	c := redis.NewClusterClient(&redis.ClusterOptions{Addrs: []string{addr}})
	defer c.Close()
	ctx := truncSetup(t, c, "")
	if err := c.ForEachMaster(ctx, func(ctx context.Context, n *redis.Client) error { return n.FlushDB(ctx).Err() }); err != nil {
		t.Fatal(err)
	}
	nodes := map[string]bool{}
	for i := 0; i < 300; i++ {
		k := rowKey("orders", fmt.Sprint(i))
		mustUpsert(t, c, k, "100-0")
		n, _ := c.MasterForKey(ctx, k)
		nodes[n.Options().Addr] = true
		if i%3 == 0 {
			if _, err := runDelete(ctx, c, rowKey("orders", fmt.Sprintf("d%d", i)), "150-0"); err != nil {
				t.Fatal(err)
			}
		}
	}
	if len(nodes) < 3 {
		t.Fatalf("rows on %d primaries, want 3", len(nodes))
	}
	mustUpsert(t, c, rowKey("orders", "new"), "300-0")
	mustUpsert(t, c, rowKey("customers", "1"), "100-0")
	res, err := runTruncate(ctx, c, "orders", "orders", "200-0")
	if err != nil {
		t.Fatal(err)
	}
	if res.rows != 300 || res.tombstones != 100 {
		t.Fatalf("deleted %d rows and %d markers, want 300 and 100", res.rows, res.tombstones)
	}
	if !exists(c, rowKey("orders", "new")) || !exists(c, rowKey("customers", "1")) {
		t.Fatal("a newer row or another table was deleted")
	}
	r := writeOps(ctx, c, []targetOp{{id: "1", version: "150-0", key: rowKey("orders", "5"), table: "orders",
		fields: map[string]interface{}{"v": "old"}}})
	if r.unconfirmed() != 0 || exists(c, rowKey("orders", "5")) || applied(r.cmds[0]) {
		t.Fatal("an older change was written after the truncate")
	}
}

// A truncate is acked only once its deletes are on the replica.
func TestIntegrationTruncateWaitsForTheReplica(t *testing.T) { // Jira story F006 + Jira story F032
	p, r := testReplica(t)
	p.FlushDB(context.Background())
	ctx := truncSetup(t, p, "")
	useReplication(t, newReplicaWait(1, 200*time.Millisecond))
	target := hookedClient(t, p.Options().Addr)
	const stream = "data:t.orders"
	msgs := truncStream(t, p, stream, []string{"c", "t"})
	applyBatch(ctx, p, target, stream, "g", msgs[:1], "envelope", "orders")
	waitFor(t, "the row on the replica", 5*time.Second, func() bool { return r.Exists(ctx, "orders:1").Val() == 1 })
	resume := pause(t, processID(t, r))
	applyBatch(ctx, p, target, stream, "g", msgs[1:], "envelope", "orders")
	if exists(p, "orders:1") {
		t.Fatal("the truncate did not run on the primary")
	}
	if n := p.XPending(ctx, stream, "g").Val().Count; n != 1 {
		t.Fatalf("pending %d, want the unconfirmed truncate", n)
	}
	resume()
	waitFor(t, "the replica", 10*time.Second, func() bool { return confirmReplicated(ctx, target) == nil })
	applyBatch(ctx, p, target, stream, "g", msgs[1:], "envelope", "orders")
	if n := p.XPending(ctx, stream, "g").Val().Count; n != 0 {
		t.Fatalf("pending %d after the replica caught up", n)
	}
}
