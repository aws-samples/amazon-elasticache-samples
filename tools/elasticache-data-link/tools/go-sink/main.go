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
	"strconv"
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
func loadSpecFromEnv() *TransformSpec {
	if inline := os.Getenv("TRANSFORMS_JSON"); inline != "" {
		s, err := LoadTransformSpec([]byte(inline))
		if err != nil {
			fmt.Printf("  transform spec (env) parse error: %v — passthrough\n", err)
			return nil
		}
		return s
	}
	if path := os.Getenv("TRANSFORMS_FILE"); path != "" {
		b, err := os.ReadFile(path)
		if err != nil {
			fmt.Printf("  transform spec file %s not read: %v — passthrough\n", path, err)
			return nil
		}
		s, err := LoadTransformSpec(b)
		if err != nil {
			fmt.Printf("  transform spec file parse error: %v — passthrough\n", err)
			return nil
		}
		return s
	}
	return nil
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
	brokerAddr := envOr("BROKER_HOST", "localhost") + ":" + envOr("BROKER_PORT", "6380")
	brokerSSL := envOr("BROKER_SSL", "false") == "true"
	targetAddr := envOr("TARGET_HOST", "localhost") + ":" + envOr("TARGET_PORT", "6379")
	targetSSL := envOr("TARGET_SSL", "true") == "true"
	targetCluster := envOr("TARGET_CLUSTER", "false") == "true"
	// Kept equal to infra/ecdl-cdc-ecs.yaml SinkBatchSize on purpose: the
	// template said 500 while this said 5000, so a deployed sink behaved
	// differently from every local run and from the published benchmarks.
	batchSize, _ := strconv.Atoi(envOr("BATCH_SIZE", "2000"))
	workers, _ := strconv.Atoi(envOr("WORKERS", "16"))
	group := envOr("CONSUMER_GROUP", "go-timed")
	eventFormat := envOr("EVENT_FORMAT", "envelope") // envelope | flat

	// One link, N tables, one sink. STREAMS carries every table's stream; see
	// streams.go for the accepted forms and why each stream's table is tracked.
	// There is deliberately no fallback default stream name: consuming a stream
	// that was not configured is worse than refusing to start.
	specs, err := parseStreams(os.Getenv)
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	}
	streamKeys := streamKeysOf(specs)

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
	transformSpec = loadSpecFromEnv()
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
	targetEndpoint := endpoint{
		label: "target", addr: targetAddr, tls: targetSSL,
		cluster: targetCluster, poolSize: workers + 5, cred: targetCred,
	}

	broker, err := dial(ctx, brokerEndpoint)
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("  Connected to %s\n", brokerEndpoint.describe())

	target, err := dial(ctx, targetEndpoint)
	if err != nil {
		fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("  Connected to %s\n", targetEndpoint.describe())

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
					if ok, _ := healthy(); !ok {
						continue
					}
					now := strconv.FormatInt(time.Now().UnixMilli(), 10)
					for _, k := range hbKeys {
						if err := target.Set(ctx, k, now, 0).Err(); err != nil {
							logErr("heartbeat", err)
						}
					}
				}
			}
		}()
		fmt.Printf("  CDC heartbeat -> %s (every 2s)\n", strings.Join(hbKeys, ", "))
	}

	for _, sk := range streamKeys {
		// BUSYGROUP is the normal "already exists". Anything else means every
		// read will fail with NOGROUP, which used to be discarded here and left a
		// sink that silently consumed nothing -- so it is fatal and ECS restarts.
		if err := broker.XGroupCreateMkStream(ctx, sk, group, "0").Err(); err != nil &&
			!strings.HasPrefix(err.Error(), "BUSYGROUP") {
			fmt.Fprintf(os.Stderr, "FATAL: cannot create consumer group %s on %s: %v\n", group, sk, err)
			os.Exit(1)
		}
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

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	start := time.Now()

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
	hasDenorm := specHasDenorm(transformSpec)
	if hasDenorm {
		fmt.Printf("  Denormalization: ON\n")
	}
	// Every worker consumes every stream, so N tables need no extra tasks and no
	// extra consumer groups: the group already load-balances across workers and
	// across sink tasks.
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		name := fmt.Sprintf("%s-w%d", consumerPrefix, i)
		wg.Add(1)
		if hasDenorm {
			go func() {
				defer wg.Done()
				workerDenorm(ctx, broker, target, specs, group, name, int64(batchSize))
			}()
		} else {
			go func() {
				defer wg.Done()
				worker(ctx, broker, target, specs, group, name, int64(batchSize), eventFormat)
			}()
		}
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
	apply := plainApply(eventFormat)
	if hasDenorm {
		apply = denormApply()
	}
	go reclaimLoop(ctx, broker, target, specs, group, consumerPrefix, apply)
	// Keep streams that nothing consumes short (Debezium's heartbeat). They are
	// written on purpose -- an idle source commits its offset only through them
	// -- but left alone they would grow by one entry per heartbeat for ever.
	go trimLoop(ctx, broker, splitList(os.Getenv("TRIM_STREAMS")), trimMaxLen, time.Minute)

	// Publish queue depth (BacklogPerTask) to CloudWatch so autoscaling can react
	// to how far behind the sink is, not to CPU (which stays low on this
	// I/O-bound work and scaled out minutes too late). No-op unless configured.
	go backlogMetricsLoop(ctx, broker, specs, group)
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
			applyBatch(context.WithoutCancel(ctx), broker, target, st.Stream, group,
				st.Messages, eventFormat, streamTable)
		}
	}
}

