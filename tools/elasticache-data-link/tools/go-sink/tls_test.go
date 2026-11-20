package main

import (
	"crypto/tls"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Certificates are verified: the config never skips verification, never pins a
// single ServerName (a cluster client dials each node by its own name), and
// refuses anything older than TLS 1.2.
func TestTLSConfigVerifiesCertificates(t *testing.T) {
	conf, err := tlsConfig(endpoint{label: "target", addr: "clustercfg.x:6379", tls: true}, "")
	if err != nil {
		t.Fatal(err)
	}
	if conf == nil {
		t.Fatal("TLS endpoint got no TLS config")
	}
	if conf.InsecureSkipVerify {
		t.Error("certificate verification is skipped")
	}
	if conf.MinVersion != tls.VersionTLS12 {
		t.Errorf("MinVersion = %x, want TLS 1.2", conf.MinVersion)
	}
	if conf.ServerName != "" {
		t.Errorf("ServerName pinned to %q; each node must be verified by its own name", conf.ServerName)
	}
	if conf.RootCAs != nil {
		t.Error("RootCAs set without TLS_CA_FILE; the system bundle should be used")
	}
}

func TestTLSConfigOffWhenEndpointIsPlain(t *testing.T) {
	conf, err := tlsConfig(endpoint{label: "broker", addr: "b:6379", tls: false}, "")
	if err != nil || conf != nil {
		t.Fatalf("plain endpoint: conf=%v err=%v, want nil,nil", conf, err)
	}
}

func TestTLSConfigRejectsUnreadableOrEmptyCAFile(t *testing.T) {
	e := endpoint{label: "target", addr: "t:6379", tls: true}
	if _, err := tlsConfig(e, filepath.Join(t.TempDir(), "missing.pem")); err == nil ||
		!strings.Contains(err.Error(), "TLS_CA_FILE") {
		t.Errorf("missing CA file: err=%v, want a TLS_CA_FILE error", err)
	}
	empty := filepath.Join(t.TempDir(), "empty.pem")
	if err := os.WriteFile(empty, []byte("not a certificate"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := tlsConfig(e, empty); err == nil || !strings.Contains(err.Error(), "no PEM") {
		t.Errorf("empty CA file: err=%v, want a 'no PEM' error", err)
	}
}
