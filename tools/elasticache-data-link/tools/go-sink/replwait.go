package main

// Waiting for a target replica before a change is acknowledged (Jira story F032).
//
// WHAT WAS WRONG
// A change was acked and XDEL'd from the broker as soon as the target primary
// answered. Valkey replicates asynchronously, so a primary that failed over
// in the next moment took with it every write its replica had not received.
// The new primary did not have them, the broker no longer had them, and
// nothing would ever write those rows again.
//
// NOW
// Every write pipeline ends, on each primary it touched and on the same
// connection that carried its writes, with WAIT <n> <TARGET_WAIT_TIMEOUT_MS>.
// A change is acked only when WAIT reports n replicas. Otherwise it stays
// pending, reclaim applies it again later (the version guard makes that a
// no-op where the write survived), and WaitShortfall counts it.
//
// WAIT counts replicas that hold everything up to the last write made on ITS
// connection. On a connection that wrote nothing it returns at once, whatever
// the replicas hold; this is why it is sent in the writes' own pipeline. The
// pipeline also sets a short-lived barrier key in the same slot just before
// WAIT. Without it, a pipeline whose writes were all skipped by the version
// guard (a reclaim of a change that was already applied, the exact case this
// is for) would wait on an older position than the change it then acks.
//
// One WAIT per primary per pipeline, not per key. n is TARGET_WAIT_REPLICAS;
// unset means 1 on a primary that has a replica and 0 (no WAIT) on one that
// has none, read from INFO replication at start and every
// TARGET_WAIT_REFRESH_SEC. TARGET_WAIT_REPLICAS=0 turns this off.

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
)

// errWaitShortfall marks a change the primary applied but too few replicas
// confirmed. It is left pending; it is neither held nor dead-lettered.
var errWaitShortfall = errors.New("not confirmed by the target's replicas")

var waitShortfall int64 // changes left pending because WAIT fell short

// replicaWait is the WAIT configuration and what is known of the target.
type replicaWait struct {
	mu       sync.RWMutex
	fixed    int            // TARGET_WAIT_REPLICAS; -1 = decide per primary
	timeout  time.Duration  // TARGET_WAIT_TIMEOUT_MS
	replicas map[string]int // connected replicas per primary address (auto)
	anyRepl  bool           // some primary had a replica at the last read
	refresh  chan struct{}  // a primary not seen before asks for a re-read
}

// replication is off until main configures it, so a test that builds its own
// clients is not affected.
var replication = &replicaWait{fixed: 0, timeout: time.Second}

// parseWaitConfig reads TARGET_WAIT_REPLICAS and TARGET_WAIT_TIMEOUT_MS. The
// timeout has to stay under the target's 10 s read timeout (connect.go), or
// the driver gives up on the pipeline before WAIT answers.
func parseWaitConfig(getenv func(string) string) (fixed int, timeout time.Duration, err error) {
	fixed = -1
	if v := strings.TrimSpace(getenv("TARGET_WAIT_REPLICAS")); v != "" && v != "auto" {
		n, e := strconv.Atoi(v)
		if e != nil || n < 0 {
			return 0, 0, fmt.Errorf("TARGET_WAIT_REPLICAS=%q: want a number of replicas, 0 to turn the wait off, or unset", v)
		}
		fixed = n
	}
	ms := 1000
	if v := strings.TrimSpace(getenv("TARGET_WAIT_TIMEOUT_MS")); v != "" {
		n, e := strconv.Atoi(v)
		if e != nil || n < 1 || n > 9000 {
			return 0, 0, fmt.Errorf("TARGET_WAIT_TIMEOUT_MS=%q: want 1 to 9000 (below the 10 s read timeout)", v)
		}
		ms = n
	}
	return fixed, time.Duration(ms) * time.Millisecond, nil
}

func newReplicaWait(fixed int, timeout time.Duration) *replicaWait {
	return &replicaWait{fixed: fixed, timeout: timeout, replicas: map[string]int{},
		refresh: make(chan struct{}, 1)}
}

