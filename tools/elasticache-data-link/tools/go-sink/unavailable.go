package main

// Unavailable values: columns Debezium could not read, sent as a placeholder.
//
// WHAT WAS WRONG
// PostgreSQL does not put an unchanged TOASTed column (a large text, jsonb or
// bytea value, stored out of line) into the WAL record of an UPDATE, so
// Debezium has no value for it and sends the placeholder
// "__debezium_unavailable_value" instead. Oracle does the same for an
// unchanged LOB when lob.enabled=true. The sink wrote that string over the
// cached value: one UPDATE to a small column of a row replaced its large
// columns with the placeholder, and nothing corrected the key until the large
// column itself changed.
//
// THE FIX
// A field whose value is the placeholder is left out of the write, so the
// HSET touches only the fields Debezium actually sent and the cache keeps the
// value it already holds for the rest. That is the right answer for an update:
// the column did not change. For a create or a snapshot read the placeholder
// should never appear (both carry the whole row), so it is also left out, the
// write becomes a merge instead of a replace (a replace would delete the cached
// value), and a WARN names the table and the field.
//
// The placeholder is Debezium's unavailable.value.placeholder. Both
// application.properties and this sink read UNAVAILABLE_VALUE_PLACEHOLDER, so
// one variable changes both; the default is Debezium's own.
//
// Debezium sends the placeholder in three shapes: the string itself (text,
// jsonb, CLOB), its bytes as base64 for a binary column (bytea, BLOB: the JSON
// converter base64-encodes bytes), and, for an array column, a list holding
// it. All three are matched.

import (
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"
)

const defaultUnavailablePlaceholder = "__debezium_unavailable_value"

// The placeholder in its string and binary (base64) forms.
var unavailableText, unavailableBinary = unavailableForms(
	envOr("UNAVAILABLE_VALUE_PLACEHOLDER", defaultUnavailablePlaceholder))

// unavailableForms returns the placeholder as a text column carries it and as
// a binary column carries it. Debezium reads a placeholder starting "hex:" as
// hex-encoded bytes for binary columns.
func unavailableForms(placeholder string) (string, string) {
	if placeholder == "" {
		placeholder = defaultUnavailablePlaceholder
	}
	raw := []byte(placeholder)
	if strings.HasPrefix(placeholder, "hex:") {
		if b, err := hex.DecodeString(placeholder[len("hex:"):]); err == nil {
			raw = b
		}
	}
	return placeholder, base64.StdEncoding.EncodeToString(raw)
}

// isUnavailable reports whether a decoded event value is the placeholder.
func isUnavailable(v interface{}) bool {
	switch x := v.(type) {
	case string:
		return x == unavailableText || x == unavailableBinary
	case []interface{}:
		if len(x) == 0 {
			return false
		}
		for _, e := range x {
			if !isUnavailable(e) {
				return false
			}
		}
		return true
	}
	return false
}

// dropUnavailable returns the row without its placeholder fields, and the
// names of the fields it removed (sorted). The row is returned unchanged, not
// copied, when there are none: almost every event.
func dropUnavailable(row map[string]interface{}) (map[string]interface{}, []string) {
	var dropped []string
	for k, v := range row {
		if isUnavailable(v) {
			dropped = append(dropped, k)
		}
	}
	if len(dropped) == 0 {
		return row, nil
	}
	sort.Strings(dropped)
	out := make(map[string]interface{}, len(row)-len(dropped))
	for k, v := range row {
		if !isUnavailable(v) {
			out[k] = v
		}
	}
	return out, dropped
}

// warnUnavailable logs a placeholder in an event that should carry the whole
// row. An update is the expected case and is not logged.
func warnUnavailable(table, op string, fields []string) {
	if len(fields) == 0 || (op != "c" && op != "r") {
		return
	}
	what := map[string]string{"c": "an insert", "r": "a snapshot read"}[op]
	logWarn("unavailable-value:"+table, fmt.Errorf(
		"%s of %s carried Debezium's unavailable-value placeholder for %s; those "+
			"field(s) were not written, so the cache keeps what it had (nothing, for a "+
			"new row). Check the source's REPLICA IDENTITY (PostgreSQL) or "+
			"lob.enabled (Oracle)", what, table, strings.Join(fields, ", ")))
}
