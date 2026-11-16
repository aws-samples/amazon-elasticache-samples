package main

// Connecting to the broker and the target, with authentication.
//
// Before this existed the sink read only host, port, TLS and cluster-mode, so a
// cache with an AUTH token or RBAC enabled was unusable -- there was no
// field to put the credential in. The control plane collected an auth token and
// stored it, which was misleading, because it looked supported.
//
// It also connected without checking: `target.Ping(ctx)` discarded its error and
// the next line printed "Target connected". A wrong password produced a sink that
// reported itself healthy and then failed every single write, filling the
// dead-letter stream. Every failure here is now fatal at startup and says which
// of the plausible causes it is.

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"
	"strings"
	"time"

	json "github.com/goccy/go-json"
	"github.com/redis/go-redis/v9"
)

// Auth modes, matching what the control plane detects from the cache itself:
//
//	none   security group only
//	token  a password with no username (ElastiCache "auth token")
//	rbac   a username and password (Valkey/Redis 6+ user)
const (
	authNone  = "none"
	authToken = "token"
	authRBAC  = "rbac"
)

// credential is a resolved username/password pair plus where it came from.
// `origin` is safe to log; the values never are.
type credential struct {
	username string
	password string
	origin   string
}

func (c credential) mode() string {
	switch {
	case c.password == "":
		return authNone
	case c.username == "":
		return authToken
	default:
		return authRBAC
	}
}

// loadCredential reads a credential for a prefix ("TARGET" or "BROKER").
//
// Three inputs, in precedence order:
//
//	<prefix>_USERNAME / <prefix>_PASSWORD  explicit, for local runs and tests
//	<prefix>_SECRET                        the whole Secrets Manager value
//	nothing                                no auth
//
// The single-variable form matters: an ElastiCache auth token is stored as a
// plaintext secret while an RBAC user is stored as JSON, and ECS can only select
// a JSON key from a secret that has one. Injecting the whole value and deciding
// here handles both shapes without the template needing to know which it has.
func loadCredential(prefix string) (credential, error) {
	user := strings.TrimSpace(os.Getenv(prefix + "_USERNAME"))
	pass := os.Getenv(prefix + "_PASSWORD")
	if pass != "" || user != "" {
		return credential{username: user, password: pass,
			origin: prefix + "_USERNAME/" + prefix + "_PASSWORD"}, nil
	}

	raw := strings.TrimSpace(os.Getenv(prefix + "_SECRET"))
	if raw == "" {
		return credential{origin: "none configured"}, nil
	}

	// A JSON object is an RBAC user. Anything else is the token itself -- note
	// a bare token could legitimately contain characters that look like JSON,
	// so only a successful decode into the expected shape counts.
	if strings.HasPrefix(raw, "{") {
		var parsed struct {
			Username string `json:"username"`
			Password string `json:"password"`
		}
		if err := json.Unmarshal([]byte(raw), &parsed); err == nil &&
			parsed.Password != "" {
			return credential{
				username: strings.TrimSpace(parsed.Username),
				password: parsed.Password,
				origin:   prefix + "_SECRET (JSON username/password)",
			}, nil
		}
		if err := json.Unmarshal([]byte(raw), &parsed); err == nil &&
			parsed.Password == "" {
			return credential{}, fmt.Errorf(
				"%s_SECRET is JSON but has no \"password\" field", prefix)
		}
	}
	return credential{password: raw,
		origin: prefix + "_SECRET (plaintext auth token)"}, nil
}

// endpoint is everything needed to open one connection.
type endpoint struct {
	label    string // "broker" or "target", used in messages
	addr     string
	tls      bool
	cluster  bool
	poolSize int
	cred     credential
}

func (e endpoint) describe() string {
	return fmt.Sprintf("%s %s (TLS=%v, cluster=%v, auth=%s)",
		e.label, e.addr, e.tls, e.cluster, e.cred.mode())
}

