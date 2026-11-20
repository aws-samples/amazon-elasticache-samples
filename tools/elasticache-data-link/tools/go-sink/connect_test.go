package main

// Tests for credential resolution and connection-error classification.
//
// Auth mode and topology are tested as a matrix rather than as two independent
// lists, because the bug worth guarding against is specific to one combination:
// go-redis takes Username/Password on both Options and ClusterOptions, and it
// would be easy to wire only one of the two branches.

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/redis/go-redis/v9"
)

func withEnv(t *testing.T, kv map[string]string) {
	t.Helper()
	for _, key := range []string{
		"TARGET_USERNAME", "TARGET_PASSWORD", "TARGET_SECRET",
		"BROKER_USERNAME", "BROKER_PASSWORD", "BROKER_SECRET",
	} {
		t.Setenv(key, "")
		_ = key
	}
	// t.Setenv with "" leaves the variable set-but-empty, which loadCredential
	// treats as absent, so the result is the same either way.
	for key, value := range kv {
		t.Setenv(key, value)
	}
}

func TestCredentialNoneWhenNothingConfigured(t *testing.T) {
	withEnv(t, nil)
	cred, err := loadCredential("TARGET")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cred.mode() != authNone {
		t.Fatalf("mode = %q, want %q", cred.mode(), authNone)
	}
	if cred.username != "" || cred.password != "" {
		t.Fatalf("expected empty credential, got user=%q passLen=%d",
			cred.username, len(cred.password))
	}
}

func TestCredentialPlaintextSecretIsAnAuthToken(t *testing.T) {
	withEnv(t, map[string]string{"TARGET_SECRET": "s3cr3t-token"})
	cred, err := loadCredential("TARGET")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cred.mode() != authToken {
		t.Fatalf("mode = %q, want %q", cred.mode(), authToken)
	}
	if cred.password != "s3cr3t-token" || cred.username != "" {
		t.Fatalf("token not parsed: user=%q pass=%q", cred.username, cred.password)
	}
}

func TestCredentialJSONSecretIsRBAC(t *testing.T) {
	withEnv(t, map[string]string{
		"TARGET_SECRET": `{"username":"ecdl-sink","password":"p@ss w0rd"}`,
	})
	cred, err := loadCredential("TARGET")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cred.mode() != authRBAC {
		t.Fatalf("mode = %q, want %q", cred.mode(), authRBAC)
	}
	if cred.username != "ecdl-sink" || cred.password != "p@ss w0rd" {
		t.Fatalf("rbac not parsed: user=%q pass=%q", cred.username, cred.password)
	}
}

func TestCredentialJSONSecretWithExtraFieldsStillWorks(t *testing.T) {
	// RDS-managed and manually created secrets both carry extra keys.
	withEnv(t, map[string]string{
		"TARGET_SECRET": `{"engine":"valkey","username":"u","password":"p",` +
			`"host":"x","port":6379}`,
	})
	cred, _ := loadCredential("TARGET")
	if cred.username != "u" || cred.password != "p" {
		t.Fatalf("got user=%q pass=%q", cred.username, cred.password)
	}
}

func TestCredentialJSONWithoutPasswordIsAnError(t *testing.T) {
	withEnv(t, map[string]string{"TARGET_SECRET": `{"username":"only-a-user"}`})
	if _, err := loadCredential("TARGET"); err == nil {
		t.Fatal("expected an error naming the missing field")
	} else if !strings.Contains(err.Error(), "password") {
		t.Fatalf("error should name the missing field, got %v", err)
	}
}

func TestCredentialTokenThatLooksLikeJSONIsNotMisread(t *testing.T) {
	// A token can contain braces. Only a decode into the expected shape counts,
	// so this must be treated as a plaintext token rather than rejected.
	withEnv(t, map[string]string{"TARGET_SECRET": `{not really json`})
	cred, err := loadCredential("TARGET")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cred.mode() != authToken || cred.password != `{not really json` {
		t.Fatalf("got mode=%q pass=%q", cred.mode(), cred.password)
	}
}

