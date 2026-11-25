package main

// Shared transform engine (CDC path). Mirrors data-plane/bulk-loader/transforms.py.
// Processing order per row: filter (on original fields) -> computed -> column
// selection -> rename. Both engines must pass data-plane/transforms-fixtures.json
// and tools/go-sink/testdata/transform_cases.json (numbers, expressions, filters).

import (
	"fmt"
	"strings"

	json "github.com/goccy/go-json"
)

type FilterSpec struct {
	Field string `json:"field"`
	Op    string `json:"op"`
	Value string `json:"value"`
}

type ComputedField struct {
	Name string `json:"name"`
	Expr string `json:"expr"`
}

// DenormRule embeds selected columns of a parent row onto a child record.
// The parent must be materialized in the cache as <ParentKeyPrefix><parent_pk>.
type DenormRule struct {
	ParentTable     string   `json:"parent_table"`
	JoinField       string   `json:"join_field"`       // FK column on the child (original row)
	ParentKeyPrefix string   `json:"parent_key_prefix"` // e.g. "customers:"
	Fields          []string `json:"fields"`            // parent columns to embed
	Prefix          string   `json:"prefix"`            // output field prefix, e.g. "cust_"
	OnParentDelete  string   `json:"on_parent_delete"`  // "null" | "leave"
}

type TableSpec struct {
	Columns     []string          `json:"columns"`
	Rename      map[string]string `json:"rename"`
	Computed    []ComputedField   `json:"computed"`
	Filter      *FilterSpec       `json:"filter"`
	Denormalize []DenormRule      `json:"denormalize"`
}

// ChildDenorm links a child table to a denorm rule that pulls from a given parent.
type ChildDenorm struct {
	ChildTable string
	Rule       DenormRule
}

// DenormFor returns the denorm rules for a child table (Phase 1: embed lookups).
func (s *TransformSpec) DenormFor(table string) []DenormRule {
	if s == nil {
		return nil
	}
	return s.Tables[table].Denormalize
}

// ChildrenOf returns, for a parent table, every (child table, rule) that embeds
// from it (Phase 2: parent-update fan-out targets).
func (s *TransformSpec) ChildrenOf(parentTable string) []ChildDenorm {
	if s == nil {
		return nil
	}
	var out []ChildDenorm
	for child, ts := range s.Tables {
		for _, r := range ts.Denormalize {
			if r.ParentTable == parentTable {
				out = append(out, ChildDenorm{ChildTable: child, Rule: r})
			}
		}
	}
	return out
}

// EmbedParentFields returns the prefixed parent fields to merge onto a child,
// given the parent row (as fetched from the cache). Missing parent fields embed
// as empty string so the child always has the expected keys.
func EmbedParentFields(rule DenormRule, parent map[string]string) map[string]string {
	out := make(map[string]string, len(rule.Fields))
	for _, f := range rule.Fields {
		out[rule.Prefix+f] = parent[f] // "" if absent
	}
	return out
}

type TransformSpec struct {
	Tables map[string]TableSpec `json:"tables"`
}

// LoadTransformSpec parses the JSON spec. Empty input -> nil spec (passthrough).
func LoadTransformSpec(b []byte) (*TransformSpec, error) {
	if len(strings.TrimSpace(string(b))) == 0 {
		return nil, nil
	}
	var s TransformSpec
	if err := json.Unmarshal(b, &s); err != nil {
		return nil, err
	}
	if err := checkSpec(&s); err != nil {
		return nil, err
	}
	return &s, nil
}

// Apply returns the output fields and whether to keep the row. keep==false means
// the row must not be in the cache (loader skips; sink DELETEs the key).
// With a nil spec or no table entry, it's a passthrough (keep all fields).
func (s *TransformSpec) Apply(table string, row map[string]string) (map[string]string, bool) {
	if s == nil {
		return row, true
	}
	ts, ok := s.Tables[table]
	if !ok {
		return row, true
	}
	if ts.Filter != nil && !evalFilter(ts.Filter, row) {
		return nil, false
	}
	computed := make(map[string]string, len(ts.Computed))
	for _, cf := range ts.Computed {
		computed[cf.Name] = formatNum(evalExpr(cf.Expr, row))
	}
	out := make(map[string]string)
	if len(ts.Columns) > 0 {
		for _, c := range ts.Columns {
			if v, ok := row[c]; ok {
				out[c] = v
			}
		}
	} else {
		for k, v := range row {
			out[k] = v
		}
	}
	for k, v := range computed {
		out[k] = v
	}
	if len(ts.Rename) > 0 {
		// A renamed field replaces a field that already has the new name. It
		// used to depend on map order, so the result changed from row to row.
		renamed := make(map[string]string, len(out))
		for k, v := range out {
			if _, ok := ts.Rename[k]; !ok {
				renamed[k] = v
			}
		}
		for k, v := range out {
			if nn, ok := ts.Rename[k]; ok {
				renamed[nn] = v
			}
		}
		out = renamed
	}
	return out, true
}

