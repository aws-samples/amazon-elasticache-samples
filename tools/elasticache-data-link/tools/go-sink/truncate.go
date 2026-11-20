package main

// TRUNCATE reaches the cache (Jira story F006).
//
// WHAT WAS WRONG
// Debezium skips truncate events unless told otherwise (skipped.operations
// defaults to "t"), so a TRUNCATE at the source never reached the broker and
// every row of the table stayed in the cache indefinitely. Had one arrived, the
// sink would have dead-lettered it: it has no row and no key.
//
// NOW
// application.properties sets skipped.operations=none, and a truncate event
// (op "t", on the table's own stream) deletes the table's rows from the target:
//
//  1. A marker, keyPrefix{ecdl-truncated}:<table>, records the truncate's
//     version (its stream id). Every write path skips a change of that table
//     whose version is not newer (truncateFilter), so a retried, reclaimed or
//     replayed change from before the truncate cannot bring a row back.
//  2. Every primary is scanned for <prefix><table>:* and, for each hash whose
//     _ecdl_v is not newer than the truncate, the key is deleted in one Lua
//     call. A row written after the truncate (an insert that followed it) is
//     newer and stays. The table's delete tombstones go the same way.
//  3. Denormalized tables: a truncated parent's children get the
//     on_parent_delete treatment (blanked) through the reverse indexes; a
//     truncated child's index entries are removed once its rows are gone.
//  4. After TRUNCATE_RESWEEP_MS the scan runs again. Another worker or task
//     may have read the marker just before it was set and written an older
//     change while the first scan ran; the second scan deletes it.
//
// Then the deletes are confirmed on the replicas (replwait.go) and the event
// is acked. Running it twice deletes nothing more, so a retry is safe.
//
// Only keys of this link's key prefix are matched: <prefix><table>: is the
// pattern, and other links' prefixes are other patterns. A link without a
// prefix whose table name equals another link's key prefix would share
// keys anyway; plan and deploy refuse two links writing one table name.
//
// DROP TABLE, dropping or detaching a partition, and TRUNCATE of one
// partition send no data event at all; see docs/operations.md.

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
)

var (
	truncateRefresh  = time.Duration(atoiOr("TRUNCATE_MARKER_REFRESH_MS", 500)) * time.Millisecond
	truncateResweep  = time.Duration(atoiOr("TRUNCATE_RESWEEP_MS", 2000)) * time.Millisecond
	truncateScanSize = int64(atoiOr("TRUNCATE_SCAN_COUNT", 1000))

	truncates     int64 // truncate events applied
	truncatedKeys int64 // rows deleted by them
)

func truncateMarkerKey(table string) string { return keyPrefix + "{ecdl-truncated}:" + table }

// KEYS[1]=marker, ARGV[1]=version. Keeps the newest truncate.
var markTruncateScript = redis.NewScript(luaNewer + `
local cur = redis.call('GET', KEYS[1])
if cur and not newer(ARGV[1], cur) then return 0 end
redis.call('SET', KEYS[1], ARGV[1])
return 1
`)

// KEYS[1]=key; ARGV[1]=truncate version, ARGV[2]=version field, ARGV[3]=the
// type this pattern holds ("hash" rows, "string" tombstones). Deletes the key
// unless it holds a newer change. Returns 1 deleted, 0 newer (kept), 2 not
// one of ours (another type, or gone).
var sweepScript = redis.NewScript(luaNewer + `
local t = redis.call('TYPE', KEYS[1])['ok']
if t ~= ARGV[3] then return 2 end
local cur
if t == 'hash' then cur = redis.call('HGET', KEYS[1], ARGV[2]) else cur = redis.call('GET', KEYS[1]) end
if cur and newer(cur, ARGV[1]) then return 0 end
redis.call('DEL', KEYS[1])
return 1
`)

// truncateMarkers caches each table's marker for TRUNCATE_MARKER_REFRESH_MS.
type truncateMarkers struct {
	mu   sync.Mutex
	vals map[string]markerEntry
}

type markerEntry struct {
	version string
	at      time.Time
}

var markers = &truncateMarkers{vals: map[string]markerEntry{}}

