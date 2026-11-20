package main

// Version guard: a retried or reordered change can never overwrite a newer one.
//
// WHAT WAS WRONG
// Writes are keyed by table:pk, which makes a replay of the SAME change
// harmless -- but not a replay of an OLDER one. Two paths applied changes out of
// order:
//   - reclaimLoop retries an entry ~30s after it failed. If a later change to the
//     same row was applied in the meantime, the retry wrote the older value over
//     it, and nothing would correct the key until the row changed again.
//   - workers consume one stream concurrently, so two changes to one row in
//     different batches could be applied in either order.
//
// THE FIX
// Every change carries a version, and a write only applies if it is newer than
// the version already stored for that key. The version is the broker STREAM
// ENTRY ID. One Debezium task writes each table's stream in source commit order,
// and stream IDs are strictly increasing, so stream order IS change order for
// every row in that table -- with no engine-specific parsing of binlog
// positions, LSNs or SCNs. (A dead letter replayed later keeps its ORIGINAL id
// as its version, in the ecdl_v field, so a replay cannot overwrite newer data
// either.) Rows written by the bulk loader carry LOAD_VERSION, the broker time
// at which capture was paused, so changes captured before the pause are skipped
// and changes after the resume apply.
//
// The stored version lives in the hash itself, in the field VERSION_FIELD
// (default "_ecdl_v"), so the check-and-write is one atomic Lua call on one key.
// A delete leaves a tombstone so an older retried upsert cannot recreate a
// deleted row. It lives TOMBSTONE_TTL_SEC (1h) plus the age of the oldest change
// still waiting in the broker (see tombstoneTTLFor); the tombstone key uses
// the row key as its hash tag, so it is always in the row's cluster slot.
//
// Scope: these scripts serve the plain path. Tables in a denormalize rule use
// the scripts in denorm.go, which add a per-parent version for the embedded
// fields. VERSION_GUARD=false restores unguarded HSET/DEL on the plain path and
// is refused when the link has denormalize rules.

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

var (
	versionGuard    = envOr("VERSION_GUARD", "true") == "true"
	versionField    = envOr("VERSION_FIELD", "_ecdl_v")
	tombstoneTTL    = atoiOr("TOMBSTONE_TTL_SEC", 3600)
	tombstoneTTLMax = atoiOr("TOMBSTONE_TTL_MAX_SEC", 7*24*3600)
	// The TTL written now. A tombstone must outlive every change older than
	// the delete that is still waiting in the broker, or that change recreates
	// the row when it is applied. A fixed 1h was shorter than a backlog after a
	// long outage. reclaimLoop sets this from the oldest waiting change.
	tombstoneTTLNow = int64(tombstoneTTL)
)

// tombstoneTTLFor is the TTL for a delete applied while the oldest waiting
// change is oldestAgeMs old: that age plus TOMBSTONE_TTL_SEC, capped at
// TOMBSTONE_TTL_MAX_SEC (never below TOMBSTONE_TTL_SEC).
func tombstoneTTLFor(oldestAgeMs int64) int64 {
	ttl := int64(tombstoneTTL) + (oldestAgeMs+999)/1000
	if ttl > int64(tombstoneTTLMax) {
		ttl = int64(tombstoneTTLMax)
	}
	if ttl < int64(tombstoneTTL) {
		ttl = int64(tombstoneTTL)
	}
	return ttl
}

func currentTombstoneTTL() int64 { return atomic.LoadInt64(&tombstoneTTLNow) }

// Shared comparison: stream IDs are "<ms>-<seq>". A malformed stored version is
// treated as older, so a bad value can never block updates to a key.
const luaNewer = `
local function parse(id)
  local d = string.find(id, '-', 1, true)
  if not d then return tonumber(id), 0 end
  return tonumber(string.sub(id, 1, d - 1)), tonumber(string.sub(id, d + 1))
end
local function newer(a, b)
  local am, as = parse(a)
  local bm, bs = parse(b)
  if am == nil then return false end
  if bm == nil then return true end
  if am ~= bm then return am > bm end
  return (as or 0) > (bs or 0)
end
local function current()
  local cur = redis.call('HGET', KEYS[1], ARGV[2])
  if (not cur) and KEYS[2] ~= KEYS[1] then cur = redis.call('GET', KEYS[2]) end
  return cur
end
`

// KEYS[1]=row key, KEYS[2]=tombstone key; ARGV[1]=version, ARGV[2]=version
// field, ARGV[3]="1" to replace (snapshot read), ARGV[4..]=field,value pairs.
// Returns 1 applied, 0 skipped as stale.
var upsertScript = redis.NewScript(luaNewer + `
local cur = current()
if cur and not newer(ARGV[1], cur) then return 0 end
if ARGV[3] == '1' then redis.call('DEL', KEYS[1]) end
local args = {}
for i = 4, #ARGV do args[#args + 1] = ARGV[i] end
args[#args + 1] = ARGV[2]
args[#args + 1] = ARGV[1]
redis.call('HSET', KEYS[1], unpack(args))
if KEYS[2] ~= KEYS[1] then redis.call('DEL', KEYS[2]) end
return 1
`)

