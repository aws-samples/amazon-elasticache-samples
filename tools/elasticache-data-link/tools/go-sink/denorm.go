package main

// Denormalization: parent fields embedded on child rows (fan-out, parent ->
// children). Correctness rules, all enforced on the target by Lua scripts:
//
//  1. A row's own columns are guarded by its version (_ecdl_v), exactly as on
//     the plain path: an older change is skipped, a delete leaves a tombstone.
//     This used to be unguarded, and ANY denormalize rule moved EVERY table in
//     the link onto that unguarded path (X4).
//  2. Each embedded parent has its own version on the child,
//     _ecdl_pv:<parent>:<join_field>, holding the parent change it came from.
//     Parent and child changes arrive on different streams, so the child's
//     version cannot order them; the parent's own stream id can.
//  3. The child records which parent it points at, _ecdl_fk:<parent>:<join>.
//     A fan-out applies only to a child that still points at that parent, so a
//     stale reverse-index entry can never write one parent's fields onto a
//     child of another.
//  4. After writing a child, its parent is read again. If the parent changed
//     while the child was being written, the newer parent fields are applied
//     (guarded by rule 2). Together with adding the child to the reverse index
//     BEFORE writing it, this closes the race in which a parent update fans out
//     just before a new child appears and the child embeds the older value.
//  5. Every key is built with the link's key prefix (X5): rows with rowKey and
//     indexes with idxKey. Deletes, fan-out and blanking used to ignore it.
//
// Only tables that take part in a rule (as child or parent) use this path; all
// other tables in the link keep the batched, guarded plain path.

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
)

// Statuses returned by the denorm scripts.
const (
	stStale   = 0 // an equal-or-newer change is already stored; nothing written
	stApplied = 1
	stReplay  = 2 // this exact change is already stored (a retry after a partial failure)
)

// KEYS[1]=row, KEYS[2]=tombstone. ARGV: version, versionField, replace("1"),
// nOwn, own field/value pairs, nGroups, then per group: fkField, fk, pvField,
// pv, nEmbed, embed field/value pairs. Returns {status, old fk per group}.
var denormUpsertScript = redis.NewScript(luaNewer + `
local v, vf, rep = ARGV[1], ARGV[2], ARGV[3]
local i = 4
local nOwn = tonumber(ARGV[i]); i = i + 1
local own = {}
for k = 1, 2 * nOwn do own[#own + 1] = ARGV[i]; i = i + 1 end
local nG = tonumber(ARGV[i]); i = i + 1
local groups = {}
for g = 1, nG do
  local grp = {fkf = ARGV[i], fk = ARGV[i + 1], pvf = ARGV[i + 2], pv = ARGV[i + 3]}
  local n = tonumber(ARGV[i + 4]); i = i + 5
  grp.kv = {}
  for k = 1, 2 * n do grp.kv[#grp.kv + 1] = ARGV[i]; i = i + 1 end
  groups[g] = grp
end
local oldfk = {}
for g = 1, nG do oldfk[g] = redis.call('HGET', KEYS[1], groups[g].fkf) or '' end
local function reply(st)
  local r = {st}
  for g = 1, nG do r[#r + 1] = oldfk[g] end
  return r
end
local cur = current()
if cur and cur == v then return reply(2) end
if cur and not newer(v, cur) then return {0} end
for g = 1, nG do
  local grp = groups[g]
  local spv = redis.call('HGET', KEYS[1], grp.pvf)
  -- Same parent, and the stored embed is from a newer parent change than the
  -- one this child read: keep the stored embed.
  if oldfk[g] == grp.fk and spv and newer(spv, grp.pv) then
    local names = {}
    for k = 1, #grp.kv, 2 do names[#names + 1] = grp.kv[k] end
    local kv = {}
    if #names > 0 then
      local vals = redis.call('HMGET', KEYS[1], unpack(names))
      for k = 1, #names do kv[#kv + 1] = names[k]; kv[#kv + 1] = vals[k] or '' end
    end
    grp.kv = kv
    grp.pv = spv
  end
end
if rep == '1' then redis.call('DEL', KEYS[1]) end
local args = {}
for k = 1, #own do args[#args + 1] = own[k] end
for g = 1, nG do
  local grp = groups[g]
  for k = 1, #grp.kv do args[#args + 1] = grp.kv[k] end
  args[#args + 1] = grp.fkf; args[#args + 1] = grp.fk
  args[#args + 1] = grp.pvf; args[#args + 1] = grp.pv
end
args[#args + 1] = vf; args[#args + 1] = v
redis.call('HSET', KEYS[1], unpack(args))
if KEYS[2] ~= KEYS[1] then redis.call('DEL', KEYS[2]) end
return reply(1)
`)

