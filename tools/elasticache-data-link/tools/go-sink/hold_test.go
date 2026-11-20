package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	cwtypes "github.com/aws/aws-sdk-go-v2/service/cloudwatch/types"
	"github.com/redis/go-redis/v9"
)

func TestTargetWideClassification(t *testing.T) {
	opErr := &net.OpError{Op: "dial", Net: "tcp", Err: syscall.ECONNREFUSED}
	for _, c := range []struct {
		err  error
		want bool
	}{
		{nil, false},
		{io.EOF, true},
		{opErr, true},
		{fmt.Errorf("wrapped: %w", syscall.ECONNRESET), true},
		{context.DeadlineExceeded, true},
		{redis.ErrClosed, true},
		{errors.New("redis: connection pool timeout"), true},
		{redis.Nil, false},
		{errPoison, false},
		{fmt.Errorf("x: %w", errPoison), false},
		{errors.New("unexpected script reply string"), false},
		{proto("OOM command not allowed when used memory > 'maxmemory'"), true},
		{proto("NOPERM this user has no permissions to run the 'evalsha' command"), true},
		{proto("CLUSTERDOWN The cluster is down"), true},
		{proto("READONLY You can't write against a read only replica."), true},
		{proto("MOVED 3999 127.0.0.1:6381"), true},
		{proto("LOADING Valkey is loading the dataset in memory"), true},
		{proto("WRONGTYPE Operation against a key holding the wrong kind of value"), false},
		{proto("ERR Error running script"), false},
	} {
		if got := targetWide(c.err); got != c.want {
			t.Errorf("targetWide(%v) = %v, want %v", c.err, got, c.want)
		}
	}
}

// proto builds a server error reply as go-redis returns it.
func proto(msg string) error {
	c := redis.NewStringCmd(context.Background())
	c.SetErr(redisErr(msg))
	return c.Err()
}

type redisErr string

func (e redisErr) Error() string { return string(e) }
func (e redisErr) RedisError()   {}

func TestDialRetryable(t *testing.T) {
	e := endpoint{label: "target", addr: "x:6379"}
	opErr := &net.OpError{Op: "dial", Net: "tcp", Err: syscall.ECONNREFUSED}
	for _, c := range []struct {
		err  error
		want bool
	}{
		{connectError(e, opErr), true},
		{connectError(e, io.EOF), true},
		{connectError(e, proto("LOADING Valkey is loading")), true},
		{connectError(e, proto("WRONGPASS invalid username-password pair")), false},
		{connectError(e, proto("NOAUTH Authentication required.")), false},
		{connectError(e, errors.New("tls: first record does not look like a TLS handshake")), false},
		{errors.New("target at x is cluster-mode ENABLED but this side is configured as a single node"), false},
		{errors.New("reading CA file: no such file"), false},
	} {
		if got := dialRetryable(c.err); got != c.want {
			t.Errorf("dialRetryable(%v) = %v, want %v", c.err, got, c.want)
		}
	}
}

func TestNextBackoffIsCapped(t *testing.T) {
	d := holdBase
	for i := 0; i < 20; i++ {
		d = nextBackoff(d)
	}
	if d != holdMax {
		t.Fatalf("back-off grew to %v, want the cap %v", d, holdMax)
	}
}

func fakeSleep(t *testing.T) *int64 {
	var slept int64
	saved := sleep
	sleep = func(d time.Duration) { atomic.AddInt64(&slept, int64(d)) }
	t.Cleanup(func() { sleep = saved })
	return &slept
}

func TestHoldLoopRetriesUntilDoneAndStaysHealthy(t *testing.T) {
	fakeSleep(t)
	n := 0
	ok := holdLoop("test", func() (bool, error) {
		n++
		if isHolding() == false {
			t.Error("not marked holding during the hold")
		}
		if h, _ := healthy(); !h {
			t.Error("unhealthy during a hold")
		}
		if atomic.LoadInt64(&metricTargetUp) != 0 {
			t.Error("TargetUp not 0 during a hold")
		}
		return n == 4, io.EOF
	}, io.EOF)
	if !ok || n != 4 || isHolding() || atomic.LoadInt64(&metricTargetUp) != 1 {
		t.Fatalf("ok=%v tries=%d holding=%v up=%d", ok, n, isHolding(), atomic.LoadInt64(&metricTargetUp))
	}
}