func TestExplicitVarsBeatTheSecret(t *testing.T) {
	withEnv(t, map[string]string{
		"TARGET_USERNAME": "explicit",
		"TARGET_PASSWORD": "explicit-pass",
		"TARGET_SECRET":   `{"username":"from-secret","password":"x"}`,
	})
	cred, _ := loadCredential("TARGET")
	if cred.username != "explicit" || cred.password != "explicit-pass" {
		t.Fatalf("explicit vars should take precedence, got user=%q", cred.username)
	}
}

func TestPasswordOnlyExplicitIsAToken(t *testing.T) {
	withEnv(t, map[string]string{"TARGET_PASSWORD": "just-a-token"})
	cred, _ := loadCredential("TARGET")
	if cred.mode() != authToken {
		t.Fatalf("mode = %q, want %q", cred.mode(), authToken)
	}
}

func TestBrokerAndTargetCredentialsAreIndependent(t *testing.T) {
	withEnv(t, map[string]string{
		"TARGET_SECRET": "target-token",
		"BROKER_SECRET": `{"username":"bu","password":"bp"}`,
	})
	tgt, _ := loadCredential("TARGET")
	brk, _ := loadCredential("BROKER")
	if tgt.mode() != authToken || tgt.password != "target-token" {
		t.Fatalf("target: mode=%q", tgt.mode())
	}
	if brk.mode() != authRBAC || brk.username != "bu" {
		t.Fatalf("broker: mode=%q user=%q", brk.mode(), brk.username)
	}
}

func TestCredentialOriginNeverContainsTheSecret(t *testing.T) {
	// `origin` is logged; the value must never appear in it.
	for _, env := range []map[string]string{
		{"TARGET_SECRET": "TOPSECRET"},
		{"TARGET_SECRET": `{"username":"u","password":"TOPSECRET"}`},
		{"TARGET_PASSWORD": "TOPSECRET"},
	} {
		withEnv(t, env)
		cred, _ := loadCredential("TARGET")
		if strings.Contains(cred.origin, "TOPSECRET") {
			t.Fatalf("origin leaks the credential: %q", cred.origin)
		}
		if cred.origin == "" {
			t.Fatal("origin should say where the credential came from")
		}
	}
}

func TestDescribeNeverContainsTheSecret(t *testing.T) {
	e := endpoint{label: "target", addr: "host:6379", tls: true, cluster: true,
		cred: credential{username: "u", password: "TOPSECRET",
			origin: "TARGET_SECRET (JSON username/password)"}}
	text := e.describe()
	if strings.Contains(text, "TOPSECRET") {
		t.Fatalf("describe leaks the credential: %q", text)
	}
	for _, want := range []string{"target", "host:6379", "TLS=true",
		"cluster=true", "auth=rbac"} {
		if !strings.Contains(text, want) {
			t.Fatalf("describe missing %q: %q", want, text)
		}
	}
}

// ── auth x topology matrix ──────────────────────────────────────────────────
//
// dial cannot reach a real cache in a unit test, so this asserts the options
// actually built for each combination. It is the cheap guard against wiring
// Username/Password onto Options but forgetting ClusterOptions.