// KEYS as above. ARGV: version, versionField, tombstone TTL, fkField... .
// Returns {status, stored fk per field} (the fks the deleted row pointed at).
var denormDeleteScript = redis.NewScript(luaNewer + `
local fks = {}
for k = 4, #ARGV do fks[#fks + 1] = redis.call('HGET', KEYS[1], ARGV[k]) or '' end
local cur = current()
local function reply(st)
  local r = {st}
  for k = 1, #fks do r[#r + 1] = fks[k] end
  return r
end
if cur and cur == ARGV[1] then return reply(2) end
if cur and not newer(ARGV[1], cur) then return {0} end
redis.call('DEL', KEYS[1])
if KEYS[2] ~= KEYS[1] then
  redis.call('SET', KEYS[2], ARGV[1], 'EX', tonumber(ARGV[3]))
end
return reply(1)
`)

// Fan-out onto one child. KEYS[1]=child. ARGV: fkField, parentPK, pvField,
// pv, embed field/value pairs. Applies only if the child exists, still points
// at this parent, and pv is newer than the stored embed. A child with no fk
// field at all was written before this version of the sink; it is accepted
// so an upgrade does not freeze existing embeds.
var embedScript = redis.NewScript(luaNewer + `
local fk = redis.call('HGET', KEYS[1], ARGV[1])
if fk then
  if fk ~= ARGV[2] then return 0 end
elseif redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
local cur = redis.call('HGET', KEYS[1], ARGV[3])
if cur and not newer(ARGV[4], cur) then return 0 end
local args = {}
for k = 5, #ARGV do args[#args + 1] = ARGV[k] end
args[#args + 1] = ARGV[3]; args[#args + 1] = ARGV[4]
redis.call('HSET', KEYS[1], unpack(args))
return 1
`)

func loadDenormScripts(ctx context.Context, c redis.Scripter) error {
	for _, s := range []*redis.Script{denormUpsertScript, denormDeleteScript, embedScript} {
		if err := s.Load(ctx, c).Err(); err != nil {
			return err
		}
	}
	return nil
}

// bareTable: 'Inventory.customers' -> 'customers'. Streams and the source
// envelope name a table by its bare name, so a schema-qualified parent_table
// used to match no table: its updates never fanned out.
func bareTable(name string) string {
	if i := strings.LastIndex(name, "."); i >= 0 {
		return name[i+1:]
	}
	return name
}

// ruleTag names one rule on a child: the parent and the join column.
func ruleTag(r DenormRule) string { return bareTable(r.ParentTable) + ":" + r.JoinField }
func fkField(r DenormRule) string { return "_ecdl_fk:" + ruleTag(r) }
func pvField(r DenormRule) string { return "_ecdl_pv:" + ruleTag(r) }
func parentKey(r DenormRule, ppk string) string {
	if r.ParentKeyPrefix != "" {
		return r.ParentKeyPrefix + ppk
	}
	return rowKey(bareTable(r.ParentTable), ppk)
}

// idxKey is the reverse index of a parent's children, under the link's key
// prefix. data-plane/bulk-loader/transforms.py idx_key must stay identical.
func idxKey(childTable, parentTable, parentPK string) string {
	return keyPrefix + "idx:" + childTable + ":by:" + bareTable(parentTable) + ":" + parentPK
}

func specHasDenorm(s *TransformSpec) bool {
	if s == nil {
		return false
	}
	for _, ts := range s.Tables {
		if len(ts.Denormalize) > 0 {
			return true
		}
	}
	return false
}

// childrenOf is ChildrenOf matched on bare names.
func childrenOf(s *TransformSpec, table string) []ChildDenorm {
	if s == nil {
		return nil
	}
	var out []ChildDenorm
	for child, ts := range s.Tables {
		for _, r := range ts.Denormalize {
			if bareTable(r.ParentTable) == table {
				out = append(out, ChildDenorm{ChildTable: child, Rule: r})
			}
		}
	}
	return out
}