// need is n for one primary: 0 means no WAIT there.
func (w *replicaWait) need(addr string) int {
	w.mu.RLock()
	defer w.mu.RUnlock()
	if w.fixed >= 0 {
		return w.fixed
	}
	n, known := w.replicas[addr]
	if !known {
		// A primary that appeared since the last read (a failover). Wait as the
		// rest of the target does until it has been read.
		if w.refresh != nil {
			select {
			case w.refresh <- struct{}{}:
			default:
			}
		}
		if w.anyRepl {
			return 1
		}
		return 0
	}
	return min(n, 1)
}

// enabled reports whether any pipeline can carry a WAIT at all.
func (w *replicaWait) enabled() bool {
	w.mu.RLock()
	defer w.mu.RUnlock()
	if w.fixed >= 0 {
		return w.fixed > 0
	}
	return w.anyRepl
}

// connectedReplicas reads connected_slaves from INFO replication.
func connectedReplicas(ctx context.Context, c infoReader) (int, error) {
	info, err := c.Info(ctx, "replication").Result()
	if err != nil {
		return 0, err
	}
	for _, line := range strings.Split(info, "\n") {
		if v, ok := strings.CutPrefix(strings.TrimSpace(line), "connected_slaves:"); ok {
			return strconv.Atoi(strings.TrimSpace(v))
		}
	}
	return 0, errors.New("INFO replication has no connected_slaves")
}

// read records each primary's replica count. A primary that cannot be read
// keeps its last count.
func (w *replicaWait) read(ctx context.Context, target redis.UniversalClient) {
	var mu sync.Mutex
	seen := map[string]int{}
	check := func(ctx context.Context, addr string, c infoReader) error {
		n, err := connectedReplicas(ctx, c)
		if err != nil {
			logWarn("target-replicas", fmt.Errorf("%s: replica count not read: %w", addr, err))
			return nil
		}
		mu.Lock()
		seen[addr] = n
		mu.Unlock()
		return nil
	}
	if cc, ok := target.(*redis.ClusterClient); ok {
		_ = cc.ForEachMaster(ctx, func(ctx context.Context, c *redis.Client) error {
			return check(ctx, c.Options().Addr, c)
		})
	} else if c, ok := target.(*redis.Client); ok {
		_ = check(ctx, c.Options().Addr, c)
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	for a, n := range seen {
		w.replicas[a] = n
	}
	w.anyRepl = false
	for _, n := range w.replicas {
		if n > 0 {
			w.anyRepl = true
		}
	}
}

// describe is the startup line.
func (w *replicaWait) describe() string {
	w.mu.RLock()
	defer w.mu.RUnlock()
	switch {
	case w.fixed == 0:
		return "OFF (TARGET_WAIT_REPLICAS=0): an acked change can be lost in a target failover"
	case w.fixed > 0:
		return fmt.Sprintf("WAIT %d replica(s) on every primary, timeout %v", w.fixed, w.timeout)
	case !w.anyRepl:
		return "none (the target has no replicas; re-read every refresh)"
	}
	parts := make([]string, 0, len(w.replicas))
	for a, n := range w.replicas {
		parts = append(parts, fmt.Sprintf("%s=%d", a, min(n, 1)))
	}
	return fmt.Sprintf("WAIT per primary (%s), timeout %v", strings.Join(parts, ", "), w.timeout)
}

// refreshLoop re-reads the replica counts every interval, and soon after a
// pipeline met a primary it did not know.
func (w *replicaWait) refreshLoop(ctx context.Context, target redis.UniversalClient, interval time.Duration) {
	if w.fixed >= 0 {
		return
	}
	tick := time.NewTicker(interval)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		case <-w.refresh:
		}
		w.read(ctx, target)
	}
}

// waitOutcome is what one primary's WAIT answered for the commands of its
// pipeline.
type waitOutcome struct {
	addr  string
	need  int
	acked int64
	err   error
}

// waitRequest collects outcomes for one writeOps call, keyed by command. A
// command re-sent to another node (MOVED) is recorded again, and the later
// outcome wins.
type waitRequest struct {
	w   *replicaWait
	mu  sync.Mutex
	out map[redis.Cmder]waitOutcome
}

type waitCtxKey struct{}

// request starts a waitRequest, or returns nil when nothing is to be waited
// for.
func (w *replicaWait) request() *waitRequest {
	if !w.enabled() {
		return nil
	}
	return &waitRequest{w: w, out: map[redis.Cmder]waitOutcome{}}
}

