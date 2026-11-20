package main

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	cwtypes "github.com/aws/aws-sdk-go-v2/service/cloudwatch/types"
	"github.com/aws/aws-sdk-go-v2/service/ecs"
	ecstypes "github.com/aws/aws-sdk-go-v2/service/ecs/types"
	"github.com/redis/go-redis/v9"
)

func TestCaptureTrackerCountsFromTheResume(t *testing.T) { // D5
	var tr captureTracker
	if got := tr.observe(true, false, 1000); got != -1 {
		t.Fatalf("running from the start: %d, want -1 (no resume seen)", got)
	}
	tr.observe(true, true, 2000)
	if got := tr.observe(false, false, 2500); got != -1 {
		t.Fatalf("an unknown reading must not count as a resume: %d", got)
	}
	if got := tr.observe(true, false, 3000); got != 0 {
		t.Fatalf("resume: %d, want 0", got)
	}
	if got := tr.observe(false, false, 4000); got != 1000 {
		t.Fatalf("after the resume: %d, want 1000", got)
	}
	tr.observe(true, true, 5000) // paused again
	if got := tr.observe(true, false, 9000); got != 0 {
		t.Fatalf("second resume: %d, want 0", got)
	}
}

func TestCaptureStaleMs(t *testing.T) {
	for _, c := range []struct {
		name          string
		age           int64
		exists, pause bool
		since         int64
		want          int64
		ok            bool
	}{
		{"paused is never stale", 900000, true, true, -1, 0, true},
		{"paused with no heartbeat yet", 0, false, true, -1, 0, true},
		{"running: heartbeat age", 15000, true, false, -1, 15000, true},
		{"just resumed: counted from the resume", 900000, true, false, 20000, 20000, true},
		{"resumed long ago: heartbeat age", 15000, true, false, 600000, 15000, true},
		{"no heartbeat after a resume", 0, false, false, 45000, 45000, true},
		{"no heartbeat and no resume seen: unknown", 0, false, false, -1, 0, false},
	} {
		got, ok := captureStaleMs(c.age, c.exists, c.pause, c.since)
		if got != c.want || ok != c.ok {
			t.Errorf("%s: got %d/%v, want %d/%v", c.name, got, ok, c.want, c.ok)
		}
	}
}

type fakeECS struct {
	desired int32
	status  string
	err     error
	missing bool
	calls   int
}

func (f *fakeECS) DescribeServices(ctx context.Context, in *ecs.DescribeServicesInput, _ ...func(*ecs.Options)) (*ecs.DescribeServicesOutput, error) {
	f.calls++
	if f.err != nil {
		return nil, f.err
	}
	if f.missing {
		return &ecs.DescribeServicesOutput{Failures: []ecstypes.Failure{{Reason: aws.String("MISSING")}}}, nil
	}
	status := f.status
	if status == "" {
		status = "ACTIVE"
	}
	return &ecs.DescribeServicesOutput{Services: []ecstypes.Service{{
		ServiceName: aws.String(in.Services[0]), Status: aws.String(status), DesiredCount: f.desired}}}, nil
}

func TestDesiredCount(t *testing.T) {
	ctx := context.Background()
	if n, err := desiredCount(ctx, &fakeECS{desired: 1}, "c", "s"); err != nil || n != 1 {
		t.Fatalf("got %d, %v", n, err)
	}
	if n, err := desiredCount(ctx, &fakeECS{desired: 0}, "c", "s"); err != nil || n != 0 {
		t.Fatalf("got %d, %v", n, err)
	}
	for name, f := range map[string]*fakeECS{
		"api error": {err: errors.New("AccessDeniedException")},
		"missing":   {missing: true},
		"draining":  {status: "DRAINING"},
	} {
		if _, err := desiredCount(ctx, f, "c", "s"); err == nil {
			t.Errorf("%s: must be an error, not a desired count of 0 (which reads as a planned pause)", name)
		}
	}
}