// usesDenorm reports whether a table takes part in any rule.
func usesDenorm(s *TransformSpec, table string) bool {
	return len(s.DenormFor(table)) > 0 || len(childrenOf(s, table)) > 0
}

// checkDenormConfig is the startup check. Errors are fatal; warnings are
// printed. Denormalization needs the envelope format (the table and the
// before-image come from it) and the version guard (rules 1-3 above).
func checkDenormConfig(s *TransformSpec, specs []streamSpec, eventFormat string, guard bool) (warnings []string, err error) {
	if !specHasDenorm(s) {
		return nil, nil
	}
	if eventFormat != "envelope" {
		return nil, fmt.Errorf("denormalize rules need EVENT_FORMAT=envelope (got %q)", eventFormat)
	}
	if !guard {
		return nil, errors.New("denormalize rules need the version guard; VERSION_GUARD=false is not supported with them")
	}
	inLink := map[string]bool{}
	for _, sp := range specs {
		inLink[sp.table] = true
	}
	for child, ts := range s.Tables {
		for _, r := range ts.Denormalize {
			p := bareTable(r.ParentTable)
			if r.JoinField == "" || p == "" || len(r.Fields) == 0 {
				return nil, fmt.Errorf("denormalize rule on %s: parent_table, join_field and fields are required", child)
			}
			if !inLink[child] {
				warnings = append(warnings, fmt.Sprintf("denormalize: child table %s is not captured by this link", child))
			}
			if !inLink[p] {
				warnings = append(warnings, fmt.Sprintf("denormalize: parent table %s is not captured by this link; "+
					"its changes will not reach %s", p, child))
			}
			if want := keyPrefix + p + ":"; r.ParentKeyPrefix != "" && r.ParentKeyPrefix != want {
				warnings = append(warnings, fmt.Sprintf("denormalize: %s reads parent keys %q but this link writes %s rows as %q",
					child, r.ParentKeyPrefix, p, want))
			}
		}
	}
	return warnings, nil
}

// parentState is what a child embeds from one parent.
type parentState struct {
	fields map[string]string
	pv     string
}

// parentFieldName is the field a rule's parent column has in the parent's
// cached hash: the parent table's rename of it, or the column itself.
//
// A child gets its parent fields from two places: its own write reads the
// parent's cached hash, and a parent change fans out to it. The hash holds
// the parent row AFTER the parent table's transform, and the fan-out used the
// raw source row, so a parent with its own rename, columns or computed field
// gave a child different values depending on which side wrote last (Jira story F198).
// Both now read the transformed parent under this name: a column the parent's
// `columns` leaves out, or a parent its filter drops, embeds as empty on both.
// data-plane/bulk-loader/transforms.py parent_field_name must stay identical.
func parentFieldName(s *TransformSpec, r DenormRule, f string) string {
	if s != nil {
		if nn, ok := s.Tables[bareTable(r.ParentTable)].Rename[f]; ok {
			return nn
		}
	}
	return f
}

// parentEmbed is what a parent change fans out for one rule: each rule field
// read from the TRANSFORMED parent row `out`, like a child's lookup reads it
// from the cache. A field from a column Debezium could not read (unavailable)
// is left out, so children keep the value they have instead of a blank.
func parentEmbed(s *TransformSpec, r DenormRule, out map[string]string, unavailable []string) map[string]string {
	skip := make(map[string]bool, len(unavailable))
	for _, c := range unavailable {
		skip[parentFieldName(s, r, c)] = true
	}
	view := make(map[string]string, len(r.Fields))
	for _, f := range r.Fields {
		view[f] = out[parentFieldName(s, r, f)]
	}
	embed := EmbedParentFields(r, view)
	for _, f := range r.Fields {
		if skip[parentFieldName(s, r, f)] {
			delete(embed, r.Prefix+f)
		}
	}
	return embed
}

// parentEmbedChanged reports whether a parent change alters what the rule
// embeds, comparing the transformed before-image with the transformed row, to
// skip a needless fan-out. A before-image the filter drops counts as changed.
func parentEmbedChanged(s *TransformSpec, table string, r DenormRule, before, out map[string]string) bool {
	prev, keep := s.Apply(table, before)
	if !keep {
		return true
	}
	for _, f := range r.Fields {
		n := parentFieldName(s, r, f)
		bv, bok := prev[n]
		av, aok := out[n]
		if bok != aok || bv != av {
			return true
		}
	}
	return false
}