// get returns the table's truncate version ("" when it was never truncated).
func (m *truncateMarkers) get(ctx context.Context, target redis.UniversalClient, table string) (string, error) {
	m.mu.Lock()
	e, ok := m.vals[table]
	m.mu.Unlock()
	if ok && time.Since(e.at) < truncateRefresh {
		return e.version, nil
	}
	v, err := target.Get(ctx, truncateMarkerKey(table)).Result()
	if errors.Is(err, redis.Nil) {
		v, err = "", nil
	}
	if err != nil {
		return "", err
	}
	m.set(table, v)
	return v, nil
}

func (m *truncateMarkers) set(table, v string) {
	m.mu.Lock()
	m.vals[table] = markerEntry{version: v, at: time.Now()}
	m.mu.Unlock()
}

func (m *truncateMarkers) invalidate(table string) {
	m.mu.Lock()
	delete(m.vals, table)
	m.mu.Unlock()
}

func (m *truncateMarkers) reset() {
	m.mu.Lock()
	m.vals = map[string]markerEntry{}
	m.mu.Unlock()
}

// beforeTruncate reports whether a change of table at version is not newer
// than the table's last truncate, so must not be applied.
func beforeTruncate(ctx context.Context, target redis.UniversalClient, table, version string) (bool, error) {
	if table == "" {
		return false, nil
	}
	tv, err := markers.get(ctx, target, table)
	if err != nil || tv == "" {
		return false, err
	}
	return !versionNewer(version, tv), nil
}

// staleCmd stands for a write skipped as older than a truncate: it acks the
// change and counts as StaleSkipped, as a guard skip does.
func staleCmd(ctx context.Context) redis.Cmder {
	c := redis.NewCmd(ctx)
	c.SetVal(int64(0))
	return c
}

// truncateFilter splits ops into those to write and those already removed by
// a truncate. res gets a stale result for the latter, or the marker read
// error for every op of a table whose marker could not be read.
func truncateFilter(ctx context.Context, target redis.UniversalClient, ops []targetOp, res *writeResult) []int {
	live := make([]int, 0, len(ops))
	for i, op := range ops {
		skip, err := beforeTruncate(ctx, target, op.table, op.version)
		switch {
		case err != nil:
			res.errs[i] = fmt.Errorf("truncate marker of %s not read: %w", op.table, err)
			res.lastErr = res.errs[i]
		case skip:
			res.cmds[i] = staleCmd(ctx)
		default:
			live = append(live, i)
		}
	}
	return live
}

// isTruncate reports whether msg is a truncate event (envelope format only).
// The substring test keeps the common case free of a second JSON decode.
func isTruncate(msg redis.XMessage, eventFormat string) (bool, string) {
	if eventFormat != "envelope" {
		return false, ""
	}
	val, ok := msg.Values["value"].(string)
	if !ok || !(strings.Contains(val, `"op":"t"`) || strings.Contains(val, `"op": "t"`)) {
		return false, ""
	}
	var evt CDCEvent
	if err := decodeJSON([]byte(val), &evt); err != nil || evt.Op != "t" {
		return false, ""
	}
	return true, evt.Source.Table
}

