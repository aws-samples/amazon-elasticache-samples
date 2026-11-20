package main

import (
	"testing"

	"github.com/redis/go-redis/v9"
)

func TestIsTruncate(t *testing.T) { // Jira story F006
	msg := func(v string) redis.XMessage {
		return redis.XMessage{ID: "1-0", Values: map[string]interface{}{"key": "default", "value": v}}
	}
	cases := []struct {
		v, format, table string
		want             bool
	}{
		{`{"before":null,"after":null,"source":{"table":"orders"},"op":"t","ts_ms":1}`, "envelope", "orders", true},
		{`{"source": {"table": "orders"}, "op": "t"}`, "envelope", "orders", true},
		{`{"source":{"table":"orders"},"op":"c","after":{"id":1,"op":"t"}}`, "envelope", "", false},
		{`{"source":{"table":"orders"},"op":"t"}`, "flat", "", false},
		{`not json "op":"t"`, "envelope", "", false},
	}
	for _, c := range cases {
		got, table := isTruncate(msg(c.v), c.format)
		if got != c.want || table != c.table {
			t.Errorf("%s (%s): got (%v, %q), want (%v, %q)", c.v, c.format, got, table, c.want, c.table)
		}
	}
	if got, _ := isTruncate(redis.XMessage{Values: map[string]interface{}{}}, "envelope"); got {
		t.Error("a message with no value is a truncate")
	}
}

func TestEscapeGlob(t *testing.T) {
	if got := escapeGlob(`a*b?c[d]e\f:`); got != `a\*b\?c\[d\]e\\f:` {
		t.Fatalf("escapeGlob = %q", got)
	}
}

func TestTruncateMarkerKeyIsOutsideEveryRowPattern(t *testing.T) {
	saved := keyPrefix
	defer func() { keyPrefix = saved }()
	for _, p := range []string{"", "v2:"} {
		keyPrefix = p
		k := truncateMarkerKey("orders")
		if k[:len(p)] != p {
			t.Fatalf("marker %q is outside the link's prefix %q", k, p)
		}
		if rk := rowKey("orders", "1"); k[:len(p)+7] == rk[:len(p)+7] {
			t.Fatalf("marker %q starts like the row key %q", k, rk)
		}
	}
}
