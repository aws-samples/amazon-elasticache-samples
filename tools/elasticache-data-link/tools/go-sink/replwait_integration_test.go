package main

// Replica wait before ack (Jira story F032) against real Valkey servers.
//
// Single node: ECDL_TEST_VALKEY is the primary and ECDL_TEST_VALKEY_REPLICA a
// replica of it on this host, for example
//
//	valkey-server --port 6418 --save '' --appendonly no --daemonize yes
//	valkey-server --port 6428 --replicaof 127.0.0.1 6418 --save '' --appendonly no --daemonize yes
//	ECDL_TEST_VALKEY=127.0.0.1:6418 ECDL_TEST_VALKEY_REPLICA=127.0.0.1:6428 go test -run Wait ./...
//
// Cluster: ECDL_TEST_VALKEY_CLUSTER, created with --cluster-replicas 1.
//
// A replica is made to fall behind by stopping its process (SIGSTOP): it stays
// connected, so its primary still counts it, but it acknowledges nothing. That
// is what a slow or partitioned replica looks like. Every test resumes it.

import (
	"context"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func testReplica(t *testing.T) (primary, replica *redis.Client) {
	t.Helper()
	p := testValkey(t).(*redis.Client)
	addr := os.Getenv("ECDL_TEST_VALKEY_REPLICA")
	if addr == "" {
		t.Skip("ECDL_TEST_VALKEY_REPLICA not set")
	}
	r := redis.NewClient(&redis.Options{Addr: addr})
	t.Cleanup(func() { r.Close() })
	info := r.Info(context.Background(), "replication").Val()
	_, port, _ := strings.Cut(p.Options().Addr, ":")
	if !strings.Contains(info, "role:slave") || !strings.Contains(info, "master_port:"+port) {
		t.Fatalf("%s is not a replica of %s", addr, p.Options().Addr)
	}
	waitFor(t, "the replica to be online", 10*time.Second, func() bool {
		return strings.Contains(r.Info(context.Background(), "replication").Val(), "master_link_status:up")
	})
	return p, r
}

func processID(t *testing.T, c *redis.Client) int {
	t.Helper()
	for _, line := range strings.Split(c.Info(context.Background(), "server").Val(), "\n") {
		if v, ok := strings.CutPrefix(strings.TrimSpace(line), "process_id:"); ok {
			pid, err := strconv.Atoi(v)
			if err != nil {
				t.Fatal(err)
			}
			return pid
		}
	}
	t.Fatal("no process_id")
	return 0
}

// pause stops a server's process until the returned resume is called (and
// at the latest when the test ends).
func pause(t *testing.T, pid int) (resume func()) {
	t.Helper()
	if err := syscall.Kill(pid, syscall.SIGSTOP); err != nil {
		t.Fatalf("SIGSTOP %d: %v", pid, err)
	}
	var once sync.Once
	resume = func() { once.Do(func() { _ = syscall.Kill(pid, syscall.SIGCONT) }) }
	t.Cleanup(resume)
	return resume
}

func useReplication(t *testing.T, w *replicaWait) {
	t.Helper()
	saved := replication
	replication = w
	t.Cleanup(func() { replication = saved })
}

func hookedClient(t *testing.T, addr string) *redis.Client {
	c := redis.NewClient(&redis.Options{Addr: addr, MaxRetries: -1})
	installWaitHook(c)
	t.Cleanup(func() { c.Close() })
	return c
}

func upserts(prefix string, n int, version string) []targetOp {
	ops := make([]targetOp, n)
	for i := range ops {
		ops[i] = targetOp{id: fmt.Sprintf("%d-0", i+1), version: version,
			key: fmt.Sprintf("%s:%d", prefix, i), fields: map[string]interface{}{"v": i}}
	}
	return ops
}

// WAIT answers how many replicas hold the last write of ITS OWN connection. On
// a connection that wrote nothing it counts a stopped replica too; in the
// writes' pipeline, after the barrier, it does not.
func TestIntegrationWaitRunsOnTheWritingConnection(t *testing.T) { // Jira story F032
	p, r := testReplica(t)
	ctx := context.Background()
	if err := loadScripts(ctx, p); err != nil {
		t.Fatal(err)
	}
	w := newReplicaWait(1, 300*time.Millisecond)
	useReplication(t, w)
	target := hookedClient(t, p.Options().Addr)

	res := writeOps(ctx, target, upserts("itest:wait:a", 5, "100-0"))
	if res.unconfirmed() != 0 {
		t.Fatalf("replica up: %d unconfirmed (%v)", res.unconfirmed(), res.lastErr)
	}

	resume := pause(t, processID(t, r))
	res = writeOps(ctx, target, upserts("itest:wait:a", 5, "200-0"))
	if res.waitShort != 5 || res.unconfirmed() != 5 {
		t.Fatalf("replica stopped: short=%d unconfirmed=%d, want 5 and 5", res.waitShort, res.unconfirmed())
	}
	// The trap the barrier and the shared pipeline avoid.
	fresh := redis.NewClient(&redis.Options{Addr: p.Options().Addr})
	defer fresh.Close()
	if n := fresh.Wait(ctx, 1, 300*time.Millisecond).Val(); n != 1 {
		t.Fatalf("WAIT on a fresh connection = %d; expected the misleading 1 that motivates the barrier", n)
	}
	resume()
	waitFor(t, "the replica to catch up", 10*time.Second, func() bool {
		return confirmReplicated(ctx, target) == nil
	})
	if got := r.HGet(ctx, "itest:wait:a:4", "_ecdl_v").Val(); got != "200-0" {
		t.Fatalf("replica holds version %q after resuming, want 200-0", got)
	}
}

// No replica, nothing to wait for: WAIT 1 answers 0 and nothing is confirmed.
func TestIntegrationWaitCountsOnlyRealReplicas(t *testing.T) {
	p, r := testReplica(t)
	ctx := context.Background()
	if err := loadScripts(ctx, p); err != nil {
		t.Fatal(err)
	}
	useReplication(t, newReplicaWait(2, 200*time.Millisecond)) // one more than exists
	target := hookedClient(t, p.Options().Addr)
	if res := writeOps(ctx, target, upserts("itest:wait:b", 3, "100-0")); res.waitShort != 3 {
		t.Fatalf("WAIT 2 with one replica: short=%d, want 3", res.waitShort)
	}
	useReplication(t, newReplicaWait(1, 200*time.Millisecond))
	if res := writeOps(ctx, target, upserts("itest:wait:b", 3, "200-0")); res.unconfirmed() != 0 {
		t.Fatalf("WAIT 1 with one replica: %v", res.lastErr)
	}
	_ = r
}

// streamOf writes n changes to table "items" on a fresh stream and group.
func streamOf(t *testing.T, broker redis.UniversalClient, stream string, n int, base int) []redis.XMessage {
	t.Helper()
	ctx := context.Background()
	broker.Del(ctx, stream)
	for i := 1; i <= n; i++ {
		broker.XAdd(ctx, &redis.XAddArgs{Stream: stream, Values: map[string]interface{}{
			"key":   fmt.Sprintf(`{"id":%d}`, i),
			"value": fmt.Sprintf(`{"op":"c","source":{"table":"items"},"after":{"id":%d,"v":"x%d"}}`, i, i+base),
		}})
	}
	if err := broker.XGroupCreateMkStream(ctx, stream, "g", "0").Err(); err != nil {
		t.Fatal(err)
	}
	res, err := broker.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "w1",
		Streams: []string{stream, ">"}, Count: int64(n)}).Result()
	if err != nil {
		t.Fatal(err)
	}
	return res[0].Messages
}