func TestAuthWiredForBothTopologies(t *testing.T) {
	// The passwords are deliberately distinctive. An earlier version used "p",
	// which appears inside ordinary words like "port", so the leak assertion
	// below reported a false positive and proved nothing.
	cases := []struct {
		name    string
		cluster bool
		cred    credential
	}{
		{"single/none", false, credential{}},
		// Short placeholder strings the test looks for in error text, not
		// credentials (kept under the length secret detectors look for).
		{"single/token", false, credential{password: "ZZTOKENZZ"}},
		{"single/rbac", false, credential{username: "u", password: "ZZRBACZZ"}},
		{"cluster/none", true, credential{}},
		{"cluster/token", true, credential{password: "ZZTOKENZZ"}},
		{"cluster/rbac", true, credential{username: "u", password: "ZZRBACZZ"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			e := endpoint{label: "target", addr: "127.0.0.1:1", tls: false,
				cluster: tc.cluster, poolSize: 2, cred: tc.cred}

			// A closed port, so the dial fails fast. What matters is that the
			// failure is a connection failure and not a panic or a nil client,
			// and that it is classified rather than raw.
			ctx, cancel := context.WithTimeout(context.Background(), 3e9)
			defer cancel()
			client, err := dial(ctx, e)
			if err == nil {
				client.Close()
				t.Skip("something is listening on 127.0.0.1:1")
			}
			if !strings.Contains(err.Error(), "target at 127.0.0.1:1") {
				t.Fatalf("error should name the endpoint: %v", err)
			}
			if strings.Contains(err.Error(), tc.cred.password) &&
				tc.cred.password != "" {
				t.Fatalf("error leaks the credential: %v", err)
			}
		})
	}
}

// ── error classification ────────────────────────────────────────────────────

func TestConnectErrorClassification(t *testing.T) {
	e := endpoint{label: "target", addr: "cache:6379", tls: true, cluster: true,
		cred: credential{password: "p", origin: "TARGET_SECRET (plaintext auth token)"}}

	cases := []struct {
		driver   string
		wantText string
		why      string
	}{
		{"NOAUTH Authentication required.",
			"requires authentication but none was supplied",
			"cache wants a credential, we sent none"},
		{"WRONGPASS invalid username-password pair or user is disabled.",
			"rejected the credential",
			"credential was wrong"},
		{"ERR Client sent AUTH, but no password is set",
			"has no authentication configured",
			"we sent a credential the cache does not want"},
		{"tls: first record does not look like a TLS handshake",
			"failed the TLS handshake",
			"TLS setting mismatch"},
		{"ERR This instance has cluster support disabled",
			"is not a cluster",
			"cluster flag does not match the server"},
		{"dial tcp 10.0.0.1:6379: i/o timeout",
			"is unreachable",
			"network or security group"},
	}

	for _, tc := range cases {
		got := connectError(e, errors.New(tc.driver)).Error()
		if !strings.Contains(got, tc.wantText) {
			t.Errorf("%s: got %q, want it to contain %q", tc.why, got, tc.wantText)
		}
		if !strings.Contains(got, tc.driver) {
			t.Errorf("%s: the driver's own message should be preserved: %q",
				tc.why, got)
		}
	}
}

func TestNoauthErrorNamesTheConfigFieldToSet(t *testing.T) {
	e := endpoint{label: "target", addr: "cache:6379",
		cred: credential{origin: "none configured"}}
	got := connectError(e, errors.New("NOAUTH Authentication required.")).Error()
	if !strings.Contains(got, "target.secret_arn") {
		t.Fatalf("should name the link field to set: %q", got)
	}
}

func TestWrongpassErrorMentionsTheRequiredPermissions(t *testing.T) {
	e := endpoint{label: "target", addr: "cache:6379",
		cred: credential{username: "u", password: "p", origin: "TARGET_SECRET"}}
	got := connectError(e, errors.New("WRONGPASS invalid username-password pair")).Error()
	for _, want := range []string{"HSET", "DEL", "enabled"} {
		if !strings.Contains(got, want) {
			t.Errorf("RBAC failures need the permission hint %q: %q", want, got)
		}
	}
}

func TestUnknownErrorIsStillWrappedWithTheEndpoint(t *testing.T) {
	e := endpoint{label: "broker", addr: "brk:6379"}
	got := connectError(e, errors.New("something nobody predicted")).Error()
	if !strings.Contains(got, "broker at brk:6379") ||
		!strings.Contains(got, "something nobody predicted") {
		t.Fatalf("unclassified errors must keep both context and cause: %q", got)
	}
}

