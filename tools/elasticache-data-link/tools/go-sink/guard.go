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
// either.)
//
// The stored version lives in the hash itself, in the field VERSION_FIELD
// (default "_ecdl_v"), so the check-and-write is one atomic Lua call on one key.
// A delete leaves a short-lived tombstone (TOMBSTONE_TTL_SEC, default 1h) so an
// older retried upsert cannot recreate a deleted row; the tombstone key uses
// the row key as its hash tag, so it is always in the row's cluster slot.
//
// Scope: the plain (non-denormalizing) path. Denormalization fans one parent
// change out to many child keys, whose versions come from different streams, so
// a per-key stream-ID comparison does not apply there. VERSION_GUARD=false
// restores unguarded HSET/DEL.

import (
	"context"
	"strconv"
	"strings"
	"sync/atomic"

	"github.com/redis/go-redis/v9"
)

var (
	versionGuard = envOr("VERSION_GUARD", "true") == "true"
	versionField = envOr("VERSION_FIELD", "_ecdl_v")
	tombstoneTTL = atoiOr("TOMBSTONE_TTL_SEC", 3600)
)

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
		version, versionField, tombstoneTTL)
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
		version, versionField, tombstoneTTL).Int()
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
