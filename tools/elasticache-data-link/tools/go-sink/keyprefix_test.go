package main

import "testing"

func TestRowKeyPrefix(t *testing.T) {
	defer func() { keyPrefix = "" }()
	keyPrefix = normalizeKeyPrefix("")
	if got := rowKey("products", "7"); got != "products:7" {
		t.Fatalf("no prefix: %q", got)
	}
	for _, raw := range []string{"cx", "cx:", " cx:: "} {
		keyPrefix = normalizeKeyPrefix(raw)
		if got := rowKey("products", "7"); got != "cx:products:7" {
			t.Fatalf("prefix %q: %q", raw, got)
		}
	}
}
