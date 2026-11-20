package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	json "github.com/goccy/go-json"
	"github.com/redis/go-redis/v9"
)

// decodeJSON unmarshals with UseNumber so JSON numbers become json.Number
// (exact original text) instead of float64. This prevents large integer PKs
// and numeric fields from being rendered in scientific notation
// (e.g. 5177239 -> "5.177239e+06") when later formatted with %v.
func decodeJSON(b []byte, v interface{}) error {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	return dec.Decode(v)
}

var (
	writes     int64
	totalRead  int64 // nanoseconds
	totalParse int64
	totalWrite int64
	totalAck   int64
	cycles     int64
	batchSizes int64
	deletes    int64

	transformSpec *TransformSpec
)

// loadSpecFromEnv loads the transform spec from TRANSFORMS_JSON (inline) or
// TRANSFORMS_FILE (path). Absent -> nil (passthrough).
//
// A spec that is set but cannot be read or parsed is an ERROR, never
// passthrough. It used to print a line and run without it, which wrote every
// column (including ones the spec existed to leave out) and skipped filters.
func loadSpecFromEnv() (*TransformSpec, error) {
	if inline := os.Getenv("TRANSFORMS_JSON"); inline != "" {
		s, err := LoadTransformSpec([]byte(inline))
		if err != nil {
			return nil, fmt.Errorf("TRANSFORMS_JSON does not parse: %w", err)
		}
		return s, nil
	}
	if path := os.Getenv("TRANSFORMS_FILE"); path != "" {
		b, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("TRANSFORMS_FILE %s not read: %w", path, err)
		}
		s, err := LoadTransformSpec(b)
		if err != nil {
			return nil, fmt.Errorf("TRANSFORMS_FILE %s does not parse: %w", path, err)
		}
		return s, nil
	}
	return nil, nil
}

// checkSpecTables refuses a spec block for a table this sink does not
// consume. Rules are looked up by the change's bare table name, so a block
// keyed 'public.users' or 'Users' for stream table 'users' never applied.
func checkSpecTables(s *TransformSpec, specs []streamSpec) error {
	if s == nil {
		return nil
	}
	have := map[string]bool{}
	names := make([]string, 0, len(specs))
	for _, sp := range specs {
		have[sp.table] = true
		names = append(names, sp.table)
	}
	var bad []string
	for name := range s.Tables {
		if !have[name] {
			bad = append(bad, name)
		}
	}
	if len(bad) == 0 {
		return nil
	}
	sort.Strings(bad)
	return fmt.Errorf("transform spec has rules for %s, which this sink does not consume (tables: %s); "+
		"those rules would never apply", strings.Join(bad, ", "), strings.Join(names, ", "))
}

type CDCEvent struct {
	After  map[string]interface{} `json:"after"`
	Before map[string]interface{} `json:"before"`
	Source struct {
		Table string `json:"table"`
	} `json:"source"`
	Op string `json:"op"`
}

