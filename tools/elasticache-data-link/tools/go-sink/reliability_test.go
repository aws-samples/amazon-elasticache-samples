package main

import "testing"

// safeTrimMinID must NEVER return an id that could evict a pending or an
// undelivered entry. XTRIM MINID keeps ids >= the boundary, so the returned id
// is always <= lastDelivered (never an undelivered id) and <= the
// oldest pending (never past an unacked entry).
func TestSafeTrimMinID(t *testing.T) {
	cases := []struct {
		name           string
		oldestPending  string
		lastDelivered  string
		want           string
	}{
		{
			// Pending exists: keep from the oldest pending id onward, so no unacked
			// entry is ever trimmed even if newer entries are already acked.
			name: "pending wins", oldestPending: "100-0", lastDelivered: "500-0", want: "100-0",
		},
		{
			// Nothing pending: everything up to lastDelivered is acked, so keep
			// lastDelivered (and any undelivered entries newer than it).
			name: "no pending keeps last delivered", oldestPending: "", lastDelivered: "500-0", want: "500-0",
		},
		{
			// Fully caught up (delivered == generated, nothing pending): trim
			// everything below the head; all leftover entries are removed.
			name: "caught up", oldestPending: "", lastDelivered: "9999-0", want: "9999-0",
		},
		{
			// Nothing delivered yet: nothing is safely known, so do not trim.
			name: "empty group position", oldestPending: "", lastDelivered: "0-0", want: "",
		},
		{
			name: "no group at all", oldestPending: "", lastDelivered: "", want: "",
		},
		{
			// Pending older than lastDelivered (the normal case) is honoured.
			name: "old pending", oldestPending: "42-0", lastDelivered: "100000-5", want: "42-0",
		},
		{
			// Read after last-delivered, the oldest pending can be NEWER (entries
			// delivered in between): the lower of the two is kept.
			name: "pending newer than last delivered", oldestPending: "600-0", lastDelivered: "500-0", want: "500-0",
		},
		{
			name: "same ms, pending lower seq", oldestPending: "500-1", lastDelivered: "500-3", want: "500-1",
		},
		{
			name: "equal", oldestPending: "500-0", lastDelivered: "500-0", want: "500-0",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := safeTrimMinID(c.oldestPending, c.lastDelivered); got != c.want {
				t.Errorf("safeTrimMinID(%q, %q) = %q, want %q",
					c.oldestPending, c.lastDelivered, got, c.want)
			}
		})
	}
}

// The boundary must always be <= lastDelivered when it returns a value, which is
// the property that makes it impossible to trim an undelivered entry (those have
// ids strictly greater than lastDelivered).
func TestSafeTrimMinIDNeverExceedsLastDelivered(t *testing.T) {
	cases := []struct{ oldestPending, lastDelivered string }{
		{"100-0", "500-0"},
		{"", "500-0"},
		{"1-0", "2-0"},
	}
	for _, c := range cases {
		got := safeTrimMinID(c.oldestPending, c.lastDelivered)
		if got == "" {
			continue
		}
		// Compare as stream ids: same ms with lower/equal seq, or lower ms.
		if !streamIDLessOrEqual(got, c.lastDelivered) {
			t.Errorf("safeTrimMinID(%q,%q)=%q exceeds lastDelivered %q",
				c.oldestPending, c.lastDelivered, got, c.lastDelivered)
		}
	}
}

// streamIDLessOrEqual is a test helper comparing "ms-seq" stream ids.
func streamIDLessOrEqual(a, b string) bool {
	am, as := splitID(a)
	bm, bs := splitID(b)
	if am != bm {
		return am < bm
	}
	return as <= bs
}

func splitID(id string) (int64, int64) {
	var ms, seq int64
	n := 0
	for i := 0; i < len(id); i++ {
		if id[i] == '-' {
			n = i
			break
		}
	}
	for i := 0; i < n; i++ {
		ms = ms*10 + int64(id[i]-'0')
	}
	for i := n + 1; i < len(id); i++ {
		seq = seq*10 + int64(id[i]-'0')
	}
	return ms, seq
}