// escapeGlob quotes the characters SCAN MATCH treats as patterns.
func escapeGlob(s string) string {
	var b strings.Builder
	for _, r := range s {
		if strings.ContainsRune(`*?[]\`, r) {
			b.WriteByte('\\')
		}
		b.WriteRune(r)
	}
	return b.String()
}

// forEachPrimary runs fn on the target, or on each primary of a cluster.
func forEachPrimary(ctx context.Context, target redis.UniversalClient, fn func(context.Context, *redis.Client) error) error {
	switch c := target.(type) {
	case *redis.ClusterClient:
		return c.ForEachMaster(ctx, fn)
	case *redis.Client:
		return fn(ctx, c)
	}
	return fmt.Errorf("unsupported target client %T", target)
}

// scanKeys calls fn with every key matching pattern on one node, in chunks.
func scanKeys(ctx context.Context, node *redis.Client, pattern string, fn func([]string) error) error {
	var cursor uint64
	for {
		keys, next, err := node.Scan(ctx, cursor, pattern, truncateScanSize).Result()
		if err != nil {
			return err
		}
		if len(keys) > 0 {
			if err := fn(keys); err != nil {
				return err
			}
		}
		if next == 0 {
			return nil
		}
		cursor = next
	}
}

// sweep deletes the keys matching pattern that hold a change not newer than
// version, on every primary. Returns how many it deleted.
func sweep(ctx context.Context, target redis.UniversalClient, pattern, version, typ string) (int64, error) {
	var n int64
	err := forEachPrimary(ctx, target, func(ctx context.Context, node *redis.Client) error {
		return scanKeys(ctx, node, pattern, func(keys []string) error {
			run := func() ([]*redis.Cmd, error) {
				pipe := node.Pipeline()
				cmds := make([]*redis.Cmd, len(keys))
				for i, k := range keys {
					cmds[i] = sweepScript.EvalSha(ctx, pipe, []string{k}, version, versionField, typ)
				}
				_, err := pipe.Exec(ctx)
				return cmds, err
			}
			cmds, err := run()
			if isNoScript(err) {
				// A node that lost its script cache (failover, restart).
				if err = sweepScript.Load(ctx, node).Err(); err == nil {
					cmds, err = run()
				}
			}
			if err != nil {
				return fmt.Errorf("%s: %w", node.Options().Addr, err)
			}
			for _, c := range cmds {
				if v, _ := c.Int64(); v == 1 {
					atomic.AddInt64(&n, 1)
				}
			}
			return nil
		})
	})
	return n, err
}

// truncateResult is what one truncate removed.
type truncateResult struct {
	rows, tombstones, indexEntries, childrenBlanked int64
}

// runTruncate applies a truncate of table at version: marker, sweep, denorm
// fix-ups, and the second sweep. table names the keys (the event's
// source.table); markerTable is the stream's table, which the write paths
// check (truncateFilter).
func runTruncate(ctx context.Context, target redis.UniversalClient, table, markerTable, version string) (truncateResult, error) {
	var res truncateResult
	if err := markTruncateScript.Run(ctx, target, []string{truncateMarkerKey(markerTable)}, version).Err(); err != nil {
		return res, fmt.Errorf("truncate marker: %w", err)
	}
	markers.invalidate(markerTable) // read again on the next write
	pass := func() error {
		rows, err := sweep(ctx, target, escapeGlob(keyPrefix+table+":")+"*", version, "hash")
		res.rows += rows
		if err != nil {
			return err
		}
		ts, err := sweep(ctx, target, "{"+escapeGlob(keyPrefix+table+":")+"*}:ecdl-del", version, "string")
		res.tombstones += ts
		if err != nil {
			return err
		}
		n, err := truncateDenorm(ctx, target, table, version)
		res.childrenBlanked += n.childrenBlanked
		res.indexEntries += n.indexEntries
		return err
	}
	if err := pass(); err != nil {
		return res, err
	}
	if truncateResweep > 0 {
		sleep(truncateResweep)
	}
	if err := pass(); err != nil {
		return res, err
	}
	return res, nil
}

// truncateDenorm: a truncated parent's children are blanked as for a delete
// (on_parent_delete "null"), and a truncated child's reverse-index entries
// for rows that are gone are removed.
func truncateDenorm(ctx context.Context, target redis.UniversalClient, table, version string) (truncateResult, error) {
	var res truncateResult
	for _, cd := range childrenOf(transformSpec, table) {
		if cd.Rule.OnParentDelete == "leave" {
			continue
		}
		blank := make(map[string]string, len(cd.Rule.Fields))
		for _, f := range cd.Rule.Fields {
			blank[cd.Rule.Prefix+f] = ""
		}
		prefix := idxKey(cd.ChildTable, cd.Rule.ParentTable, "")
		var parents []string
		err := forEachPrimary(ctx, target, func(ctx context.Context, node *redis.Client) error {
			return scanKeys(ctx, node, escapeGlob(prefix)+"*", func(keys []string) error {
				for _, k := range keys {
					parents = append(parents, strings.TrimPrefix(k, prefix))
				}
				return nil
			})
		})
		if err != nil {
			return res, err
		}
		for _, ppk := range parents {
			// The embed is versioned: a child whose parent was inserted again
			// after the truncate has a newer one and is left alone.
			n, err := fanOutN(ctx, target, cd, ppk, version, blank)
			res.childrenBlanked += n
			if err != nil {
				return res, err
			}
		}
	}
	for _, r := range transformSpec.DenormFor(table) {
		prefix := idxKey(table, r.ParentTable, "")
		var idx []string
		err := forEachPrimary(ctx, target, func(ctx context.Context, node *redis.Client) error {
			return scanKeys(ctx, node, escapeGlob(prefix)+"*", func(keys []string) error {
				idx = append(idx, keys...)
				return nil
			})
		})
		if err != nil {
			return res, err
		}
		for _, k := range idx {
			n, err := pruneIndex(ctx, target, table, r, k, strings.TrimPrefix(k, prefix))
			res.indexEntries += n
			if err != nil {
				return res, err
			}
		}
	}
	return res, nil
}

// pruneIndex removes from one reverse index the children that no longer
// exist or no longer point at that parent. A child that is being written
// right now is added to the index before it is written (denorm.go rule 4), so
// this runs only in the sweeps, never in the write path.
func pruneIndex(ctx context.Context, target redis.UniversalClient, child string, r DenormRule, idx, ppk string) (int64, error) {
	members, err := target.SMembers(ctx, idx).Result()
	if err != nil || len(members) == 0 {
		return 0, err
	}
	pipe := target.Pipeline()
	fks := make([]*redis.StringCmd, len(members))
	for i, m := range members {
		fks[i] = pipe.HGet(ctx, rowKey(child, m), fkField(r))
	}
	if _, err := pipe.Exec(ctx); err != nil && !errors.Is(err, redis.Nil) {
		return 0, err
	}
	var gone []interface{}
	for i, c := range fks {
		fk, err := c.Result()
		if errors.Is(err, redis.Nil) || (err == nil && fk != ppk) {
			gone = append(gone, members[i])
		}
	}
	if len(gone) == 0 {
		return 0, nil
	}
	return target.SRem(ctx, idx, gone...).Result()
}

// applyTruncateMsg applies one truncate event from the live path: held while
// the target is down, left pending on any other failure (reclaim retries it),
// acked once its deletes are confirmed on the replicas.
func applyTruncateMsg(ctx context.Context, broker, target redis.UniversalClient, stream, group, streamTable string, msg redis.XMessage, table string) {
	err := truncateOne(ctx, target, stream, streamTable, msg, table)
	if targetWide(err) {
		atomic.AddInt64(&heldTotal, 1)
		holdLoop(stream+" truncate "+msg.ID, func() (bool, error) {
			err = truncateOne(ctx, target, stream, streamTable, msg, table)
			return !targetWide(err), err
		}, err)
	}
	if err == nil {
		err = confirmReplicated(ctx, target)
		if err != nil {
			noteShortfall(1, err)
			return
		}
	}
	if err != nil {
		markWriteErr()
		logErr("truncate", fmt.Errorf("%s %s left pending for retry: %w", stream, msg.ID, err))
		return
	}
	markWriteOK()
	if err := ackAndTrim(ctx, broker, stream, group, msg.ID); err != nil {
		logErr("broker-ack", fmt.Errorf("%s: truncate %s stays pending: %w", stream, msg.ID, err))
	}
}

// truncateOne runs a truncate and logs what it removed.
func truncateOne(ctx context.Context, target redis.UniversalClient, stream, streamTable string, msg redis.XMessage, table string) error {
	if table == "" {
		table = streamTable
	}
	r, err := runTruncate(ctx, target, table, streamTable, msgVersion(msg))
	if err != nil {
		return err
	}
	atomic.AddInt64(&truncates, 1)
	atomic.AddInt64(&truncatedKeys, r.rows)
	atomic.AddInt64(&deletes, r.rows)
	// Every truncate gets its line: the per-kind rate limit of logWarn could
	// hide one table's truncate behind another's.
	errLog.out(fmt.Sprintf("WARN truncate: %s %s: table %s truncated at the source; deleted %d row(s) and %d "+
		"delete marker(s) older than the truncate, blanked %d child embed(s), removed %d index entr(ies)",
		stream, msg.ID, table, r.rows, r.tombstones, r.childrenBlanked, r.indexEntries))
	return nil
}