func main() {
	// `ecdl-go-sink dlq list|replay ...` inspects and replays dead letters with
	// the same broker settings the sink uses. See dlq.go.
	if len(os.Args) > 1 && os.Args[1] == "dlq" {
		os.Exit(runDLQ(os.Args[2:]))
	}
	markStarted()
	if err := checkNumericEnv(os.Getenv); err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	}
	brokerAddr := brokerAddrFromEnv()
	brokerSSL := envOr("BROKER_SSL", "false") == "true"
	targetAddr := envOr("TARGET_HOST", "localhost") + ":" + envOr("TARGET_PORT", "6379")
	targetSSL := envOr("TARGET_SSL", "true") == "true"
	targetCluster := envOr("TARGET_CLUSTER", "false") == "true"
	// Kept equal to infra/ecdl-cdc-ecs.yaml SinkBatchSize on purpose: the
	// template said 500 while this said 5000, so a deployed sink behaved
	// differently from every local run and from the published benchmarks.
	batchSize := atoiOr("BATCH_SIZE", 2000)
	workers := atoiOr("WORKERS", 16)
	group := envOr("CONSUMER_GROUP", "go-timed")
	eventFormat := envOr("EVENT_FORMAT", "envelope") // envelope | flat

	// One link, N tables, one sink. STREAMS carries every table's stream; see
	// streams.go for the accepted forms and why each stream's table is tracked.
	// There is deliberately no fallback default stream name: consuming a stream
	// that was not configured is worse than refusing to start.
	specs, err := parseStreams(os.Getenv)
	if err == nil {
		err = checkUniqueTables(specs)
	}
	if err == nil {
		err = checkTrimStreams(splitList(os.Getenv("TRIM_STREAMS")), specs)
	}
	if err == nil {
		err = checkKeyPrefix(os.Getenv("KEY_PREFIX"))
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	}
	streamKeys := streamKeysOf(specs)
	// Per-table freshness (freshness.go). A threshold naming a table this link
	// does not have stops the sink: an alarm that silently watches nothing is
	// worse than refusing to start.
	thresholds, err := parseStaleThresholds(os.Getenv("TABLE_STALE_SECONDS"), specs)
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	}
	freshness = &freshnessMonitor{specs: specs, heartbeat: os.Getenv("CAPTURE_HEARTBEAT_STREAM"),
		thresholds: thresholds}

	fmt.Printf("%s\n", buildInfo())
	fmt.Printf("  Broker: %s, Target: %s (TLS=%v)\n", brokerAddr, targetAddr, targetSSL)
	fmt.Printf("  Workers: %d, Batch: %d\n", workers, batchSize)
	fmt.Printf("  Tables: %d — %s\n", len(specs), describeStreams(specs))

	// Health/metrics endpoint for ECS container health checks and monitoring.
	startHealthServer(envOr("HEALTH_PORT", "8081"))

	// advanced.key_prefix from the link. It was documented and validated but
	// never reached the sink, so it did nothing; two links writing the same
	// table into one cache could not be separated.
	keyPrefix = normalizeKeyPrefix(os.Getenv("KEY_PREFIX"))
	if keyPrefix != "" {
		fmt.Printf("  Key prefix: %q (keys are %s<table>:<pk>)\n", keyPrefix, keyPrefix)
	}
	// Load the shared transform spec (passthrough if none configured).
	transformSpec, err = loadSpecFromEnv()
	if err == nil {
		err = checkSpecTables(transformSpec, specs)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	}
	if transformSpec != nil {
		fmt.Printf("  Transforms: %d table rule(s) loaded\n", len(transformSpec.Tables))
	} else {
		fmt.Println("  Transforms: none (passthrough)")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Both connections are proved before any work starts, and a failure is fatal.
	// Previously the Ping errors were discarded and the next line claimed the
	// connection was up, so a rejected credential produced a sink that looked
	// healthy and dead-lettered everything it read.
	brokerCred, err := loadCredential("BROKER")
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	}
	targetCred, err := loadCredential("TARGET")
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	}

	brokerEndpoint := endpoint{
		label: "broker", addr: brokerAddr, tls: brokerSSL,
		cluster: false, // always single-shard: Debezium's Redis sink is not
		// cluster aware, and one stream lives in one hash slot anyway.
		poolSize: workers + 5, cred: brokerCred,
	}
	waitFixed, waitTimeout, err := parseWaitConfig(os.Getenv)
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	}
	targetEndpoint := endpoint{
		label: "target", addr: targetAddr, tls: targetSSL,
		cluster: targetCluster, poolSize: workers + 5, cred: targetCred,
		onClient: installWaitHook,
	}

	broker, err := dial(ctx, brokerEndpoint)
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("  Connected to %s\n", brokerEndpoint.describe())

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	// A group that cannot be created means every read fails with NOGROUP, so
	// a refusal is fatal. A full broker (OOM) or one failing over is waited
	// out instead (startup.go): a replacement task could not do better.
	if ok, err := ensureGroups(ctx, broker, streamKeys, group, sigCh); err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	} else if !ok {
		fmt.Println("  shutdown while waiting for the consumer group")
		return
	}

	// Publish queue depth (BacklogPerTask) to CloudWatch so autoscaling can react
	// to how far behind the sink is, not to CPU (which stays low on this
	// I/O-bound work and scaled out minutes too late). No-op unless configured.
	// Started before the target is reached, so a target that is down at start
	// is still reported (TargetUp 0, growing lag) instead of a silent crash loop.
	go backlogMetricsLoop(ctx, broker, specs, group)

	target, err := dialTarget(ctx, targetEndpoint, sigCh)
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	}
	if target == nil {
		fmt.Println("  shutdown while waiting for the target")
		return
	}
	fmt.Printf("  Connected to %s\n", targetEndpoint.describe())
	// Replica wait before ack (replwait.go). Configured before any worker
	// starts, so no change is acked without it.
	rw := newReplicaWait(waitFixed, waitTimeout)
	if waitFixed < 0 {
		rw.read(ctx, target)
	}
	replication = rw
	go rw.refreshLoop(ctx, target, time.Duration(atoiOr("TARGET_WAIT_REFRESH_SEC", 30))*time.Second)
	fmt.Printf("  Replica wait: %s\n", rw.describe())
	warnEviction(ctx, target)
	go targetEvictionLoop(ctx, target, time.Duration(atoiOr("METRIC_INTERVAL_SEC", 20))*time.Second)

	// CDC freshness heartbeat: while the sink is alive and consuming, write a
	// liveness timestamp to the target so a downstream reader (e.g. the query
	// proxy) can detect a stalled/stopped pipeline and stop serving stale cache.
	if hbPrefix := os.Getenv("HEARTBEAT_PREFIX"); hbPrefix != "" {
		// ONE HEARTBEAT PER TABLE: <prefix>:<table>. The comment here already
		// claimed this was per-table, but the code wrote a single key named after
		// the one STREAM_TABLE -- so with several tables in a link a reader
		// checking freshness for table B was reading table A's timestamp, and a
		// stalled B looked fresh.
		hbKeys := make([]string, 0, len(specs))
		for _, s := range specs {
			hbKeys = append(hbKeys, hbPrefix+":"+s.table)
		}
		// Each key holds the table's fresh-as-of time (ms): the cache has every
		// change to the table captured before it. It used to hold the current
		// time, so a table that had stopped updating still looked fresh.
		go func() {
			t := time.NewTicker(2 * time.Second)
			defer t.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-t.C:
					// Only while healthy. A heartbeat written on a timer regardless
					// told a reader "fresh" while the sink was failing every write.
					// Nor while changes are held for the target: the cache is
					// behind, and health stays OK during a hold on purpose.
					if ok, _ := healthy(); !ok || isHolding() {
						continue
					}
					if err := writeHeartbeats(ctx, freshness, broker, target, hbPrefix); err != nil {
						logErr("heartbeat", err)
					}
				}
			}
		}()
		fmt.Printf("  CDC heartbeat -> %s (every 2s)\n", strings.Join(hbKeys, ", "))
	}

	if versionGuard {
		if err := loadScripts(ctx, target); err != nil {
			fmt.Fprintf(os.Stderr, "FATAL: cannot load the version-guard scripts on the target: %v\n"+
				"  The target must allow Lua scripting (EVAL/EVALSHA). Set VERSION_GUARD=false to "+
				"write without the stale-overwrite guard.\n", err)
			os.Exit(1)
		}
		fmt.Printf("  Version guard: ON (field %s, tombstone TTL %ds)\n", versionField, tombstoneTTL)
	} else {
		fmt.Println("  Version guard: OFF (VERSION_GUARD=false) -- a retried older change can overwrite a newer one")
	}

	start := time.Now()
	go errLog.flushLoop(ctx) // suppressed error counts, once their interval ends

	// Consumer names must be unique across tasks so multiple sink tasks can
	// share one stream's load in the same group (horizontal scaling). Default
	// the prefix to the hostname, which ECS makes unique per task.
	consumerPrefix := os.Getenv("CONSUMER_PREFIX")
	if consumerPrefix == "" {
		if hn, err := os.Hostname(); err == nil && hn != "" {
			consumerPrefix = hn
		} else {
			consumerPrefix = "w"
		}
	}
	// Denormalization is chosen PER TABLE: only tables in a rule (as child or
	// parent) take the denorm path; the rest keep the batched path. Any rule
	// used to move every table in the link onto an unguarded path.
	warns, err := checkDenormConfig(transformSpec, specs, eventFormat, versionGuard)
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	}
	for _, w := range warns {
		fmt.Printf("  WARN: %s\n", w)
	}
	if specHasDenorm(transformSpec) {
		if err := loadDenormScripts(ctx, target); err != nil {
			fmt.Fprintf(os.Stderr, "FATAL: cannot load the denormalization scripts on the target: %v\n", err)
			os.Exit(1)
		}
		var dt []string
		for _, s := range specs {
			if usesDenorm(transformSpec, s.table) {
				dt = append(dt, s.table)
			}
		}
		fmt.Printf("  Denormalization: ON for %s (guarded)\n", strings.Join(dt, ", "))
	}
	// Every worker consumes every stream, so N tables need no extra tasks and no
	// extra consumer groups: the group already load-balances across workers and
	// across sink tasks.
	count := readCount(int64(batchSize), len(specs), int64(atoiOr("MAX_READ_ENTRIES", 20000)))
	if count < int64(batchSize) {
		fmt.Printf("  Read size: %d per stream (BATCH_SIZE %d x %d streams is over MAX_READ_ENTRIES)\n",
			count, batchSize, len(specs))
	}
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		name := fmt.Sprintf("%s-w%d", consumerPrefix, i)
		wg.Add(1)
		go func() {
			defer wg.Done()
			worker(ctx, broker, target, specs, group, name, count, eventFormat)
		}()
	}

	// Reliability: retry failed writes and dead-letter the ones that exhaust
	// MAX_DELIVERIES, so a change that cannot be applied is never silently
	// dropped.
	//
	// This now runs for EVERY stream and in EVERY configuration. It used to be
	// given only the first stream, and to be skipped altogether when
	// denormalization was on -- so the two configurations that most needed a
	// retry path (many tables; fan-out writes) were the two without one. The
	// retry uses the same apply logic the live worker used, or a replay would
	// write a different value than the fast path would have.
	go reclaimLoop(ctx, broker, target, specs, group, consumerPrefix, eventFormat)
	// Keep streams that nothing consumes short (Debezium's heartbeat). They are
	// written on purpose -- an idle source commits its offset only through them
	// -- but left alone they would grow by one entry per heartbeat indefinitely.
	go trimLoop(ctx, broker, splitList(os.Getenv("TRIM_STREAMS")), trimMaxLen, time.Minute)

	dlqNames := make([]string, 0, len(specs))
	for _, s := range specs {
		dlqNames = append(dlqNames, dlqPrefix+s.key)
	}
	fmt.Printf("  Reliability: retry+DLQ ON (max_deliveries=%d, retry_idle=%v, dlq=%s)\n",
		maxDeliveries, retryIdle, strings.Join(dlqNames, ", "))

	// Stats every 5s
	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				w := atomic.LoadInt64(&writes)
				c := atomic.LoadInt64(&cycles)
				elapsed := time.Since(start).Seconds()
				if c > 0 {
					avgRead := time.Duration(atomic.LoadInt64(&totalRead) / c)
					avgParse := time.Duration(atomic.LoadInt64(&totalParse) / c)
					avgWrite := time.Duration(atomic.LoadInt64(&totalWrite) / c)
					avgAck := time.Duration(atomic.LoadInt64(&totalAck) / c)
					avgBatch := atomic.LoadInt64(&batchSizes) / c
					d := atomic.LoadInt64(&deletes)
					fmt.Printf("  [%.0fs] writes=%d deletes=%d rate=%.0f/sec | per-cycle: read=%v parse=%v write=%v ack=%v batch=%d\n",
						elapsed, w, d, float64(w)/elapsed, avgRead, avgParse, avgWrite, avgAck, avgBatch)
				}
			}
		}
	}()

	<-sigCh
	stoppingFlag.Store(true) // ends any hold (hold.go); its changes stay pending
	// Stop reading, then let in-flight batches finish (they run on a context
	// that ignores this cancel) so a scale-in or deploy does not leave writes waiting
	// for the 30s reclaim. Bounded below the ECS 30s stop timeout.
	cancel()
	grace := time.Duration(atoiOr("SHUTDOWN_GRACE_SEC", 20)) * time.Second
	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	select {
	case <-done:
		fmt.Println("  shutdown: all workers finished their batches")
	case <-time.After(grace):
		fmt.Printf("  shutdown: workers still busy after %v; unacked entries stay pending for the other tasks\n", grace)
	}
	elapsed := time.Since(start).Seconds()
	w := atomic.LoadInt64(&writes)
	c := atomic.LoadInt64(&cycles)
	fmt.Printf("\n=== FINAL ===\n")
	fmt.Printf("  Writes: %d in %.1fs = %.0f/sec\n", w, elapsed, float64(w)/elapsed)
	if c > 0 {
		fmt.Printf("  Cycles: %d\n", c)
		fmt.Printf("  Avg batch size: %d\n", atomic.LoadInt64(&batchSizes)/c)
		fmt.Printf("  Avg Read:  %v\n", time.Duration(atomic.LoadInt64(&totalRead)/c))
		fmt.Printf("  Avg Parse: %v\n", time.Duration(atomic.LoadInt64(&totalParse)/c))
		fmt.Printf("  Avg Write: %v\n", time.Duration(atomic.LoadInt64(&totalWrite)/c))
		fmt.Printf("  Avg Ack:   %v\n", time.Duration(atomic.LoadInt64(&totalAck)/c))
		total := atomic.LoadInt64(&totalRead) + atomic.LoadInt64(&totalParse) + atomic.LoadInt64(&totalWrite) + atomic.LoadInt64(&totalAck)
		fmt.Printf("  %% Read:  %.1f%%\n", 100*float64(atomic.LoadInt64(&totalRead))/float64(total))
		fmt.Printf("  %% Parse: %.1f%%\n", 100*float64(atomic.LoadInt64(&totalParse))/float64(total))
		fmt.Printf("  %% Write: %.1f%%\n", 100*float64(atomic.LoadInt64(&totalWrite))/float64(total))
		fmt.Printf("  %% Ack:   %.1f%%\n", 100*float64(atomic.LoadInt64(&totalAck))/float64(total))
	}
}

