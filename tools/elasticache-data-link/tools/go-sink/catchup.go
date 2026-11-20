package main

// Catching up: changes replayed from an older position (Jira story F043).
//
// WHAT HAPPENS
// The version guard orders changes by broker stream id (guard.go). Debezium
// sometimes reads the source log again from an older position: after a bulk
// load (it resumes from the offset committed before the pause, and the load
// already holds newer values), after a restart from a rolled-back offset, and
// after a broker snapshot restore. The replayed changes get new, larger
// stream ids, so the guard applies them, and until the replay has drained a
// cached row can show an older value than it held before. The cache ends
// right; it is behind during the replay. Ordering by source log position
// instead (decision D4) would skip them, but needs a comparable position from
// all four engines on every change and on every loaded row.
//
// WHAT THIS DOES
// Shows it. A change's source.ts_ms is when it was committed at the source; a
// live change reaches the broker within seconds of that. A change captured
// more than CATCHING_UP_SECONDS (default 60) after it was committed is a
// replay (or Debezium is that far behind). Every metric interval, the oldest
// and the newest change waiting in each table's stream are timed this way;
// a table with such a change is "catching up". It is published as
// CatchingUpTables and logged as WARN catching-up:<table>, and an INFO line
// says when it has caught up. A dead letter being replayed keeps its original
// version (ecdl_v), which is what it is timed by, so it does not count.
// With EVENT_FORMAT=flat there is no source.ts_ms and nothing is shown.

import (
	"fmt"
	"sort"
	"time"

	"github.com/redis/go-redis/v9"
)

var catchingUpSec = int64(atoiOr("CATCHING_UP_SECONDS", 60))

// replayAgeMs is how long after its commit at the source a waiting change was
// captured: its version time minus source.ts_ms. 0 when not known (no entry,
// no envelope, no source time, or a source clock ahead of the broker's).
func replayAgeMs(m *redis.XMessage) int64 {
	if m == nil || m.ID == "" {
		return 0
	}
	val, _ := m.Values["value"].(string)
	if val == "" {
		return 0
	}
	var evt struct {
		Source struct {
			TsMs int64 `json:"ts_ms"`
		} `json:"source"`
		Payload *struct {
			Source struct {
				TsMs int64 `json:"ts_ms"`
			} `json:"source"`
		} `json:"payload"`
	}
	if err := decodeJSON([]byte(val), &evt); err != nil {
		return 0
	}
	committed := evt.Source.TsMs
	if committed == 0 && evt.Payload != nil {
		committed = evt.Payload.Source.TsMs
	}
	captured := streamIDMs(msgVersion(*m))
	if committed <= 0 || captured <= committed {
		return 0
	}
	return captured - committed
}

// catchingUpLine describes a table catching up, or "" when it is not.
func catchingUpLine(r tableReading, limitSec int64) string {
	if r.replayMs <= limitSec*1000 {
		return ""
	}
	return fmt.Sprintf("applying changes committed up to %s before they were captured, "+
		"a replay from an older position (after a load, a restart from an older offset or a broker "+
		"restore). Its cached rows can show older values until the replay has drained",
		(time.Duration(r.replayMs) * time.Millisecond).Round(time.Second))
}

// catchingUp lists the tables catching up, sorted.
func catchingUp(readings []tableReading, limitSec int64) []string {
	var out []string
	for _, r := range readings {
		if line := catchingUpLine(r, limitSec); line != "" {
			out = append(out, r.table+": "+line)
		}
	}
	sort.Strings(out)
	return out
}

// noteCatchingUp logs each table catching up, and once when it has caught
// up. It returns how many are catching up.
func (m *freshnessMonitor) noteCatchingUp(readings []tableReading) int {
	now := map[string]bool{}
	for _, r := range readings {
		if line := catchingUpLine(r, catchingUpSec); line != "" {
			now[r.table] = true
			logWarn("catching-up:"+r.table, fmt.Errorf("%s", line))
		}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	for table := range m.catching {
		if !now[table] {
			fmt.Printf("  %s: caught up; no waiting change was captured more than %ds after its commit\n",
				table, catchingUpSec)
		}
	}
	m.catching = now
	return len(now)
}