// lookupParent reads a parent's embedded fields and version from the cache.
// A read error is returned (the change is retried); it used to be ignored,
// which embedded blank values. A missing parent embeds blanks; with
// on_parent_delete=null its version is the delete's (from the tombstone), so
// stored values from before the delete are not kept. Fields are keyed by the
// rule's column names, read under parentFieldName.
func lookupParent(ctx context.Context, target redis.UniversalClient, s *TransformSpec, r DenormRule, ppk string) (parentState, error) {
	if ppk == "" {
		return parentState{fields: map[string]string{}}, nil
	}
	key := parentKey(r, ppk)
	names := make([]string, 0, len(r.Fields)+1)
	for _, f := range r.Fields {
		names = append(names, parentFieldName(s, r, f))
	}
	names = append(names, versionField)
	vals, err := target.HMGet(ctx, key, names...).Result()
	if err != nil {
		return parentState{}, err
	}
	ps := parentState{fields: make(map[string]string, len(r.Fields))}
	present := false
	for i := range names {
		if i >= len(vals) || vals[i] == nil {
			continue
		}
		present = true
		if i == len(names)-1 {
			ps.pv = fmt.Sprintf("%v", vals[i])
		} else {
			ps.fields[r.Fields[i]] = fmt.Sprintf("%v", vals[i])
		}
	}
	if !present && r.OnParentDelete != "leave" {
		tv, err := target.Get(ctx, tombstoneKey(key)).Result()
		if err != nil && !errors.Is(err, redis.Nil) {
			return parentState{}, err
		}
		ps.pv = tv
	}
	return ps, nil
}

// embedGroup is one rule's part of a child write.
type embedGroup struct {
	rule  DenormRule
	fk    string
	pv    string
	embed map[string]string
}

func denormUpsertArgs(version string, replace bool, own map[string]string, groups []embedGroup) []interface{} {
	r := "0"
	if replace {
		r = "1"
	}
	args := []interface{}{version, versionField, r}
	pairs := make([]interface{}, 0, 2*len(own))
	for k, v := range own {
		if k == versionField || strings.HasPrefix(k, "_ecdl_fk:") || strings.HasPrefix(k, "_ecdl_pv:") {
			continue // a source column never overwrites the sink's bookkeeping
		}
		pairs = append(pairs, k, v)
	}
	args = append(args, len(pairs)/2)
	args = append(args, pairs...)
	args = append(args, len(groups))
	for _, g := range groups {
		args = append(args, fkField(g.rule), g.fk, pvField(g.rule), g.pv, len(g.embed))
		for k, v := range g.embed {
			args = append(args, k, v)
		}
	}
	return args
}

// scriptReply turns {status, strings...} into its parts.
func scriptReply(v interface{}, err error) (int64, []string, error) {
	if err != nil {
		return 0, nil, err
	}
	arr, ok := v.([]interface{})
	if !ok || len(arr) == 0 {
		return 0, nil, fmt.Errorf("unexpected script reply %T", v)
	}
	st, ok := arr[0].(int64)
	if !ok {
		return 0, nil, fmt.Errorf("unexpected script status %T", arr[0])
	}
	rest := make([]string, 0, len(arr)-1)
	for _, x := range arr[1:] {
		s, _ := x.(string)
		rest = append(rest, s)
	}
	return st, rest, nil
}

func runEmbed(ctx context.Context, c redis.Scripter, childKey string, r DenormRule, parentPK, pv string, embed map[string]string) (bool, error) {
	args := []interface{}{fkField(r), parentPK, pvField(r), pv}
	for k, v := range embed {
		args = append(args, k, v)
	}
	n, err := embedScript.Run(ctx, c, []string{childKey}, args...).Int()
	return n == 1, err
}

// beforeChildWrite is nil except in tests, which use it to change a parent
// between a child's parent lookup and its write (the rule-4 race).
var beforeChildWrite func()

