package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func TestWaitConfigParses(t *testing.T) { // Jira story F032
	env := func(m map[string]string) func(string) string { return func(k string) string { return m[k] } }
	cases := []struct {
		in      map[string]string
		fixed   int
		timeout time.Duration
		bad     bool
	}{
		{in: nil, fixed: -1, timeout: time.Second},
		{in: map[string]string{"TARGET_WAIT_REPLICAS": "auto"}, fixed: -1, timeout: time.Second},
		{in: map[string]string{"TARGET_WAIT_REPLICAS": "0"}, fixed: 0, timeout: time.Second},
		{in: map[string]string{"TARGET_WAIT_REPLICAS": "2", "TARGET_WAIT_TIMEOUT_MS": "250"}, fixed: 2, timeout: 250 * time.Millisecond},
		{in: map[string]string{"TARGET_WAIT_REPLICAS": "-1"}, bad: true},
		{in: map[string]string{"TARGET_WAIT_REPLICAS": "one"}, bad: true},
		{in: map[string]string{"TARGET_WAIT_TIMEOUT_MS": "0"}, bad: true},     // WAIT 0 blocks indefinitely
		{in: map[string]string{"TARGET_WAIT_TIMEOUT_MS": "10000"}, bad: true}, // not under the read timeout
	}
	for _, c := range cases {
		f, to, err := parseWaitConfig(env(c.in))
		if c.bad {
			if err == nil {
				t.Errorf("%v: accepted", c.in)
			}
			continue
		}
		if err != nil || f != c.fixed || to != c.timeout {
			t.Errorf("%v: got (%d, %v, %v), want (%d, %v)", c.in, f, to, err, c.fixed, c.timeout)
		}
	}
}

func TestWaitNeedPerPrimary(t *testing.T) {
	w := newReplicaWait(-1, time.Second)
	if w.enabled() || w.need("a:1") != 0 {
		t.Fatal("auto with nothing read: must not wait")
	}
	w.replicas = map[string]int{"a:1": 2, "b:1": 0}
	w.anyRepl = true
	if !w.enabled() {
		t.Fatal("a primary has replicas: must wait")
	}
	if n := w.need("a:1"); n != 1 {
		t.Fatalf("auto, 2 replicas: need %d, want 1", n)
	}
	if n := w.need("b:1"); n != 0 {
		t.Fatalf("auto, no replica: need %d, want 0 (no WAIT there)", n)
	}
	if n := w.need("new:1"); n != 1 {
		t.Fatalf("auto, primary not read yet on a replicated target: need %d, want 1", n)
	}
	select {
	case <-w.refresh:
	default:
		t.Fatal("an unknown primary did not ask for a re-read")
	}
	if n := newReplicaWait(3, time.Second).need("x"); n != 3 {
		t.Fatalf("fixed 3: need %d", n)
	}
	if off := newReplicaWait(0, time.Second); off.enabled() || off.request() != nil {
		t.Fatal("TARGET_WAIT_REPLICAS=0 still waits")
	}
}

func TestBarrierKeyIsInTheKeysSlot(t *testing.T) {
	for _, c := range []struct{ key, tag string }{
		{"orders:1", "orders:1"},
		{"{orders:1}:ecdl-del", "orders:1"},
		{"a{b}c", "b"},
		{"a{}c", "a{}c"}, // an empty tag hashes the whole key
		{"x{y", "x{y"},
		{"x}y{", "x}y{"},
	} {
		if got := hashTag(c.key); got != c.tag {
			t.Errorf("hashTag(%q) = %q, want %q", c.key, got, c.tag)
		}
		if b := barrierKey(c.key); keySlot(b) != keySlot(c.key) {
			t.Errorf("barrier %q for %q is in slot %d, the key in %d", b, c.key, keySlot(b), keySlot(c.key))
		}
	}
	// Known slots (CLUSTER KEYSLOT on Valkey).
	for key, slot := range map[string]int{"foo": 12182, "bar": 5061, "{user1000}.following": 3443, "": 0} {
		if got := keySlot(key); got != slot {
			t.Errorf("keySlot(%q) = %d, want %d", key, got, slot)
		}
	}
	for s := range slotTags {
		if keySlot("{"+slotTags[s]+"}") != s {
			t.Fatalf("slot %d has tag %q of another slot", s, slotTags[s])
		}
	}
}