// The Jira story F032 scenario end to end: a replica that does not confirm keeps the
// batch pending; reclaim applies it again later, from a connection that wrote
// nothing, and acks it only once the replica has it.
func TestIntegrationWaitShortfallLeavesTheBatchPendingForReclaim(t *testing.T) { // Jira story F032
	p, r := testReplica(t)
	ctx := context.Background()
	p.FlushDB(ctx)
	if err := loadScripts(ctx, p); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the flush to replicate", 5*time.Second, func() bool { return r.DBSize(ctx).Val() == 0 })
	rw := newReplicaWait(-1, 200*time.Millisecond)
	target := hookedClient(t, p.Options().Addr)
	rw.read(ctx, target)
	if !rw.enabled() {
		t.Fatal("auto mode did not see the replica")
	}
	useReplication(t, rw)
	// Every claim is then past MAX_DELIVERIES: a shortfall must still not be
	// dead-lettered.
	savedIdle, savedMax := retryIdle, maxDeliveries
	retryIdle, maxDeliveries = 0, 1
	t.Cleanup(func() { retryIdle, maxDeliveries = savedIdle, savedMax })
	short0 := waitShortfall

	const stream = "data:t.waititems"
	msgs := streamOf(t, p, stream, 20, 0)
	resume := pause(t, processID(t, r))
	applyBatch(ctx, p, target, stream, "g", msgs, "envelope", "items")
	if n := p.XPending(ctx, stream, "g").Val().Count; n != 20 {
		t.Fatalf("pending after a WAIT shortfall = %d, want all 20", n)
	}
	if p.XLen(ctx, stream).Val() != 20 {
		t.Fatal("entries were deleted from the stream without replica confirmation")
	}
	if p.HGet(ctx, "items:20", "v").Val() != "x20" {
		t.Fatal("the primary did not get the writes")
	}
	if got := waitShortfall - short0; got != 20 {
		t.Fatalf("WaitShortfall counted %d, want 20", got)
	}

	// Reclaim while the replica is still stopped, from a new client whose
	// connection never wrote: every re-apply is skipped by the guard (the
	// primary already has them), so only the barrier stops WAIT from
	// answering 1.
	fresh := hookedClient(t, p.Options().Addr)
	claimed, deliveries, err := claimIdle(ctx, p, stream, "g", "w-reclaim")
	if err != nil || len(claimed) != 20 {
		t.Fatalf("claim: %d %v", len(claimed), err)
	}
	applyReclaimed(ctx, p, fresh, stream, "g", "items", "envelope", claimed, deliveries)
	if n := p.XPending(ctx, stream, "g").Val().Count; n != 20 {
		t.Fatalf("reclaim acked %d change(s) the replica does not hold", 20-n)
	}
	if n := p.XLen(ctx, dlqPrefix+stream).Val(); n != 0 {
		t.Fatalf("a WAIT shortfall dead-lettered %d change(s)", n)
	}

	resume()
	waitFor(t, "reclaim to ack once the replica is back", 10*time.Second, func() bool {
		c, d, err := claimIdle(ctx, p, stream, "g", "w-reclaim")
		if err == nil && len(c) > 0 {
			applyReclaimed(ctx, p, fresh, stream, "g", "items", "envelope", c, d)
		}
		return p.XPending(ctx, stream, "g").Val().Count == 0
	})
	if p.XLen(ctx, stream).Val() != 0 {
		t.Fatal("acked entries left in the stream")
	}
	if got := r.HGet(ctx, "items:20", "v").Val(); got != "x20" {
		t.Fatalf("replica has items:20 v=%q", got)
	}
}