// processDenorm applies one change of a table that takes part in a rule.
// It returns errPoison for a message that can never apply, any other error
// for one to retry, nil when done (applied or skipped as stale). Every step is
// safe to repeat, so a retry after a partial failure completes the change.
func processDenorm(ctx context.Context, target redis.UniversalClient, msg redis.XMessage, streamTable string) error {
	val, ok := msg.Values["value"].(string)
	if !ok {
		return errPoison
	}
	var evt CDCEvent
	if err := decodeJSON([]byte(val), &evt); err != nil {
		return errPoison
	}
	table := evt.Source.Table
	if table == "" {
		table = streamTable
	}
	spec := transformSpec
	version := msgVersion(msg)
	if evt.Op == "t" {
		return truncateOne(ctx, target, streamTable, streamTable, msg, table)
	}
	// A change from before the table's last truncate is not applied
	// (truncate.go); acting on it would bring a row or an index entry back.
	truncated := func() (bool, error) {
		old, err := beforeTruncate(ctx, target, streamTable, version)
		if old {
			atomic.AddInt64(&staleSkipped, 1)
		}
		return old, err
	}
	var before map[string]string
	if evt.Before != nil {
		before = stringifyRow(evt.Before)
	}

	if evt.Op == "d" {
		pk := pkFromMsg(msg, evt.Before)
		if pk == "" {
			return errPoison
		}
		if old, err := truncated(); old || err != nil {
			return err
		}
		return denormDelete(ctx, target, spec, table, pk, version, before)
	}
	if evt.After == nil {
		return errPoison
	}
	pk := pkFromMsg(msg, evt.After)
	if pk == "" {
		return errPoison
	}
	if old, err := truncated(); old || err != nil {
		return err
	}
	// A column Debezium could not read (an unchanged TOAST or LOB value) is
	// left out of the row's own write and of the fan-out, so the cached value
	// stays on the row and on its children; see unavailable.go.
	afterRow, dropped := dropUnavailable(evt.After)
	warnUnavailable(table, evt.Op, dropped)
	after := stringifyRow(afterRow)
	if f := spec.MissingFilterField(table, after); f != "" {
		filterUnreadable(table, f)
		return errPoison
	}
	out, keep := spec.Apply(table, after)
	if !keep {
		// Filtered out: the row leaves the cache exactly as if it were deleted.
		return denormDelete(ctx, target, spec, table, pk, version, before)
	}
	key := rowKey(table, pk)

	// As CHILD: read each parent, and add this row to the parent's index
	// BEFORE writing it (rule 4).
	rules := spec.DenormFor(table)
	groups := make([]embedGroup, len(rules))
	for i, r := range rules {
		ppk := after[r.JoinField]
		ps, err := lookupParent(ctx, target, spec, r, ppk)
		if err != nil {
			return err
		}
		groups[i] = embedGroup{rule: r, fk: ppk, pv: ps.pv, embed: EmbedParentFields(r, ps.fields)}
		if ppk != "" {
			if err := target.SAdd(ctx, idxKey(table, r.ParentTable, ppk), pk).Err(); err != nil {
				return err
			}
		}
	}
	if beforeChildWrite != nil {
		beforeChildWrite()
	}
	st, oldFks, err := scriptReply(denormUpsertScript.Run(ctx, target, []string{key, tombstoneKey(key)},
		denormUpsertArgs(version, evt.Op == "r" && len(dropped) == 0, out, groups)...).Result())
	if err != nil {
		return err
	}
	if st == stStale {
		// A newer change to this row is stored. Its own processing maintains
		// the index and the fan-out; acting on this one would undo it.
		atomic.AddInt64(&staleSkipped, 1)
		return nil
	}
	if st == stApplied {
		atomic.AddInt64(&writes, 1)
	}
	for i, g := range groups {
		// Leave the index of the parent this row no longer points at. The stored
		// fk is known only when this call applied; the before-image covers a
		// retry and an engine that sends one.
		stale := map[string]bool{}
		if st == stApplied && i < len(oldFks) {
			stale[oldFks[i]] = true
		}
		stale[before[g.rule.JoinField]] = true
		if st == stReplay && evt.Op != "c" && before[g.rule.JoinField] == "" {
			warnIndexUnknown(table, pk, g.rule)
		}
		for old := range stale {
			if old != "" && old != g.fk {
				if err := target.SRem(ctx, idxKey(table, g.rule.ParentTable, old), pk).Err(); err != nil {
					return err
				}
			}
		}
		if g.fk == "" {
			continue
		}
		// Rule 4: re-read the parent; apply it if it changed meanwhile.
		ps, err := lookupParent(ctx, target, spec, g.rule, g.fk)
		if err != nil {
			return err
		}
		if ps.pv != g.pv {
			if _, err := runEmbed(ctx, target, key, g.rule, g.fk, ps.pv, EmbedParentFields(g.rule, ps.fields)); err != nil {
				return err
			}
		}
	}

	// As PARENT: fan the embedded fields out to every child.
	for _, cd := range childrenOf(spec, table) {
		if before != nil && st == stApplied && !parentEmbedChanged(spec, table, cd.Rule, before, out) {
			continue // embedded fields unchanged
		}
		if err := fanOut(ctx, target, cd, pk, version, parentEmbed(spec, cd.Rule, out, dropped)); err != nil {
			return err
		}
	}
	return nil
}

