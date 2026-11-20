package main

// Jira story F041: a child write racing a parent change, a failed parent read, and a
// parent that a filter removes. Against a real Valkey, standalone
// (ECDL_TEST_VALKEY) and cluster (ECDL_TEST_VALKEY_CLUSTER); each is skipped
// when its variable is unset.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/redis/go-redis/v9"
)

// eachTarget runs f against the standalone and the cluster test servers.
func eachTarget(t *testing.T, f func(t *testing.T, c redis.UniversalClient)) {
	t.Run("standalone", func(t *testing.T) { f(t, testValkey(t)) })
	t.Run("cluster", func(t *testing.T) { f(t, testValkeyCluster(t)) })
}

func testValkeyCluster(t *testing.T) redis.UniversalClient {
	t.Helper()
	addr := os.Getenv("ECDL_TEST_VALKEY_CLUSTER")
	if addr == "" {
		t.Skip("ECDL_TEST_VALKEY_CLUSTER not set")
	}
	c := redis.NewClusterClient(&redis.ClusterOptions{Addrs: []string{addr}})
	if err := c.Ping(context.Background()).Err(); err != nil {
		t.Fatalf("valkey cluster at %s: %v", addr, err)
	}
	t.Cleanup(func() { c.Close() })
	return c
}

// flushAll empties a test server (every primary of a cluster).
func flushAll(t *testing.T, ctx context.Context, c redis.UniversalClient) {
	t.Helper()
	var err error
	if cc, ok := c.(*redis.ClusterClient); ok {
		err = cc.ForEachMaster(ctx, func(ctx context.Context, n *redis.Client) error { return n.FlushDB(ctx).Err() })
	} else {
		err = c.FlushDB(ctx).Err()
	}
	if err != nil {
		t.Fatal(err)
	}
}

// setupDenormOn is setupDenorm on a given client and spec.
func setupDenormOn(t *testing.T, c redis.UniversalClient, prefix, spec string) *dn {
	t.Helper()
	ctx := context.Background()
	flushAll(t, ctx, c)
	oldSpec, oldPrefix := transformSpec, keyPrefix
	t.Cleanup(func() { transformSpec, keyPrefix, beforeChildWrite = oldSpec, oldPrefix, nil })
	keyPrefix = normalizeKeyPrefix(prefix)
	s, err := LoadTransformSpec([]byte(spec))
	if err != nil {
		t.Fatal(err)
	}
	transformSpec = s
	if err := loadDenormScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	return &dn{t: t, c: c, ctx: ctx}
}

func ordersByCustomer(onDelete, customers string) string {
	return `{"tables":{"orders":{"denormalize":[{"parent_table":"customers","join_field":"cid",
 "fields":["name","tier"],"prefix":"c_","on_parent_delete":"` + onDelete + `"}]},
 "customers":` + customers + `}}`
}

// failCmd fails the n-th (1-based) command with the given name, once.
type failCmd struct {
	name string
	n    int64
	seen atomic.Int64
}

func (h *failCmd) DialHook(next redis.DialHook) redis.DialHook { return next }
func (h *failCmd) ProcessHook(next redis.ProcessHook) redis.ProcessHook {
	return func(ctx context.Context, cmd redis.Cmder) error {
		if cmd.Name() == h.name && h.seen.Add(1) == h.n {
			err := errors.New("LOADING injected failure")
			cmd.SetErr(err)
			return err
		}
		return next(ctx, cmd)
	}
}
func (h *failCmd) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return next
}

func TestIntegrationDenormParentReadErrorIsNotAcked(t *testing.T) { // Jira story F041
	eachTarget(t, func(t *testing.T, c redis.UniversalClient) {
		for _, n := range []int64{1, 2} { // the lookup, then the re-read after the write
			t.Run(fmt.Sprintf("hmget#%d", n), func(t *testing.T) {
				d := setupDenormOn(t, c, "v2", ordersByCustomer("null", "{}"))
				d.customer("c", "100-0", "c", 7, "Ann", "gold")
				h := &failCmd{name: "hmget", n: n}
				c.AddHook(h)
				defer func() { h.n = -1 }() // hooks cannot be removed; disarm it
				m := evMsg("200-0", "c", "orders", 1, nil, row("id", 1, "cid", 7, "qty", 5))
				err := processDenorm(d.ctx, c, m, "orders")
				if err == nil || !strings.Contains(err.Error(), "injected") {
					t.Fatalf("a failed parent read returned %v; it must be returned so the change stays pending", err)
				}
				if n == 1 && d.exists("orders", 1) {
					t.Fatal("the child was written with blank parent fields after a failed read")
				}
				// The retry completes it.
				d.apply(m, "orders")
				d.want("orders", 1, "c_name", "Ann")
				d.want("orders", 1, "_ecdl_pv:customers:cid", "100-0")
			})
		}
	})
}

