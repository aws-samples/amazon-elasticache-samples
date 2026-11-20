package main

// One link, N tables, one sink service.
//
// A link has a table list, Debezium routes each table to its own broker stream,
// and this file works out which streams this sink must consume and which table
// each one carries.
//
// WHAT WAS WRONG. worker() took a single `stream string` and read
// `Streams: []string{stream, ">"}`. STREAM_KEYS (plural) existed but was only
// honoured on the denormalization path, so a link with three tables consumed the
// first stream and left the other two accumulating in the broker forever --
// silently, because the sink reported itself healthy and its own metrics only
// counted what it did read. Running one sink service per table was the
// workaround, which multiplies tasks, consumer groups and cost by the table count.
//
// WHY A TABLE NAME IS CARRIED ALONGSIDE EACH STREAM. With EVENT_FORMAT=envelope
// (the shipped default) each message names its own table in `source.table`, so
// the mapping is not needed to route a write. It IS needed for:
//   - EVENT_FORMAT=flat, where there is no envelope and therefore no table
//   - the per-table freshness heartbeat
//   - naming things in diagnostics
// So the table is derived from the stream name and can be overridden explicitly.

import (
	"fmt"
	"strings"
)

// streamSpec is one broker stream and the source table whose changes it carries.
type streamSpec struct {
	key   string // broker stream, e.g. data:checkout.Inventory.orders
	table string // source table, e.g. orders
}

// tableFromStream recovers the table name from a stream name.
//
// Debezium composes a topic as <prefix>.<...>.<table> and the RegexRouter in
// application.properties rewrites it to data:<topic>, so the table is always the
// last dot-separated segment. That holds for all four engines even though the
// number of segments differs (MySQL 3, PostgreSQL 3, SQL Server 4, Oracle 2) --
// which is exactly why this reads the LAST segment rather than counting from the
// front. ecdl_link/resolve.py composes these names from the source ARN using the
// same rule, so the two sides cannot disagree.
//
// Returns "" when there is nothing to take, so the caller can report an error rather
// than proceeding with an empty table name and writing keys called ":123".
func tableFromStream(stream string) string {
	s := strings.TrimSpace(stream)
	if i := strings.LastIndex(s, "."); i >= 0 {
		s = s[i+1:]
	} else if i := strings.LastIndex(s, ":"); i >= 0 {
		// No dots at all: a bare "data:orders" style name, as the run_*.sh
		// benchmark harnesses use.
		s = s[i+1:]
	}
	return strings.TrimSpace(s)
}

// parseStreams resolves the streams this sink consumes, from the environment.
//
// In precedence order:
//
//	STREAMS      "s1=table1,s2,s3=table3"  -- the form the template uses. An entry
//	                                          without "=" has its table derived.
//	STREAM_KEYS  "s1,s2"                   -- older multi-stream form, tables derived
//	STREAM_KEY   + STREAM_TABLE            -- original single-table form
//
// The older two are still honoured because benchmark harnesses and manually run
// containers set them, and silently ignoring a variable someone has set is worse
// than supporting it.
func parseStreams(getenv func(string) string) ([]streamSpec, error) {
	if raw := strings.TrimSpace(getenv("STREAMS")); raw != "" {
		return parseStreamList(raw, "STREAMS")
	}
	if raw := strings.TrimSpace(getenv("STREAM_KEYS")); raw != "" {
		return parseStreamList(raw, "STREAM_KEYS")
	}
	key := strings.TrimSpace(getenv("STREAM_KEY"))
	if key == "" {
		return nil, fmt.Errorf("no streams configured: set STREAMS to a comma-separated list " +
			"of the broker streams this link's tables are written to, for example " +
			"STREAMS=data:mylink.Inventory.orders,data:mylink.Inventory.customers " +
			"(each entry may be written stream=table to name the table explicitly)")
	}
	table := strings.TrimSpace(getenv("STREAM_TABLE"))
	if table == "" {
		table = tableFromStream(key)
	}
	if table == "" {
		return nil, fmt.Errorf("STREAM_KEY=%q has no table name in it and STREAM_TABLE is not set", key)
	}
	return []streamSpec{{key: key, table: table}}, nil
}

// parseStreamList parses "s1=table1,s2,s3=table3".
//
// A duplicate stream is a hard error, not a de-duplication: two entries for one
// stream means the operator believes something about this link that is not true
// (most likely two tables whose topics collide), and quietly dropping one would
// hide it.
func parseStreamList(raw, varName string) ([]streamSpec, error) {
	specs := make([]streamSpec, 0, 4)
	seen := make(map[string]bool)
	for _, item := range strings.Split(raw, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue // trailing comma, or a list built by string concatenation
		}
		key, table := item, ""
		if i := strings.Index(item, "="); i >= 0 {
			key = strings.TrimSpace(item[:i])
			table = strings.TrimSpace(item[i+1:])
		}
		if key == "" {
			return nil, fmt.Errorf("%s contains an entry with no stream name: %q", varName, item)
		}
		if table == "" {
			table = tableFromStream(key)
		}
		if table == "" {
			return nil, fmt.Errorf("%s entry %q: cannot determine the table name. Debezium names a "+
				"topic <prefix>.<...>.<table>, so a stream should end in the table name; "+
				"write it as %s=<table> to say so explicitly", varName, item, key)
		}
		if seen[key] {
			return nil, fmt.Errorf("%s lists stream %q twice", varName, key)
		}
		seen[key] = true
		specs = append(specs, streamSpec{key: key, table: table})
	}
	if len(specs) == 0 {
		return nil, fmt.Errorf("%s is set but contains no usable entries: %q", varName, raw)
	}
	return specs, nil
}

// tableFor returns the table carried by a stream, and whether it is one this
// sink was configured to consume.
func tableFor(specs []streamSpec, stream string) (string, bool) {
	for _, s := range specs {
		if s.key == stream {
			return s.table, true
		}
	}
	return "", false
}

// streamKeys is the plain list of stream names, for XREADGROUP and XGROUP CREATE.
func streamKeysOf(specs []streamSpec) []string {
	out := make([]string, 0, len(specs))
	for _, s := range specs {
		out = append(out, s.key)
	}
	return out
}

// readArgs builds the XREADGROUP Streams argument for N streams:
// [s1, s2, ..., ">", ">", ...]. Redis requires every stream name first and then
// one id per stream, not interleaved pairs.
func readArgs(specs []streamSpec) []string {
	arg := make([]string, 0, len(specs)*2)
	for _, s := range specs {
		arg = append(arg, s.key)
	}
	for range specs {
		arg = append(arg, ">")
	}
	return arg
}

// describeStreams is the startup line, so the log says exactly which streams
// this task consumes and which table each carries.
func describeStreams(specs []streamSpec) string {
	parts := make([]string, 0, len(specs))
	for _, s := range specs {
		parts = append(parts, fmt.Sprintf("%s -> %s:*", s.key, s.table))
	}
	return strings.Join(parts, ", ")
}