func TestIntegrationWaitOKAcksAtOnce(t *testing.T) {
	p, _ := testReplica(t)
	ctx := context.Background()
	if err := loadScripts(ctx, p); err != nil {
		t.Fatal(err)
	}
	rw := newReplicaWait(-1, time.Second)
	target := hookedClient(t, p.Options().Addr)
	rw.read(ctx, target)
	useReplication(t, rw)
	const stream = "data:t.waitok"
	msgs := streamOf(t, p, stream, 50, 0)
	applyBatch(ctx, p, target, stream, "g", msgs, "envelope", "items")
	if n := p.XPending(ctx, stream, "g").Val().Count; n != 0 {
		t.Fatalf("replica up: %d left pending", n)
	}
	if p.XLen(ctx, stream).Val() != 0 {
		t.Fatal("acked entries not deleted")
	}
}

// TARGET_WAIT_REPLICAS=0: no WAIT at all, even with a stopped replica.
func TestIntegrationWaitDisabledAcksWithoutReplicas(t *testing.T) {
	p, r := testReplica(t)
	ctx := context.Background()
	if err := loadScripts(ctx, p); err != nil {
		t.Fatal(err)
	}
	useReplication(t, newReplicaWait(0, time.Second))
	target := hookedClient(t, p.Options().Addr)
	const stream = "data:t.waitoff"
	msgs := streamOf(t, p, stream, 10, 0)
	pause(t, processID(t, r))
	start := time.Now()
	applyBatch(ctx, p, target, stream, "g", msgs, "envelope", "items")
	if n := p.XPending(ctx, stream, "g").Val().Count; n != 0 {
		t.Fatalf("wait disabled: %d left pending", n)
	}
	if d := time.Since(start); d > 500*time.Millisecond {
		t.Fatalf("wait disabled but the batch took %v", d)
	}
}