// tlsConfig verifies the server certificate and its hostname, TLS 1.2 or newer.
//
// ElastiCache presents an Amazon-issued wildcard certificate for the cluster's
// own domain (*.<cluster>.<id>.<region>.cache.amazonaws.com), which covers the
// configuration endpoint, the primary endpoint and every node address the
// cluster hands back, so normal verification succeeds. ServerName is left
// empty on purpose: crypto/tls then takes it from each address dialled, which
// is what a cluster client needs when it connects to individual nodes.
//
// caFile adds a private CA (a self-hosted Valkey with its own certificates).
// There is deliberately no option to skip verification.
func tlsConfig(e endpoint, caFile string) (*tls.Config, error) {
	if !e.tls {
		return nil, nil
	}
	conf := &tls.Config{MinVersion: tls.VersionTLS12}
	if caFile != "" {
		pem, err := os.ReadFile(caFile)
		if err != nil {
			return nil, fmt.Errorf("%s: TLS_CA_FILE %s: %w", e.label, caFile, err)
		}
		pool, err := x509.SystemCertPool()
		if err != nil || pool == nil {
			pool = x509.NewCertPool()
		}
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("%s: TLS_CA_FILE %s holds no PEM certificate", e.label, caFile)
		}
		conf.RootCAs = pool
	}
	return conf, nil
}

// Pooled connections that sat idle are where a write after a quiet period
// failed: the end-to-end test saw "EOF" on the first write after a pause, and
// that change reached the cache only 30-45 s later, through reclaimLoop. go-redis
// keeps an idle connection for 30 minutes by default, long past the point where
// the server or the network may have dropped it. Closing it after a minute
// means a write after a pause dials a fresh connection instead of finding a
// dead one. applyBatch also retries a failed write at once (writeRetries), so a
// connection that dies anyway costs milliseconds, not the reclaim interval.
var (
	connMaxIdle   = time.Duration(atoiOr("CONN_MAX_IDLE_SEC", 60)) * time.Second
	driverRetries = atoiOr("DRIVER_MAX_RETRIES", 3)
)

// dial opens a client and proves it works before returning.
//
// Returns an error rather than a client that might not be usable. The caller is
// expected to treat that as fatal: a sink that cannot reach its target has
// nothing useful to do, and continuing would dead-letter every message it read.
func dial(ctx context.Context, e endpoint) (redis.UniversalClient, error) {
	tlsConf, err := tlsConfig(e, os.Getenv("TLS_CA_FILE"))
	if err != nil {
		return nil, err
	}

	var client redis.UniversalClient
	if e.cluster {
		client = redis.NewClusterClient(&redis.ClusterOptions{
			Addrs:           []string{e.addr},
			Username:        e.cred.username,
			Password:        e.cred.password,
			PoolSize:        e.poolSize,
			ReadTimeout:     10 * time.Second,
			WriteTimeout:    10 * time.Second,
			DialTimeout:     5 * time.Second,
			ConnMaxIdleTime: connMaxIdle,
			MaxRetries:      driverRetries,
			TLSConfig:       tlsConf,
		})
	} else {
		client = redis.NewClient(&redis.Options{
			Addr:            e.addr,
			Username:        e.cred.username,
			Password:        e.cred.password,
			PoolSize:        e.poolSize,
			ReadTimeout:     10 * time.Second,
			WriteTimeout:    10 * time.Second,
			DialTimeout:     5 * time.Second,
			ConnMaxIdleTime: connMaxIdle,
			MaxRetries:      driverRetries,
			TLSConfig:       tlsConf,
		})
	}

	pingCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	if err := client.Ping(pingCtx).Err(); err != nil {
		client.Close()
		return nil, connectError(e, err)
	}
	if err := assertTopology(pingCtx, client, e); err != nil {
		client.Close()
		return nil, err
	}
	return client, nil
}

// assertTopology refuses to run if the cluster-mode flag contradicts the server.
//
// One direction already fails with a clear error on its own: a cluster client sends
// CLUSTER SLOTS and a single node rejects it. The other direction is the
// dangerous one and is why this check exists -- a PLAIN client pointed at a
// cluster-mode-enabled configuration endpoint connects perfectly, PINGs
// perfectly, and then returns MOVED for every key that does not happen to live
// on the node it connected to. Plain clients do not follow MOVED, so with three
// shards roughly two thirds of writes fail, get retried, and end up in the
// dead-letter stream. Partial silent loss, reported as a healthy sink.
//
// Cheaper to detect than to debug: `INFO cluster` reports cluster_enabled on
// both topologies.
// infoReader is the only capability assertTopology needs. Narrowing it keeps the
// check testable without faking the whole client interface.
type infoReader interface {
	Info(ctx context.Context, sections ...string) *redis.StringCmd
}