// denormDelete removes a row (deleted, or filtered out), leaves its parents'
// indexes, and applies on_parent_delete to its children.
func denormDelete(ctx context.Context, target redis.UniversalClient, spec *TransformSpec, table, pk, version string, before map[string]string) error {
	key := rowKey(table, pk)
	rules := spec.DenormFor(table)
	args := []interface{}{version, versionField, currentTombstoneTTL()}
	for _, r := range rules {
		args = append(args, fkField(r))
	}
	st, fks, err := scriptReply(denormDeleteScript.Run(ctx, target, []string{key, tombstoneKey(key)}, args...).Result())
	if err != nil {
		return err
	}
	if st == stStale {
		atomic.AddInt64(&staleSkipped, 1)
		return nil
	}
	if st == stApplied {
		atomic.AddInt64(&deletes, 1)
	}
	for i, r := range rules {
		// The stored fk covers a before-image without the column (PostgreSQL
		// sends only the key unless REPLICA IDENTITY FULL).
		if st == stReplay && before[r.JoinField] == "" {
			warnIndexUnknown(table, pk, r)
		}
		for _, old := range []string{at(fks, i), before[r.JoinField]} {
			if old != "" {
				if err := target.SRem(ctx, idxKey(table, r.ParentTable, old), pk).Err(); err != nil {
					return err
				}
			}
		}
	}
	for _, cd := range childrenOf(spec, table) {
		if cd.Rule.OnParentDelete == "leave" {
			continue
		}
		blank := make(map[string]string, len(cd.Rule.Fields))
		for _, f := range cd.Rule.Fields {
			blank[cd.Rule.Prefix+f] = ""
		}
		if err := fanOut(ctx, target, cd, pk, version, blank); err != nil {
			return err
		}
	}
	return nil
}

// warnIndexUnknown: a retried change found its own write already stored, so
// the row's previous parent is no longer on the row, and the change has no
// before-image naming it. If the first attempt failed between the write and
// leaving that parent's index, the entry stays. It is harmless (a fan-out
// checks _ecdl_fk and skips the row) but uses memory, so it is reported.
func warnIndexUnknown(table, pk string, r DenormRule) {
	logWarn("denorm-index:"+table, fmt.Errorf("a retried change of %s %s has no before-image value for %s, "+
		"so its previous parent is unknown; if the first attempt failed before leaving that parent's index, "+
		"%s still lists %s. That entry is harmless (fan-out checks %s) but is not removed; a full before-image "+
		"(PostgreSQL REPLICA IDENTITY FULL) avoids it",
		table, pk, r.JoinField, idxKey(table, r.ParentTable, "<old "+r.JoinField+">"), pk, fkField(r)))
}

func at(s []string, i int) string {
	if i < len(s) {
		return s[i]
	}
	return ""
}

var fanOutChunk = atoiOr("FANOUT_CHUNK", 500)

// fanOut applies a parent's embed to every child in its index, guarded per
// child by embedScript, in pipelines of FANOUT_CHUNK.
func fanOut(ctx context.Context, target redis.UniversalClient, cd ChildDenorm, parentPK, version string, embed map[string]string) error {
	_, err := fanOutN(ctx, target, cd, parentPK, version, embed)
	return err
}

// fanOutN is fanOut, returning how many children it changed.
func fanOutN(ctx context.Context, target redis.UniversalClient, cd ChildDenorm, parentPK, version string, embed map[string]string) (int64, error) {
	var total int64
	members, err := target.SMembers(ctx, idxKey(cd.ChildTable, cd.Rule.ParentTable, parentPK)).Result()
	if err != nil {
		return 0, err
	}
	args := []interface{}{fkField(cd.Rule), parentPK, pvField(cd.Rule), version}
	for k, v := range embed {
		args = append(args, k, v)
	}
	for startAt := 0; startAt < len(members); startAt += fanOutChunk {
		chunk := members[startAt:min(startAt+fanOutChunk, len(members))]
		n, err := fanOutChunkOnce(ctx, target, cd.ChildTable, chunk, args)
		if isNoScript(err) {
			if err = loadDenormScripts(ctx, target); err == nil {
				n, err = fanOutChunkOnce(ctx, target, cd.ChildTable, chunk, args)
			}
		}
		if err != nil {
			return total, err
		}
		atomic.AddInt64(&writes, n)
		total += n
	}
	return total, nil
}