// Auto mode on a primary without replicas waits for nothing.
func TestIntegrationWaitAutoIsZeroWithoutReplicas(t *testing.T) {
	p, r := testReplica(t)
	ctx := context.Background()
	if err := loadScripts(ctx, p); err != nil {
		t.Fatal(err)
	}
	// Detach the replica: the primary now has none.
	_, port, _ := strings.Cut(p.Options().Addr, ":")
	host, _, _ := strings.Cut(p.Options().Addr, ":")
	if err := r.Do(ctx, "replicaof", "no", "one").Err(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		r.Do(context.Background(), "replicaof", host, port)
		waitFor(t, "the replica to re-attach", 20*time.Second, func() bool {
			return strings.Contains(r.Info(context.Background(), "replication").Val(), "master_link_status:up")
		})
	})
	waitFor(t, "the primary to drop the replica", 5*time.Second, func() bool {
		n, _ := connectedReplicas(ctx, p)
		return n == 0
	})
	target := hookedClient(t, p.Options().Addr)
	rw := newReplicaWait(-1, time.Second)
	rw.read(ctx, target)
	if rw.enabled() {
		t.Fatal("auto mode waits on a target with no replicas")
	}
	useReplication(t, rw)
	const stream = "data:t.waitnorepl"
	msgs := streamOf(t, p, stream, 10, 0)
	applyBatch(ctx, p, target, stream, "g", msgs, "envelope", "items")
	if n := p.XPending(ctx, stream, "g").Val().Count; n != 0 {
		t.Fatalf("no replicas, auto: %d left pending", n)
	}
	// With TARGET_WAIT_REPLICAS=1 the same target cannot confirm anything.
	useReplication(t, newReplicaWait(1, 100*time.Millisecond))
	msgs = streamOf(t, p, stream, 10, 100)
	applyBatch(ctx, p, target, stream, "g", msgs, "envelope", "items")
	if n := p.XPending(ctx, stream, "g").Val().Count; n != 10 {
		t.Fatalf("WAIT 1 with no replica: pending %d, want 10", n)
	}
}

// The denormalized path confirms with one barrier per primary.
func TestIntegrationWaitDenormBatch(t *testing.T) {
	p, r := testReplica(t)
	d := setupDenorm(t, "", "customers", "null")
	ctx := d.ctx
	useReplication(t, newReplicaWait(1, 200*time.Millisecond))
	target := hookedClient(t, p.Options().Addr)
	const stream = "data:t.orders"
	p.Del(ctx, stream)
	for i := 1; i <= 5; i++ {
		p.XAdd(ctx, &redis.XAddArgs{Stream: stream, Values: map[string]interface{}{
			"key":   fmt.Sprintf(`{"id":%d}`, i),
			"value": fmt.Sprintf(`{"op":"c","source":{"table":"orders"},"after":{"id":%d,"cid":7,"qty":1}}`, i),
		}})
	}
	p.XGroupCreateMkStream(ctx, stream, "g", "0")
	read := func() []redis.XMessage {
		res, _ := p.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "w", Streams: []string{stream, ">"}, Count: 2}).Result()
		return res[0].Messages
	}
	resume := pause(t, processID(t, r))
	applyDenormBatch(ctx, p, target, stream, "g", "orders", read())
	if n := p.XPending(ctx, stream, "g").Val().Count; n != 2 {
		t.Fatalf("denorm, replica stopped: pending %d, want 2", n)
	}
	resume()
	waitFor(t, "the replica", 10*time.Second, func() bool { return confirmReplicated(ctx, target) == nil })
	applyDenormBatch(ctx, p, target, stream, "g", "orders", read())
	if n := p.XPending(ctx, stream, "g").Val().Count; n != 2 {
		t.Fatalf("denorm, replica up: pending %d, want only the first 2", n)
	}
}