// MissingFilterField returns the table's filter field when the row does not
// have it, else "". A NULL column is present (as ""), so it is evaluated as
// usual; a field that is absent cannot be. That happens when the source column
// was renamed or dropped, or when Debezium sent its unavailable-value
// placeholder for it (an unchanged TOAST or LOB value, removed by
// dropUnavailable). Apply would read "" and drop the row, so every change to
// the table turned into a delete (review finding H2). The caller dead-letters
// the change instead: nothing is deleted, the change is kept for a replay, and
// the ERROR names the field.
func (s *TransformSpec) MissingFilterField(table string, row map[string]string) string {
	if s == nil {
		return ""
	}
	ts, ok := s.Tables[table]
	if !ok || ts.Filter == nil {
		return ""
	}
	if _, ok := row[ts.Filter.Field]; ok {
		return ""
	}
	return ts.Filter.Field
}

// missingFilterFieldOf reports, for a CDC event's text, its table and the
// filter field its after-image lacks ("" when it has it or is not an event).
// The dead letter records it as the reason instead of "poison".
func missingFilterFieldOf(val string) (string, string) {
	var evt CDCEvent
	if err := decodeJSON([]byte(val), &evt); err != nil || evt.After == nil || evt.Op == "d" {
		return "", ""
	}
	after, _ := dropUnavailable(evt.After)
	return evt.Source.Table, transformSpec.MissingFilterField(evt.Source.Table, stringifyRow(after))
}

// filterUnreadable logs why a change was dead-lettered by MissingFilterField.
func filterUnreadable(table, field string) {
	logErr("filter-field-missing:"+table, fmt.Errorf(
		"table %s: a change has no field %q, which the link's filter reads, so whether the row "+
			"belongs in the cache cannot be decided. The change is dead-lettered, not applied, and "+
			"the cached row is left as it was. If the source column was renamed or dropped, fix "+
			"the filter in the link and apply it, then `ecdl-go-sink dlq replay`. If the column "+
			"holds large values (PostgreSQL TOAST, Oracle LOB), filter on another column", table, field))
}

func evalFilter(f *FilterSpec, row map[string]string) bool {
	got := row[f.Field]
	switch f.Op {
	case "=":
		return got == f.Value
	case "!=":
		return got != f.Value
	case ">":
		return toFloat(got) > toFloat(f.Value)
	case ">=":
		return toFloat(got) >= toFloat(f.Value)
	case "<":
		return toFloat(got) < toFloat(f.Value)
	case "<=":
		return toFloat(got) <= toFloat(f.Value)
	}
	return true
}

// stringifyRow converts a decoded JSON row (json.Number/string/bool/nil) into a
// map[string]string using exact text, so transforms operate on stable values.
func stringifyRow(m map[string]interface{}) map[string]string {
	out := make(map[string]string, len(m))
	for k, v := range m {
		if v == nil {
			out[k] = ""
			continue
		}
		switch x := v.(type) {
		case string:
			out[k] = x
		case bool:
			if x {
				out[k] = "true"
			} else {
				out[k] = "false"
			}
		case float64:
			out[k] = formatNum(x)
		case map[string]interface{}, []interface{}:
			// A PostgreSQL array or a Debezium struct. %v printed Go syntax
			// ("[1 2]", "map[a:1]") that no reader can parse; JSON (sorted keys,
			// no spaces) is what the bulk loader writes for the same value.
			out[k] = nestedJSON(x)
		default:
			out[k] = fmt.Sprintf("%v", v)
		}
	}
	return out
}
