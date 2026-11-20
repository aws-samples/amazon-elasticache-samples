package main

// Dead-letter inspection and replay.
//
// A change that cannot be applied is moved to dlq:<stream> rather than lost, but
// there was no way to get it back: replaying meant manually written XRANGE/XADD in
// redis-cli against a broker only reachable from inside the VPC. This is that
// procedure as a subcommand of the sink binary, so it runs with exactly the
// sink's broker settings and credentials -- for example as a one-off ECS task
// on the sink's own task definition (docs/operations.md has the command):
//
//	ecdl-go-sink dlq list   [--limit N]
//	ecdl-go-sink dlq replay [--limit N] [--dry-run] [--force]
//	ecdl-go-sink dlq purge  [--stream S] [--older-than D] [--yes]
//
// REPLAY moves each dead letter back onto its source stream, where the running
// sink applies it like any other change, and removes it from the DLQ in the
// same MULTI so it is never in both places or neither. The replayed entry
// carries its ORIGINAL version (ecdl_v), so the sink's version guard skips it
// if the row has changed since -- a replay can never overwrite newer data.
//
// Replay only after fixing whatever caused the failure, or the entries
// return to the DLQ after MAX_DELIVERIES.

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

const dlqUsage = "usage: ecdl-go-sink dlq list|replay|purge [--limit N] [--dry-run] [--force] " +
	"[--stream S] [--older-than D] [--yes]"

// dlqOptions are the subcommand's flags.
type dlqOptions struct {
	limit     int64
	dryRun    bool
	force     bool
	stream    string        // purge: only this source stream's DLQ
	olderThan time.Duration // purge: only dead letters older than this
	yes       bool          // purge: really delete
}

func runDLQ(args []string) int {
	if len(args) == 0 || (args[0] != "list" && args[0] != "replay" && args[0] != "purge") {
		fmt.Fprintln(os.Stderr, dlqUsage)
		return 2
	}
	action := args[0]
	fs := flag.NewFlagSet("dlq "+action, flag.ContinueOnError)
	var o dlqOptions
	fs.Int64Var(&o.limit, "limit", 1000, "maximum entries per stream")
	fs.BoolVar(&o.dryRun, "dry-run", false, "show what replay would do; change nothing")
	fs.BoolVar(&o.force, "force", false, "replay dead letters older than TOMBSTONE_TTL_SEC too")
	fs.StringVar(&o.stream, "stream", "", "purge: only the dead letters of this source stream")
	fs.DurationVar(&o.olderThan, "older-than", 0, "purge: only dead letters older than this (e.g. 72h)")
	fs.BoolVar(&o.yes, "yes", false, "purge: delete (without it, purge only reports)")
	if err := fs.Parse(args[1:]); err != nil {
		return 2
	}

	specs, err := parseStreams(os.Getenv)
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		return 1
	}
	cred, err := loadCredential("BROKER")
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		return 1
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	broker, err := dial(ctx, endpoint{
		label: "broker", addr: brokerAddrFromEnv(),
		tls: envOr("BROKER_SSL", "false") == "true", poolSize: 4, cred: cred,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		return 1
	}
	defer broker.Close()
	return dlqRun(ctx, broker, specs, action, o, os.Stdout, os.Stderr)
}

// dlqStream is one dead-letter stream and the source stream it belongs to.
// inLink is false for a DLQ whose table is no longer in the link.
type dlqStream struct {
	dlq, source string
	inLink      bool
}

// findDLQs lists this link's configured dead-letter streams, then every other
// dlq:* stream on the broker (Jira story F132). A table removed from the link keeps its
// dead letters, but they used to vanish from list, replay and DLQDepth, and
// the dead-letter alarm went back to OK with changes still missing from the
// cache. The broker belongs to one link, so every dlq:* stream on it is this
// link's.
func findDLQs(ctx context.Context, broker redis.UniversalClient, specs []streamSpec) ([]dlqStream, error) {
	out := make([]dlqStream, 0, len(specs))
	seen := map[string]bool{}
	for _, s := range specs {
		out = append(out, dlqStream{dlq: dlqPrefix + s.key, source: s.key, inLink: true})
		seen[dlqPrefix+s.key] = true
	}
	var extra []string
	var cursor uint64
	for {
		keys, next, err := broker.ScanType(ctx, cursor, globEscape(dlqPrefix)+"*", 1000, "stream").Result()
		if err != nil {
			return nil, err
		}
		for _, k := range keys {
			if !seen[k] {
				seen[k] = true
				extra = append(extra, k)
			}
		}
		if cursor = next; cursor == 0 {
			break
		}
	}
	sort.Strings(extra)
	for _, k := range extra {
		out = append(out, dlqStream{dlq: k, source: strings.TrimPrefix(k, dlqPrefix)})
	}
	return out, nil
}