func TestHoldLoopStopsOnShutdown(t *testing.T) {
	fakeSleep(t)
	defer stoppingFlag.Store(false)
	n := 0
	ok := holdLoop("test", func() (bool, error) {
		n++
		if n == 3 {
			stoppingFlag.Store(true)
		}
		return false, io.EOF
	}, io.EOF)
	if ok || n != 3 || isHolding() {
		t.Fatalf("ok=%v tries=%d holding=%v", ok, n, isHolding())
	}
}

func TestDialTargetWaitsOutAnOutage(t *testing.T) {
	saved, savedBase := dialFn, holdBase
	holdBase = time.Millisecond
	defer func() { dialFn, holdBase = saved, savedBase }()
	opErr := &net.OpError{Op: "dial", Net: "tcp", Err: syscall.ECONNREFUSED}
	calls := 0
	dialFn = func(ctx context.Context, e endpoint) (redis.UniversalClient, error) {
		calls++
		if calls < 4 {
			if calls > 1 && !isHolding() {
				t.Error("not holding while waiting for the target")
			}
			return nil, connectError(e, opErr)
		}
		return redis.NewClient(&redis.Options{Addr: "127.0.0.1:1"}), nil
	}
	c, err := dialTarget(context.Background(), endpoint{label: "target"}, make(chan os.Signal))
	if err != nil || c == nil || calls != 4 || isHolding() {
		t.Fatalf("c=%v err=%v calls=%d holding=%v", c, err, calls, isHolding())
	}
	c.Close()

	// A rejected credential is a configuration error: fatal at once.
	calls = 0
	dialFn = func(ctx context.Context, e endpoint) (redis.UniversalClient, error) {
		calls++
		return nil, connectError(e, proto("WRONGPASS invalid username-password pair"))
	}
	if c, err := dialTarget(context.Background(), endpoint{label: "target"}, make(chan os.Signal)); err == nil || c != nil || calls != 1 {
		t.Fatalf("WRONGPASS: c=%v err=%v calls=%d", c, err, calls)
	}

	// Shutdown while waiting.
	stop := make(chan os.Signal, 1)
	stop <- syscall.SIGTERM
	dialFn = func(ctx context.Context, e endpoint) (redis.UniversalClient, error) {
		return nil, connectError(e, opErr)
	}
	if c, err := dialTarget(context.Background(), endpoint{label: "target"}, stop); c != nil || err != nil || isHolding() {
		t.Fatalf("shutdown: c=%v err=%v holding=%v", c, err, isHolding())
	}
}

func TestLevelDatumsLeaveOutUnreadLevels(t *testing.T) {
	names := func(ds []cwtypes.MetricDatum) map[string]float64 {
		m := map[string]float64{}
		for _, d := range ds {
			m[*d.MetricName] = *d.Value
		}
		return m
	}
	ok := names(levelDatums(nil, true, 5, 10, 2, 0, 3000, 1))
	for _, n := range []string{"TargetUp", "SinkRunningTasks", "BacklogPerTask", "SinkBacklog", "DLQDepth", "OldestEntryAgeSeconds"} {
		if _, has := ok[n]; !has {
			t.Errorf("%s missing when the broker was read", n)
		}
	}
	if ok["OldestEntryAgeSeconds"] != 3 {
		t.Errorf("age = %v s", ok["OldestEntryAgeSeconds"])
	}
	bad := names(levelDatums(nil, false, 0, 0, 2, 0, 0, 0))
	if len(bad) != 2 || bad["TargetUp"] != 0 {
		t.Fatalf("unread broker: %v (a failed read must not publish 0 backlog, 0 DLQ, 0 lag)", bad)
	}
}