func (r *waitRequest) context(ctx context.Context) context.Context {
	if r == nil {
		return ctx
	}
	return context.WithValue(ctx, waitCtxKey{}, r)
}

func (r *waitRequest) record(cmds []redis.Cmder, o waitOutcome) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, c := range cmds {
		r.out[c] = o
	}
}

// confirmed reports whether c's primary had enough replicas confirm it, and
// otherwise the reason. A command no hooked pipeline carried counts as not
// confirmed: failing closed is the point.
func (r *waitRequest) confirmed(c redis.Cmder) (bool, error) {
	r.mu.Lock()
	o, ok := r.out[c]
	r.mu.Unlock()
	switch {
	case !ok:
		return false, fmt.Errorf("%w: no WAIT ran on its connection", errWaitShortfall)
	case o.need == 0:
		return true, nil
	case o.err != nil:
		return false, fmt.Errorf("%w: WAIT on %s failed: %v", errWaitShortfall, o.addr, o.err)
	case o.acked < int64(o.need):
		return false, fmt.Errorf("%w: WAIT %d on %s answered %d within %v", errWaitShortfall,
			o.need, o.addr, o.acked, r.w.timeout)
	}
	return true, nil
}

// apply turns every confirmed op whose WAIT fell short into an unconfirmed
// one, counts them, and logs a WARN (at most once per interval).
func (r *waitRequest) apply(res *writeResult) {
	if r == nil {
		return
	}
	var why error
	for i, c := range res.cmds {
		if c == nil {
			continue
		}
		if ok, err := r.confirmed(c); !ok {
			res.cmds[i] = nil
			res.errs[i] = err
			res.waitShort++
			why = err
		}
	}
	if res.waitShort > 0 {
		atomic.AddInt64(&waitShortfall, int64(res.waitShort))
		if res.lastErr == nil {
			res.lastErr = why
		}
		logWarn("target-wait", fmt.Errorf("%d change(s) applied on the primary but left pending, "+
			"so reclaim applies them again: %w", res.waitShort, why))
	}
}

// hashTag is the part of a key that picks its cluster slot.
func hashTag(key string) string {
	if s := strings.IndexByte(key, '{'); s >= 0 {
		if e := strings.IndexByte(key[s+1:], '}'); e > 0 {
			return key[s+1 : s+1+e]
		}
	}
	return key
}

// keySlot is the cluster slot of a key: CRC16 (XMODEM) of its hash tag.
func keySlot(key string) int {
	var crc uint16
	for _, b := range []byte(hashTag(key)) {
		crc ^= uint16(b) << 8
		for i := 0; i < 8; i++ {
			if crc&0x8000 != 0 {
				crc = crc<<1 ^ 0x1021
			} else {
				crc <<= 1
			}
		}
	}
	return int(crc) & 16383
}

var (
	slotTagsOnce sync.Once
	slotTags     [16384]string
)

// barrierKey is a key in key's slot, written just before WAIT. It is built
// from a short tag known to hash to that slot, rather than from the key's own
// tag, which cannot be wrapped in braces when it contains a '}'.
func barrierKey(key string) string {
	slotTagsOnce.Do(func() {
		for i, left := 0, 16384; left > 0; i++ {
			t := "ecdl-wait-" + strconv.Itoa(i)
			if s := keySlot(t); slotTags[s] == "" {
				slotTags[s] = t
				left--
			}
		}
	})
	return "{" + slotTags[keySlot(key)] + "}"
}

// firstKey is the first key of a command this sink pipelines, or "".
func firstKey(c redis.Cmder) string {
	args := c.Args()
	name := strings.ToLower(c.Name())
	if name == "evalsha" || name == "eval" {
		if len(args) > 3 {
			if n, _ := strconv.Atoi(fmt.Sprint(args[2])); n > 0 {
				k, _ := args[3].(string)
				return k
			}
		}
		return ""
	}
	if len(args) > 1 {
		k, _ := args[1].(string)
		return k
	}
	return ""
}

// waitHook adds the barrier and WAIT to a pipeline that asks for them, on the
// client of one node: for a cluster, go-redis runs each node's share of a
// pipeline through that node's hooks, on one connection.
type waitHook struct{ addr string }

