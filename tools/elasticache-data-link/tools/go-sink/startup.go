package main

// Startup checks: configuration the sink refuses before it reads anything.
//
// Each of these used to be accepted and then misbehave quietly:
//   - a numeric variable that did not parse was read as 0 or as its default
//     (BATCH_SIZE=abc meant XREADGROUP with no COUNT, i.e. unbounded reads)
//     (Jira story F051)
//   - two streams carrying the same bare table name (one table in two
//     schemas) wrote the same <table>:<pk> keys, each overwriting the other
//     (Jira story F039)
//   - a KEY_PREFIX with '{' or '}' became a hash tag, so no row got a delete
//     tombstone (Jira story F048)
//   - a data stream listed in TRIM_STREAMS was capped at 100 entries,
//     deleting changes nobody had applied (Jira story F049)
//   - the dlq tool and the sink defaulted BROKER_PORT differently (Jira story F047)

import (
	"context"
	"errors"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
)

// defaultBrokerPort is the port both the sink and the dlq tool use when
// BROKER_PORT is unset. 6379 is what Debezium (application.properties) and the
// broker template use; the sink alone said 6380 and the dlq tool said 6379, so
// the two commands run with the same environment reached different brokers.
const defaultBrokerPort = "6379"

func brokerAddrFromEnv() string {
	return envOr("BROKER_HOST", "localhost") + ":" + envOr("BROKER_PORT", defaultBrokerPort)
}

// numericEnv is one integer setting and the smallest value that makes sense.
type numericEnv struct {
	name     string
	min, max int64
}

// numericEnvs lists every integer the sink reads from its environment. A value
// that is set must parse and be in range; unset keeps the default.
var numericEnvs = []numericEnv{
	{"BATCH_SIZE", 1, 100000}, {"WORKERS", 1, 1024}, {"MAX_READ_ENTRIES", 1, 1000000},
	{"MAX_DELIVERIES", 1, 1000}, {"RETRY_IDLE_MS", 1, 86400000}, {"RECLAIM_INTERVAL_MS", 1, 86400000},
	{"RECLAIM_BATCH", 1, 100000}, {"CONSUMER_GC_IDLE_MS", 1, 1 << 40}, {"HEALTH_STALL_SEC", 1, 86400},
	{"WRITE_RETRIES", 0, 100}, {"WRITE_RETRY_BASE_MS", 0, 60000}, {"HOLD_BASE_MS", 1, 3600000},
	{"HOLD_MAX_MS", 1, 3600000}, {"TOMBSTONE_TTL_SEC", 1, 1 << 31}, {"TOMBSTONE_TTL_MAX_SEC", 1, 1 << 31},
	{"METRIC_INTERVAL_SEC", 1, 3600}, {"CONSUMER_ACTIVE_IDLE_MS", 1, 1 << 40},
	{"TARGET_WAIT_REFRESH_SEC", 1, 86400}, {"SHUTDOWN_GRACE_SEC", 0, 3600},
	{"CONN_MAX_IDLE_SEC", 0, 86400}, {"DRIVER_MAX_RETRIES", -1, 100}, {"FANOUT_CHUNK", 1, 100000},
	{"TRUNCATE_MARKER_REFRESH_MS", 1, 3600000}, {"TRUNCATE_RESWEEP_MS", 1, 3600000},
	{"TRUNCATE_SCAN_COUNT", 1, 1000000}, {"DLQ_MAX_LEN", 1, 1 << 40}, {"CATCHING_UP_SECONDS", 1, 86400},
	{"HEALTH_PORT", 1, 65535}, {"BROKER_PORT", 1, 65535}, {"TARGET_PORT", 1, 65535},
}

