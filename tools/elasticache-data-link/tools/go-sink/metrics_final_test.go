package main

import "testing"

// Review finding M2: the counters of the last interval are published once
// more at shutdown, and a sink without metrics shuts down as before.
func TestFinalMetricsAreFlushedAtShutdown(t *testing.T) {
	t.Cleanup(func() { setFinalMetrics(nil) })
	setFinalMetrics(nil)
	flushFinalMetrics() // metrics off: nothing to do, no panic

	calls := 0
	setFinalMetrics(func() { calls++ })
	flushFinalMetrics()
	if calls != 1 {
		t.Fatalf("final publish ran %d times, want 1", calls)
	}
}