// Cluster with a replica per primary: one WAIT per primary, and a stopped
// replica holds back only the changes on its own primary.
func TestIntegrationWaitClusterPerPrimary(t *testing.T) { // Jira story F032
	addr := os.Getenv("ECDL_TEST_VALKEY_CLUSTER")
	if addr == "" {
		t.Skip("ECDL_TEST_VALKEY_CLUSTER not set")
	}
	ctx := context.Background()
	var mu sync.Mutex
	waits := map[string]int{}
	c := redis.NewClusterClient(&redis.ClusterOptions{Addrs: []string{addr}, MaxRetries: -1,
		NewClient: func(o *redis.Options) *redis.Client {
			cl := redis.NewClient(o)
			installWaitHook(cl)
			cl.AddHook(waitCounter{mu: &mu, n: waits, addr: o.Addr})
			return cl
		}})
	defer c.Close()
	if err := loadScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	rw := newReplicaWait(-1, 200*time.Millisecond)
	rw.read(ctx, c)
	if len(rw.replicas) < 3 || !rw.enabled() {
		t.Fatalf("auto read %v: want 3 primaries with replicas (create the cluster with --cluster-replicas 1)", rw.replicas)
	}
	for a, n := range rw.replicas {
		if n < 1 {
			t.Skipf("primary %s has no replica", a)
		}
	}
	useReplication(t, rw)
	for _, k := range []string{"orders:1", "a{}c", "x}y{", "{t}:1", "pfx:orders:{7}"} {
		if want := c.ClusterKeySlot(ctx, k).Val(); int64(keySlot(k)) != want || int64(keySlot(barrierKey(k))) != want {
			t.Fatalf("%q: server slot %d, ours %d, barrier %d", k, want, keySlot(k), keySlot(barrierKey(k)))
		}
	}

	ops := upserts("itest:cw", 300, "100-0")
	ops = append(ops, targetOp{id: "x", version: "100-0", key: "odd}{key", fields: map[string]interface{}{"v": 1}})
	for _, op := range ops {
		c.Del(ctx, op.key, tombstoneKey(op.key))
	}
	res := writeOps(ctx, c, ops)
	if res.unconfirmed() != 0 {
		t.Fatalf("all replicas up: %d unconfirmed: %v", res.unconfirmed(), res.lastErr)
	}
	mu.Lock()
	if len(waits) != 3 {
		t.Fatalf("WAIT sent to %v, want each of the 3 primaries once", waits)
	}
	for a, n := range waits {
		if n != 1 {
			t.Fatalf("primary %s got %d WAITs for one pipeline", a, n)
		}
	}
	mu.Unlock()

	// Stop the replica of the primary that owns ops[0].
	owner, err := c.MasterForKey(ctx, ops[0].key)
	if err != nil {
		t.Fatal(err)
	}
	var replicaAddr string
	for _, line := range strings.Split(owner.Info(ctx, "replication").Val(), "\n") {
		if v, ok := strings.CutPrefix(strings.TrimSpace(line), "slave0:"); ok {
			var ip, port string
			for _, kv := range strings.Split(v, ",") {
				k, val, _ := strings.Cut(kv, "=")
				switch k {
				case "ip":
					ip = val
				case "port":
					port = val
				}
			}
			replicaAddr = ip + ":" + port
		}
	}
	rc := redis.NewClient(&redis.Options{Addr: replicaAddr})
	defer rc.Close()
	resume := pause(t, processID(t, rc))
	newer := append([]targetOp(nil), ops...)
	for i := range newer {
		newer[i].version = "200-0"
	}
	check := func(what string, res writeResult) {
		t.Helper()
		onOwner := 0
		for i, op := range ops {
			n, _ := c.MasterForKey(ctx, op.key)
			mine := n.Options().Addr == owner.Options().Addr
			if mine {
				onOwner++
			}
			if mine != (res.cmds[i] == nil) {
				t.Fatalf("%s: %s (on the stopped replica's primary: %v) confirmed=%v", what, op.key, mine, res.cmds[i] != nil)
			}
		}
		if res.waitShort != onOwner || onOwner == 0 {
			t.Fatalf("%s: short=%d, want the %d on the stopped replica's primary", what, res.waitShort, onOwner)
		}
	}
	check("newer writes", writeOps(ctx, c, newer))
	// The same changes again (a reclaim), from connections that never wrote:
	// the guard skips every one, so nothing is written and only the barrier
	// keeps WAIT honest.
	fresh := redis.NewClusterClient(&redis.ClusterOptions{Addrs: []string{addr}, MaxRetries: -1,
		NewClient: func(o *redis.Options) *redis.Client { cl := redis.NewClient(o); installWaitHook(cl); return cl }})
	defer fresh.Close()
	check("re-applied writes", writeOps(ctx, fresh, newer))
	resume()
	waitFor(t, "the replica", 10*time.Second, func() bool { return confirmReplicated(ctx, c) == nil })
}

