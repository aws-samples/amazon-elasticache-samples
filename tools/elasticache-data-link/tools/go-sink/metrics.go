package main

// Backlog metric for autoscaling.
//
// Why this exists: the sink's autoscaling was on CPU (ECSServiceAverageCPU at
// 60%), but the sink is I/O-bound -- it spends its time on broker and target
// round trips, not on CPU -- so CPU stays low while the queue grows and the
// scale-out fires minutes late. Observed: a 37k/s burst against one task built a
// ~500k backlog and the oldest entry waited ~2 minutes (the highest CDC lag reached
// 119s) before CPU crossed 60% and a second task started. Queue DEPTH is the
// signal that actually reflects "the sink is behind", so the sink publishes it
// and a target-tracking policy scales on it, reacting in ~60s instead of ~3 min.
//
// It publishes BacklogPerTask = totalBacklog / runningTasks so the target value
// is stable as tasks scale (the canonical queue-based scaling pattern). Running
// tasks are estimated from the consumer group itself -- each task's workers share
// a consumer-name prefix (the task's hostname) -- so no ECS API call or extra
// permission is needed, only cloudwatch:PutMetricData.

import (
	"context"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/cloudwatch"
	cwtypes "github.com/aws/aws-sdk-go-v2/service/cloudwatch/types"
	"github.com/aws/aws-sdk-go-v2/service/ecs"
	"github.com/redis/go-redis/v9"
)

// metricBacklogPerTask is the last published value, exposed on /metrics so the
// signal the autoscaler sees is visible without a CloudWatch round trip.
var metricBacklogPerTask int64

// Alarm inputs, also exposed on /metrics.
//
//	metricDLQDepth     entries in the dead-letter streams: changes NOT in the cache
//	metricOldestAgeMs  age of the oldest change still waiting in the broker, i.e.
//	                   how far behind the source the cache is on the sink side
var (
	metricDLQDepth    int64
	metricOldestAgeMs int64
)

// oldestEntryAgeMs is how long the first entry resident in a stream has been
// there, by the broker's own clock (so a skewed task clock cannot distort it).
// The sink XDELs every entry once applied, so the first resident entry IS the
// oldest unapplied change. Returns 0 for an empty stream, and an error when the
// broker could not be read: a failed read used to report 0, which told the lag
// alarm the sink was caught up exactly when it could not tell.
func oldestEntryAgeMs(ctx context.Context, broker redis.UniversalClient, stream string) (int64, error) {
	first, err := broker.XRangeN(ctx, stream, "-", "+", 1).Result()
	if err != nil {
		return 0, err
	}
	if len(first) == 0 {
		return 0, nil
	}
	now, err := broker.Time(ctx).Result()
	if err != nil {
		return 0, err
	}
	return entryAgeMs(first[0].ID, now.UnixMilli()), nil
}

// entryAgeMs is the age of a stream id "<ms>-<seq>" at nowMs; 0 if unparseable.
func entryAgeMs(id string, nowMs int64) int64 {
	ms, _, _ := strings.Cut(id, "-")
	t, err := strconv.ParseInt(ms, 10, 64)
	if err != nil || t > nowMs {
		return 0
	}
	return nowMs - t
}

// loadAWSConfig is awsconfig.LoadDefaultConfig, replaceable in tests.
var loadAWSConfig = awsconfig.LoadDefaultConfig

func metricsEnabled() bool {
	if v := os.Getenv("BACKLOG_METRIC_ENABLED"); v != "" {
		return v == "true" || v == "1"
	}
	// Default on only when there are dimensions to attach the metric to, so a
	// local run without a cluster/service does not try to talk to CloudWatch.
	return os.Getenv("CLUSTER_NAME") != "" && os.Getenv("SERVICE_NAME") != ""
}

// runningTasksFromConsumers estimates how many sink TASKS are live from the
// consumer group. Each task names its consumers "<hostname>-w<n>", so the number
// of distinct hostnames among recently-active consumers is the task count. Stale
// consumer names from stopped tasks linger in the group, so only consumers idle
// less than activeIdle are counted; never returns less than 1.
func runningTasksFromConsumers(consumers []redis.XInfoConsumer, activeIdle time.Duration) int {
	hosts := map[string]bool{}
	for _, c := range consumers {
		if c.Idle > activeIdle {
			continue
		}
		name := c.Name
		if i := strings.LastIndex(name, "-w"); i > 0 {
			name = name[:i]
		}
		hosts[name] = true
	}
	if len(hosts) == 0 {
		return 1
	}
	return len(hosts)
}

