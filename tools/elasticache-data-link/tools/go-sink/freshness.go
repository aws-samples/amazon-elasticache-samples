package main

// Per-table freshness (Jira stories F089, F101, F102).
//
// Every alarm was per pipeline, so one table could stop getting changes (a
// renamed table, an include-list or topic mismatch) while the others kept the
// pipeline green. Everything here is read from the broker, so it is the same
// for every sink task and survives a restart:
//
//   - A table stream's last-generated-id is the broker time of the last change
//     Debezium captured for that table. It stays after the sink deletes the
//     applied entries, so LastChangeAgeSeconds{Table} = now - that time.
//   - The oldest entry still in a table's stream is its oldest change not yet
//     applied. With none waiting, the table is applied up to Debezium's newest
//     heartbeat. That is the table's "fresh as of" time: the cache holds every
//     change to it that was captured before then.
//
// Published every metric interval:
//
//	LastChangeAgeSeconds{Table}  time since the last captured change (absent
//	                             until the table's first change)
//	StaleTables                  listed tables (TABLE_STALE_SECONDS) with no
//	                             change for longer than their threshold
//	CacheFreshnessSeconds        now - the oldest "fresh as of" of all tables
//
// A quiet table has no changes, which is normal, so only the tables the link
// lists with a threshold count as stale. Debezium's own delay in reading the
// source log is not included: changes are timed when they reach the broker.

import (
	"context"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	cwtypes "github.com/aws/aws-sdk-go-v2/service/cloudwatch/types"
	"github.com/redis/go-redis/v9"
)

// tableReading is one table's state on the broker.
type tableReading struct {
	table        string
	lastChangeMs int64 // 0: no change ever captured
	oldestMs     int64 // 0: nothing waiting to be applied
	replayMs     int64 // the larger capture delay of the oldest and newest waiting change (catchup.go)
}

// streamIDMs is the millisecond part of a stream id; 0 for "0-0" or garbage.
func streamIDMs(id string) int64 {
	ms, _, _ := strings.Cut(id, "-")
	n, err := strconv.ParseInt(ms, 10, 64)
	if err != nil || n < 0 {
		return 0
	}
	return n
}

func readTable(ctx context.Context, broker redis.UniversalClient, s streamSpec) (tableReading, error) {
	r := tableReading{table: s.table}
	info, err := broker.XInfoStream(ctx, s.key).Result()
	if err != nil {
		if strings.Contains(strings.ToLower(err.Error()), "no such key") {
			return r, nil // Debezium has never written a change for it
		}
		return r, err
	}
	r.lastChangeMs = streamIDMs(info.LastGeneratedID)
	if info.Length > 0 {
		r.oldestMs = streamIDMs(info.FirstEntry.ID)
		r.replayMs = max(replayAgeMs(&info.FirstEntry), replayAgeMs(&info.LastEntry))
	}
	return r, nil
}

// freshAsOfMs: the cache has every change to the table captured before this
// time. 0 when it cannot be known (nothing waiting and no heartbeat).
func freshAsOfMs(r tableReading, heartbeatMs int64) int64 {
	if r.oldestMs > 0 {
		return r.oldestMs - 1
	}
	return heartbeatMs
}

// parseStaleThresholds reads "orders=3600,customers=600" (seconds).
func parseStaleThresholds(raw string, specs []streamSpec) (map[string]int64, error) {
	out := map[string]int64{}
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return out, nil
	}
	known := map[string]string{}
	for _, s := range specs {
		known[strings.ToLower(s.table)] = s.table
	}
	for _, part := range strings.Split(raw, ",") {
		name, value, ok := strings.Cut(strings.TrimSpace(part), "=")
		name = strings.TrimSpace(name)
		if !ok || name == "" {
			return nil, fmt.Errorf("TABLE_STALE_SECONDS entry %q is not table=seconds", part)
		}
		secs, err := strconv.ParseInt(strings.TrimSpace(value), 10, 64)
		if err != nil || secs < 60 {
			return nil, fmt.Errorf("TABLE_STALE_SECONDS %s: %q is not a number of seconds >= 60", name, value)
		}
		table, found := known[strings.ToLower(name)]
		if !found {
			return nil, fmt.Errorf("TABLE_STALE_SECONDS names %s, which is not one of this link's tables", name)
		}
		out[table] = secs
	}
	return out, nil
}

// staleTables lists the listed tables with no change for longer than their
// threshold. A table that has never had a change counts from startMs (when
// this sink first looked).
func staleTables(readings []tableReading, thresholds map[string]int64, nowMs, startMs int64) []string {
	var out []string
	for _, r := range readings {
		limit, listed := thresholds[r.table]
		if !listed {
			continue
		}
		since := r.lastChangeMs
		if since == 0 {
			since = startMs
		}
		if since > 0 && nowMs-since > limit*1000 {
			out = append(out, fmt.Sprintf("%s: no change captured for %s (threshold %s)",
				r.table, (time.Duration(nowMs-since)*time.Millisecond).Round(time.Second),
				time.Duration(limit)*time.Second))
		}
	}
	sort.Strings(out)
	return out
}

// freshness is set in main before the loops start.
var freshness *freshnessMonitor