type waitCounter struct {
	mu   *sync.Mutex
	n    map[string]int
	addr string
}

func (h waitCounter) DialHook(next redis.DialHook) redis.DialHook          { return next }
func (h waitCounter) ProcessHook(next redis.ProcessHook) redis.ProcessHook { return next }
func (h waitCounter) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return func(ctx context.Context, cmds []redis.Cmder) error {
		for _, c := range cmds {
			if c.Name() == "wait" {
				h.mu.Lock()
				h.n[h.addr]++
				h.mu.Unlock()
			}
		}
		return next(ctx, cmds)
	}
}

// Throughput with and without the wait: ECDL_BENCH_WAIT=1 go test -run WaitThroughput -v
func TestIntegrationWaitThroughput(t *testing.T) {
	if os.Getenv("ECDL_BENCH_WAIT") == "" {
		t.Skip("ECDL_BENCH_WAIT not set")
	}
	ctx := context.Background()
	run := func(name string, target redis.UniversalClient, w *replicaWait) {
		useReplication(t, w)
		const total, batch = 10000, 2000
		var times []time.Duration
		for round := 0; round < 11; round++ {
			start := time.Now()
			for b := 0; b < total/batch; b++ {
				ops := upserts(fmt.Sprintf("itest:bench:%d", b), batch, fmt.Sprintf("%d-%d", 1000+round, b))
				if res := writeOps(ctx, target, ops); res.unconfirmed() != 0 {
					t.Fatalf("%s: %v", name, res.lastErr)
				}
			}
			times = append(times, time.Since(start))
		}
		sort.Slice(times, func(i, j int) bool { return times[i] < times[j] })
		med := times[len(times)/2]
		t.Logf("%-22s %d writes in batches of %d: median %v = %.0f writes/s (best %v)", name, total, batch,
			med, float64(total)/med.Seconds(), times[0])
	}
	if p := os.Getenv("ECDL_TEST_VALKEY"); p != "" && os.Getenv("ECDL_TEST_VALKEY_REPLICA") != "" {
		c := hookedClient(t, p)
		if err := loadScripts(ctx, c); err != nil {
			t.Fatal(err)
		}
		run("single, no wait", c, newReplicaWait(0, time.Second))
		run("single, WAIT 1", c, newReplicaWait(1, time.Second))
	}
	if a := os.Getenv("ECDL_TEST_VALKEY_CLUSTER"); a != "" {
		c := redis.NewClusterClient(&redis.ClusterOptions{Addrs: []string{a},
			NewClient: func(o *redis.Options) *redis.Client { cl := redis.NewClient(o); installWaitHook(cl); return cl }})
		defer c.Close()
		if err := loadScripts(ctx, c); err != nil {
			t.Fatal(err)
		}
		run("cluster x3, no wait", c, newReplicaWait(0, time.Second))
		run("cluster x3, WAIT 1", c, newReplicaWait(1, time.Second))
	}
}
