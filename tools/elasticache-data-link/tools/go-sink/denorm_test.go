package main

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func testSpec(t *testing.T, js string) *TransformSpec {
	t.Helper()
	s, err := LoadTransformSpec([]byte(js))
	if err != nil {
		t.Fatal(err)
	}
	return s
}

const ordersCustomers = `{"tables":{"orders":{"denormalize":[{"parent_table":"Inventory.customers",
 "join_field":"cid","fields":["name"]}]}}}`

func TestIdxKeyUsesPrefixAndBareParent(t *testing.T) {
	defer func() { keyPrefix = "" }()
	keyPrefix = ""
	if got := idxKey("orders", "Inventory.customers", "7"); got != "idx:orders:by:customers:7" {
		t.Fatalf("no prefix: %q", got)
	}
	keyPrefix = normalizeKeyPrefix("v2")
	if got := idxKey("orders", "customers", "7"); got != "v2:idx:orders:by:customers:7" {
		t.Fatalf("prefix: %q", got)
	}
}

func TestParentKeyDefaultsToRowKey(t *testing.T) {
	defer func() { keyPrefix = "" }()
	keyPrefix = "v2:"
	r := DenormRule{ParentTable: "Inventory.customers"}
	if got := parentKey(r, "7"); got != "v2:customers:7" {
		t.Fatalf("default parent key %q", got)
	}
	r.ParentKeyPrefix = "v2:customers:"
	if got := parentKey(r, "7"); got != "v2:customers:7" {
		t.Fatalf("explicit parent key %q", got)
	}
}

func TestUsesDenormPerTable(t *testing.T) {
	s := testSpec(t, ordersCustomers)
	for table, want := range map[string]bool{"orders": true, "customers": true, "products": false} {
		if got := usesDenorm(s, table); got != want {
			t.Errorf("usesDenorm(%s) = %v", table, got)
		}
	}
	if usesDenorm(nil, "orders") {
		t.Error("nil spec")
	}
}

func TestCheckDenormConfig(t *testing.T) {
	defer func() { keyPrefix = "" }()
	specs := []streamSpec{{key: "data:x.orders", table: "orders"}, {key: "data:x.customers", table: "customers"}}
	s := testSpec(t, ordersCustomers)
	if w, err := checkDenormConfig(s, specs, "envelope", true); err != nil || len(w) != 0 {
		t.Fatalf("valid config: w=%v err=%v", w, err)
	}
	if _, err := checkDenormConfig(s, specs, "flat", true); err == nil {
		t.Fatal("flat format accepted")
	}
	if _, err := checkDenormConfig(s, specs, "envelope", false); err == nil {
		t.Fatal("VERSION_GUARD=false accepted")
	}
	if w, _ := checkDenormConfig(s, specs[:1], "envelope", true); len(w) != 1 || !strings.Contains(w[0], "customers") {
		t.Fatalf("missing parent not warned: %v", w)
	}
	keyPrefix = "v2:"
	bad := testSpec(t, `{"tables":{"orders":{"denormalize":[{"parent_table":"customers","join_field":"cid",
	 "parent_key_prefix":"customers:","fields":["name"]}]}}}`)
	if w, _ := checkDenormConfig(bad, specs, "envelope", true); len(w) != 1 || !strings.Contains(w[0], "v2:customers:") {
		t.Fatalf("prefix mismatch not warned: %v", w)
	}
	empty := testSpec(t, `{"tables":{"orders":{"denormalize":[{"parent_table":"customers","fields":["name"]}]}}}`)
	if _, err := checkDenormConfig(empty, specs, "envelope", true); err == nil {
		t.Fatal("rule without join_field accepted")
	}
	if w, err := checkDenormConfig(nil, nil, "flat", false); err != nil || w != nil {
		t.Fatal("no rules must need nothing")
	}
}

func TestDenormUpsertArgsDropBookkeepingColumns(t *testing.T) {
	r := DenormRule{ParentTable: "customers", JoinField: "cid"}
	args := denormUpsertArgs("5-0", false, map[string]string{
		"qty": "1", "_ecdl_v": "x", "_ecdl_fk:customers:cid": "y", "_ecdl_pv:a:b": "z"},
		[]embedGroup{{rule: r, fk: "7", pv: "3-0", embed: map[string]string{"c_name": "A"}}})
	want := []interface{}{"5-0", versionField, "0", 1, "qty", "1", 1,
		"_ecdl_fk:customers:cid", "7", "_ecdl_pv:customers:cid", "3-0", 1, "c_name", "A"}
	if len(args) != len(want) {
		t.Fatalf("args %v", args)
	}
	for i := range want {
		if args[i] != want[i] {
			t.Fatalf("arg %d = %v, want %v (%v)", i, args[i], want[i], args)
		}
	}
}

func TestProcessDenormPoison(t *testing.T) {
	old := transformSpec
	defer func() { transformSpec = old }()
	transformSpec = testSpec(t, ordersCustomers)
	for name, m := range map[string]redis.XMessage{
		"no value":  {ID: "1-0", Values: map[string]interface{}{}},
		"bad json":  {ID: "1-0", Values: map[string]interface{}{"value": "{"}},
		"no after":  {ID: "1-0", Values: map[string]interface{}{"value": `{"op":"u","source":{"table":"orders"}}`}},
		"no pk":     {ID: "1-0", Values: map[string]interface{}{"value": `{"op":"u","source":{"table":"orders"},"after":{"qty":1}}`}},
		"delete pk": {ID: "1-0", Values: map[string]interface{}{"value": `{"op":"d","source":{"table":"orders"},"before":{"qty":1}}`}},
	} {
		if err := processDenorm(context.Background(), nil, m, "orders"); !errors.Is(err, errPoison) {
			t.Errorf("%s: err = %v, want poison", name, err)
		}
	}
}

func TestProcessDenormRetryStopsOnTransportFailure(t *testing.T) {
	old, oldSleep := transformSpec, sleep
	defer func() { transformSpec, sleep = old, oldSleep }()
	transformSpec = testSpec(t, ordersCustomers)
	var slept []time.Duration
	sleep = func(d time.Duration) { slept = append(slept, d) }
	c := redis.NewClient(&redis.Options{Addr: "127.0.0.1:1", MaxRetries: -1, DialTimeout: 50 * time.Millisecond})
	defer c.Close()
	m := evMsg("1-0", "c", "orders", 1, nil, row("id", 1, "cid", 7))
	err := processDenormRetry(context.Background(), c, m, "orders")
	if err == nil || errors.Is(err, errPoison) {
		t.Fatalf("err = %v, want a transport error", err)
	}
	if len(slept) != writeRetries {
		t.Fatalf("retried %d times, want %d", len(slept), writeRetries)
	}
}