// freshnessMonitor reads every table once per call.
type freshnessMonitor struct {
	specs      []streamSpec
	heartbeat  string // Debezium's heartbeat stream; "" if not configured
	thresholds map[string]int64
	mu         sync.Mutex
	startMs    int64
	catching   map[string]bool // tables catching up at the last reading (catchup.go)
}

type freshnessSnapshot struct {
	nowMs, heartbeatMs int64
	readings           []tableReading
}

func (m *freshnessMonitor) read(ctx context.Context, broker redis.UniversalClient) (freshnessSnapshot, error) {
	var snap freshnessSnapshot
	now, err := broker.Time(ctx).Result()
	if err != nil {
		return snap, err
	}
	snap.nowMs = now.UnixMilli()
	m.mu.Lock()
	if m.startMs == 0 {
		m.startMs = snap.nowMs
	}
	m.mu.Unlock()
	if m.heartbeat != "" {
		last, err := broker.XRevRangeN(ctx, m.heartbeat, "+", "-", 1).Result()
		if err != nil {
			return snap, err
		}
		if len(last) > 0 {
			snap.heartbeatMs = streamIDMs(last[0].ID)
		}
	}
	for _, s := range m.specs {
		r, err := readTable(ctx, broker, s)
		if err != nil {
			return snap, fmt.Errorf("%s: %w", s.key, err)
		}
		snap.readings = append(snap.readings, r)
	}
	return snap, nil
}

// cacheFreshnessMs: now - the oldest fresh-as-of time; ok false if any table's
// is unknown.
func (s freshnessSnapshot) cacheFreshnessMs() (int64, bool) {
	if len(s.readings) == 0 {
		return 0, false
	}
	oldest := int64(0)
	for _, r := range s.readings {
		f := freshAsOfMs(r, s.heartbeatMs)
		if f == 0 {
			return 0, false
		}
		if oldest == 0 || f < oldest {
			oldest = f
		}
	}
	if oldest > s.nowMs {
		return 0, true
	}
	return s.nowMs - oldest, true
}

func (m *freshnessMonitor) datums(ctx context.Context, broker redis.UniversalClient, dims []cwtypes.Dimension) []cwtypes.MetricDatum {
	snap, err := m.read(ctx, broker)
	if err != nil {
		logErr("freshness-read", fmt.Errorf("per-table freshness not published this interval: %w", err))
		return nil
	}
	var out []cwtypes.MetricDatum
	for _, r := range snap.readings {
		if r.lastChangeMs == 0 {
			continue
		}
		age := float64(0)
		if snap.nowMs > r.lastChangeMs {
			age = float64(snap.nowMs-r.lastChangeMs) / 1000
		}
		tableDims := append(append([]cwtypes.Dimension{}, dims...),
			cwtypes.Dimension{Name: aws.String("Table"), Value: aws.String(r.table)})
		out = append(out, cwtypes.MetricDatum{MetricName: aws.String("LastChangeAgeSeconds"),
			Value: aws.Float64(age), Unit: cwtypes.StandardUnitSeconds, Dimensions: tableDims})
	}
	if len(m.thresholds) > 0 {
		m.mu.Lock()
		start := m.startMs
		m.mu.Unlock()
		stale := staleTables(snap.readings, m.thresholds, snap.nowMs, start)
		for _, line := range stale {
			logWarn("table-stale", fmt.Errorf("%s", line))
		}
		out = append(out, cwtypes.MetricDatum{MetricName: aws.String("StaleTables"),
			Value: aws.Float64(float64(len(stale))), Unit: cwtypes.StandardUnitCount, Dimensions: dims})
	}
	out = append(out, cwtypes.MetricDatum{MetricName: aws.String("CatchingUpTables"),
		Value: aws.Float64(float64(m.noteCatchingUp(snap.readings))), Unit: cwtypes.StandardUnitCount, Dimensions: dims})
	if f, ok := snap.cacheFreshnessMs(); ok {
		out = append(out, cwtypes.MetricDatum{MetricName: aws.String("CacheFreshnessSeconds"),
			Value: aws.Float64(float64(f) / 1000), Unit: cwtypes.StandardUnitSeconds, Dimensions: dims})
	}
	return out
}

// heartbeatValues: what the optional per-table heartbeat keys
// (HEARTBEAT_PREFIX) hold, the table's fresh-as-of time in ms. They used to
// hold the current time, so a stalled table looked fresh. A table whose time
// is unknown is left out (its key is not refreshed).
func (s freshnessSnapshot) heartbeatValues() map[string]int64 {
	out := map[string]int64{}
	for _, r := range s.readings {
		if f := freshAsOfMs(r, s.heartbeatMs); f > 0 {
			out[r.table] = f
		}
	}
	return out
}

// writeHeartbeats sets <prefix>:<table> on the target to each table's
// fresh-as-of time (ms). A table whose time is unknown keeps its old value.
func writeHeartbeats(ctx context.Context, m *freshnessMonitor, broker redis.UniversalClient,
	target redis.UniversalClient, prefix string) error {
	snap, err := m.read(ctx, broker)
	if err != nil {
		return fmt.Errorf("freshness not read from the broker: %w", err)
	}
	values := snap.heartbeatValues()
	for _, s := range m.specs {
		v, ok := values[s.table]
		if !ok {
			continue
		}
		if err := target.Set(ctx, prefix+":"+s.table, strconv.FormatInt(v, 10), 0).Err(); err != nil {
			return err
		}
	}
	return nil
}