// KEYS as above; ARGV[1]=version, ARGV[2]=version field, ARGV[3]=tombstone TTL.
var deleteScript = redis.NewScript(luaNewer + `
local cur = current()
if cur and not newer(ARGV[1], cur) then return 0 end
redis.call('DEL', KEYS[1])
if KEYS[2] ~= KEYS[1] then
  redis.call('SET', KEYS[2], ARGV[1], 'EX', tonumber(ARGV[3]))
end
return 1
`)

// tombstoneKey puts the tombstone in the row's own cluster slot by using the
// whole row key as the hash tag. A key that already contains braces has its
// own hash tag, which this cannot reproduce, so it gets no tombstone (the
// script then checks only the hash's own version).
func tombstoneKey(key string) string {
	if strings.ContainsAny(key, "{}") {
		warnBraceKey(key)
		return key
	}
	return "{" + key + "}:ecdl-del"
}

// msgVersion is the version a message carries: its original stream id when it
// is a replayed dead letter, otherwise its own stream id.
func msgVersion(msg redis.XMessage) string {
	if v, ok := msg.Values["ecdl_v"].(string); ok && v != "" {
		return v
	}
	return msg.ID
}

// versionNewer is luaNewer in Go, for tests and for the replay tool.
func versionNewer(a, b string) bool {
	parse := func(id string) (int64, int64, bool) {
		ms, seq, _ := strings.Cut(id, "-")
		m, err := strconv.ParseInt(ms, 10, 64)
		if err != nil {
			return 0, 0, false
		}
		s, _ := strconv.ParseInt(seq, 10, 64)
		return m, s, true
	}
	am, as, aok := parse(a)
	bm, bs, bok := parse(b)
	if !aok {
		return false
	}
	if !bok {
		return true
	}
	if am != bm {
		return am > bm
	}
	return as > bs
}

func upsertArgs(version string, replace bool, fields map[string]interface{}) []interface{} {
	r := "0"
	if replace {
		r = "1"
	}
	args := make([]interface{}, 0, 3+2*len(fields))
	args = append(args, version, versionField, r)
	for k, v := range fields {
		if k == versionField {
			continue // never let a source column overwrite the version
		}
		args = append(args, k, v)
	}
	return args
}

// queueUpsert / queueDelete add a guarded write to a pipeline (EVALSHA).
func queueUpsert(ctx context.Context, pipe redis.Pipeliner, key, version string, replace bool, fields map[string]interface{}) *redis.Cmd {
	return upsertScript.EvalSha(ctx, pipe, []string{key, tombstoneKey(key)},
		upsertArgs(version, replace, fields)...)
}

func queueDelete(ctx context.Context, pipe redis.Pipeliner, key, version string) *redis.Cmd {
	return deleteScript.EvalSha(ctx, pipe, []string{key, tombstoneKey(key)},
		version, versionField, currentTombstoneTTL())
}

// runUpsert / runDelete are the single-call forms (reclaim path). Script.Run
// falls back to EVAL when the script is not cached on that node.
func runUpsert(ctx context.Context, c redis.Scripter, key, version string, replace bool, fields map[string]interface{}) (bool, error) {
	n, err := upsertScript.Run(ctx, c, []string{key, tombstoneKey(key)},
		upsertArgs(version, replace, fields)...).Int()
	return n == 1, err
}

func runDelete(ctx context.Context, c redis.Scripter, key, version string) (bool, error) {
	n, err := deleteScript.Run(ctx, c, []string{key, tombstoneKey(key)},
		version, versionField, currentTombstoneTTL()).Int()
	return n == 1, err
}

// loadScripts caches both scripts on every node (a ClusterClient broadcasts
// SCRIPT LOAD to all shards), so the pipelined EVALSHA path finds them.
func loadScripts(ctx context.Context, c redis.Scripter) error {
	if err := upsertScript.Load(ctx, c).Err(); err != nil {
		return err
	}
	return deleteScript.Load(ctx, c).Err()
}

func isNoScript(err error) bool {
	return err != nil && strings.HasPrefix(err.Error(), "NOSCRIPT")
}

// applied reports whether a successful write command actually changed the key.
// A guarded write that answered 0 was skipped as stale and is counted; an
// unguarded HSET/DEL always counts as applied.
func applied(c redis.Cmder) bool {
	g, ok := c.(*redis.Cmd)
	if !ok {
		return true
	}
	if n, err := g.Int(); err == nil && n == 0 {
		atomic.AddInt64(&staleSkipped, 1)
		return false
	}
	return true
}

