package main

// Capture freshness (D5).
//
// Nothing measured whether Debezium was still capturing. ECS health, the sink's
// lag and the error-log filters all stayed green while Debezium was stuck,
// throttled, crash-looping or retrying, because the sink only sees what reaches
// the broker and an empty broker looks caught up.
//
// Debezium writes a heartbeat to data:__debezium-heartbeat.<topic prefix> every
// heartbeat.interval.ms (10 s) from its streaming loop, so the age of the newest
// entry, by the broker's own clock, is how long capture has made no progress.
//
// A planned pause (Debezium scaled to 0 for a bulk load) also stops the
// heartbeat. The sink reads the Debezium service's desired count from ECS: at 0,
// capture is paused on purpose and the alarm input is 0. After a resume the
// heartbeat is still old until Debezium has started, so the alarm input counts
// from the resume instead.
//
// Published every metric interval:
//
//	CaptureAgeSeconds    age of the newest heartbeat (absent until the first one)
//	CapturePaused        1 while the Debezium service's desired count is 0
//	CaptureStaleSeconds  the alarm input: 0 while paused, otherwise the heartbeat
//	                     age, or the time since the resume if that is shorter.
//	                     Not sent when it cannot be known, so the alarm treats
//	                     the gap as breaching.

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"

	"github.com/aws/aws-sdk-go-v2/aws"
	cwtypes "github.com/aws/aws-sdk-go-v2/service/cloudwatch/types"
	"github.com/aws/aws-sdk-go-v2/service/ecs"
	"github.com/redis/go-redis/v9"
)

// Exposed on /metrics; -1 when not known.
var (
	metricCaptureAgeMs   int64 = -1
	metricCapturePaused  int64 = -1
	metricCaptureStaleMs int64 = -1
)

// newestEntryAgeMs is the age of the newest entry in a stream by the broker's
// clock. exists is false for a missing or empty stream.
func newestEntryAgeMs(ctx context.Context, broker redis.UniversalClient, stream string) (age int64, exists bool, err error) {
	last, err := broker.XRevRangeN(ctx, stream, "+", "-", 1).Result()
	if err != nil {
		return 0, false, err
	}
	if len(last) == 0 {
		return 0, false, nil
	}
	now, err := broker.Time(ctx).Result()
	if err != nil {
		return 0, false, err
	}
	return entryAgeMs(last[0].ID, now.UnixMilli()), true, nil
}

// serviceDescriber is the one ECS call the sink makes.
type serviceDescriber interface {
	DescribeServices(ctx context.Context, in *ecs.DescribeServicesInput, opts ...func(*ecs.Options)) (*ecs.DescribeServicesOutput, error)
}

// desiredCount reads a service's desired task count.
func desiredCount(ctx context.Context, c serviceDescriber, cluster, service string) (int32, error) {
	out, err := c.DescribeServices(ctx, &ecs.DescribeServicesInput{
		Cluster: aws.String(cluster), Services: []string{service}})
	if err != nil {
		return 0, err
	}
	for _, s := range out.Services {
		if aws.ToString(s.ServiceName) == service {
			if st := aws.ToString(s.Status); st != "" && st != "ACTIVE" {
				return 0, fmt.Errorf("service %s is %s", service, st)
			}
			return s.DesiredCount, nil
		}
	}
	if len(out.Failures) > 0 {
		return 0, fmt.Errorf("service %s: %s", service, aws.ToString(out.Failures[0].Reason))
	}
	return 0, errors.New("service " + service + " not found")
}

// captureTracker remembers when capture was last resumed (desired count went
// from 0 to more), as seen by this sink.
type captureTracker struct {
	seenPaused  bool
	resumedAtMs int64 // 0: no resume seen
}

// observe records one reading of the desired count (known=false when it could
// not be read) and returns how long ago capture was resumed, or -1.
func (t *captureTracker) observe(known, paused bool, nowMs int64) int64 {
	if known {
		if paused {
			t.seenPaused = true
		} else if t.seenPaused {
			t.seenPaused = false
			t.resumedAtMs = nowMs
		}
	}
	if t.resumedAtMs == 0 {
		return -1
	}
	return nowMs - t.resumedAtMs
}