// streamBacklog returns undelivered + unacked for one stream's group. Undelivered
// is entries_added - entries_read rather than XINFO 'lag', which Valkey returns
// as NULL once entries have been XDEL'd (the sink XDELs after every ack), so lag
// is null in normal operation and would under-report the backlog to zero.
// An error means the backlog is unknown (a failed read used to count as 0).
func streamBacklog(ctx context.Context, broker redis.UniversalClient, stream, group string) (int64, error) {
	st, err := broker.XInfoStream(ctx, stream).Result()
	if err != nil {
		return 0, err
	}
	added := st.EntriesAdded
	var read, pending int64
	groups, err := broker.XInfoGroups(ctx, stream).Result()
	if err != nil {
		return 0, err
	}
	for _, g := range groups {
		if g.Name == group {
			read = g.EntriesRead
			pending = g.Pending
			break
		}
	}
	undelivered := added - read
	if undelivered < 0 {
		undelivered = 0
	}
	return undelivered + pending, nil
}

// backlogMetricsLoop publishes BacklogPerTask (and SinkBacklog / SinkRunningTasks
// for observability) to CloudWatch on a fixed interval, so a target-tracking
// scaling policy can react to queue depth. No-op when disabled.
func backlogMetricsLoop(ctx context.Context, broker redis.UniversalClient, specs []streamSpec, group string) {
	if !metricsEnabled() {
		fmt.Println("  Backlog metric: off (set CLUSTER_NAME + SERVICE_NAME, or BACKLOG_METRIC_ENABLED=true)")
		return
	}
	ns := envOr("METRIC_NAMESPACE", "ECDL/CDCSink")
	cluster := os.Getenv("CLUSTER_NAME")
	service := os.Getenv("SERVICE_NAME")
	// No hardcoded region: an unset region used to fall back to us-east-2, so a
	// sink anywhere else published its metrics to the wrong region and the
	// autoscaling policy and alarms saw nothing. ECS sets AWS_REGION.
	region := envOr("AWS_REGION", os.Getenv("METRIC_REGION"))
	interval := time.Duration(atoiOr("METRIC_INTERVAL_SEC", 20)) * time.Second
	activeIdle := time.Duration(atoiOr("CONSUMER_ACTIVE_IDLE_MS", 60000)) * time.Millisecond

	var opts []func(*awsconfig.LoadOptions) error
	if region != "" {
		opts = append(opts, awsconfig.WithRegion(region))
	}
	cfg, err := loadAWSConfig(ctx, opts...)
	if err != nil {
		// ERROR, not an info line (Jira story F093): with no metrics the lag,
		// dead-letter and scaling signals are missing, so this has to reach the
		// sink-errors alarm. sink-metrics-missing fires as well.
		logErr("metric-config", fmt.Errorf("metrics are NOT published (AWS config error): %w", err))
		return
	}
	cw := cloudwatch.NewFromConfig(cfg)
	// D5: capture freshness from Debezium's heartbeat (capture.go).
	capture := newCaptureMonitor(os.Getenv("CAPTURE_HEARTBEAT_STREAM"), cluster,
		os.Getenv("CAPTURE_SERVICE_NAME"), ecs.NewFromConfig(cfg))
	if capture != nil {
		fmt.Printf("  Capture freshness: heartbeat %s, planned pause from %s desired count\n",
			capture.stream, capture.service)
	}
	dims := []cwtypes.Dimension{
		{Name: aws.String("ClusterName"), Value: aws.String(cluster)},
		{Name: aws.String("ServiceName"), Value: aws.String(service)},
	}
	fmt.Printf("  Backlog metric: %s BacklogPerTask every %v (dims %s / %s, region %s)\n",
		ns, interval, cluster, service, region)

	// Counters are published as the change since the last publish, so a Sum
	// over any period is the count in that period. StaleSkipped used to be the
	// running total since the task started, which no alarm could use.
	var last deliveryCounts
	var lastErrs map[string]int64
	// Counters are published from here and once more at shutdown (M2), so the
	// two never send the same interval twice.
	var countersMu sync.Mutex
	publishCounters := func(ctx context.Context) error {
		countersMu.Lock()
		defer countersMu.Unlock()
		now, nowErrs := readDeliveryCounts(), errLog.errorTotals()
		if _, err := cw.PutMetricData(ctx, &cloudwatch.PutMetricDataInput{
			Namespace: aws.String(ns), MetricData: append(now.minus(last).datums(dims), errorDatums(dims, nowErrs, lastErrs)...),
		}); err != nil {
			return err // keep `last`, so the next publish carries this interval too
		}
		last, lastErrs = now, nowErrs
		return nil
	}
	setFinalMetrics(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := publishCounters(ctx); err != nil {
			logErr("metric-put", fmt.Errorf("final counters not published: %w", err))
			return
		}
		fmt.Println("  shutdown: final metrics published")
	})
	tick := time.NewTicker(interval)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		// A level that could not be read is left out (missing data), never
		// published as 0, which read as "caught up" and "no dead letters".
		var backlog, dlqDepth, oldestMs int64
		var readErr error
		for _, s := range specs {
			b, err := streamBacklog(ctx, broker, s.key, group)
			if err != nil {
				readErr = err
			}
			backlog += b
			age, err := oldestEntryAgeMs(ctx, broker, s.key)
			if err != nil {
				readErr = err
			}
			if age > oldestMs {
				oldestMs = age
			}
		}
		// Every dlq:* stream, including those of tables removed from the link
		// (Jira story F132): their changes are still missing from the cache.
		if n, err := dlqDepthOf(ctx, broker, specs); err != nil {
			readErr = err
		} else {
			dlqDepth = n
		}
		if readErr != nil {
			logErr("metric-read", fmt.Errorf("broker levels not published this interval: %w", readErr))
		}
		atomic.StoreInt64(&metricDLQDepth, dlqDepth)
		atomic.StoreInt64(&metricOldestAgeMs, oldestMs)
		tasks := 1
		if len(specs) > 0 {
			if cons, err := broker.XInfoConsumers(ctx, specs[0].key, group).Result(); err == nil {
				tasks = runningTasksFromConsumers(cons, activeIdle)
			}
		}
		perTask := float64(backlog) / float64(tasks)
		atomic.StoreInt64(&metricBacklogPerTask, int64(perTask))
		levels := levelDatums(dims, readErr == nil, perTask, backlog, tasks, dlqDepth, oldestMs,
			atomic.LoadInt64(&metricTargetUp))
		if capture != nil {
			levels = append(levels, capture.datums(ctx, broker, dims)...)
		}
		if freshness != nil {
			levels = append(levels, freshness.datums(ctx, broker, dims)...)
		}
		if _, err := cw.PutMetricData(ctx, &cloudwatch.PutMetricDataInput{
			Namespace: aws.String(ns), MetricData: levels,
		}); err != nil {
			logErr("metric-put", err)
			continue // keep `last`, so the next publish carries this interval too
		}
		if err := publishCounters(ctx); err != nil {
			logErr("metric-put", err)
		}
	}
}