// evictionPolicy reads maxmemory_policy from INFO memory ("" if not reported).
// CONFIG GET is not allowed on ElastiCache; INFO is.
func evictionPolicy(ctx context.Context, c infoReader) (string, error) {
	info, err := c.Info(ctx, "memory").Result()
	if err != nil {
		return "", err
	}
	for _, line := range strings.Split(info, "\n") {
		if v, ok := strings.CutPrefix(strings.TrimSpace(line), "maxmemory_policy:"); ok {
			return strings.TrimSpace(v), nil
		}
	}
	return "", nil
}

// evictionWarning explains what a policy other than noeviction does to the
// guard, or returns "" when there is nothing to say.
func evictionWarning(policy string) string {
	switch {
	case policy == "" || policy == "noeviction":
		return ""
	case strings.HasPrefix(policy, "volatile-"):
		return "target maxmemory-policy is " + policy + ": under memory pressure it evicts keys with a TTL " +
			"first, and the delete tombstones are the only such keys ECDL writes; an evicted tombstone lets an " +
			"older change recreate a deleted row. Use noeviction (writes then fail and are held, not lost)"
	default:
		return "target maxmemory-policy is " + policy + ": under memory pressure it evicts cached rows and delete " +
			"tombstones, so rows silently disappear from the cache and deleted rows can come back. Use noeviction " +
			"(writes then fail and are held, not lost)"
	}
}

// warnEviction checks every shard once at startup.
func warnEviction(ctx context.Context, target redis.UniversalClient) {
	check := func(ctx context.Context, c infoReader) error {
		p, err := evictionPolicy(ctx, c)
		if err != nil {
			fmt.Printf("  note: could not read the target's maxmemory-policy (%v)\n", err)
			return nil
		}
		if w := evictionWarning(p); w != "" {
			logWarn("target-eviction", errors.New(w))
		}
		return nil
	}
	if cc, ok := target.(*redis.ClusterClient); ok {
		_ = cc.ForEachMaster(ctx, func(ctx context.Context, c *redis.Client) error { return check(ctx, c) })
		return
	}
	_ = check(ctx, target)
}

// targetEvicted counts keys the target evicted while this sink was watching,
// summed over its primaries; published as TargetEvictedKeys. Any eviction means
// the target deleted keys on its own (rows under allkeys-*, delete markers
// under volatile-*), so the alarm on it fires on the first one.
var targetEvicted int64

// evictedKeys reads evicted_keys from INFO stats.
func evictedKeys(ctx context.Context, c infoReader) (int64, error) {
	info, err := c.Info(ctx, "stats").Result()
	if err != nil {
		return 0, err
	}
	for _, line := range strings.Split(info, "\n") {
		if v, ok := strings.CutPrefix(strings.TrimSpace(line), "evicted_keys:"); ok {
			n, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64)
			if err != nil {
				return 0, fmt.Errorf("evicted_keys %q: %w", v, err)
			}
			return n, nil
		}
	}
	return 0, errors.New("INFO stats has no evicted_keys")
}

// evictionTracker turns each node's cumulative evicted_keys into increases.
// The first reading of a node is its baseline (evictions before the sink
// started are not counted). A counter that went down means the node restarted
// or a replica took over; that reading becomes the new baseline.
type evictionTracker struct{ last map[string]int64 }

func (t *evictionTracker) observe(node string, v int64) int64 {
	if t.last == nil {
		t.last = map[string]int64{}
	}
	prev, seen := t.last[node]
	t.last[node] = v
	if !seen || v < prev {
		return 0
	}
	return v - prev
}

// targetEvictionLoop polls every primary's evicted_keys and adds increases to
// targetEvicted. A node that cannot be read is skipped for that round.
func targetEvictionLoop(ctx context.Context, target redis.UniversalClient, interval time.Duration) {
	var mu sync.Mutex
	tracker := &evictionTracker{}
	check := func(ctx context.Context, node string, c infoReader) error {
		n, err := evictedKeys(ctx, c)
		if err != nil {
			return nil
		}
		mu.Lock()
		inc := tracker.observe(node, n)
		mu.Unlock()
		if inc > 0 {
			atomic.AddInt64(&targetEvicted, inc)
			logWarn("target-evicted", fmt.Errorf("the target node %s evicted %d key(s); its "+
				"maxmemory-policy deletes keys when it is full. Set noeviction and reload the link", node, inc))
		}
		return nil
	}
	tick := time.NewTicker(interval)
	defer tick.Stop()
	for {
		if cc, ok := target.(*redis.ClusterClient); ok {
			_ = cc.ForEachMaster(ctx, func(ctx context.Context, c *redis.Client) error {
				return check(ctx, c.Options().Addr, c)
			})
		} else {
			_ = check(ctx, "target", target)
		}
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}
