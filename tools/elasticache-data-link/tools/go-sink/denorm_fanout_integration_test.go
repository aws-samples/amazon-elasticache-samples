package main

// Jira story F042: a fan-out must not recreate a deleted child, a snapshot read replaces
// a child in one step, and a change without a before-image still leaves the
// old parent's index (or says it could not). Standalone and cluster.

import (
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

// captureWarnings collects the lines logWarn/logErr print.
func captureWarnings(t *testing.T) func() []string {
	t.Helper()
	var mu sync.Mutex
	var lines []string
	old := errLog
	errLog = newErrLogger(time.Hour)
	errLog.out = func(s string) { mu.Lock(); lines = append(lines, s); mu.Unlock() }
	t.Cleanup(func() { errLog = old })
	return func() []string {
		mu.Lock()
		defer mu.Unlock()
		return append([]string(nil), lines...)
	}
}

func TestIntegrationDenormFanOutSkipsDeletedChildren(t *testing.T) { // Jira story F042
	eachTarget(t, func(t *testing.T, c redis.UniversalClient) {
		d := setupDenormOn(t, c, "v2", ordersByCustomer("null", "{}"))
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		d.order("200-0", "c", 1, 7, 5, nil)
		d.order("210-0", "c", 2, 7, 5, nil)
		d.order("300-0", "d", 1, 0, 0, row("id", 1))
		// A stale entry, as a lost SREM would leave, and a legacy child (no fk
		// field) that has also been deleted.
		c.SAdd(d.ctx, idxKey("orders", "customers", "7"), "1", "3")
		d.customer("c", "400-0", "u", 7, "Anna", "gold")
		d.apply(evMsg("500-0", "d", "customers", 7, row("id", 7), nil), "customers") // null fan-out too
		for _, pk := range []int{1, 3} {
			if d.exists("orders", pk) {
				t.Fatalf("a fan-out recreated deleted child %d: %v", pk,
					c.HGetAll(d.ctx, rowKey("orders", "1")).Val())
			}
		}
		d.want("orders", 2, "c_name", "")
		d.want("orders", 2, "_ecdl_pv:customers:cid", "500-0")
	})
}

func TestIntegrationDenormSnapshotReadIsOneStep(t *testing.T) { // Jira story F042
	eachTarget(t, func(t *testing.T, c redis.UniversalClient) {
		d := setupDenormOn(t, c, "", ordersByCustomer("null", "{}"))
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		d.order("300-0", "u", 1, 7, 5, nil)
		c.HSet(d.ctx, rowKey("orders", "1"), "old_col", "x")
		// An OLDER snapshot read arrives late. A DEL before the guarded write
		// would delete the row and then skip the write.
		d.order("200-0", "r", 1, 7, 9, nil)
		d.want("orders", 1, "qty", "5")
		d.want("orders", 1, "old_col", "x")
		d.want("orders", 1, "c_name", "Ann")
		// A newer one replaces the row whole, embed and bookkeeping included.
		d.order("400-0", "r", 1, 7, 6, nil)
		if c.HExists(d.ctx, rowKey("orders", "1"), "old_col").Val() {
			t.Fatal("snapshot read kept a field from the old shape")
		}
		d.want("orders", 1, "qty", "6")
		d.want("orders", 1, "c_name", "Ann")
		d.want("orders", 1, "_ecdl_fk:customers:cid", "7")
		d.want("orders", 1, "_ecdl_pv:customers:cid", "100-0")
	})
}

func TestIntegrationDenormMoveWithoutBeforeImage(t *testing.T) { // Jira story F042
	eachTarget(t, func(t *testing.T, c redis.UniversalClient) {
		d := setupDenormOn(t, c, "v2", ordersByCustomer("null", "{}"))
		warns := captureWarnings(t)
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		d.customer("c", "110-0", "c", 8, "Bob", "gold")
		d.order("200-0", "c", 1, 7, 5, nil)
		d.order("300-0", "u", 1, 8, 5, nil) // no before-image: the stored fk is used
		if d.inIdx(7, 1) || !d.inIdx(8, 1) {
			t.Fatal("index not moved without a before-image")
		}
		d.order("400-0", "r", 1, 7, 5, nil) // a snapshot read never has one
		if d.inIdx(8, 1) || !d.inIdx(7, 1) {
			t.Fatal("index not moved by a snapshot read")
		}
		if w := warns(); len(w) != 0 {
			t.Fatalf("warned although the old parent was known: %v", w)
		}
	})
}

func TestIntegrationDenormRetryWithoutBeforeImageWarns(t *testing.T) { // Jira story F042
	eachTarget(t, func(t *testing.T, c redis.UniversalClient) {
		d := setupDenormOn(t, c, "v2", ordersByCustomer("null", "{}"))
		warns := captureWarnings(t)
		d.customer("c", "100-0", "c", 7, "Ann", "gold")
		d.customer("c", "110-0", "c", 8, "Bob", "gold")
		d.order("200-0", "c", 1, 7, 5, nil)
		// The first attempt writes the row and then fails to leave parent 7's
		// index. The retry finds the row already at 300-0 and pointing at 8,
		// and there is no before-image to say it used to point at 7.
		h := &failCmd{name: "srem", n: 1}
		c.AddHook(h)
		m := evMsg("300-0", "u", "orders", 1, nil, row("id", 1, "cid", 8, "qty", 5))
		if err := processDenorm(d.ctx, c, m, "orders"); err == nil {
			t.Fatal("injected SREM failure not returned")
		}
		d.apply(m, "orders")
		d.want("orders", 1, "c_name", "Bob")
		got := strings.Join(warns(), "\n")
		if !strings.Contains(got, "WARN denorm-index:orders") || !strings.Contains(got, idxKey("orders", "customers", "<old cid>")) {
			t.Fatalf("no warning for an index entry that may be left behind; got %q", got)
		}
		// The entry left behind is harmless: parent 7's changes skip the child.
		d.customer("c", "500-0", "u", 7, "Zed", "gold")
		d.want("orders", 1, "c_name", "Bob")

		// A delete retried the same way.
		d.order("600-0", "c", 2, 8, 1, nil)
		h2 := &failCmd{name: "srem", n: 1}
		c.AddHook(h2)
		del := evMsg("700-0", "d", "orders", 2, row("id", 2), nil)
		if err := processDenorm(d.ctx, c, del, "orders"); err == nil {
			t.Fatal("injected SREM failure not returned")
		}
		before := len(warns())
		errLog.last = map[string]time.Time{} // let the next line through
		d.apply(del, "orders")
		if len(warns()) == before {
			t.Fatal("no warning for a retried delete without a before-image")
		}
		// With a full before-image there is nothing to warn about.
		d.order("800-0", "c", 3, 8, 1, nil)
		h3 := &failCmd{name: "srem", n: 1}
		c.AddHook(h3)
		del = evMsg("900-0", "d", "orders", 3, row("id", 3, "cid", 8, "qty", 1), nil)
		if err := processDenorm(d.ctx, c, del, "orders"); err == nil {
			t.Fatal("injected SREM failure not returned")
		}
		before = len(warns())
		errLog.last = map[string]time.Time{}
		d.apply(del, "orders")
		if len(warns()) != before {
			t.Fatalf("warned although the before-image named the parent: %v", warns()[before:])
		}
		if d.inIdx(8, 3) {
			t.Fatal("the retry did not leave the index")
		}
	})
}