// finalMetrics publishes the counters of the last interval at shutdown (review
// finding M2). A task stopping lost up to METRIC_INTERVAL_SEC of them (dead
// letters, vanished entries, evictions), so a scale-in right after a loss could
// hide it from the alarms.
var finalMetrics struct {
	sync.Mutex
	publish func()
}

func setFinalMetrics(f func()) {
	finalMetrics.Lock()
	finalMetrics.publish = f
	finalMetrics.Unlock()
}

// flushFinalMetrics is called by main once the workers have finished.
func flushFinalMetrics() {
	finalMetrics.Lock()
	f := finalMetrics.publish
	finalMetrics.Unlock()
	if f != nil {
		f()
	}
}

// deliveryCounts are the sink's cumulative delivery counters.
type deliveryCounts struct {
	writes, deletes, staleSkipped, resends, recovered, unconfirmed, retried, dlq, vanished, evicted int64
	waitShort, refused, dlqDropped                                                                  int64
}

func readDeliveryCounts() deliveryCounts {
	return deliveryCounts{
		writes: atomic.LoadInt64(&writes), deletes: atomic.LoadInt64(&deletes),
		staleSkipped: atomic.LoadInt64(&staleSkipped), resends: atomic.LoadInt64(&writeResends),
		recovered: atomic.LoadInt64(&writesRecovered), unconfirmed: atomic.LoadInt64(&writesUnconfirmed),
		retried: atomic.LoadInt64(&retried), dlq: atomic.LoadInt64(&dlqSent),
		vanished: atomic.LoadInt64(&pendingVanished), evicted: atomic.LoadInt64(&targetEvicted),
		waitShort: atomic.LoadInt64(&waitShortfall),
		refused:   atomic.LoadInt64(&writesRefused), dlqDropped: atomic.LoadInt64(&dlqDropped),
	}
}

func (c deliveryCounts) minus(o deliveryCounts) deliveryCounts {
	return deliveryCounts{c.writes - o.writes, c.deletes - o.deletes, c.staleSkipped - o.staleSkipped,
		c.resends - o.resends, c.recovered - o.recovered, c.unconfirmed - o.unconfirmed,
		c.retried - o.retried, c.dlq - o.dlq, c.vanished - o.vanished, c.evicted - o.evicted,
		c.waitShort - o.waitShort, c.refused - o.refused, c.dlqDropped - o.dlqDropped}
}