// worker consumes EVERY stream in specs, not just the first one.
//
// One XREADGROUP covers all of them, so N tables cost one round trip per cycle
// rather than N sinks. Each stream in the reply is then applied with ITS OWN
// table: with EVENT_FORMAT=flat the table cannot be recovered from the payload,
// so taking it from the stream is the only correct source, and previously every
// message was attributed to the single STREAM_TABLE.
func worker(ctx context.Context, broker, target redis.UniversalClient, specs []streamSpec, group, name string, batch int64, eventFormat string) {
	sarg := readArgs(specs)
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		// STEP 1: READ from broker. One call, all streams.
		t0 := time.Now()
		results, err := broker.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group: group, Consumer: name,
			Streams: sarg, Count: batch, Block: 500 * time.Millisecond,
		}).Result()
		t1 := time.Now()

		if err != nil && !errors.Is(err, redis.Nil) {
			if ctx.Err() != nil {
				return // shutting down
			}
			// Used to be a bare `continue`: a broker that rejected every read
			// (NOGROUP, auth, OOM, network) produced a sink that logged nothing.
			logErr("broker-read", err)
			if isNoGroup(err) {
				recreateGroups(ctx, broker, streamKeysOf(specs), group)
			}
			time.Sleep(200 * time.Millisecond) // do not spin on a hard failure
			continue
		}
		markReadOK()
		if len(results) == 0 {
			continue
		}

		// One read = one cycle, however many streams it returned.
		atomic.AddInt64(&totalRead, int64(t1.Sub(t0)))
		atomic.AddInt64(&cycles, 1)

		// The reply holds one entry per stream that had messages. Iterating it is
		// the whole fix: this used to read results[0] only, so on a busy link the
		// tables that happened to sort later were never applied.
		for _, st := range results {
			if len(st.Messages) == 0 {
				continue
			}
			streamTable, known := tableFor(specs, st.Stream)
			if !known {
				// Cannot happen with the arg list we built, but attributing rows
				// to the wrong table would corrupt the cache, so refuse rather
				// than guess.
				continue
			}
			// WithoutCancel: a batch that was read is finished even during
			// shutdown, so a SIGTERM (scale-in, deploy) does not abort writes
			// half-way and leave them to the 30s reclaim.
			if usesDenorm(transformSpec, streamTable) {
				applyDenormBatch(context.WithoutCancel(ctx), broker, target, st.Stream, group,
					streamTable, st.Messages)
				continue
			}
			applyBatch(context.WithoutCancel(ctx), broker, target, st.Stream, group,
				st.Messages, eventFormat, streamTable)
		}
	}
}

