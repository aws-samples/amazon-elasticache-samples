package main

// Denormalization against a real Valkey (skipped unless ECDL_TEST_VALKEY is
// set; see guard_integration_test.go). Every scenario runs twice: without a
// key prefix and with one, since X5 was prefix-only.

import (
	"context"
	"encoding/json"
	"fmt"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

const denormSpecJSON = `{"tables":{"orders":{"denormalize":[{"parent_table":"%s","join_field":"cid",
 "parent_key_prefix":"%s","fields":["name","tier"],"prefix":"c_","on_parent_delete":"%s"}]},
 "customers":{},"products":{}}}`

// dn sets up a link with orders -> customers and returns helpers.
type dn struct {
	t   *testing.T
	c   redis.UniversalClient
	ctx context.Context
}

func setupDenorm(t *testing.T, prefix, parentTable, onDelete string) *dn {
	t.Helper()
	c, ctx := testValkey(t), context.Background()
	c.FlushDB(ctx)
	oldSpec, oldPrefix := transformSpec, keyPrefix
	t.Cleanup(func() { transformSpec, keyPrefix, beforeChildWrite = oldSpec, oldPrefix, nil })
	keyPrefix = normalizeKeyPrefix(prefix)
	s, err := LoadTransformSpec([]byte(fmt.Sprintf(denormSpecJSON, parentTable, keyPrefix+"customers:", onDelete)))
	if err != nil {
		t.Fatal(err)
	}
	transformSpec = s
	if err := loadDenormScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	return &dn{t: t, c: c, ctx: ctx}
}

func evMsg(id, op, table string, pk int, before, after map[string]interface{}) redis.XMessage {
	e := map[string]interface{}{"op": op, "source": map[string]string{"table": table}}
	if before != nil {
		e["before"] = before
	}
	if after != nil {
		e["after"] = after
	}
	b, _ := json.Marshal(e)
	return redis.XMessage{ID: id, Values: map[string]interface{}{
		"value": string(b), "key": fmt.Sprintf(`{"id":%d}`, pk)}}
}

func row(kv ...interface{}) map[string]interface{} {
	m := map[string]interface{}{}
	for i := 0; i+1 < len(kv); i += 2 {
		m[kv[i].(string)] = kv[i+1]
	}
	return m
}

func (d *dn) apply(m redis.XMessage, table string) {
	d.t.Helper()
	if err := processDenorm(d.ctx, d.c, m, table); err != nil {
		d.t.Fatalf("processDenorm %s %s: %v", table, m.ID, err)
	}
}

func (d *dn) customer(id, ver, op string, pk int, name, tier string) {
	d.apply(evMsg(ver, op, "customers", pk, nil, row("id", pk, "name", name, "tier", tier)), "customers")
}

func (d *dn) order(ver, op string, pk, cid int, qty int, before map[string]interface{}) {
	var after map[string]interface{}
	if op != "d" {
		after = row("id", pk, "cid", cid, "qty", qty)
	}
	d.apply(evMsg(ver, op, "orders", pk, before, after), "orders")
}

func (d *dn) get(table string, pk int, f string) string {
	return d.c.HGet(d.ctx, rowKey(table, fmt.Sprint(pk)), f).Val()
}

func (d *dn) want(table string, pk int, f, v string) {
	d.t.Helper()
	if got := d.get(table, pk, f); got != v {
		d.t.Fatalf("%s %s.%s = %q, want %q", rowKey(table, fmt.Sprint(pk)), table, f, got, v)
	}
}

func (d *dn) exists(table string, pk int) bool {
	return d.c.Exists(d.ctx, rowKey(table, fmt.Sprint(pk))).Val() == 1
}

func (d *dn) inIdx(parentPK, childPK int) bool {
	return d.c.SIsMember(d.ctx, idxKey("orders", "customers", fmt.Sprint(parentPK)), fmt.Sprint(childPK)).Val()
}

func eachPrefix(t *testing.T, f func(t *testing.T, prefix string)) {
	for _, p := range []string{"", "v2"} {
		t.Run("prefix="+p, func(t *testing.T) { f(t, p) })
	}
}

func TestIntegrationDenormEmbedAndFanOut(t *testing.T) {
	eachPrefix(t, func(t *testing.T, p string) {
		d := setupDenorm(t, p, "customers", "null")
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		d.order("200-0", "c", 1, 7, 5, nil)
		d.want("orders", 1, "c_name", "Ann")
		d.want("orders", 1, "qty", "5")
		d.want("orders", 1, versionField, "200-0")
		d.want("orders", 1, "_ecdl_fk:customers:cid", "7")
		d.want("orders", 1, "_ecdl_pv:customers:cid", "100-0")
		if !d.inIdx(7, 1) {
			t.Fatal("child not in the prefixed reverse index")
		}
		// Parent update fans out.
		d.apply(evMsg("300-0", "u", "customers", 7, row("id", 7, "name", "Ann", "tier", "gold"),
			row("id", 7, "name", "Anna", "tier", "gold")), "customers")
		d.want("orders", 1, "c_name", "Anna")
		d.want("orders", 1, "_ecdl_pv:customers:cid", "300-0")
		d.want("orders", 1, versionField, "200-0") // the child's own version is untouched
		if p != "" && d.c.Exists(d.ctx, "orders:1", "customers:7", "idx:orders:by:customers:7").Val() != 0 {
			t.Fatal("an unprefixed key was written on a prefixed link")
		}
	})
}

func TestIntegrationDenormOlderChildChangeIsSkipped(t *testing.T) { // X4
	eachPrefix(t, func(t *testing.T, p string) {
		d := setupDenorm(t, p, "customers", "null")
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		d.order("300-0", "u", 1, 7, 5, nil)
		d.order("200-0", "u", 1, 7, 1, nil) // older retry arrives late
		d.want("orders", 1, "qty", "5")
		d.want("orders", 1, versionField, "300-0")
	})
}

func TestIntegrationDenormDeleteIsNotResurrected(t *testing.T) { // X4 + X5
	eachPrefix(t, func(t *testing.T, p string) {
		d := setupDenorm(t, p, "customers", "null")
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		d.order("200-0", "c", 1, 7, 5, nil)
		d.order("400-0", "d", 1, 0, 0, row("id", 1, "cid", 7, "qty", 5))
		if d.exists("orders", 1) {
			t.Fatal("delete missed the row")
		}
		if d.inIdx(7, 1) {
			t.Fatal("deleted child left in the reverse index")
		}
		d.order("300-0", "u", 1, 7, 9, nil) // older update retried after the delete
		if d.exists("orders", 1) {
			t.Fatal("an older update recreated a deleted row")
		}
		// A parent update must not recreate it through a fan-out either.
		d.c.SAdd(d.ctx, idxKey("orders", "customers", "7"), "1") // stale index entry
		d.customer("c", "500-0", "u", 7, "Zed", "gold")
		if d.exists("orders", 1) {
			t.Fatal("a fan-out recreated a deleted child")
		}
		d.order("600-0", "c", 1, 7, 2, nil) // a genuine re-insert applies
		d.want("orders", 1, "qty", "2")
		d.want("orders", 1, "c_name", "Zed")
	})
}

func TestIntegrationDenormDeleteTombstoneFollowsTheBacklog(t *testing.T) {
	d := setupDenorm(t, "", "customers", "null")
	defer atomic.StoreInt64(&tombstoneTTLNow, int64(tombstoneTTL))
	atomic.StoreInt64(&tombstoneTTLNow, 9999)
	d.order("200-0", "c", 1, 7, 5, nil)
	d.order("300-0", "d", 1, 0, 0, row("id", 1, "cid", 7))
	if ttl := d.c.TTL(d.ctx, tombstoneKey(rowKey("orders", "1"))).Val(); ttl < 9990*time.Second {
		t.Fatalf("denorm tombstone TTL %v, want the current 9999s", ttl)
	}
}

func TestIntegrationDenormDeleteWithKeyOnlyBeforeImage(t *testing.T) {
	// PostgreSQL without REPLICA IDENTITY FULL sends only the key: the index is
	// still cleaned, from the fk stored on the row.
	d := setupDenorm(t, "v2", "customers", "null")
	d.customer("c", "100-0", "c", 7, "Ann", "gold")
	d.order("200-0", "c", 1, 7, 5, nil)
	d.order("300-0", "d", 1, 0, 0, row("id", 1))
	if d.exists("orders", 1) || d.inIdx(7, 1) {
		t.Fatal("key-only delete left the row or its index entry")
	}
}

func TestIntegrationDenormOlderParentRetryDoesNotRevertChildren(t *testing.T) {
	eachPrefix(t, func(t *testing.T, p string) {
		d := setupDenorm(t, p, "customers", "null")
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		d.order("200-0", "c", 1, 7, 5, nil)
		d.customer("c", "400-0", "u", 7, "New", "gold")
		d.customer("c", "300-0", "u", 7, "Old", "gold") // retried late
		d.want("customers", 7, "name", "New")
		d.want("orders", 1, "c_name", "New")
		// Even a fan-out that reaches the child directly with the older version
		// is refused by the per-parent version.
		ok, err := runEmbed(d.ctx, d.c, rowKey("orders", "1"), transformSpec.DenormFor("orders")[0], "7", "300-0",
			map[string]string{"c_name": "Old"})
		if err != nil || ok {
			t.Fatalf("older embed: ok=%v err=%v", ok, err)
		}
	})
}

func TestIntegrationDenormForeignKeyChangeMovesIndex(t *testing.T) {
	eachPrefix(t, func(t *testing.T, p string) {
		d := setupDenorm(t, p, "customers", "null")
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		d.customer("c", "110-0", "c", 8, "Bob", "silver")
		d.order("200-0", "c", 1, 7, 5, nil)
		d.order("300-0", "u", 1, 8, 5, row("id", 1, "cid", 7, "qty", 5))
		d.want("orders", 1, "c_name", "Bob")
		if d.inIdx(7, 1) || !d.inIdx(8, 1) {
			t.Fatal("index not moved to the new parent")
		}
		d.customer("c", "400-0", "u", 7, "Ann2", "gold") // old parent changes
		d.want("orders", 1, "c_name", "Bob")
		d.customer("c", "500-0", "u", 8, "Bob2", "silver") // new parent changes
		d.want("orders", 1, "c_name", "Bob2")
	})
}

func TestIntegrationDenormStaleIndexEntryCannotCrossWrite(t *testing.T) {
	d := setupDenorm(t, "v2", "customers", "null")
	d.customer("c", "100-0", "c", 7, "Ann", "gold")
	d.customer("c", "110-0", "c", 8, "Bob", "silver")
	d.order("200-0", "c", 1, 8, 5, nil)
	// A stale entry: an older change (cid 7) was SADDed and then skipped.
	d.order("150-0", "u", 1, 7, 1, nil)
	if !d.inIdx(7, 1) {
		t.Skip("stale SADD not produced; nothing to check")
	}
	d.customer("c", "300-0", "u", 7, "Ann2", "gold")
	d.want("orders", 1, "c_name", "Bob")
	d.want("orders", 1, "qty", "5")
}

func TestIntegrationDenormParentChangedDuringChildWrite(t *testing.T) { // rule 4
	eachPrefix(t, func(t *testing.T, p string) {
		d := setupDenorm(t, p, "customers", "null")
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		once := false
		beforeChildWrite = func() {
			if once {
				return
			}
			once = true
			// The parent update lands, and fans out, between the child's lookup
			// and its write: the child is in the index but not yet written.
			if err := processDenorm(d.ctx, d.c, evMsg("300-0", "u", "customers", 7, nil,
				row("id", 7, "name", "Anna", "tier", "gold")), "customers"); err != nil {
				t.Fatal(err)
			}
		}
		d.order("200-0", "c", 1, 7, 5, nil)
		d.want("orders", 1, "c_name", "Anna")
		d.want("orders", 1, "_ecdl_pv:customers:cid", "300-0")
	})
}

func TestIntegrationDenormChildBeforeParent(t *testing.T) {
	eachPrefix(t, func(t *testing.T, p string) {
		d := setupDenorm(t, p, "customers", "null")
		d.order("200-0", "c", 1, 7, 5, nil)
		d.want("orders", 1, "c_name", "")
		d.customer("c", "300-0", "c", 7, "Ann", "gold")
		d.want("orders", 1, "c_name", "Ann")
	})
}

func TestIntegrationDenormParentDeleteNull(t *testing.T) {
	eachPrefix(t, func(t *testing.T, p string) {
		d := setupDenorm(t, p, "customers", "null")
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		d.order("200-0", "c", 1, 7, 5, nil)
		d.apply(evMsg("300-0", "d", "customers", 7, row("id", 7), nil), "customers")
		if d.exists("customers", 7) {
			t.Fatal("parent not deleted")
		}
		d.want("orders", 1, "c_name", "")
		d.want("orders", 1, "qty", "5")
		// An older parent update retried after the delete restores neither the
		// parent nor the children's embed.
		d.customer("c", "250-0", "u", 7, "Ghost", "gold")
		if d.exists("customers", 7) {
			t.Fatal("deleted parent recreated")
		}
		d.want("orders", 1, "c_name", "")
		// A child written after the delete embeds blanks, versioned by the delete.
		d.order("400-0", "c", 2, 7, 1, nil)
		d.want("orders", 2, "c_name", "")
		d.want("orders", 2, "_ecdl_pv:customers:cid", "300-0")
		// The parent comes back: both children get it.
		d.customer("c", "500-0", "c", 7, "Back", "gold")
		d.want("orders", 1, "c_name", "Back")
		d.want("orders", 2, "c_name", "Back")
	})
}

func TestIntegrationDenormParentDeleteLeave(t *testing.T) {
	d := setupDenorm(t, "v2", "customers", "leave")
	d.customer("c", "100-0", "c", 7, "Ann", "gold")
	d.order("200-0", "c", 1, 7, 5, nil)
	d.apply(evMsg("300-0", "d", "customers", 7, row("id", 7), nil), "customers")
	d.want("orders", 1, "c_name", "Ann")
	d.order("400-0", "u", 1, 7, 6, nil) // a later child change keeps the last values
	d.want("orders", 1, "c_name", "Ann")
	d.want("orders", 1, "qty", "6")
}

func TestIntegrationDenormReplayFinishesAPartialFanOut(t *testing.T) {
	d := setupDenorm(t, "v2", "customers", "null")
	d.customer("c", "100-0", "c", 7, "Ann", "gold")
	d.order("200-0", "c", 1, 7, 5, nil)
	// First attempt wrote the parent row and then failed before the fan-out.
	m := evMsg("300-0", "u", "customers", 7, nil, row("id", 7, "name", "Anna", "tier", "gold"))
	if _, _, err := scriptReply(denormUpsertScript.Run(d.ctx, d.c,
		[]string{rowKey("customers", "7"), tombstoneKey(rowKey("customers", "7"))},
		denormUpsertArgs("300-0", false, map[string]string{"id": "7", "name": "Anna", "tier": "gold"}, nil)...).Result()); err != nil {
		t.Fatal(err)
	}
	d.want("orders", 1, "c_name", "Ann")
	d.apply(m, "customers") // the retry: the row is already at 300-0
	d.want("orders", 1, "c_name", "Anna")
	d.apply(m, "customers") // and again: still a no-op
	d.want("orders", 1, "c_name", "Anna")
}

func TestIntegrationDenormSnapshotReplaceKeepsEmbed(t *testing.T) {
	d := setupDenorm(t, "", "customers", "null")
	d.customer("c", "100-0", "c", 7, "Ann", "gold")
	d.order("200-0", "c", 1, 7, 5, nil)
	d.c.HSet(d.ctx, rowKey("orders", "1"), "dropped_col", "x")
	d.order("300-0", "r", 1, 7, 5, nil)
	if d.c.HExists(d.ctx, rowKey("orders", "1"), "dropped_col").Val() {
		t.Fatal("snapshot read kept a field from the old shape")
	}
	d.want("orders", 1, "c_name", "Ann")
	d.want("orders", 1, "_ecdl_fk:customers:cid", "7")
}

func TestIntegrationDenormFilteredChildLeavesCacheAndIndex(t *testing.T) {
	d := setupDenorm(t, "v2", "customers", "null")
	transformSpec.Tables["orders"] = TableSpec{Denormalize: transformSpec.Tables["orders"].Denormalize,
		Filter: &FilterSpec{Field: "qty", Op: ">", Value: "0"}}
	d.customer("c", "100-0", "c", 7, "Ann", "gold")
	d.order("200-0", "c", 1, 7, 5, nil)
	d.order("300-0", "u", 1, 7, 0, nil)
	if d.exists("orders", 1) || d.inIdx(7, 1) {
		t.Fatal("filtered-out child left the row or its index entry")
	}
}

func TestIntegrationDenormNullForeignKey(t *testing.T) {
	d := setupDenorm(t, "v2", "customers", "null")
	d.apply(evMsg("200-0", "c", "orders", 1, nil, row("id", 1, "cid", nil, "qty", 5)), "orders")
	d.want("orders", 1, "c_name", "")
	d.want("orders", 1, "qty", "5")
	n, _ := d.c.Keys(d.ctx, keyPrefix+"idx:*").Result()
	if len(n) != 0 {
		t.Fatalf("index written for a null foreign key: %v", n)
	}
}

func TestIntegrationDenormLegacyChildGetsFanOut(t *testing.T) {
	// A child written by an older sink has no fk field; it still receives
	// parent updates so an upgrade does not freeze embeds.
	d := setupDenorm(t, "", "customers", "null")
	d.c.HSet(d.ctx, rowKey("orders", "1"), "id", "1", "cid", "7", "c_name", "Old")
	d.c.SAdd(d.ctx, idxKey("orders", "customers", "7"), "1")
	d.customer("c", "300-0", "c", 7, "New", "gold")
	d.want("orders", 1, "c_name", "New")
}

func TestIntegrationDenormLoadedChildFollowsLaterChanges(t *testing.T) {
	// Exactly what the bulk loader writes (bulk_loader.py denorm flush): the
	// load version, the fk and the embedded parent's version.
	eachPrefix(t, func(t *testing.T, p string) {
		d := setupDenorm(t, p, "customers", "null")
		d.c.HSet(d.ctx, rowKey("customers", "7"), "id", "7", "name", "Ann", "tier", "gold", versionField, "1000-0")
		d.c.HSet(d.ctx, rowKey("orders", "1"), "id", "1", "cid", "7", "qty", "5", "c_name", "Ann", "c_tier", "gold",
			versionField, "1000-0", "_ecdl_fk:customers:cid", "7", "_ecdl_pv:customers:cid", "1000-0")
		d.c.SAdd(d.ctx, idxKey("orders", "customers", "7"), "1")
		d.customer("c", "900-0", "u", 7, "Before", "gold") // captured before the pause
		d.want("orders", 1, "c_name", "Ann")
		d.customer("c", "1100-0", "u", 7, "After", "gold")
		d.want("orders", 1, "c_name", "After")
		d.order("950-0", "u", 1, 7, 1, nil) // child change from before the pause
		d.want("orders", 1, "qty", "5")
	})
}

func TestIntegrationDenormSchemaQualifiedParent(t *testing.T) {
	d := setupDenorm(t, "v2", "Inventory.customers", "null")
	d.customer("c", "100-0", "c", 7, "Ann", "gold")
	d.order("200-0", "c", 1, 7, 5, nil)
	d.customer("c", "300-0", "u", 7, "Anna", "gold")
	d.want("orders", 1, "c_name", "Anna")
}

func TestIntegrationDenormSourceColumnCannotForgeBookkeeping(t *testing.T) {
	d := setupDenorm(t, "", "customers", "null")
	d.customer("c", "100-0", "c", 7, "Ann", "gold")
	d.apply(evMsg("200-0", "c", "orders", 1, nil,
		row("id", 1, "cid", 7, "_ecdl_v", "999999-0", "_ecdl_fk:customers:cid", "8")), "orders")
	d.want("orders", 1, versionField, "200-0")
	d.want("orders", 1, "_ecdl_fk:customers:cid", "7")
}

func TestIntegrationDenormNoScriptIsRecovered(t *testing.T) {
	d := setupDenorm(t, "", "customers", "null")
	d.customer("c", "100-0", "c", 7, "Ann", "gold")
	d.order("200-0", "c", 1, 7, 5, nil)
	d.c.ScriptFlush(d.ctx)
	d.customer("c", "300-0", "u", 7, "Anna", "gold") // fan-out uses EVALSHA in a pipeline
	d.want("orders", 1, "c_name", "Anna")
}

func TestIntegrationSinkApplyRoutesPerTable(t *testing.T) {
	d := setupDenorm(t, "v2", "customers", "null")
	if err := loadScripts(d.ctx, d.c); err != nil {
		t.Fatal(err)
	}
	apply := sinkApply("envelope")
	m := evMsg("200-0", "c", "products", 1, nil, row("id", 1, "sku", "a"))
	if err := apply(d.ctx, d.c, m, "products"); err != nil {
		t.Fatal(err)
	}
	if err := apply(d.ctx, d.c, evMsg("100-0", "u", "products", 1, nil, row("id", 1, "sku", "old")), "products"); err != nil {
		t.Fatal(err)
	}
	d.want("products", 1, "sku", "a") // the plain table stays guarded
	if d.c.HExists(d.ctx, rowKey("products", "1"), "_ecdl_fk:customers:cid").Val() {
		t.Fatal("a table outside every rule went through the denorm path")
	}
}