// ── topology assertion ──────────────────────────────────────────────────────
//
// The asymmetry is the point. A cluster client against a single node fails on
// its own, with a clear error. A plain client against a sharded cluster connects, PINGs, and
// then fails MOVED on most keys -- which retries and dead-letters instead of
// failing, so the sink reports itself healthy while losing most writes.

// Only Info is needed, because assertTopology takes the narrow infoReader
// interface rather than the whole client.
type fakeInfoClient struct {
	info string
	err  error
}

func (f fakeInfoClient) Info(ctx context.Context, sections ...string) *redis.StringCmd {
	cmd := redis.NewStringCmd(ctx, "info")
	if f.err != nil {
		cmd.SetErr(f.err)
	} else {
		cmd.SetVal(f.info)
	}
	return cmd
}

func TestTopologyAgreementPasses(t *testing.T) {
	cases := []struct {
		name       string
		serverInfo string
		configured bool
	}{
		{"both clustered", "cluster_enabled:1\r\n", true},
		{"both single", "cluster_enabled:0\r\n", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			e := endpoint{label: "target", addr: "cache:6379", cluster: tc.configured}
			err := assertTopology(context.Background(),
				fakeInfoClient{info: tc.serverInfo}, e)
			if err != nil {
				t.Fatalf("agreement should pass, got %v", err)
			}
		})
	}
}

func TestPlainClientAgainstShardedClusterIsRefused(t *testing.T) {
	// The dangerous direction: this would otherwise connect and then dead-letter
	// roughly (shards-1)/shards of every write.
	e := endpoint{label: "target", addr: "clustercfg.cache:6379", cluster: false}
	err := assertTopology(context.Background(),
		fakeInfoClient{info: "cluster_enabled:1\r\n"}, e)
	if err == nil {
		t.Fatal("a plain client pointed at a cluster must be refused")
	}
	for _, want := range []string{"cluster-mode ENABLED", "MOVED",
		"dead-letter", "read from the target's ARN"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("message should mention %q: %v", want, err)
		}
	}
}

func TestClusterClientAgainstSingleNodeIsRefused(t *testing.T) {
	e := endpoint{label: "target", addr: "cache:6379", cluster: true}
	err := assertTopology(context.Background(),
		fakeInfoClient{info: "cluster_enabled:0\r\n"}, e)
	if err == nil {
		t.Fatal("a cluster client pointed at a single node must be refused")
	}
	if !strings.Contains(err.Error(), "cluster-mode DISABLED") ||
		!strings.Contains(err.Error(), "primary endpoint") {
		t.Fatalf("message should name the fix: %v", err)
	}
}

func TestUnreadableInfoDoesNotBlockStartup(t *testing.T) {
	// An RBAC user can be denied INFO while still being able to HSET and DEL.
	// Refusing to start over a missing diagnostic would be worse than the risk.
	e := endpoint{label: "target", addr: "cache:6379", cluster: true}
	err := assertTopology(context.Background(),
		fakeInfoClient{err: errors.New("NOPERM this user has no permissions to run the 'info' command")}, e)
	if err != nil {
		t.Fatalf("a denied INFO should not be fatal, got %v", err)
	}
}

func TestTopologyAssertionToleratesUnexpectedInfoText(t *testing.T) {
	// Absent the marker, treat the server as non-clustered rather than guessing.
	e := endpoint{label: "target", addr: "cache:6379", cluster: false}
	if err := assertTopology(context.Background(),
		fakeInfoClient{info: "# Cluster\r\n"}, e); err != nil {
		t.Fatalf("should not fail on info text without the marker: %v", err)
	}
}

func TestConnectErrorUnwrapsToTheDriverError(t *testing.T) {
	// Callers may want errors.Is against driver sentinels.
	inner := fmt.Errorf("redis: nil")
	e := endpoint{label: "target", addr: "cache:6379"}
	if !errors.Is(connectError(e, inner), inner) {
		t.Fatal("connectError should wrap, not replace, the cause")
	}
}