// captureStaleMs is the alarm input. ok is false when it cannot be known.
func captureStaleMs(ageMs int64, exists, paused bool, sinceResumeMs int64) (int64, bool) {
	if paused {
		return 0, true
	}
	if !exists {
		// No heartbeat yet. After a resume this sink saw, that is how long
		// Debezium has had to produce one; otherwise it is unknown.
		if sinceResumeMs >= 0 {
			return sinceResumeMs, true
		}
		return 0, false
	}
	if sinceResumeMs >= 0 && sinceResumeMs < ageMs {
		return sinceResumeMs, true
	}
	return ageMs, true
}

// captureMonitor collects one interval's capture readings.
type captureMonitor struct {
	stream, cluster, service string
	ecs                      serviceDescriber // nil: desired count not read
	tracker                  captureTracker
}

// newCaptureMonitor is nil when no heartbeat stream is configured.
func newCaptureMonitor(stream, cluster, service string, client serviceDescriber) *captureMonitor {
	if strings.TrimSpace(stream) == "" {
		return nil
	}
	m := &captureMonitor{stream: stream, cluster: cluster, service: service}
	if service != "" && cluster != "" && client != nil {
		m.ecs = client
	}
	return m
}

func (m *captureMonitor) datums(ctx context.Context, broker redis.UniversalClient, dims []cwtypes.Dimension) []cwtypes.MetricDatum {
	d := func(name string, v float64, unit cwtypes.StandardUnit) cwtypes.MetricDatum {
		return cwtypes.MetricDatum{MetricName: aws.String(name), Value: aws.Float64(v), Unit: unit, Dimensions: dims}
	}
	var out []cwtypes.MetricDatum
	known, paused := false, false
	if m.ecs != nil {
		n, err := desiredCount(ctx, m.ecs, m.cluster, m.service)
		if err != nil {
			logErr("capture-desired", fmt.Errorf("could not read %s's desired count, so a planned pause "+
				"cannot be told from a stall: %w", m.service, err))
		} else {
			known, paused = true, n == 0
			out = append(out, d("CapturePaused", b2f(paused), cwtypes.StandardUnitCount))
		}
	}
	pausedValue := int64(-1)
	if known {
		pausedValue = b2i(paused)
	}
	atomic.StoreInt64(&metricCapturePaused, pausedValue)

	age, exists, err := newestEntryAgeMs(ctx, broker, m.stream)
	if err != nil {
		logErr("capture-heartbeat", fmt.Errorf("could not read %s: %w", m.stream, err))
		atomic.StoreInt64(&metricCaptureAgeMs, -1)
		atomic.StoreInt64(&metricCaptureStaleMs, -1)
		return out // stale unknown: left out, so the alarm treats it as breaching
	}
	if exists {
		atomic.StoreInt64(&metricCaptureAgeMs, age)
		out = append(out, d("CaptureAgeSeconds", float64(age)/1000, cwtypes.StandardUnitSeconds))
	} else {
		atomic.StoreInt64(&metricCaptureAgeMs, -1)
	}
	nowMs := nowBrokerMs(ctx, broker)
	since := m.tracker.observe(known, paused, nowMs)
	stale, ok := captureStaleMs(age, exists, paused, since)
	if !ok {
		atomic.StoreInt64(&metricCaptureStaleMs, -1)
		return out
	}
	atomic.StoreInt64(&metricCaptureStaleMs, stale)
	return append(out, d("CaptureStaleSeconds", float64(stale)/1000, cwtypes.StandardUnitSeconds))
}

// nowBrokerMs is the broker's clock, so resume times and heartbeat ages use
// one clock. Falls back to 0 (no resume tracking) when it cannot be read.
var nowBrokerMs = func(ctx context.Context, broker redis.UniversalClient) int64 {
	t, err := broker.Time(ctx).Result()
	if err != nil {
		return 0
	}
	return t.UnixMilli()
}

func b2f(b bool) float64 {
	if b {
		return 1
	}
	return 0
}

func b2i(b bool) int64 {
	if b {
		return 1
	}
	return 0
}
