package main

// A row written by the bulk loader carries LOAD_VERSION (the broker time at which
// capture was paused) in the version field. Changes captured before the pause are
// older than the loaded data and must be skipped; changes after the resume are
// newer and must apply. Runs against a real Valkey (see guard_integration_test.go).

import (
	"context"
	"testing"
)

func TestIntegrationLoadedRowVersusOlderAndNewerChanges(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	if err := loadScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	key := "itest:loaded:1"
	c.Del(ctx, key, tombstoneKey(key))
	// As the loader writes it: the row's fields plus the load version.
	c.HSet(ctx, key, "qty", "10", versionField, "1000-0")

	// A change captured before the pause (still pending when the load ran).
	if ok, err := runUpsert(ctx, c, key, "999-7", false, map[string]interface{}{"qty": "9"}); err != nil || ok {
		t.Fatalf("older change: applied=%v err=%v; must be skipped", ok, err)
	}
	if ok, err := runDelete(ctx, c, key, "999-8"); err != nil || ok {
		t.Fatalf("older delete: applied=%v err=%v; must be skipped", ok, err)
	}
	if v := c.HGet(ctx, key, "qty").Val(); v != "10" {
		t.Fatalf("loaded value changed by an older change: %q", v)
	}
	// The first change after capture resumed.
	if ok, err := runUpsert(ctx, c, key, "1000-1", false, map[string]interface{}{"qty": "11"}); err != nil || !ok {
		t.Fatalf("newer change: applied=%v err=%v; must apply", ok, err)
	}
	if v := c.HGet(ctx, key, "qty").Val(); v != "11" {
		t.Fatalf("newer change not applied: %q", v)
	}
	// A later delete applies and leaves a tombstone an older retry cannot pass.
	if ok, err := runDelete(ctx, c, key, "2000-0"); err != nil || !ok {
		t.Fatalf("newer delete: applied=%v err=%v", ok, err)
	}
	if ok, _ := runUpsert(ctx, c, key, "1500-0", false, map[string]interface{}{"qty": "15"}); ok {
		t.Fatal("an older upsert recreated a deleted row")
	}
}

func TestIntegrationUnversionedLoadedRowAcceptsAnyChange(t *testing.T) {
	// A load run without LOAD_VERSION writes no version: every change applies.
	c, ctx := testValkey(t), context.Background()
	if err := loadScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	key := "itest:loaded:2"
	c.Del(ctx, key, tombstoneKey(key))
	c.HSet(ctx, key, "qty", "10")
	if ok, err := runUpsert(ctx, c, key, "1-0", false, map[string]interface{}{"qty": "1"}); err != nil || !ok {
		t.Fatalf("change over an unversioned row: applied=%v err=%v", ok, err)
	}
}