// globEscape quotes the SCAN MATCH special characters.
func globEscape(s string) string {
	var b strings.Builder
	for _, r := range s {
		if strings.ContainsRune(`*?[]\`, r) {
			b.WriteByte('\\')
		}
		b.WriteRune(r)
	}
	return b.String()
}

// dlqRun is the subcommand against a connected broker. Exit codes: 0 done,
// 1 failed, 3 replay stopped at --limit with dead letters left unread (Jira story F133:
// it used to print "replayed 1000" as if that were all of them).
func dlqRun(ctx context.Context, broker redis.UniversalClient, specs []streamSpec, action string, o dlqOptions,
	out, errOut io.Writer) int {
	dlqs, err := findDLQs(ctx, broker, specs)
	if err != nil {
		fmt.Fprintf(errOut, "ERROR listing dead-letter streams: %v\n", err)
		return 1
	}
	if action == "purge" {
		return dlqPurge(ctx, broker, dlqs, o, out, errOut)
	}
	replayed, skipped, unread := 0, 0, int64(0)
	for _, d := range dlqs {
		entries, err := broker.XRangeN(ctx, d.dlq, "-", "+", o.limit).Result()
		if err != nil {
			fmt.Fprintf(errOut, "ERROR reading %s: %v\n", d.dlq, err)
			return 1
		}
		size, err := broker.XLen(ctx, d.dlq).Result()
		if err != nil {
			fmt.Fprintf(errOut, "ERROR reading %s: %v\n", d.dlq, err)
			return 1
		}
		if size == 0 && d.inLink {
			fmt.Fprintf(out, "%s: 0 dead letter(s)\n", d.dlq)
			continue
		}
		note := ""
		if !d.inLink {
			note = " (its table is no longer in this link: replay cannot apply them; snapshot the table " +
				"if it is added back, or purge them)"
		}
		fmt.Fprintf(out, "%s: %d dead letter(s)%s\n", d.dlq, size, note)
		for _, e := range entries {
			fmt.Fprintf(out, "  %s  reason=%v retries=%v failed_at=%v orig_id=%v key=%v error=%q\n",
				e.ID, e.Values["reason"], e.Values["retries"], e.Values["failed_at"],
				e.Values["orig_id"], e.Values["key"], fmt.Sprint(e.Values["error"]))
			if action != "replay" {
				continue
			}
			values, err := replayValues(e)
			if err == nil && !d.inLink {
				err = fmt.Errorf("%s is not consumed by this link, so a replay would never be applied", d.source)
			}
			if err == nil && !o.force {
				err = replayAgeCheck(fmt.Sprint(values["ecdl_v"]), time.Now().UnixMilli())
			}
			if err != nil {
				fmt.Fprintf(errOut, "    skipped: %v\n", err)
				skipped++
				continue
			}
			if o.dryRun {
				fmt.Fprintf(out, "    would replay onto %s\n", d.source)
				replayed++
				continue
			}
			pipe := broker.TxPipeline()
			pipe.XAdd(ctx, &redis.XAddArgs{Stream: d.source, Values: values})
			pipe.XDel(ctx, d.dlq, e.ID)
			if _, err := pipe.Exec(ctx); err != nil {
				fmt.Fprintf(errOut, "ERROR replaying %s: %v (left in the DLQ)\n", e.ID, err)
				return 1
			}
			replayed++
		}
		if left := size - int64(len(entries)); left > 0 {
			unread += left
			fmt.Fprintf(out, "  ... %d more not shown (--limit %d)\n", left, o.limit)
		}
	}
	if action != "replay" {
		return 0
	}
	verb := "replayed"
	if o.dryRun {
		verb = "would replay"
	}
	fmt.Fprintf(out, "%s %d dead letter(s), skipped %d (left in the DLQ)", verb, replayed, skipped)
	if unread > 0 {
		fmt.Fprintf(out, "; PARTIAL: %d more were not read because of --limit %d. Run replay again, or "+
			"raise --limit\n", unread, o.limit)
		return 3
	}
	fmt.Fprintln(out)
	return 0
}

// dlqPurge deletes dead letters (Jira story F025: there was no way to, short of
// redis-cli). Without --yes it only reports what it would delete. A purged
// change is gone for good: snapshot its table afterwards if the rows matter.
func dlqPurge(ctx context.Context, broker redis.UniversalClient, dlqs []dlqStream, o dlqOptions,
	out, errOut io.Writer) int {
	minID := ""
	if o.olderThan > 0 {
		now, err := broker.Time(ctx).Result()
		if err != nil {
			fmt.Fprintf(errOut, "ERROR reading the broker clock: %v\n", err)
			return 1
		}
		minID = fmt.Sprintf("%d-0", now.Add(-o.olderThan).UnixMilli())
	}
	total, found := int64(0), false
	for _, d := range dlqs {
		if o.stream != "" && d.source != o.stream && d.dlq != o.stream {
			continue
		}
		found = true
		var n int64
		var err error
		if minID == "" {
			n, err = broker.XLen(ctx, d.dlq).Result()
		} else {
			// A DLQ holds at most DLQ_MAX_LEN entries, so reading the range is fine.
			var older []redis.XMessage
			older, err = broker.XRange(ctx, d.dlq, "-", "("+minID).Result()
			n = int64(len(older))
		}
		if err != nil {
			fmt.Fprintf(errOut, "ERROR reading %s: %v\n", d.dlq, err)
			return 1
		}
		if n == 0 {
			continue
		}
		total += n
		if !o.yes {
			fmt.Fprintf(out, "%s: would delete %d dead letter(s)\n", d.dlq, n)
			continue
		}
		if minID == "" {
			err = broker.Del(ctx, d.dlq).Err()
		} else {
			err = broker.XTrimMinID(ctx, d.dlq, minID).Err()
		}
		if err != nil {
			fmt.Fprintf(errOut, "ERROR purging %s: %v\n", d.dlq, err)
			return 1
		}
		fmt.Fprintf(out, "%s: deleted %d dead letter(s)\n", d.dlq, n)
	}
	if o.stream != "" && !found {
		fmt.Fprintf(errOut, "ERROR no dead-letter stream for %s\n", o.stream)
		return 1
	}
	if !o.yes {
		fmt.Fprintf(out, "would delete %d dead letter(s); run again with --yes to delete them\n", total)
		return 0
	}
	fmt.Fprintf(out, "deleted %d dead letter(s). They are not in the cache: snapshot their tables "+
		"(ecdl signal snapshot --tables <t>) if the rows matter\n", total)
	return 0
}

// replayValues rebuilds a source-stream entry from a dead letter: the original
// key and value, plus the original version for the guard.
func replayValues(e redis.XMessage) (map[string]interface{}, error) {
	value, _ := e.Values["value"].(string)
	if value == "" {
		return nil, fmt.Errorf("no value recorded (a poison message with no payload)")
	}
	key, _ := e.Values["key"].(string)
	version, _ := e.Values["ecdl_v"].(string)
	if version == "" {
		// Dead letters written before the guard existed have only orig_id.
		version, _ = e.Values["orig_id"].(string)
	}
	out := map[string]interface{}{"value": value, "ecdl_replayed_from": e.ID}
	if key != "" {
		out["key"] = key
	}
	if version != "" {
		out["ecdl_v"] = version
	}
	return out, nil
}

// replayAgeCheck refuses a change older than TOMBSTONE_TTL_SEC. A delete that
// came after it may have left a tombstone that has expired since, and then the
// replay would bring a deleted row back. Snapshot the table instead
// (ecdl signal snapshot --tables <t>), or pass --force after checking the row.
func replayAgeCheck(version string, nowMs int64) error {
	age := entryAgeMs(version, nowMs) // 0 when the version is missing or unparseable
	if limit := int64(tombstoneTTL) * 1000; age > limit {
		return fmt.Errorf("the change is %s old, more than TOMBSTONE_TTL_SEC (%ds): a later delete's "+
			"tombstone may have expired, so a replay could recreate a deleted row. Snapshot the table "+
			"instead (ecdl signal snapshot --tables <t>), or use --force after checking the row",
			time.Duration(age)*time.Millisecond, tombstoneTTL)
	}
	return nil
}