// applyBatch is one stream's slice of a read cycle. A truncate event
// (truncate.go) splits it: the changes before it are applied first, then the
// truncate, then the rest, so the order holds even without the version guard.
func applyBatch(ctx context.Context, broker, target redis.UniversalClient, stream, group string,
	msgs []redis.XMessage, eventFormat, streamTable string) {
	start := 0
	for i, m := range msgs {
		t, table := isTruncate(m, eventFormat)
		if !t {
			continue
		}
		if i > start {
			applyPlainBatch(ctx, broker, target, stream, group, msgs[start:i], eventFormat, streamTable)
		}
		applyTruncateMsg(ctx, broker, target, stream, group, streamTable, m, table)
		start = i + 1
	}
	if start < len(msgs) {
		applyPlainBatch(ctx, broker, target, stream, group, msgs[start:], eventFormat, streamTable)
	}
}

// applyPlainBatch is plan, write, then ack only what the target confirmed.
// Split out of worker() so the multi-stream loop does not nest four levels
// deep; the logic is unchanged from the single-stream version.
func applyPlainBatch(ctx context.Context, broker, target redis.UniversalClient, stream, group string,
	msgs []redis.XMessage, eventFormat, streamTable string) {
	{
		batchLen := int64(len(msgs))

		// STEP 2: PLAN each message (parse + transform). Unparseable /
		// structurally invalid messages ("poison") are collected for the DLQ
		// instead of being silently dropped.
		t2 := time.Now()
		// Kept in message order, writes and deletes together, so a re-send and an
		// unguarded target (VERSION_GUARD=false) apply them in the order they
		// happened. They used to be sent as all writes, then all deletes.
		ops := make([]targetOp, 0, batchLen)
		var poison []redis.XMessage
		for _, msg := range msgs {
			key, fields, isDel, isPoison, replace := planMessage(msg, eventFormat, streamTable)
			if isPoison {
				poison = append(poison, msg)
				continue
			}
			ops = append(ops, targetOp{id: msg.ID, version: msgVersion(msg), key: key,
				table: streamTable, fields: fields, replace: replace, del: isDel})
		}
		t3 := time.Now()
		parseDur := t3.Sub(t2)

		// STEP 3: WRITE to target, re-sending at once on a transient failure
		// (writes.go). With the version guard on (the default) each write is one
		// atomic EVALSHA that applies only if this change is newer than the one
		// already stored for the key -- see guard.go. A stale change is
		// "skipped", which is a success for acking purposes: a newer value is
		// already in place.
		t4 := time.Now()
		res := writeOps(ctx, target, ops)
		// Target down: keep this batch and retry it, rather than reading more
		// (hold.go). Returns early only on shutdown.
		res = holdBatch(ctx, target, stream, ops, res)
		t5 := time.Now()
		writeDur := t5.Sub(t4)
		// Changes left only because their replicas did not confirm them in
		// time were applied: they are counted and logged as WaitShortfall
		// (replwait.go), not as failed writes.
		left := res.unconfirmed() - res.waitShort
		refused := res.refusedCount()
		atomic.AddInt64(&writesRefused, int64(refused))
		switch {
		case left > 0:
			// One refused change (a WRONGTYPE on one hot key) does not make the
			// batch a failed write: the rest were applied, so the target is up
			// (Jira story F040). Refusals are counted on their own, as WritesRefused.
			if left < len(ops) {
				markWriteOK()
			}
			markWriteErr()
			atomic.AddInt64(&writesUnconfirmed, int64(left))
			logErr("target-write", fmt.Errorf("%s: %d of %d change(s) not confirmed after %d attempt(s), %d of "+
				"them refused by the target; left pending for reclaim (retried after %v, dead-lettered after "+
				"%d deliveries): %w", stream, left, len(ops), res.sends, refused, retryIdle, maxDeliveries, res.lastErr))
		case res.sends > 1:
			markWriteOK()
			logWarn("target-write-retry", fmt.Errorf("%s: %d change(s) confirmed on attempt %d after: %w",
				stream, len(ops), res.sends, res.firstErr))
		case len(ops) > 0:
			markWriteOK()
		}

		// STEP 4: ACK + trim ONLY entries whose write the target CONFIRMED.
		// Anything unconfirmed stays in the pending list (PEL) for reclaimLoop()
		// to replay -- never acked, never deleted. Writes are idempotent (keyed
		// by table:pk), so a replay is a harmless no-op. This is the
		// confirmed-apply-before-trim invariant: a broker entry is removed only
		// once the target accepted the write and, with a replica wait, once
		// its replicas hold it, so a target failover cannot lose it.
		t6 := time.Now()
		ackIDs := make([]string, 0, batchLen)
		okW := 0
		okD := 0
		stale := 0
		for i, op := range ops {
			c := res.cmds[i]
			if c == nil {
				continue
			}
			ackIDs = append(ackIDs, op.id)
			if applied(c) {
				if op.del {
					okD++
				} else {
					okW++
				}
			} else {
				stale++
			}
		}
		warnStale(stream, stale)
		// Poison messages are structurally invalid and never entered the target
		// pipeline, so their handling is independent of execErr: each is
		// dead-lettered and acked in one MULTI (deadLetter). If that fails,
		// the entry is left pending so reclaimLoop dead-letters it later --
		// acking it without the dead letter would drop the change from both
		// the stream and the DLQ (a silent loss, which OLTP cannot tolerate).
		for _, m := range poison {
			if err := deadLetter(ctx, broker, m, stream, group, "poison", 0, errPoison); err != nil {
				logErr("dlq-write", fmt.Errorf("%s %s left pending: %w", stream, m.ID, err))
			}
		}
		// Atomic ack+trim (MULTI), so an entry is never acked without being
		// deleted. See ackAndTrim for the failure cases this prevents.
		if len(ackIDs) > 0 {
			if err := ackAndTrim(ctx, broker, stream, group, ackIDs...); err != nil {
				// Not a loss: the writes succeeded and the entries stay pending, so
				// reclaim acks them later (the guard makes the replay a no-op).
				logErr("broker-ack", fmt.Errorf("%s: %d entr(ies) stay pending: %w",
					stream, len(ackIDs), err))
			}
		}
		t7 := time.Now()
		ackDur := t7.Sub(t6)

		// Record timings + counters (count only successful writes/deletes).
		// `cycles` is deliberately NOT incremented here: it counts READ cycles,
		// and one read now covers every stream. Incrementing per stream would
		// divide the per-cycle averages by the table count and make a
		// multi-table link look faster per cycle than it is.
		atomic.AddInt64(&totalParse, int64(parseDur))
		atomic.AddInt64(&totalWrite, int64(writeDur))
		atomic.AddInt64(&totalAck, int64(ackDur))
		atomic.AddInt64(&batchSizes, batchLen)
		atomic.AddInt64(&writes, int64(okW))
		atomic.AddInt64(&deletes, int64(okD))
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// writeBody sends a non-HTML response with its type stated and sniffing off,
// so a browser pointed at these endpoints can never treat the body as a page.
func writeBody(w http.ResponseWriter, status int, contentType, body string) {
	h := w.Header()
	h.Set("Content-Type", contentType)
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_, _ = strings.NewReader(body).WriteTo(w)
}

// healthMux serves /health (liveness), /version and /metrics (counters) so ECS
// can health-check the container and monitoring can scrape progress.
func healthMux() *http.ServeMux {
	mux := http.NewServeMux()
	// 503 when the sink cannot make progress (see health.go). It used to answer
	// 200 unconditionally, so ECS never replaced a sink that was failing every
	// read or write.
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		ok, reason := healthy()
		if !ok {
			writeBody(w, http.StatusServiceUnavailable, "text/plain; charset=utf-8", reason+"\n")
			return
		}
		writeBody(w, http.StatusOK, "text/plain; charset=utf-8", "ok\n")
	})
	// Which build is actually running. The ECS health check reads /health, which
	// stays a bare "ok" so its contract does not change.
	mux.HandleFunc("/version", func(w http.ResponseWriter, r *http.Request) {
		writeBody(w, http.StatusOK, "application/json", buildJSON()+"\n")
	})
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		body := fmt.Sprintf(`{"writes":%d,"deletes":%d,"cycles":%d,"dlq":%d,"retried":%d,"backlog_per_task":%d,"stale_skipped":%d,"dlq_depth":%d,"oldest_entry_age_ms":%d,"write_resends":%d,"writes_recovered":%d,"writes_unconfirmed":%d,"pending_vanished":%d,"target_evicted":%d,"target_up":%d,"holding":%d,"held_total":%d,"capture_age_ms":%d,"capture_paused":%d,"capture_stale_ms":%d,"wait_shortfall":%d,"truncates":%d,"truncated_keys":%d,"writes_refused":%d,"dlq_dropped":%d,"errors_total":%s}`+"\n",
			atomic.LoadInt64(&writes), atomic.LoadInt64(&deletes), atomic.LoadInt64(&cycles),
			atomic.LoadInt64(&dlqSent), atomic.LoadInt64(&retried),
			atomic.LoadInt64(&metricBacklogPerTask), atomic.LoadInt64(&staleSkipped),
			atomic.LoadInt64(&metricDLQDepth), atomic.LoadInt64(&metricOldestAgeMs),
			atomic.LoadInt64(&writeResends), atomic.LoadInt64(&writesRecovered),
			atomic.LoadInt64(&writesUnconfirmed), atomic.LoadInt64(&pendingVanished),
			atomic.LoadInt64(&targetEvicted), atomic.LoadInt64(&metricTargetUp), atomic.LoadInt64(&holding), atomic.LoadInt64(&heldTotal),
			atomic.LoadInt64(&metricCaptureAgeMs), atomic.LoadInt64(&metricCapturePaused),
			atomic.LoadInt64(&metricCaptureStaleMs), atomic.LoadInt64(&waitShortfall),
			atomic.LoadInt64(&truncates), atomic.LoadInt64(&truncatedKeys),
			atomic.LoadInt64(&writesRefused), atomic.LoadInt64(&dlqDropped), errorTotalsJSON())
		writeBody(w, http.StatusOK, "application/json", body)
	})
	return mux
}

