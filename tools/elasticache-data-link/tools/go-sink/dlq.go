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
//	ecdl-go-sink dlq replay [--limit N] [--dry-run]
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
	"os"
	"time"

	"github.com/redis/go-redis/v9"
)

func runDLQ(args []string) int {
	if len(args) == 0 || (args[0] != "list" && args[0] != "replay") {
		fmt.Fprintln(os.Stderr, "usage: ecdl-go-sink dlq list|replay [--limit N] [--dry-run]")
		return 2
	}
	action := args[0]
	fs := flag.NewFlagSet("dlq "+action, flag.ContinueOnError)
	limit := fs.Int64("limit", 1000, "maximum entries per stream")
	dryRun := fs.Bool("dry-run", false, "show what replay would do; change nothing")
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
		label: "broker", addr: envOr("BROKER_HOST", "localhost") + ":" + envOr("BROKER_PORT", "6379"),
		tls: envOr("BROKER_SSL", "false") == "true", poolSize: 4, cred: cred,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		return 1
	}
	defer broker.Close()

	total := 0
	for _, s := range specs {
		dlq := dlqPrefix + s.key
		entries, err := broker.XRangeN(ctx, dlq, "-", "+", *limit).Result()
		if err != nil {
			fmt.Fprintf(os.Stderr, "ERROR reading %s: %v\n", dlq, err)
			return 1
		}
		size, _ := broker.XLen(ctx, dlq).Result()
		fmt.Printf("%s: %d dead letter(s)\n", dlq, size)
		for _, e := range entries {
			fmt.Printf("  %s  reason=%v retries=%v failed_at=%v orig_id=%v key=%v\n",
				e.ID, e.Values["reason"], e.Values["retries"], e.Values["failed_at"],
				e.Values["orig_id"], e.Values["key"])
			if action != "replay" {
				continue
			}
			values, err := replayValues(e)
			if err != nil {
				fmt.Fprintf(os.Stderr, "    skipped: %v\n", err)
				continue
			}
			if *dryRun {
				fmt.Printf("    would replay onto %s\n", s.key)
				continue
			}
			pipe := broker.TxPipeline()
			pipe.XAdd(ctx, &redis.XAddArgs{Stream: s.key, Values: values})
			pipe.XDel(ctx, dlq, e.ID)
			if _, err := pipe.Exec(ctx); err != nil {
				fmt.Fprintf(os.Stderr, "ERROR replaying %s: %v (left in the DLQ)\n", e.ID, err)
				return 1
			}
			total++
		}
	}
	if action == "replay" && !*dryRun {
		fmt.Printf("replayed %d dead letter(s)\n", total)
	}
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