// applyBatch is one stream's slice of a read cycle: plan, write, then ack only
// what the target confirmed. Split out of worker() so the multi-stream loop does
// not nest four levels deep; the logic is unchanged from the single-stream version.
func applyBatch(ctx context.Context, broker, target redis.UniversalClient, stream, group string,
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
				fields: fields, replace: replace, del: isDel})
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
		t5 := time.Now()
		writeDur := t5.Sub(t4)
		left := res.unconfirmed()
		switch {
		case left > 0:
			markWriteErr()
			atomic.AddInt64(&writesUnconfirmed, int64(left))
			logErr("target-write", fmt.Errorf("%s: %d of %d change(s) not confirmed after %d attempt(s); "+
				"left pending for reclaim (retried after %v, dead-lettered after %d deliveries): %w",
				stream, left, len(ops), res.sends, retryIdle, maxDeliveries, res.lastErr))
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
		// once the target accepted the write. (Durability AFTER the target
		// accepts is the target's job.)
		t6 := time.Now()
		ackIDs := make([]string, 0, batchLen)
		okW := 0
		okD := 0
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
			}
		}
		// Poison messages are structurally invalid and never entered the target
		// pipeline, so their handling is independent of execErr: dead-letter them
		// FIRST and only ack the original once the DLQ write is durable. If the
		// DLQ XADD fails, the entry is left pending so reclaimLoop dead-letters it
		// later -- acking it here would drop the change from both the stream and
		// the DLQ (a silent loss, which OLTP cannot tolerate).
		dlqOK := 0
		for _, m := range poison {
			if err := toDLQ(ctx, broker, m, stream, "poison", 0); err == nil {
				ackIDs = append(ackIDs, m.ID)
				dlqOK++
			} else {
				logErr("dlq-write", fmt.Errorf("%s %s left pending: %w", stream, m.ID, err))
			}
		}
		if dlqOK > 0 {
			atomic.AddInt64(&dlqSent, int64(dlqOK))
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
		body := fmt.Sprintf(`{"writes":%d,"deletes":%d,"cycles":%d,"dlq":%d,"retried":%d,"backlog_per_task":%d,"stale_skipped":%d,"dlq_depth":%d,"oldest_entry_age_ms":%d,"write_resends":%d,"writes_recovered":%d,"writes_unconfirmed":%d}`+"\n",
			atomic.LoadInt64(&writes), atomic.LoadInt64(&deletes), atomic.LoadInt64(&cycles),
			atomic.LoadInt64(&dlqSent), atomic.LoadInt64(&retried),
			atomic.LoadInt64(&metricBacklogPerTask), atomic.LoadInt64(&staleSkipped),
			atomic.LoadInt64(&metricDLQDepth), atomic.LoadInt64(&metricOldestAgeMs),
			atomic.LoadInt64(&writeResends), atomic.LoadInt64(&writesRecovered),
			atomic.LoadInt64(&writesUnconfirmed))
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

// ─────────────────────────────────────────────────────────────────────────
// Denormalization worker (FAN-OUT: parent -> children).
// Consumes parent + child streams; embeds parent fields onto children via a
// cache lookup (Phase 1); maintains a reverse index and fans parent updates
// out to children, diff-gated + batched (Phase 2). Correctness-first: each
// message is processed individually rather than via the batched fast path.
// (This does NOT do fan-in / child->parent aggregation — that's a separate
// feature.)
// ─────────────────────────────────────────────────────────────────────────

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

func idxKey(childTable, parentTable, parentPK string) string {
	return "idx:" + childTable + ":by:" + parentTable + ":" + parentPK
}

func toIface(m map[string]string) map[string]interface{} {
	out := make(map[string]interface{}, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

// embeddedChanged reports whether any embedded (parent) field differs between
// the before- and after-images — used to skip needless fan-out.
func embeddedChanged(rule DenormRule, before, after map[string]string) bool {
	for _, f := range rule.Fields {
		if before[f] != after[f] {
			return true
		}
	}
	return false
}

func workerDenorm(ctx context.Context, broker, target redis.UniversalClient, specs []streamSpec, group, name string, batch int64) {
	sarg := readArgs(specs)
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		results, err := broker.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group: group, Consumer: name, Streams: sarg, Count: batch, Block: 500 * time.Millisecond,
		}).Result()
		if err != nil && !errors.Is(err, redis.Nil) {
			if ctx.Err() != nil {
				return
			}
			logErr("broker-read", err)
			time.Sleep(200 * time.Millisecond)
			continue
		}
		markReadOK()
		if len(results) == 0 {
			continue
		}
		// Finish what was read even if shutdown starts mid-batch.
		ctx := context.WithoutCancel(ctx)
		atomic.AddInt64(&cycles, 1)
		for _, st := range results {
			// ACK ONLY WHAT SUCCEEDED. This loop used to call processMsg and then
			// ack every message unconditionally, discarding the result -- so on
			// this path a failed write was acked, XDEL'd from the broker, and
			// gone, with no retry and nothing in the DLQ. The plain path already
			// enforced confirmed-apply-before-trim; the denormalization path did
			// not, which made denormalization the one configuration that could
			// silently lose a change.
			ackIDs := make([]string, 0, len(st.Messages))
			for _, msg := range st.Messages {
				switch err := processMsg(ctx, target, msg); {
				case err == nil:
					markWriteOK()
					ackIDs = append(ackIDs, msg.ID)
				case errors.Is(err, errPoison):
					// Never going to succeed: dead-letter it first, with the same
					// ack-after-DLQ rule as the poison handling in applyBatch.
					if e := toDLQ(ctx, broker, msg, st.Stream, "poison", 0); e == nil {
						atomic.AddInt64(&dlqSent, 1)
						ackIDs = append(ackIDs, msg.ID)
					} else {
						logErr("dlq-write", e)
					}
				default:
					// Transient: leave it unacked in the PEL so reclaimLoop
					// retries it and eventually dead-letters it.
					markWriteErr()
					logErr("target-write", fmt.Errorf("%s %s left pending for retry: %w",
						st.Stream, msg.ID, err))
				}
			}
			atomic.AddInt64(&batchSizes, int64(len(st.Messages)))
			// Atomic ack+trim so a failed XDEL can never leave an acked entry
			// resident (the orphan leak); on failure it stays pending for reclaim.
			if len(ackIDs) > 0 {
				if err := ackAndTrim(ctx, broker, st.Stream, group, ackIDs...); err != nil {
					logErr("broker-ack", err)
				}
			}
		}
	}
}

