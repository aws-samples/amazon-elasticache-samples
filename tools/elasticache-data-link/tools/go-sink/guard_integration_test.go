package main

// Runs the version-guard Lua scripts against a real Valkey. Skipped unless
// ECDL_TEST_VALKEY is set, e.g.
//
//	docker run -d -p 127.0.0.1:6399:6379 public.ecr.aws/valkey/valkey:8
//	ECDL_TEST_VALKEY=localhost:6399 go test -run Integration ./...

import (
	"context"
	"os"
	"testing"

	"github.com/redis/go-redis/v9"
)

func testValkey(t *testing.T) redis.UniversalClient {
	addr := os.Getenv("ECDL_TEST_VALKEY")
	if addr == "" {
		t.Skip("ECDL_TEST_VALKEY not set")
	}
	c := redis.NewClient(&redis.Options{Addr: addr})
	if err := c.Ping(context.Background()).Err(); err != nil {
		t.Fatalf("valkey at %s: %v", addr, err)
	}
	return c
}

func TestIntegrationGuardRejectsAnOlderRetry(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	key := "itest:orders:1"
	c.Del(ctx, key, tombstoneKey(key))

	if ok, err := runUpsert(ctx, c, key, "200-0", false, map[string]interface{}{"qty": "5"}); err != nil || !ok {
		t.Fatalf("first write: ok=%v err=%v", ok, err)
	}
	// The retry of an OLDER change arrives late: it must be skipped.
	if ok, err := runUpsert(ctx, c, key, "100-0", false, map[string]interface{}{"qty": "1"}); err != nil || ok {
		t.Fatalf("older retry: ok=%v err=%v (must be skipped)", ok, err)
	}
	if got := c.HGet(ctx, key, "qty").Val(); got != "5" {
		t.Fatalf("qty = %q, the older retry overwrote the newer value", got)
	}
	// The same change again (a replay) is a no-op, not an error.
	if ok, _ := runUpsert(ctx, c, key, "200-0", false, map[string]interface{}{"qty": "5"}); ok {
		t.Fatal("a replay of the same version must be skipped")
	}
	// A newer change applies and records its version.
	if ok, _ := runUpsert(ctx, c, key, "300-1", false, map[string]interface{}{"qty": "7"}); !ok {
		t.Fatal("newer change was skipped")
	}
	if got := c.HGet(ctx, key, versionField).Val(); got != "300-1" {
		t.Fatalf("stored version = %q", got)
	}
}

func TestIntegrationDeleteTombstoneBlocksResurrection(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	key := "itest:orders:2"
	c.Del(ctx, key, tombstoneKey(key))

	runUpsert(ctx, c, key, "100-0", false, map[string]interface{}{"qty": "1"})
	if ok, err := runDelete(ctx, c, key, "200-0"); err != nil || !ok {
		t.Fatalf("delete: ok=%v err=%v", ok, err)
	}
	if c.Exists(ctx, key).Val() != 0 {
		t.Fatal("row still present after delete")
	}
	// An older upsert retried after the delete must not bring the row back.
	if ok, _ := runUpsert(ctx, c, key, "150-0", false, map[string]interface{}{"qty": "9"}); ok {
		t.Fatal("an older upsert recreated a deleted row")
	}
	if c.Exists(ctx, key).Val() != 0 {
		t.Fatal("deleted row recreated")
	}
	if ttl := c.TTL(ctx, tombstoneKey(key)).Val(); ttl <= 0 {
		t.Fatalf("tombstone has no TTL (%v)", ttl)
	}
	// A genuine re-insert after the delete applies and clears the tombstone.
	if ok, _ := runUpsert(ctx, c, key, "250-0", false, map[string]interface{}{"qty": "3"}); !ok {
		t.Fatal("re-insert after delete was skipped")
	}
	if c.Exists(ctx, tombstoneKey(key)).Val() != 0 {
		t.Fatal("tombstone not cleared by the re-insert")
	}
}

func TestIntegrationReplaceDropsOldFieldsAndUnversionedRowsAccept(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	key := "itest:orders:3"
	c.Del(ctx, key, tombstoneKey(key))
	// A row written by the bulk loader has no version: the first CDC change applies.
	c.HSet(ctx, key, "qty", "1", "legacy", "x")
	if ok, _ := runUpsert(ctx, c, key, "100-0", true, map[string]interface{}{"qty": "2"}); !ok {
		t.Fatal("first versioned write over a loaded row was skipped")
	}
	if c.HExists(ctx, key, "legacy").Val() {
		t.Fatal("a replace (snapshot read) kept a field from the old shape")
	}
}

func TestIntegrationPipelinedEvalShaPath(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	key := "itest:orders:4"
	c.Del(ctx, key, tombstoneKey(key))
	if err := loadScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	pipe := c.Pipeline()
	a := queueUpsert(ctx, pipe, key, "500-0", false, map[string]interface{}{"qty": "5"})
	b := queueUpsert(ctx, pipe, key, "400-0", false, map[string]interface{}{"qty": "4"})
	d := queueDelete(ctx, pipe, key, "450-0")
	if _, err := pipe.Exec(ctx); err != nil {
		t.Fatal(err)
	}
	if !applied(a) || applied(b) || applied(d) {
		t.Fatalf("applied = %v %v %v; want true false false", applied(a), applied(b), applied(d))
	}
	if got := c.HGet(ctx, key, "qty").Val(); got != "5" {
		t.Fatalf("qty = %q", got)
	}
}