// checkNumericEnv refuses a numeric setting that does not parse or is out of
// range, naming every bad one at once.
func checkNumericEnv(getenv func(string) string) error {
	var bad []string
	for _, e := range numericEnvs {
		raw := strings.TrimSpace(getenv(e.name))
		if raw == "" {
			continue
		}
		n, err := strconv.ParseInt(raw, 10, 64)
		if err != nil {
			bad = append(bad, fmt.Sprintf("%s=%q is not a whole number", e.name, raw))
			continue
		}
		if n < e.min || n > e.max {
			bad = append(bad, fmt.Sprintf("%s=%d is outside %d..%d", e.name, n, e.min, e.max))
		}
	}
	if len(bad) > 0 {
		return errors.New("bad numeric setting(s): " + strings.Join(bad, "; "))
	}
	return nil
}

// readCount is the per-stream COUNT for XREADGROUP. COUNT applies to EACH
// stream, so one read can return batch x streams entries, and a task holds
// up to WORKERS times that in memory. The total per read is capped at
// MAX_READ_ENTRIES (default 20000, so the default 2,000 batch is unchanged up
// to 10 tables).
func readCount(batch int64, streams int, maxEntries int64) int64 {
	if streams < 1 {
		streams = 1
	}
	per := maxEntries / int64(streams)
	if per < 1 {
		per = 1
	}
	if batch < per {
		return batch
	}
	return per
}

// checkUniqueTables refuses two streams that carry the same table name. Keys
// are <table>:<pk> with the bare table name (the same shape the bulk loader
// writes), so sales.users and hr.users would write the same keys. ecdl_link
// refuses such a link at validation; this stops a hand-set STREAMS too.
func checkUniqueTables(specs []streamSpec) error {
	by := map[string][]string{}
	for _, s := range specs {
		by[s.table] = append(by[s.table], s.key)
	}
	var dup []string
	for table, keys := range by {
		if len(keys) > 1 {
			dup = append(dup, fmt.Sprintf("%s (streams %s)", table, strings.Join(keys, ", ")))
		}
	}
	if len(dup) == 0 {
		return nil
	}
	sort.Strings(dup)
	return fmt.Errorf("more than one stream carries the table %s; cache keys are <table>:<pk>, so their rows "+
		"would overwrite each other. Put tables with the same name in different links with different "+
		"advanced.key_prefix values", strings.Join(dup, "; "))
}

// checkKeyPrefix refuses braces. A prefix with '{...}' is a cluster hash tag:
// every row would hash to one slot, and tombstoneKey cannot build a tombstone
// in the row's slot, so deletes would get no tombstone at all. The link
// schema already refuses them; this covers a hand-set KEY_PREFIX.
func checkKeyPrefix(raw string) error {
	if strings.ContainsAny(raw, "{}") {
		return fmt.Errorf("KEY_PREFIX=%q contains '{' or '}'; use letters, digits, '.', '_' and '-'", raw)
	}
	return nil
}

// checkTrimStreams refuses a TRIM_STREAMS entry that is a data stream or a
// dead-letter stream of this sink: trimming it would delete changes that were
// never applied, or dead letters.
func checkTrimStreams(trim []string, specs []streamSpec) error {
	own := map[string]bool{}
	for _, s := range specs {
		own[s.key] = true
		own[dlqPrefix+s.key] = true
	}
	var bad []string
	for _, t := range trim {
		if own[t] || strings.HasPrefix(t, dlqPrefix) {
			bad = append(bad, t)
		}
	}
	if len(bad) > 0 {
		return fmt.Errorf("TRIM_STREAMS lists %s, which this sink consumes or dead-letters into; trimming it "+
			"would delete changes that were not applied", strings.Join(bad, ", "))
	}
	return nil
}

var braceKeyWarned atomic.Bool

// warnBraceKey logs once that a row key has braces and so gets no tombstone
// (see tombstoneKey). The table or primary key value has them; nothing else
// is affected, so it is a warning, not a refusal.
func warnBraceKey(key string) {
	if braceKeyWarned.CompareAndSwap(false, true) {
		logWarn("brace-key", fmt.Errorf("row key %q contains '{' or '}', so its deletes get no tombstone "+
			"and a retried older change can bring a deleted row back (logged once)", key))
	}
}

// groupWaiting is 1 while startup waits for the broker to accept the consumer
// group. Health stays OK during it: a replacement task would wait the same way.
var groupWaiting int64