func TestNoCaptureMonitorWithoutAStream(t *testing.T) {
	if newCaptureMonitor(" ", "c", "s", &fakeECS{}) != nil {
		t.Fatal("no heartbeat stream: nothing to measure")
	}
	if m := newCaptureMonitor("hb", "", "s", &fakeECS{}); m == nil || m.ecs != nil {
		t.Fatal("without a cluster the desired count is not read")
	}
}

func datumMap(ds []cwtypes.MetricDatum) map[string]float64 {
	out := map[string]float64{}
	for _, d := range ds {
		out[*d.MetricName] = *d.Value
	}
	return out
}

func TestIntegrationCaptureDatums(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	stream := "itest:data:__debezium-heartbeat.d5"
	c.Del(ctx, stream)
	defer c.Del(ctx, stream)
	f := &fakeECS{desired: 1}
	m := newCaptureMonitor(stream, "c", "svc", f)

	// No heartbeat yet, running, no resume seen: stale is unknown and left out.
	got := datumMap(m.datums(ctx, c, nil))
	if _, ok := got["CaptureStaleSeconds"]; ok {
		t.Fatalf("no heartbeat must leave the alarm input out: %v", got)
	}
	if got["CapturePaused"] != 0 {
		t.Fatalf("CapturePaused = %v", got["CapturePaused"])
	}

	// A heartbeat 50 s old.
	now := c.Time(ctx).Val()
	c.XAdd(ctx, &redis.XAddArgs{Stream: stream, ID: fmt.Sprintf("%d-0", now.Add(-50*time.Second).UnixMilli()),
		Values: []string{"k", "v"}})
	got = datumMap(m.datums(ctx, c, nil))
	if a := got["CaptureAgeSeconds"]; a < 49 || a > 55 {
		t.Fatalf("CaptureAgeSeconds = %v, want about 50", a)
	}
	if got["CaptureStaleSeconds"] != got["CaptureAgeSeconds"] {
		t.Fatalf("running: stale %v != age %v", got["CaptureStaleSeconds"], got["CaptureAgeSeconds"])
	}

	// Planned pause: not stale, whatever the heartbeat age.
	f.desired = 0
	got = datumMap(m.datums(ctx, c, nil))
	if got["CapturePaused"] != 1 || got["CaptureStaleSeconds"] != 0 {
		t.Fatalf("paused: %v", got)
	}

	// Resumed: the old heartbeat does not count; time since the resume does.
	f.desired = 1
	got = datumMap(m.datums(ctx, c, nil))
	if got["CapturePaused"] != 0 || got["CaptureStaleSeconds"] > 2 || got["CaptureAgeSeconds"] < 49 {
		t.Fatalf("just resumed: %v", got)
	}

	// The desired count cannot be read: no CapturePaused, stale is the age
	// (unknown is not treated as paused).
	f.err = errors.New("AccessDeniedException")
	m2 := newCaptureMonitor(stream, "c", "svc", f)
	got = datumMap(m2.datums(ctx, c, nil))
	if _, ok := got["CapturePaused"]; ok {
		t.Fatalf("CapturePaused sent without a reading: %v", got)
	}
	if got["CaptureStaleSeconds"] < 49 {
		t.Fatalf("unknown pause must not hide a stall: %v", got)
	}
}

func TestIntegrationCaptureBrokerDownLeavesTheAlarmInputOut(t *testing.T) {
	c := testValkey(t).(*redis.Client)
	dead := redis.NewClient(&redis.Options{Addr: c.Options().Addr})
	dead.Close()
	m := newCaptureMonitor("hb", "c", "svc", &fakeECS{desired: 1})
	got := datumMap(m.datums(context.Background(), dead, nil))
	if _, ok := got["CaptureStaleSeconds"]; ok {
		t.Fatalf("an unreadable broker must leave the alarm input out: %v", got)
	}
	if _, ok := got["CaptureAgeSeconds"]; ok {
		t.Fatalf("CaptureAgeSeconds sent without a reading: %v", got)
	}
}
