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
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/cloudwatch"
	cwtypes "github.com/aws/aws-sdk-go-v2/service/cloudwatch/types"
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
// oldest unapplied change. Returns 0 for an empty stream.
func oldestEntryAgeMs(ctx context.Context, broker redis.UniversalClient, stream string) int64 {
	first, err := broker.XRangeN(ctx, stream, "-", "+", 1).Result()
	if err != nil || len(first) == 0 {
		return 0
	}
	now, err := broker.Time(ctx).Result()
	if err != nil {
		return 0
	}
	return entryAgeMs(first[0].ID, now.UnixMilli())
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
func streamBacklog(ctx context.Context, broker redis.UniversalClient, stream, group string) int64 {
	st, err := broker.XInfoStream(ctx, stream).Result()
	if err != nil {
		return 0
	}
	added := st.EntriesAdded
	var read, pending int64
	if groups, err := broker.XInfoGroups(ctx, stream).Result(); err == nil {
		for _, g := range groups {
			if g.Name == group {
				read = g.EntriesRead
				pending = g.Pending
				break
			}
		}
	}
	undelivered := added - read
	if undelivered < 0 {
		undelivered = 0
	}
	return undelivered + pending
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
	cfg, err := awsconfig.LoadDefaultConfig(ctx, opts...)
	if err != nil {
		fmt.Printf("  Backlog metric: disabled (AWS config error: %v)\n", err)
		return
	}
	cw := cloudwatch.NewFromConfig(cfg)
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
	tick := time.NewTicker(interval)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		var backlog, dlqDepth, oldestMs int64
		for _, s := range specs {
			backlog += streamBacklog(ctx, broker, s.key, group)
			if n, err := broker.XLen(ctx, dlqPrefix+s.key).Result(); err == nil {
				dlqDepth += n
			}
			if age := oldestEntryAgeMs(ctx, broker, s.key); age > oldestMs {
				oldestMs = age
			}
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
		now := readDeliveryCounts()
		delta := now.minus(last)
		atomic.StoreInt64(&metricBacklogPerTask, int64(perTask))
		if _, err := cw.PutMetricData(ctx, &cloudwatch.PutMetricDataInput{
			Namespace: aws.String(ns),
			MetricData: []cwtypes.MetricDatum{
				{MetricName: aws.String("BacklogPerTask"), Value: aws.Float64(perTask),
					Unit: cwtypes.StandardUnitCount, Dimensions: dims},
				{MetricName: aws.String("SinkBacklog"), Value: aws.Float64(float64(backlog)),
					Unit: cwtypes.StandardUnitCount, Dimensions: dims},
				{MetricName: aws.String("SinkRunningTasks"), Value: aws.Float64(float64(tasks)),
					Unit: cwtypes.StandardUnitCount, Dimensions: dims},
				{MetricName: aws.String("DLQDepth"), Value: aws.Float64(float64(dlqDepth)),
					Unit: cwtypes.StandardUnitCount, Dimensions: dims},
				{MetricName: aws.String("OldestEntryAgeSeconds"),
					Value: aws.Float64(float64(oldestMs) / 1000), Unit: cwtypes.StandardUnitSeconds,
					Dimensions: dims},
			},
		}); err != nil {
			logErr("metric-put", err)
			continue // keep `last`, so the next publish carries this interval too
		}
		if _, err := cw.PutMetricData(ctx, &cloudwatch.PutMetricDataInput{
			Namespace: aws.String(ns), MetricData: delta.datums(dims),
		}); err != nil {
			logErr("metric-put", err)
			continue
		}
		last = now
	}
}

// deliveryCounts are the sink's cumulative delivery counters.
type deliveryCounts struct {
	writes, deletes, staleSkipped, resends, recovered, unconfirmed, retried, dlq int64
}

func readDeliveryCounts() deliveryCounts {
	return deliveryCounts{
		writes: atomic.LoadInt64(&writes), deletes: atomic.LoadInt64(&deletes),
		staleSkipped: atomic.LoadInt64(&staleSkipped), resends: atomic.LoadInt64(&writeResends),
		recovered: atomic.LoadInt64(&writesRecovered), unconfirmed: atomic.LoadInt64(&writesUnconfirmed),
		retried: atomic.LoadInt64(&retried), dlq: atomic.LoadInt64(&dlqSent),
	}
}

func (c deliveryCounts) minus(o deliveryCounts) deliveryCounts {
	return deliveryCounts{c.writes - o.writes, c.deletes - o.deletes, c.staleSkipped - o.staleSkipped,
		c.resends - o.resends, c.recovered - o.recovered, c.unconfirmed - o.unconfirmed,
		c.retried - o.retried, c.dlq - o.dlq}
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
func (c deliveryCounts) datums(dims []cwtypes.Dimension) []cwtypes.MetricDatum {
	pairs := []struct {
		name string
		v    int64
	}{
		{"Writes", c.writes}, {"Deletes", c.deletes}, {"StaleSkipped", c.staleSkipped},
		{"WriteResends", c.resends}, {"WritesRecovered", c.recovered},
		{"WritesUnconfirmed", c.unconfirmed}, {"ReclaimRetried", c.retried},
		{"DeadLettered", c.dlq},
	}
	out := make([]cwtypes.MetricDatum, 0, len(pairs))
	for _, p := range pairs {
		out = append(out, cwtypes.MetricDatum{MetricName: aws.String(p.name),
			Value: aws.Float64(float64(p.v)), Unit: cwtypes.StandardUnitCount, Dimensions: dims})
	}
	return out
}