// datums names each counter for CloudWatch:
//
//	Writes, Deletes        changes applied to the target
//	StaleSkipped           changes the version guard refused as older than the cache
//	WriteResends           pipelines re-sent at once after a failed write
//	WritesRecovered        changes confirmed by such a re-send
//	WritesUnconfirmed      changes left pending for reclaim after every re-send
//	ReclaimRetried         pending changes reclaim later applied
//	DeadLettered           changes moved to the DLQ
//	PendingVanished        pending changes deleted from the stream before they
//	                       were applied (lost; must stay 0)
//	TargetEvictedKeys      keys the target evicted (its eviction policy deleted
//	                       them; must stay 0)
//	WaitShortfall          changes applied on a target primary but left pending
//	                       because its replicas did not confirm them in time
//	WritesRefused          unconfirmed changes the target refused for
//	                       themselves (WRONGTYPE, ...), not for being down
//	DLQDropped             dead letters dropped at DLQ_MAX_LEN (must stay 0)
func (c deliveryCounts) datums(dims []cwtypes.Dimension) []cwtypes.MetricDatum {
	pairs := []struct {
		name string
		v    int64
	}{
		{"Writes", c.writes}, {"Deletes", c.deletes}, {"StaleSkipped", c.staleSkipped},
		{"WriteResends", c.resends}, {"WritesRecovered", c.recovered},
		{"WritesUnconfirmed", c.unconfirmed}, {"ReclaimRetried", c.retried},
		{"DeadLettered", c.dlq}, {"PendingVanished", c.vanished},
		{"TargetEvictedKeys", c.evicted}, {"WaitShortfall", c.waitShort},
		{"WritesRefused", c.refused}, {"DLQDropped", c.dlqDropped},
	}
	out := make([]cwtypes.MetricDatum, 0, len(pairs))
	for _, p := range pairs {
		out = append(out, cwtypes.MetricDatum{MetricName: aws.String(p.name),
			Value: aws.Float64(float64(p.v)), Unit: cwtypes.StandardUnitCount, Dimensions: dims})
	}
	return out
}

// levelDatums are the gauges for one interval. TargetUp is always sent. The
// broker levels are sent only when every read succeeded: a missing datum lets
// the lag alarm treat the gap as breaching, where a 0 read as caught up.
func levelDatums(dims []cwtypes.Dimension, brokerOK bool, perTask float64, backlog int64, tasks int,
	dlqDepth, oldestMs, targetUp int64) []cwtypes.MetricDatum {
	d := func(name string, v float64, unit cwtypes.StandardUnit) cwtypes.MetricDatum {
		return cwtypes.MetricDatum{MetricName: aws.String(name), Value: aws.Float64(v), Unit: unit, Dimensions: dims}
	}
	out := []cwtypes.MetricDatum{
		d("TargetUp", float64(targetUp), cwtypes.StandardUnitCount),
		d("SinkRunningTasks", float64(tasks), cwtypes.StandardUnitCount),
	}
	if !brokerOK {
		return out
	}
	return append(out,
		d("BacklogPerTask", perTask, cwtypes.StandardUnitCount),
		d("SinkBacklog", float64(backlog), cwtypes.StandardUnitCount),
		d("DLQDepth", float64(dlqDepth), cwtypes.StandardUnitCount),
		d("OldestEntryAgeSeconds", float64(oldestMs)/1000, cwtypes.StandardUnitSeconds))
}

// errorDatums are Errors{Kind}: the ERROR lines of each kind since the last
// publish (Jira story F096), so an alarm or a dashboard can tell a broker read failure
// from a dead-letter write. A kind with no new errors sends nothing. There are
// a few dozen kinds, well under the PutMetricData limit of 1,000 datums.
func errorDatums(dims []cwtypes.Dimension, now, last map[string]int64) []cwtypes.MetricDatum {
	kinds := make([]string, 0, len(now))
	for k, v := range now {
		if v > last[k] {
			kinds = append(kinds, k)
		}
	}
	sort.Strings(kinds)
	out := make([]cwtypes.MetricDatum, 0, len(kinds))
	for _, k := range kinds {
		d := append(append([]cwtypes.Dimension(nil), dims...),
			cwtypes.Dimension{Name: aws.String("Kind"), Value: aws.String(k)})
		out = append(out, cwtypes.MetricDatum{MetricName: aws.String("Errors"),
			Value: aws.Float64(float64(now[k] - last[k])), Unit: cwtypes.StandardUnitCount, Dimensions: d})
	}
	return out
}

// dlqDepthOf is the number of dead letters in every dead-letter stream on the
// broker (findDLQs).
func dlqDepthOf(ctx context.Context, broker redis.UniversalClient, specs []streamSpec) (int64, error) {
	dlqs, err := findDLQs(ctx, broker, specs)
	if err != nil {
		return 0, err
	}
	var total int64
	for _, d := range dlqs {
		n, err := broker.XLen(ctx, d.dlq).Result()
		if err != nil {
			return 0, err
		}
		total += n
	}
	return total, nil
}
