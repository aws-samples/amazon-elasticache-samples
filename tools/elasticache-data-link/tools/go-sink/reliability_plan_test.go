package main

// Unit tests for planMessage: the single place that decides what a CDC event
// becomes on the target (HSET / DEL / full-replace) and, crucially, whether an
// event is "poison" (unprocessable) so the reliability layer dead-letters it
// instead of dropping it silently.
//
// planMessage is a pure function (no broker/target client), so these run with a
// bare `go test` and need no infrastructure. The retry/reclaim/DLQ *mechanics*
// that consume this decision (reclaimLoop, toDLQ, processOne side effects) drive
// a live broker+target, so they are not covered here.

import (
	"testing"

	"github.com/redis/go-redis/v9"
)

// msgWithValue builds a broker message carrying one CDC/flat payload string,
// the same shape Debezium's Redis sink writes into the stream.
func msgWithValue(value interface{}) redis.XMessage {
	return redis.XMessage{ID: "1-0", Values: map[string]interface{}{"value": value}}
}

// TestPlanMessageRouting covers the poison paths (must be dead-lettered) and the
// three good outcomes (HSET, DEL, full-replace) for both the CDC-envelope and
// the flat event formats. transformSpec is nil here, i.e. passthrough.
func TestPlanMessageRouting(t *testing.T) {
	transformSpec = nil
	t.Cleanup(func() { transformSpec = nil })

	cases := []struct {
		name        string
		format      string
		msg         redis.XMessage
		wantKey     string
		wantDelete  bool
		wantPoison  bool
		wantReplace bool
		wantFields  bool // expect non-nil field map (an HSET)
	}{
		// ---- poison: unprocessable, retrying cannot help, goes straight to DLQ ----
		{
			name:       "missing value field is poison",
			msg:        redis.XMessage{ID: "1-0", Values: map[string]interface{}{}},
			wantPoison: true,
		},
		{
			name:       "value that is not a string is poison",
			msg:        msgWithValue(42),
			wantPoison: true,
		},
		{
			name:       "unparseable CDC json is poison",
			msg:        msgWithValue("{not valid json"),
			wantPoison: true,
		},
		{
			name:       "create with no after image is poison",
			msg:        msgWithValue(`{"op":"c","source":{"table":"products"}}`),
			wantPoison: true,
		},
		{
			name:       "delete with no primary key is poison",
			msg:        msgWithValue(`{"op":"d","before":{},"source":{"table":"products"}}`),
			wantPoison: true,
		},

		// ---- good outcomes on the CDC envelope ----
		{
			// Review finding M10: the whole row, so the key is replaced.
			name:        "create replaces the key",
			msg:         msgWithValue(`{"op":"c","after":{"id":1,"name":"widget"},"source":{"table":"products"}}`),
			wantKey:     "products:1",
			wantFields:  true,
			wantReplace: true,
		},
		{
			// Review finding M10: the whole row, so the key is replaced.
			name:        "update replaces the key",
			msg:         msgWithValue(`{"op":"u","after":{"id":2,"name":"gadget"},"source":{"table":"products"}}`),
			wantKey:     "products:2",
			wantFields:  true,
			wantReplace: true,
		},
		{
			name:        "snapshot read becomes a full replace",
			msg:         msgWithValue(`{"op":"r","after":{"id":3,"name":"seed"},"source":{"table":"products"}}`),
			wantKey:     "products:3",
			wantFields:  true,
			wantReplace: true,
		},
		{
			name:       "delete becomes a DEL",
			msg:        msgWithValue(`{"op":"d","before":{"id":4},"source":{"table":"products"}}`),
			wantKey:    "products:4",
			wantDelete: true,
		},

		// ---- flat event format ----
		{
			name:       "flat row becomes an HSET",
			format:     "flat",
			msg:        msgWithValue(`{"id":5,"color":"red"}`),
			wantKey:    "products:5",
			wantFields: true,
		},
		{
			name:       "flat unparseable json is poison",
			format:     "flat",
			msg:        msgWithValue("{bad"),
			wantPoison: true,
		},
		{
			name:       "flat with no primary key is poison",
			format:     "flat",
			msg:        msgWithValue(`{"color":"red"}`),
			wantPoison: true,
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			key, fields, isDelete, poison, replace := planMessage(c.msg, c.format, "products")

			if poison != c.wantPoison {
				t.Fatalf("poison = %v, want %v", poison, c.wantPoison)
			}
			if poison {
				return // key/fields are undefined on the poison path
			}
			if key != c.wantKey {
				t.Errorf("key = %q, want %q", key, c.wantKey)
			}
			if isDelete != c.wantDelete {
				t.Errorf("isDelete = %v, want %v", isDelete, c.wantDelete)
			}
			if replace != c.wantReplace {
				t.Errorf("replace = %v, want %v", replace, c.wantReplace)
			}
			if (fields != nil) != c.wantFields {
				t.Errorf("fields non-nil = %v, want %v", fields != nil, c.wantFields)
			}
		})
	}
}