func fanOutChunkOnce(ctx context.Context, target redis.UniversalClient, child string, members []string, args []interface{}) (int64, error) {
	pipe := target.Pipeline()
	cmds := make([]*redis.Cmd, len(members))
	for i, cpk := range members {
		cmds[i] = embedScript.EvalSha(ctx, pipe, []string{rowKey(child, cpk)}, args...)
	}
	if _, err := pipe.Exec(ctx); err != nil {
		return 0, err
	}
	var n int64
	for _, c := range cmds {
		if v, _ := c.Int64(); v == 1 {
			n++
		}
	}
	return n, nil
}

// processDenormRetry re-runs a change at once on a dropped connection or a
// retryable refusal, as writeOps does for the plain path, instead of leaving
// it for the 30 s reclaim. Each step is idempotent.
func processDenormRetry(ctx context.Context, target redis.UniversalClient, msg redis.XMessage, table string) error {
	var err error
	for attempt := 0; attempt <= writeRetries; attempt++ {
		if attempt > 0 {
			atomic.AddInt64(&writeResends, 1)
			sleep(writeRetryBase << (attempt - 1))
		}
		err = processDenorm(ctx, target, msg, table)
		if err == nil || errors.Is(err, errPoison) || ctx.Err() != nil {
			return err
		}
		if isServerReply(err) && !retryableReply(err) {
			return err // a definite refusal: reclaim retries it, then the DLQ
		}
		if isNoScript(err) {
			_ = loadDenormScripts(ctx, target)
		}
	}
	return err
}

// applyDenormBatch is applyBatch for a table that takes part in a rule: each
// change in order, acking only what was applied (or dead-lettered).
func applyDenormBatch(ctx context.Context, broker, target redis.UniversalClient, stream, group, table string, msgs []redis.XMessage) {
	t0 := time.Now()
	ackIDs := make([]string, 0, len(msgs))  // dead-lettered: acked whatever the target's replicas say
	applied := make([]string, 0, len(msgs)) // applied: acked once the replicas hold them
	for _, msg := range msgs {
		err := processDenormRetry(ctx, target, msg, table)
		if targetWide(err) {
			// Target down: hold THIS change, in order, rather than moving on
			// (hold.go). On shutdown, it and the rest stay pending.
			atomic.AddInt64(&heldTotal, 1)
			holdLoop(stream+" "+msg.ID, func() (bool, error) {
				err = processDenormRetry(ctx, target, msg, table)
				return !targetWide(err), err
			}, err)
			if sinkStopping() && targetWide(err) {
				break
			}
		}
		switch {
		case err == nil:
			markWriteOK()
			applied = append(applied, msg.ID)
		case errors.Is(err, errPoison):
			if e := deadLetter(ctx, broker, msg, stream, group, "poison", 0, errPoison); e != nil {
				logErr("dlq-write", fmt.Errorf("%s %s left pending: %w", stream, msg.ID, e))
			}
		default:
			markWriteErr()
			atomic.AddInt64(&writesUnconfirmed, 1)
			logErr("target-write", fmt.Errorf("%s %s left pending for retry: %w", stream, msg.ID, err))
		}
	}
	// These changes were written with single commands on pooled connections,
	// so one barrier write and WAIT per primary confirms all of them
	// (replwait.go). Not confirmed: they stay pending for reclaim.
	if len(applied) > 0 {
		if err := confirmReplicated(ctx, target); err != nil {
			noteShortfall(len(applied), err)
		} else {
			ackIDs = append(ackIDs, applied...)
		}
	}
	atomic.AddInt64(&totalWrite, int64(time.Since(t0)))
	atomic.AddInt64(&batchSizes, int64(len(msgs)))
	if len(ackIDs) > 0 {
		if err := ackAndTrim(ctx, broker, stream, group, ackIDs...); err != nil {
			logErr("broker-ack", fmt.Errorf("%s: %d entr(ies) stay pending: %w", stream, len(ackIDs), err))
		}
	}
}
