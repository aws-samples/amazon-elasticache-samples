package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func envMap(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func TestNumericEnvRefusesGarbageAndRange(t *testing.T) { // Jira story F051
	if err := checkNumericEnv(envMap(nil)); err != nil {
		t.Fatalf("nothing set: %v", err)
	}
	if err := checkNumericEnv(envMap(map[string]string{"BATCH_SIZE": "2000", "WORKERS": " 8 ",
		"WRITE_RETRIES": "0", "DRIVER_MAX_RETRIES": "-1"})); err != nil {
		t.Fatalf("valid values refused: %v", err)
	}
	err := checkNumericEnv(envMap(map[string]string{"BATCH_SIZE": "abc", "WORKERS": "0",
		"RETRY_IDLE_MS": "1e3", "HEALTH_PORT": "70000"}))
	if err == nil {
		t.Fatal("garbage accepted")
	}
	for _, want := range []string{`BATCH_SIZE="abc"`, "WORKERS=0", `RETRY_IDLE_MS="1e3"`, "HEALTH_PORT=70000"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error does not name %s: %v", want, err)
		}
	}
	if err := checkNumericEnv(envMap(map[string]string{"BATCH_SIZE": "-5"})); err == nil {
		t.Fatal("negative batch accepted")
	}
}

func TestEveryAtoiOrSettingIsChecked(t *testing.T) { // Jira story F051: a new setting must join the list
	names := map[string]bool{}
	for _, e := range numericEnvs {
		names[e.name] = true
	}
	files, _ := os.ReadDir(".")
	for _, f := range files {
		if !strings.HasSuffix(f.Name(), ".go") || strings.HasSuffix(f.Name(), "_test.go") {
			continue
		}
		b, _ := os.ReadFile(f.Name())
		for _, part := range strings.Split(string(b), `atoiOr("`)[1:] {
			name := part[:strings.Index(part, `"`)]
			if !names[name] {
				t.Errorf("%s reads %s with atoiOr but checkNumericEnv does not check it", f.Name(), name)
			}
		}
	}
}

func TestReadCountCapsEntriesPerRead(t *testing.T) { // Jira story F051
	cases := []struct {
		batch   int64
		streams int
		max     int64
		want    int64
	}{
		{2000, 1, 20000, 2000}, {2000, 10, 20000, 2000}, {2000, 40, 20000, 500},
		{2000, 0, 20000, 2000}, {5, 100000, 20000, 1}, {100000, 1, 20000, 20000},
	}
	for _, c := range cases {
		if got := readCount(c.batch, c.streams, c.max); got != c.want {
			t.Errorf("readCount(%d, %d, %d) = %d, want %d", c.batch, c.streams, c.max, got, c.want)
		}
	}
}

func TestDuplicateTableNamesAreRefused(t *testing.T) { // Jira story F039
	ok := []streamSpec{{"data:p.sales.users", "users"}, {"data:p.sales.orders", "orders"}}
	if err := checkUniqueTables(ok); err != nil {
		t.Fatal(err)
	}
	bad := append(ok, streamSpec{"data:p.hr.users", "users"})
	err := checkUniqueTables(bad)
	if err == nil || !strings.Contains(err.Error(), "data:p.sales.users, data:p.hr.users") {
		t.Fatalf("got %v", err)
	}
	// Case differs, keys differ: not a collision.
	if err := checkUniqueTables([]streamSpec{{"a.Users", "Users"}, {"b.users", "users"}}); err != nil {
		t.Fatal(err)
	}
	// An explicit table override that collides is caught too.
	specs, _ := parseStreamList("data:p.a.x=users,data:p.b.users", "STREAMS")
	if checkUniqueTables(specs) == nil {
		t.Fatal("override collision accepted")
	}
}