// TestPlanMessageFilteredRowBecomesDelete proves the safety-relevant transform
// rule: a row that no longer passes its filter must DELETE the cached key, not
// leave a stale HSET behind. A row that passes is kept as an HSET.
func TestPlanMessageFilteredRowBecomesDelete(t *testing.T) {
	spec, err := LoadTransformSpec([]byte(
		`{"tables":{"products":{"filter":{"field":"status","op":"=","value":"active"}}}}`))
	if err != nil {
		t.Fatalf("load spec: %v", err)
	}
	transformSpec = spec
	t.Cleanup(func() { transformSpec = nil })

	// status != active -> filtered out -> the key must be deleted from the cache.
	filtered := msgWithValue(`{"op":"u","after":{"id":7,"status":"inactive"},"source":{"table":"products"}}`)
	key, fields, isDelete, poison, _ := planMessage(filtered, "", "products")
	if poison {
		t.Fatalf("filtered row unexpectedly poison")
	}
	if !isDelete {
		t.Errorf("filtered row: isDelete = false, want true (stale key must be removed)")
	}
	if fields != nil {
		t.Errorf("filtered row: fields = %v, want nil", fields)
	}
	if key != "products:7" {
		t.Errorf("key = %q, want products:7", key)
	}

	// status == active -> kept as an HSET.
	kept := msgWithValue(`{"op":"u","after":{"id":8,"status":"active"},"source":{"table":"products"}}`)
	_, keptFields, keptDelete, keptPoison, _ := planMessage(kept, "", "products")
	if keptPoison || keptDelete || keptFields == nil {
		t.Errorf("active row should be a kept HSET: delete=%v poison=%v fieldsNil=%v",
			keptDelete, keptPoison, keptFields == nil)
	}
}

// TestPlanMessageMissingFilterFieldIsDeadLettered: review finding H2. A change
// without the filter's field (the source column renamed or dropped, or a TOAST
// placeholder removed) used to read "" and delete the row from the cache. It
// is now a dead letter: nothing is deleted and the change can be replayed.
func TestPlanMessageMissingFilterFieldIsDeadLettered(t *testing.T) {
	spec, err := LoadTransformSpec([]byte(
		`{"tables":{"products":{"filter":{"field":"status","op":"=","value":"active"}}}}`))
	if err != nil {
		t.Fatalf("load spec: %v", err)
	}
	transformSpec = spec
	t.Cleanup(func() { transformSpec = nil })

	for name, value := range map[string]string{
		"renamed column": `{"op":"u","after":{"id":7,"state":"active"},"source":{"table":"products"}}`,
		"placeholder":    `{"op":"u","after":{"id":7,"status":"__debezium_unavailable_value"},"source":{"table":"products"}}`,
		"snapshot read":  `{"op":"r","after":{"id":7,"state":"active"},"source":{"table":"products"}}`,
	} {
		_, fields, isDelete, poison, _ := planMessage(msgWithValue(value), "", "products")
		if !poison || isDelete || fields != nil {
			t.Errorf("%s: poison=%v delete=%v fields=%v, want a dead letter and no delete",
				name, poison, isDelete, fields)
		}
	}
	// Flat events take the same path.
	flat := redis.XMessage{ID: "1-0", Values: map[string]interface{}{
		"value": `{"id":7,"state":"active"}`, "key": `{"id":7}`}}
	if _, _, isDelete, poison, _ := planMessage(flat, "flat", "products"); !poison || isDelete {
		t.Errorf("flat: poison=%v delete=%v, want a dead letter", poison, isDelete)
	}
	// NULL is a value: the field is present, so the filter decides (and drops it).
	null := msgWithValue(`{"op":"u","after":{"id":7,"status":null},"source":{"table":"products"}}`)
	if _, _, isDelete, poison, _ := planMessage(null, "", "products"); poison || !isDelete {
		t.Errorf("null status: poison=%v delete=%v, want a filtered delete", poison, isDelete)
	}
	// The dead letter names the cause, so `dlq list` says what to fix.
	if table, field := missingFilterFieldOf(`{"op":"u","after":{"id":7,"state":"x"},"source":{"table":"products"}}`); table != "products" || field != "status" {
		t.Errorf("missingFilterFieldOf = %q %q, want products status", table, field)
	}
	if _, field := missingFilterFieldOf(`not json`); field != "" {
		t.Errorf("unparseable: field %q, want none", field)
	}
	// A deletion needs no filter.
	del := msgWithValue(`{"op":"d","before":{"id":7},"source":{"table":"products"}}`)
	if _, _, isDelete, poison, _ := planMessage(del, "", "products"); poison || !isDelete {
		t.Errorf("delete: poison=%v delete=%v", poison, isDelete)
	}
}