func (h waitHook) DialHook(next redis.DialHook) redis.DialHook          { return next }
func (h waitHook) ProcessHook(next redis.ProcessHook) redis.ProcessHook { return next }
func (h waitHook) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return func(ctx context.Context, cmds []redis.Cmder) error {
		r, _ := ctx.Value(waitCtxKey{}).(*waitRequest)
		if r == nil || len(cmds) == 0 {
			return next(ctx, cmds)
		}
		n := r.w.need(h.addr)
		if n <= 0 {
			err := next(ctx, cmds)
			r.record(cmds, waitOutcome{addr: h.addr})
			return err
		}
		key := ""
		for _, c := range cmds {
			if key = firstKey(c); key != "" {
				break
			}
		}
		if key == "" {
			key = "ecdl-wait"
		}
		barrier := redis.NewStatusCmd(ctx, "set", barrierKey(key), "1", "px", 1000)
		wait := redis.NewIntCmd(ctx, "wait", n, r.w.timeout.Milliseconds())
		all := make([]redis.Cmder, 0, len(cmds)+2)
		all = append(append(all, cmds...), barrier, wait)
		err := next(ctx, all)
		o := waitOutcome{addr: h.addr, need: n}
		o.acked, o.err = wait.Result()
		if o.err == nil && barrier.Err() != nil {
			o.err = barrier.Err()
		}
		r.record(cmds, o)
		// The pipeline's own error is about its own commands. A failed WAIT is
		// reported through the outcome, not as a failed write.
		for _, c := range cmds {
			if e := c.Err(); e != nil {
				return e
			}
		}
		if err != nil && !isServerReply(err) {
			return err
		}
		return nil
	}
}

// installWaitHook is called for the target client and, for a cluster, for
// every node client it creates.
func installWaitHook(c *redis.Client) { c.AddHook(waitHook{addr: c.Options().Addr}) }

// barrierKeys returns one key on each primary of the target.
func barrierKeys(ctx context.Context, target redis.UniversalClient) ([]string, error) {
	cc, ok := target.(*redis.ClusterClient)
	if !ok {
		return []string{"{ecdl-wait}"}, nil
	}
	var mu sync.Mutex
	masters := map[string]bool{}
	if err := cc.ForEachMaster(ctx, func(ctx context.Context, c *redis.Client) error {
		mu.Lock()
		masters[c.Options().Addr] = true
		mu.Unlock()
		return nil
	}); err != nil {
		return nil, err
	}
	covered := map[string]bool{}
	var keys []string
	for i := 0; i < 100000 && len(covered) < len(masters); i++ {
		k := "{ecdl-wait-" + strconv.Itoa(i) + "}"
		n, err := cc.MasterForKey(ctx, k)
		if err != nil {
			return nil, err
		}
		if a := n.Options().Addr; !covered[a] {
			covered[a] = true
			keys = append(keys, k)
		}
	}
	return keys, nil
}

// confirmReplicated waits for every primary's replicas to hold everything
// written before the call: the denormalized path and a truncate write with
// single commands, so they confirm with one barrier write per primary. nil
// when confirmed or when no wait is configured.
func confirmReplicated(ctx context.Context, target redis.UniversalClient) error {
	r := replication.request()
	if r == nil {
		return nil
	}
	keys, err := barrierKeys(ctx, target)
	if err != nil {
		return fmt.Errorf("%w: primaries not listed: %v", errWaitShortfall, err)
	}
	pipe := target.Pipeline()
	cmds := make([]redis.Cmder, len(keys))
	for i, k := range keys {
		cmds[i] = pipe.Set(ctx, k, "1", time.Second)
	}
	_, _ = pipe.Exec(r.context(ctx))
	for _, c := range cmds {
		if c.Err() != nil {
			return fmt.Errorf("%w: barrier write failed: %v", errWaitShortfall, c.Err())
		}
		if ok, err := r.confirmed(c); !ok {
			return err
		}
	}
	return nil
}

// noteShortfall counts and logs n changes left pending by confirmReplicated.
func noteShortfall(n int, err error) {
	atomic.AddInt64(&waitShortfall, int64(n))
	logWarn("target-wait", fmt.Errorf("%d change(s) applied on the primary but left pending, "+
		"so reclaim applies them again: %w", n, err))
}