func TestFirstKey(t *testing.T) {
	ctx := context.Background()
	for _, c := range []struct {
		cmd  redis.Cmder
		want string
	}{
		{redis.NewCmd(ctx, "evalsha", "abc", 2, "k1", "k2", "v"), "k1"},
		{redis.NewCmd(ctx, "evalsha", "abc", 0, "v"), ""},
		{redis.NewIntCmd(ctx, "hset", "k", "f", "v"), "k"},
		{redis.NewIntCmd(ctx, "del", "k"), "k"},
		{redis.NewCmd(ctx, "asking"), ""},
		{redis.NewIntCmd(ctx, "wait", 1, 1000), ""},
	} {
		if got := firstKey(c.cmd); got != c.want {
			t.Errorf("firstKey(%v) = %q, want %q", c.cmd.Args(), got, c.want)
		}
	}
}

func TestWaitConfirmFailsClosed(t *testing.T) {
	r := newReplicaWait(1, 50*time.Millisecond).request()
	ctx := context.Background()
	unseen := redis.NewCmd(ctx, "evalsha")
	short := redis.NewCmd(ctx, "evalsha")
	failed := redis.NewCmd(ctx, "evalsha")
	ok := redis.NewCmd(ctx, "evalsha")
	none := redis.NewCmd(ctx, "evalsha")
	r.record([]redis.Cmder{short}, waitOutcome{addr: "a", need: 1, acked: 0})
	r.record([]redis.Cmder{failed}, waitOutcome{addr: "a", need: 1, err: io.EOF})
	r.record([]redis.Cmder{ok}, waitOutcome{addr: "a", need: 1, acked: 1})
	r.record([]redis.Cmder{none}, waitOutcome{addr: "b"})
	for name, c := range map[string]redis.Cmder{"no WAIT ran": unseen, "short": short, "WAIT failed": failed} {
		if good, err := r.confirmed(c); good || !errors.Is(err, errWaitShortfall) {
			t.Errorf("%s: confirmed=%v err=%v", name, good, err)
		}
	}
	for name, c := range map[string]redis.Cmder{"acked": ok, "no replica required": none} {
		if good, err := r.confirmed(c); !good {
			t.Errorf("%s: not confirmed: %v", name, err)
		}
	}
	// A later attempt (a MOVED re-send) replaces the earlier outcome.
	r.record([]redis.Cmder{short}, waitOutcome{addr: "c", need: 1, acked: 1})
	if good, _ := r.confirmed(short); !good {
		t.Error("the later outcome did not win")
	}
}

func TestWaitShortfallIsNeitherHeldNorDeadLettered(t *testing.T) {
	err := fmt.Errorf("%w: WAIT on a: %v", errWaitShortfall, io.EOF)
	if targetWide(err) {
		t.Fatal("a WAIT shortfall is treated as a target outage: the batch would be held, not left for reclaim")
	}
	res := writeResult{cmds: []redis.Cmder{nil}, errs: []error{err}, waitShort: 1}
	if len(res.held()) != 0 {
		t.Fatal("a WAIT shortfall was held")
	}
}

func TestWaitApplyUnconfirmsShortOps(t *testing.T) {
	saved := waitShortfall
	defer func() { waitShortfall = saved }()
	ctx := context.Background()
	a, b := redis.NewCmd(ctx, "evalsha"), redis.NewCmd(ctx, "evalsha")
	r := newReplicaWait(1, time.Second).request()
	r.record([]redis.Cmder{a}, waitOutcome{addr: "x", need: 1, acked: 1})
	r.record([]redis.Cmder{b}, waitOutcome{addr: "y", need: 1, acked: 0})
	res := writeResult{cmds: []redis.Cmder{a, b, nil}, errs: make([]error, 3)}
	r.apply(&res)
	if res.cmds[0] == nil || res.cmds[1] != nil || res.waitShort != 1 || !errors.Is(res.errs[1], errWaitShortfall) {
		t.Fatalf("apply: cmds=%v errs=%v short=%d", res.cmds, res.errs, res.waitShort)
	}
	if waitShortfall != saved+1 {
		t.Fatalf("WaitShortfall counted %d, want 1", waitShortfall-saved)
	}
	var nilReq *waitRequest
	nilReq.apply(&res) // disabled: a no-op
}

func TestDeliveryCountsCarryWaitShortfall(t *testing.T) {
	c := deliveryCounts{waitShort: 7}.minus(deliveryCounts{waitShort: 2})
	for _, d := range c.datums(nil) {
		if *d.MetricName == "WaitShortfall" {
			if *d.Value != 5 {
				t.Fatalf("WaitShortfall = %v, want 5", *d.Value)
			}
			return
		}
	}
	t.Fatal("no WaitShortfall datum")
}