// ensureGroup makes sure the consumer group exists on one stream. It reads
// XINFO GROUPS first, which a full broker still answers, so a replacement task
// starts at once when the group is already there; only a missing group is
// created (XGROUP CREATE ... MKSTREAM is refused while the broker is over
// maxmemory). It returns created=true when it created the group.
func ensureGroup(ctx context.Context, broker redis.UniversalClient, stream, group string) (bool, error) {
	groups, err := broker.XInfoGroups(ctx, stream).Result()
	if err == nil {
		noteGroups(stream, groups) // Jira story F026: before the first ack
		for _, g := range groups {
			if g.Name == group {
				return false, nil
			}
		}
	} else if !strings.Contains(err.Error(), "no such key") {
		return false, err
	}
	// "0", not "$": a group recreated on a stream that still holds entries
	// must deliver them (the guard skips any already applied).
	err = broker.XGroupCreateMkStream(ctx, stream, group, "0").Err()
	if err != nil && strings.HasPrefix(err.Error(), "BUSYGROUP") {
		return false, nil // another task created it between the two calls
	}
	return err == nil, err
}

// groupRetryable reports an error that time can fix: a full broker, a broker
// loading or failing over, or no connection.
func groupRetryable(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	for _, p := range []string{"OOM", "LOADING", "READONLY", "MASTERDOWN", "BUSY", "TRYAGAIN"} {
		if strings.HasPrefix(msg, p) {
			return true
		}
	}
	return !isServerReply(err) && isTransport(err)
}

// ensureGroups waits until every stream has the group (Jira story F037). A full broker
// used to stop the sink with FATAL at XGROUP CREATE, so new and replacement
// tasks crash-looped exactly when the broker most needed draining. Now that
// is an ERROR line every 10s and a retry; any other refusal stays fatal.
// Returns false when stop closed first.
func ensureGroups(ctx context.Context, broker redis.UniversalClient, streams []string, group string,
	stop <-chan os.Signal) (bool, error) {
	wait := holdBase
	defer atomic.StoreInt64(&groupWaiting, 0)
	for {
		var last error
		for _, s := range streams {
			if _, err := ensureGroup(ctx, broker, s, group); err != nil {
				if !groupRetryable(err) {
					return false, fmt.Errorf("cannot create consumer group %s on %s: %w", group, s, err)
				}
				last = fmt.Errorf("%s: %w", s, err)
			}
		}
		if last == nil {
			return true, nil
		}
		atomic.StoreInt64(&groupWaiting, 1)
		logErr("broker-group", fmt.Errorf("consumer group not ready, retrying in %v (a full broker refuses "+
			"XGROUP CREATE until the sink or an operator frees memory): %w", wait, last))
		select {
		case <-stop:
			return false, nil
		case <-ctx.Done():
			return false, nil
		case <-time.After(wait):
		}
		wait = nextBackoff(wait)
	}
}

// isNoGroup reports a read refused because a stream's group is gone (the
// stream or the group was deleted).
func isNoGroup(err error) bool {
	return err != nil && strings.HasPrefix(err.Error(), "NOGROUP")
}

var lastRegroupMs int64

// recreateGroups puts back a group that vanished from one of the streams
// (Jira story F050). One XREADGROUP covers every stream, so NOGROUP on one used to fail
// every table's reads until /health restarted the task. Called by any worker
// that sees NOGROUP; at most once a second across workers.
func recreateGroups(ctx context.Context, broker redis.UniversalClient, streams []string, group string) {
	now := nowMs()
	last := atomic.LoadInt64(&lastRegroupMs)
	if now-last < 1000 || !atomic.CompareAndSwapInt64(&lastRegroupMs, last, now) {
		return
	}
	for _, s := range streams {
		created, err := ensureGroup(ctx, broker, s, group)
		if err != nil {
			logErr("broker-group", fmt.Errorf("%s: recreating consumer group %s: %w", s, group, err))
			continue
		}
		if created {
			logErr("broker-group", fmt.Errorf("%s: consumer group %s was missing (stream or group deleted); "+
				"recreated it from the start of the stream", s, group))
		}
	}
}