func TestKeyPrefixWithBracesIsRefused(t *testing.T) { // Jira story F048
	for _, ok := range []string{"", "v2", "orders-v2:", "a.b_c"} {
		if err := checkKeyPrefix(ok); err != nil {
			t.Errorf("%q: %v", ok, err)
		}
	}
	for _, bad := range []string{"{v2}", "v2{", "}"} {
		if checkKeyPrefix(bad) == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestBraceKeyWarnsOnce(t *testing.T) { // Jira story F048
	braceKeyWarned.Store(false)
	var lines []string
	old := errLog
	errLog = newErrLogger(0)
	errLog.out = func(s string) { lines = append(lines, s) }
	defer func() { errLog = old; braceKeyWarned.Store(false) }()
	if got := tombstoneKey("orders:{a}"); got != "orders:{a}" {
		t.Fatalf("tombstoneKey = %q", got)
	}
	tombstoneKey("orders:{b}")
	tombstoneKey("orders:1")
	if len(lines) != 1 || !strings.Contains(lines[0], "WARN brace-key") {
		t.Fatalf("lines = %q", lines)
	}
}

func TestTrimStreamsMayNotOverlapDataOrDLQ(t *testing.T) { // Jira story F049
	specs := []streamSpec{{"data:p.s.orders", "orders"}}
	if err := checkTrimStreams([]string{"data:__debezium-heartbeat.p"}, specs); err != nil {
		t.Fatal(err)
	}
	if err := checkTrimStreams(nil, specs); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{"data:p.s.orders", dlqPrefix + "data:p.s.orders", dlqPrefix + "data:other"} {
		err := checkTrimStreams([]string{"data:__debezium-heartbeat.p", bad}, specs)
		if err == nil || !strings.Contains(err.Error(), bad) {
			t.Errorf("%s: %v", bad, err)
		}
	}
}

func TestBrokerPortDefaultIsShared(t *testing.T) { // Jira story F047
	t.Setenv("BROKER_HOST", "")
	t.Setenv("BROKER_PORT", "")
	if got := brokerAddrFromEnv(); got != "localhost:6379" {
		t.Fatalf("default = %s", got)
	}
	t.Setenv("BROKER_HOST", "b")
	t.Setenv("BROKER_PORT", "6390")
	if got := brokerAddrFromEnv(); got != "b:6390" {
		t.Fatalf("set = %s", got)
	}
	for _, f := range []string{"main.go", "dlq.go"} {
		b, _ := os.ReadFile(f)
		if strings.Contains(string(b), `"BROKER_PORT"`) {
			t.Errorf("%s reads BROKER_PORT itself; use brokerAddrFromEnv", f)
		}
	}
}

func TestGroupRetryable(t *testing.T) {
	for _, s := range []string{"OOM command not allowed when used memory > 'maxmemory'.", "LOADING x", "READONLY y"} {
		if !groupRetryable(errors.New(s)) {
			t.Errorf("%s not retryable", s)
		}
	}
	for _, s := range []string{"WRONGTYPE Operation against a key", "NOPERM user", "ERR unknown"} {
		if groupRetryable(errors.New(s)) {
			t.Errorf("%s retryable", s)
		}
	}
}

func TestIntegrationEnsureGroupCreatesAndKeeps(t *testing.T) { // Jira story F037
	c, ctx := testValkey(t), context.Background()
	s := "itest:group:" + t.Name()
	c.Del(ctx, s)
	defer c.Del(ctx, s)
	created, err := ensureGroup(ctx, c, s, "g")
	if err != nil || !created {
		t.Fatalf("first: created=%v err=%v", created, err)
	}
	created, err = ensureGroup(ctx, c, s, "g")
	if err != nil || created {
		t.Fatalf("second: created=%v err=%v", created, err)
	}
	// A WRONGTYPE key is a configuration error, not something to wait out.
	c.Del(ctx, s)
	c.Set(ctx, s, "x", 0)
	ok, err := ensureGroups(ctx, c, []string{s}, "g", make(chan os.Signal))
	if ok || err == nil || !strings.Contains(err.Error(), "WRONGTYPE") {
		t.Fatalf("wrong type: ok=%v err=%v", ok, err)
	}
}

// ownedValkey is a Valkey the test may reconfigure (CONFIG SET). Only set
// ECDL_TEST_VALKEY_OWNED=1 for a server you started yourself.
func ownedValkey(t *testing.T) redis.UniversalClient {
	t.Helper()
	if os.Getenv("ECDL_TEST_VALKEY_OWNED") != "1" {
		t.Skip("ECDL_TEST_VALKEY_OWNED not set (the test changes maxmemory)")
	}
	return testValkey(t)
}

// withFullBroker sets maxmemory below used memory, so every write that can
// grow memory is refused with OOM, as on a full noeviction broker.
func withFullBroker(t *testing.T, c redis.UniversalClient) func() {
	t.Helper()
	ctx := context.Background()
	if err := c.ConfigSet(ctx, "maxmemory-policy", "noeviction").Err(); err != nil {
		t.Fatal(err)
	}
	if err := c.ConfigSet(ctx, "maxmemory", "1").Err(); err != nil {
		t.Fatal(err)
	}
	restored := false
	restore := func() {
		if !restored {
			restored = true
			c.ConfigSet(ctx, "maxmemory", "0")
		}
	}
	t.Cleanup(restore)
	return restore
}

func TestIntegrationFullBrokerExistingGroupStartsAtOnce(t *testing.T) { // Jira story F037
	c, ctx := ownedValkey(t), context.Background()
	s := "itest:oom:" + t.Name()
	c.Del(ctx, s)
	defer c.Del(ctx, s)
	if err := c.XGroupCreateMkStream(ctx, s, "g", "0").Err(); err != nil {
		t.Fatal(err)
	}
	withFullBroker(t, c)
	if err := c.XGroupCreateMkStream(ctx, s, "g2", "0").Err(); err == nil || !strings.HasPrefix(err.Error(), "OOM") {
		t.Fatalf("the broker is not full: %v", err)
	}
	start := time.Now()
	stop := make(chan os.Signal, 1)
	timer := time.AfterFunc(2*time.Second, func() { stop <- os.Interrupt })
	defer timer.Stop()
	ok, err := ensureGroups(ctx, c, []string{s}, "g", stop)
	if !ok || err != nil || time.Since(start) > time.Second {
		t.Fatalf("ok=%v err=%v after %v", ok, err, time.Since(start))
	}
}

func TestIntegrationFullBrokerMissingGroupWaitsThenCreates(t *testing.T) { // Jira story F037
	c, ctx := ownedValkey(t), context.Background()
	s := "itest:oom:" + t.Name()
	c.Del(ctx, s)
	defer c.Del(ctx, s)
	oldBase := holdBase
	holdBase = 20 * time.Millisecond
	defer func() { holdBase = oldBase }()
	var lines []string
	old := errLog
	errLog = newErrLogger(0)
	errLog.out = func(s string) { lines = append(lines, s) }
	defer func() { errLog = old }()
	restore := withFullBroker(t, c)
	type result struct {
		ok  bool
		err error
	}
	done := make(chan result, 1)
	go func() {
		ok, err := ensureGroups(ctx, c, []string{s}, "g", make(chan os.Signal))
		done <- result{ok, err}
	}()
	time.Sleep(150 * time.Millisecond)
	if atomic.LoadInt64(&groupWaiting) != 1 {
		t.Fatal("not marked as waiting")
	}
	if ok, _ := healthy(); !ok {
		t.Fatal("health failed while waiting for the broker")
	}
	select {
	case r := <-done:
		t.Fatalf("returned while the broker was full: %+v", r)
	default:
	}
	restore()
	select {
	case r := <-done:
		if !r.ok || r.err != nil {
			t.Fatalf("after memory freed: %+v", r)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("did not create the group after memory was freed")
	}
	if atomic.LoadInt64(&groupWaiting) != 0 {
		t.Fatal("still marked as waiting")
	}
	if len(lines) == 0 || !strings.Contains(lines[0], "ERROR broker-group") || !strings.Contains(lines[0], "OOM") {
		t.Fatalf("lines = %q", lines)
	}
	if n := len(c.XInfoGroups(ctx, s).Val()); n != 1 {
		t.Fatalf("groups = %d", n)
	}
}

func TestIntegrationFullBrokerWaitStopsOnSignal(t *testing.T) { // Jira story F037
	c, ctx := ownedValkey(t), context.Background()
	s := "itest:oom:" + t.Name()
	c.Del(ctx, s)
	withFullBroker(t, c)
	stop := make(chan os.Signal, 1)
	stop <- os.Interrupt
	ok, err := ensureGroups(ctx, c, []string{s}, "g", stop)
	if ok || err != nil {
		t.Fatalf("ok=%v err=%v", ok, err)
	}
}

func TestIntegrationNoGroupOnOneStreamIsRecreated(t *testing.T) { // Jira story F050
	c, ctx := testValkey(t), context.Background()
	a, b := "itest:nogroup:a:"+t.Name(), "itest:nogroup:b:"+t.Name()
	c.Del(ctx, a, b)
	defer c.Del(ctx, a, b)
	for _, s := range []string{a, b} {
		c.XGroupCreateMkStream(ctx, s, "g", "0")
		c.XAdd(ctx, &redis.XAddArgs{Stream: s, Values: []string{"value", "x"}})
	}
	c.XGroupDestroy(ctx, a, "g") // someone deleted one table's group
	read := func() error {
		return c.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "w",
			Streams: []string{a, b, ">", ">"}, Count: 10, Block: -1}).Err()
	}
	err := read()
	if !isNoGroup(err) {
		t.Fatalf("expected NOGROUP, got %v", err)
	}
	atomic.StoreInt64(&lastRegroupMs, 0)
	recreateGroups(ctx, c, []string{a, b}, "g")
	res, err := c.XReadGroup(ctx, &redis.XReadGroupArgs{Group: "g", Consumer: "w",
		Streams: []string{a, b, ">", ">"}, Count: 10, Block: -1}).Result()
	if err != nil {
		t.Fatalf("still failing: %v", err)
	}
	// The recreated group starts at 0, so stream a's entry is delivered.
	got := map[string]int{}
	for _, st := range res {
		got[st.Stream] = len(st.Messages)
	}
	if got[a] != 1 || got[b] != 1 {
		t.Fatalf("delivered %v", got)
	}
	// Rate limit: a second call within a second does nothing.
	c.XGroupDestroy(ctx, a, "g")
	recreateGroups(ctx, c, []string{a, b}, "g")
	if !isNoGroup(read()) {
		t.Fatal("recreated twice within a second")
	}
}

func TestIntegrationWorkerKeepsReadingAfterNoGroup(t *testing.T) { // Jira story F050
	c, ctx := testValkey(t), context.Background()
	if err := loadScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	a := streamSpec{key: "itest:ng:" + t.Name() + ".nga", table: "nga"}
	b := streamSpec{key: "itest:ng:" + t.Name() + ".ngb", table: "ngb"}
	add := func(s streamSpec, id int) {
		c.XAdd(ctx, &redis.XAddArgs{Stream: s.key, Values: map[string]interface{}{
			"key":   fmt.Sprintf(`{"id":%d}`, id),
			"value": fmt.Sprintf(`{"op":"c","source":{"table":"%s"},"after":{"id":%d,"v":"x"}}`, s.table, id),
		}})
	}
	c.Del(ctx, a.key, b.key, "nga:1", "ngb:1", "nga:2", "ngb:2")
	t.Cleanup(func() { c.Del(ctx, a.key, b.key, "nga:1", "ngb:1", "nga:2", "ngb:2") })
	for _, s := range []streamSpec{a, b} {
		c.XGroupCreateMkStream(ctx, s.key, "g", "0")
		add(s, 1)
	}
	c.XGroupDestroy(ctx, a.key, "g")
	atomic.StoreInt64(&lastRegroupMs, 0)
	wctx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() { worker(wctx, c, c, []streamSpec{a, b}, "g", "w1", 10, "envelope"); close(done) }()
	defer func() { cancel(); <-done }()
	waitFor(t, "both tables to apply after NOGROUP", 3*time.Second, func() bool {
		return c.Exists(ctx, "nga:1", "ngb:1").Val() == 2
	})
	add(a, 2)
	add(b, 2)
	waitFor(t, "later changes to apply", 3*time.Second, func() bool {
		return c.Exists(ctx, "nga:2", "ngb:2").Val() == 2
	})
}

// TestHelperRunMain runs main() in a child process (see runMain).
func TestHelperRunMain(t *testing.T) {
	if os.Getenv("ECDL_RUN_MAIN") != "1" {
		t.Skip("helper")
	}
	os.Args = []string{"ecdl-go-sink"}
	main()
}

// runMain runs the sink's main() with env and returns its exit code and output.
// Used for configuration that must stop the sink before it connects anywhere.
func runMain(t *testing.T, env ...string) (int, string) {
	t.Helper()
	// The test binary itself, re-run to execute main() alone. exec.Cmd rather than
	// exec.Command: the program is this process's own executable, not a name to look up.
	self, err := os.Executable()
	if err != nil {
		t.Fatalf("os.Executable: %v", err)
	}
	cmd := &exec.Cmd{Path: self, Args: []string{self, "-test.run=^TestHelperRunMain$"}}
	cmd.Env = append([]string{"ECDL_RUN_MAIN=1", "HEALTH_PORT=18617", "BROKER_HOST=127.0.0.1", "BROKER_PORT=1",
		"PATH=" + os.Getenv("PATH")}, env...)
	out, err := cmd.CombinedOutput()
	code := 0
	if ee, ok := err.(*exec.ExitError); ok {
		code = ee.ExitCode()
	} else if err != nil {
		t.Fatal(err)
	}
	return code, string(out)
}

func TestStartupRefusesBadConfigBeforeConnecting(t *testing.T) { // Jira story F039 Jira story F048 Jira story F049 Jira story F051
	cases := []struct {
		env  []string
		want string
	}{
		{[]string{"STREAMS=data:p.a.users,data:p.b.users"}, "more than one stream carries the table users"},
		{[]string{"STREAMS=data:p.a.users", "KEY_PREFIX={v2}"}, "KEY_PREFIX"},
		{[]string{"STREAMS=data:p.a.users", "TRIM_STREAMS=data:p.a.users"}, "TRIM_STREAMS lists data:p.a.users"},
		{[]string{"STREAMS=data:p.a.users", "BATCH_SIZE=abc"}, `BATCH_SIZE="abc"`},
	}
	for _, c := range cases {
		code, out := runMain(t, c.env...)
		if code != 1 || !strings.Contains(out, "FATAL") || !strings.Contains(out, c.want) {
			t.Errorf("%v: exit %d, output:\n%s", c.env, code, out)
		}
		if strings.Contains(out, "Connected to") {
			t.Errorf("%v: connected before refusing", c.env)
		}
	}
}