func TestIntegrationDenormFilteredOutParentAppliesOnParentDelete(t *testing.T) { // Jira story F041
	filter := `{"filter":{"field":"tier","op":"!=","value":"banned"}}`
	eachTarget(t, func(t *testing.T, c redis.UniversalClient) {
		t.Run("null", func(t *testing.T) {
			d := setupDenormOn(t, c, "v2", ordersByCustomer("null", filter))
			d.customer("c", "100-0", "c", 7, "Ann", "gold")
			d.order("200-0", "c", 1, 7, 5, nil)
			d.apply(evMsg("300-0", "u", "customers", 7, row("id", 7, "name", "Ann", "tier", "gold"),
				row("id", 7, "name", "Ann", "tier", "banned")), "customers")
			if d.exists("customers", 7) {
				t.Fatal("the filtered-out parent stayed in the cache")
			}
			d.want("orders", 1, "c_name", "")
			d.want("orders", 1, "c_tier", "")
			d.want("orders", 1, "_ecdl_pv:customers:cid", "300-0")
			// A child written afterwards also embeds blanks, versioned by the filter-out.
			d.order("400-0", "c", 2, 7, 1, nil)
			d.want("orders", 2, "c_name", "")
			d.want("orders", 2, "_ecdl_pv:customers:cid", "300-0")
			// It matches again: both children get it back.
			d.apply(evMsg("500-0", "u", "customers", 7, row("id", 7, "name", "Ann", "tier", "banned"),
				row("id", 7, "name", "Ann", "tier", "gold")), "customers")
			d.want("orders", 1, "c_name", "Ann")
			d.want("orders", 2, "c_tier", "gold")
		})
		t.Run("leave", func(t *testing.T) {
			d := setupDenormOn(t, c, "", ordersByCustomer("leave", filter))
			d.customer("c", "100-0", "c", 7, "Ann", "gold")
			d.order("200-0", "c", 1, 7, 5, nil)
			d.apply(evMsg("300-0", "u", "customers", 7, nil, row("id", 7, "name", "Ann", "tier", "banned")), "customers")
			if d.exists("customers", 7) {
				t.Fatal("the filtered-out parent stayed in the cache")
			}
			d.want("orders", 1, "c_name", "Ann")
		})
	})
}

func TestIntegrationDenormParentDeletedDuringChildWrite(t *testing.T) { // Jira story F041, rule 4
	eachTarget(t, func(t *testing.T, c redis.UniversalClient) {
		d := setupDenormOn(t, c, "v2", ordersByCustomer("null", "{}"))
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		once := false
		beforeChildWrite = func() {
			if once {
				return
			}
			once = true
			if err := processDenorm(d.ctx, c, evMsg("300-0", "d", "customers", 7, row("id", 7), nil), "customers"); err != nil {
				t.Fatal(err)
			}
		}
		d.order("200-0", "c", 1, 7, 5, nil)
		d.want("orders", 1, "c_name", "")
		d.want("orders", 1, "_ecdl_pv:customers:cid", "300-0")
	})
}

// afterCmd runs f once, right after the n-th command with the given name.
type afterCmd struct {
	name string
	n    int64
	seen atomic.Int64
	f    func()
}

func (h *afterCmd) DialHook(next redis.DialHook) redis.DialHook { return next }
func (h *afterCmd) ProcessHook(next redis.ProcessHook) redis.ProcessHook {
	return func(ctx context.Context, cmd redis.Cmder) error {
		err := next(ctx, cmd)
		if cmd.Name() == h.name && h.seen.Add(1) == h.n {
			h.f()
		}
		return err
	}
}
func (h *afterCmd) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return next
}

func TestIntegrationDenormParentChangedAfterChildReRead(t *testing.T) { // Jira story F041, rule 4
	// The parent changes after the child's second read of it. Its fan-out is
	// then the only thing that can bring the child up to date, so the child
	// must already be in the reverse index.
	eachTarget(t, func(t *testing.T, c redis.UniversalClient) {
		d := setupDenormOn(t, c, "v2", ordersByCustomer("null", "{}"))
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		h := &afterCmd{name: "hmget", n: 2, f: func() {
			if err := processDenorm(d.ctx, c, evMsg("300-0", "u", "customers", 7, nil,
				row("id", 7, "name", "Anna", "tier", "gold")), "customers"); err != nil {
				t.Error(err)
			}
		}}
		c.AddHook(h)
		d.order("200-0", "c", 1, 7, 5, nil)
		d.want("orders", 1, "c_name", "Anna")
		d.want("orders", 1, "_ecdl_pv:customers:cid", "300-0")
	})
}

// A child insert and an update of its parent applied at the same moment, as
// two sink workers do, for many independent pairs. Every child must end with
// its parent's new value: none may keep the old one because the parent's
// fan-out ran before the child was in the index.
func TestIntegrationDenormConcurrentChildInsertAndParentUpdate(t *testing.T) { // Jira story F041
	eachTarget(t, func(t *testing.T, c redis.UniversalClient) {
		d := setupDenormOn(t, c, "v2", ordersByCustomer("null", "{}"))
		const pairs = 400
		for p := 1; p <= pairs; p++ {
			d.customer("c", "100-0", "c", p, "old", "gold")
		}
		var wg sync.WaitGroup
		errs := make(chan error, 2*pairs)
		run := func(m redis.XMessage, table string, start <-chan struct{}) {
			defer wg.Done()
			<-start
			if err := processDenorm(d.ctx, c, m, table); err != nil {
				errs <- err
			}
		}
		for p := 1; p <= pairs; p++ {
			start := make(chan struct{})
			wg.Add(2)
			go run(evMsg("300-0", "u", "customers", p, nil, row("id", p, "name", "new", "tier", "gold")), "customers", start)
			go run(evMsg("200-0", "c", "orders", p, nil, row("id", p, "cid", p, "qty", 1)), "orders", start)
			close(start)
			if p%16 == 0 {
				wg.Wait()
			}
		}
		wg.Wait()
		close(errs)
		for err := range errs {
			t.Fatal(err)
		}
		for p := 1; p <= pairs; p++ {
			key := rowKey("orders", fmt.Sprint(p))
			got := c.HGet(d.ctx, key, "c_name").Val()
			if got != "new" {
				t.Fatalf("child %d embeds %q, want its parent's new value", p, got)
			}
			if !c.SIsMember(d.ctx, idxKey("orders", "customers", fmt.Sprint(p)), fmt.Sprint(p)).Val() {
				t.Fatalf("child %d not in the reverse index", p)
			}
		}
	})
}