// processMsg applies one message on the denormalization path.
//
// It RETURNS AN ERROR so that workerDenorm acks only what succeeded (see the
// comment there). errPoison means the message can
// never succeed as written (bad JSON, no primary key) and should be
// dead-lettered; any other error means try again.
func processMsg(ctx context.Context, target redis.UniversalClient, msg redis.XMessage) error {
	val, ok := msg.Values["value"].(string)
	if !ok {
		return errPoison
	}
	var evt CDCEvent
	if err := decodeJSON([]byte(val), &evt); err != nil {
		return errPoison
	}
	table := evt.Source.Table
	spec := transformSpec

	// ── DELETE ──────────────────────────────────────────────────────────
	if evt.Op == "d" {
		pk := pkFromMsg(msg, evt.Before)
		if pk == "" {
			return errPoison
		}
		if err := target.Del(ctx, table+":"+pk).Err(); err != nil {
			return err
		}
		atomic.AddInt64(&deletes, 1)
		if evt.Before != nil { // as CHILD: drop from parent index
			before := stringifyRow(evt.Before)
			for _, r := range spec.DenormFor(table) {
				if ppk := before[r.JoinField]; ppk != "" {
					if err := target.SRem(ctx, idxKey(table, r.ParentTable, ppk), pk).Err(); err != nil {
						return err
					}
				}
			}
		}
		for _, cd := range spec.ChildrenOf(table) { // as PARENT: apply delete policy
			if err := applyParentDelete(ctx, target, cd, pk); err != nil {
				return err
			}
		}
		return nil
	}

	if evt.After == nil {
		return errPoison
	}
	pk := pkFromMsg(msg, evt.After)
	after := stringifyRow(evt.After)
	key := rowKey(table, pk)

	if pk == "" {
		return errPoison
	}

	out, keep := spec.Apply(table, after)
	if !keep {
		if err := target.Del(ctx, key).Err(); err != nil {
			return err
		}
		atomic.AddInt64(&deletes, 1)
		if evt.Before != nil {
			before := stringifyRow(evt.Before)
			for _, r := range spec.DenormFor(table) {
				if ppk := before[r.JoinField]; ppk != "" {
					if err := target.SRem(ctx, idxKey(table, r.ParentTable, ppk), pk).Err(); err != nil {
						return err
					}
				}
			}
		}
		return nil
	}

	// Phase 1: embed parent fields (this table as CHILD) via cache lookup.
	for _, r := range spec.DenormFor(table) {
		ppk := after[r.JoinField]
		if ppk == "" {
			continue
		}
		vals, err := target.HMGet(ctx, r.ParentKeyPrefix+ppk, r.Fields...).Result()
		parent := make(map[string]string, len(r.Fields))
		if err == nil {
			for i, f := range r.Fields {
				if i < len(vals) && vals[i] != nil {
					parent[f] = fmt.Sprintf("%v", vals[i])
				}
			}
		}
		for k, v := range EmbedParentFields(r, parent) {
			out[k] = v
		}
	}

	// A snapshot read is the authoritative full row, so replace the key rather
	// than merge -- this drops a removed/renamed field left over from an old
	// shape. `out` already carries the embedded parent fields (Phase 1 above),
	// so DEL then HSET preserves the denormalized values.
	if evt.Op == "r" {
		if err := target.Del(ctx, key).Err(); err != nil {
			return err
		}
	}
	if err := target.HSet(ctx, key, toIface(out)).Err(); err != nil {
		return err
	}
	atomic.AddInt64(&writes, 1)

	// Phase 2: reverse-index maintenance (this table as CHILD).
	for _, r := range spec.DenormFor(table) {
		ppk := after[r.JoinField]
		if ppk != "" {
			if err := target.SAdd(ctx, idxKey(table, r.ParentTable, ppk), pk).Err(); err != nil {
				return err
			}
		}
		if evt.Before != nil {
			old := stringifyRow(evt.Before)[r.JoinField]
			if old != "" && old != ppk {
				if err := target.SRem(ctx, idxKey(table, r.ParentTable, old), pk).Err(); err != nil {
					return err
				}
			}
		}
	}

	// Phase 2: parent-update fan-out (this table as PARENT).
	children := spec.ChildrenOf(table)
	if len(children) == 0 {
		return nil
	}
	var before map[string]string
	if evt.Before != nil {
		before = stringifyRow(evt.Before)
	}
	for _, cd := range children {
		if before != nil && !embeddedChanged(cd.Rule, before, after) {
			continue // diff-gate: embedded fields unchanged
		}
		embed := toIface(EmbedParentFields(cd.Rule, after))
		members, err := target.SMembers(ctx, idxKey(cd.ChildTable, table, pk)).Result()
		if err != nil {
			// The index read failed, so we cannot know which children to update.
			// Returning the error keeps the entry pending for a retry; continuing
			// would leave those children holding stale embedded parent fields with
			// nothing to correct them.
			return err
		}
		if len(members) == 0 {
			continue
		}
		pipe := target.Pipeline()
		for _, cpk := range members {
			pipe.HSet(ctx, cd.ChildTable+":"+cpk, embed)
		}
		if _, err := pipe.Exec(ctx); err != nil {
			return err
		}
		atomic.AddInt64(&writes, int64(len(members)))
	}
	return nil
}

// applyParentDelete handles a parent row deletion for one child relationship:
// "null" blanks the embedded fields on every child; "leave" does nothing.
func applyParentDelete(ctx context.Context, target redis.UniversalClient, cd ChildDenorm, parentPK string) error {
	if cd.Rule.OnParentDelete == "leave" {
		return nil
	}
	members, err := target.SMembers(ctx, idxKey(cd.ChildTable, cd.Rule.ParentTable, parentPK)).Result()
	if err != nil {
		return err
	}
	if len(members) == 0 {
		return nil
	}
	blank := make(map[string]interface{}, len(cd.Rule.Fields))
	for _, f := range cd.Rule.Fields {
		blank[cd.Rule.Prefix+f] = ""
	}
	pipe := target.Pipeline()
	for _, cpk := range members {
		pipe.HSet(ctx, cd.ChildTable+":"+cpk, blank)
	}
	_, err = pipe.Exec(ctx)
	return err
}