func assertTopology(ctx context.Context, client infoReader, e endpoint) error {
	info, err := client.Info(ctx, "cluster").Result()
	if err != nil {
		// Not fatal. INFO can be restricted by an RBAC user that is still
		// perfectly able to run HSET and DEL, and refusing to start over a
		// missing diagnostic would be worse than the risk it guards against.
		fmt.Printf("  note: could not verify %s topology (%v); "+
			"continuing with cluster=%v as configured\n", e.label, err, e.cluster)
		return nil
	}

	serverClustered := strings.Contains(info, "cluster_enabled:1")
	if serverClustered == e.cluster {
		return nil
	}

	if serverClustered && !e.cluster {
		return fmt.Errorf(
			"%s at %s is cluster-mode ENABLED but this side is configured as a "+
				"single node. A plain client connects to a sharded cluster and "+
				"then fails MOVED on most keys, which would dead-letter the "+
				"majority of writes rather than fail outright. Cluster mode is "+
				"read from the target's ARN, so this usually means it was set "+
				"manually or the endpoint is not the configuration endpoint",
			e.label, e.addr)
	}
	return fmt.Errorf(
		"%s at %s is cluster-mode DISABLED but this side is configured for a "+
			"cluster. Use the primary endpoint, and let cluster mode be read from "+
			"the ARN", e.label, e.addr)
}

// connectError turns a driver error into something an operator can act on.
//
// The distinction that matters most is "the cache wants a credential and got
// none" versus "the credential was rejected" versus "we cannot reach it at all".
// Those have completely different fixes, and the raw driver text does not make
// clear which one applies.
func connectError(e endpoint, err error) error {
	text := err.Error()
	switch {
	case strings.Contains(text, "NOAUTH"),
		strings.Contains(text, "Authentication required"):
		return fmt.Errorf(
			"%s at %s requires authentication but none was supplied "+
				"(credential: %s). Set the link's %s.secret_arn so the task is "+
				"given one: %w",
			e.label, e.addr, e.cred.origin, e.label, err)

	case strings.Contains(text, "WRONGPASS"),
		strings.Contains(text, "invalid username-password"),
		strings.Contains(text, "invalid password"),
		strings.Contains(text, "user is disabled"):
		return fmt.Errorf(
			"%s at %s rejected the credential from %s (auth mode %s). For RBAC "+
				"the secret needs both a username and a password, and the user "+
				"must be enabled and permitted to run HSET and DEL: %w",
			e.label, e.addr, e.cred.origin, e.cred.mode(), err)

	case strings.Contains(text, "without any password configured"),
		strings.Contains(text, "no password is set"):
		return fmt.Errorf(
			"%s at %s has no authentication configured, but a credential was "+
				"supplied from %s. Remove the secret reference: %w",
			e.label, e.addr, e.cred.origin, err)

	case strings.Contains(text, "tls:"),
		strings.Contains(text, "certificate"),
		strings.Contains(text, "handshake"):
		return fmt.Errorf(
			"%s at %s failed the TLS handshake (TLS=%v). This usually means the "+
				"cluster's in-transit encryption setting and this side disagree: %w",
			e.label, e.addr, e.tls, err)

	case strings.Contains(text, "CLUSTER"),
		strings.Contains(text, "cluster support disabled"),
		strings.Contains(text, "This instance has cluster support disabled"):
		return fmt.Errorf(
			"%s at %s is not a cluster, but this side is configured for cluster "+
				"mode. cluster mode is detected from the ARN and should not be "+
				"set manually: %w", e.label, e.addr, err)

	case strings.Contains(text, "i/o timeout"),
		strings.Contains(text, "connection refused"),
		strings.Contains(text, "no such host"),
		strings.Contains(text, "context deadline exceeded"):
		return fmt.Errorf(
			"%s at %s is unreachable. Check that the task runs in the same VPC "+
				"and that the %s security group allows the task's security "+
				"group on its port: %w", e.label, e.addr, e.label, err)
	}
	return fmt.Errorf("%s at %s: %w", e.label, e.addr, err)
}