// startHealthServer serves healthMux on HEALTH_BIND:port.
func startHealthServer(port string) {
	mux := healthMux()
	// Loopback by default: the only caller that needs it is the ECS container
	// health check, which runs inside the task. It used to bind every
	// interface. Set HEALTH_BIND=0.0.0.0 only where something outside the
	// container must scrape it (e.g. a local harness publishing to 127.0.0.1).
	addr := envOr("HEALTH_BIND", "127.0.0.1") + ":" + port
	srv := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logErr("health-server", err)
		}
	}()
	fmt.Printf("  Health server on %s\n", addr)
}

// pkFromMsg derives the row identity from the Debezium record key (the table's
// primary key), supporting single and composite keys. Falls back to the row's
// "id" field if the key field is absent.
func pkFromMsg(msg redis.XMessage, row map[string]interface{}) string {
	if k, ok := msg.Values["key"].(string); ok && k != "" {
		var keyObj map[string]interface{}
		if err := decodeJSON([]byte(k), &keyObj); err == nil && len(keyObj) > 0 {
			if len(keyObj) == 1 {
				for _, v := range keyObj {
					return fmt.Sprintf("%v", v)
				}
			}
			// Composite key: join values in a deterministic (sorted) column order.
			names := make([]string, 0, len(keyObj))
			for name := range keyObj {
				names = append(names, name)
			}
			sort.Strings(names)
			parts := make([]string, 0, len(names))
			for _, name := range names {
				parts = append(parts, fmt.Sprintf("%v", keyObj[name]))
			}
			return strings.Join(parts, ":")
		}
	}
	// Fallback (single-PK convenience): use row's id if present.
	if v, ok := row["id"]; ok {
		return fmt.Sprintf("%v", v)
	}
	return ""
}
