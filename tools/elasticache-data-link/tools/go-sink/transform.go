package main

// Shared transform engine (CDC path). Mirrors data-plane/bulk-loader/transforms.py.
// Processing order per row: filter (on original fields) -> computed -> column
// selection -> rename. Both engines must pass data-plane/transforms-fixtures.json.

import (
	"fmt"
	"strconv"
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
		renamed := make(map[string]string, len(out))
		for k, v := range out {
			if nn, ok := ts.Rename[k]; ok {
				renamed[nn] = v
			} else {
				renamed[k] = v
			}
		}
		out = renamed
	}
	return out, true
}

func toFloat(s string) float64 {
	f, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil {
		return 0
	}
	return f
}

// formatNum: %.6f then strip trailing zeros and a trailing dot (matches Python).
func formatNum(v float64) string {
	s := strconv.FormatFloat(v, 'f', 6, 64)
	if strings.Contains(s, ".") {
		s = strings.TrimRight(s, "0")
		s = strings.TrimRight(s, ".")
	}
	return s
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

// ── tiny arithmetic expression evaluator: + - * / , parens, unary minus,
//    numeric literals, identifiers (row field coerced to float, missing->0) ──
type exprParser struct {
	s   string
	pos int
	row map[string]string
}

func evalExpr(expr string, row map[string]string) float64 {
	p := &exprParser{s: expr, row: row}
	v := p.parseExpr()
	return v
}

func (p *exprParser) skipSpace() {
	for p.pos < len(p.s) && (p.s[p.pos] == ' ' || p.s[p.pos] == '\t') {
		p.pos++
	}
}

func (p *exprParser) parseExpr() float64 { // + and -
	v := p.parseTerm()
	for {
		p.skipSpace()
		if p.pos >= len(p.s) {
			break
		}
		c := p.s[p.pos]
		if c == '+' {
			p.pos++
			v += p.parseTerm()
		} else if c == '-' {
			p.pos++
			v -= p.parseTerm()
		} else {
			break
		}
	}
	return v
}

func (p *exprParser) parseTerm() float64 { // * and /
	v := p.parseFactor()
	for {
		p.skipSpace()
		if p.pos >= len(p.s) {
			break
		}
		c := p.s[p.pos]
		if c == '*' {
			p.pos++
			v *= p.parseFactor()
		} else if c == '/' {
			p.pos++
			d := p.parseFactor()
			if d != 0 {
				v /= d
			} else {
				v = 0
			}
		} else {
			break
		}
	}
	return v
}

func (p *exprParser) parseFactor() float64 {
	p.skipSpace()
	if p.pos >= len(p.s) {
		return 0
	}
	c := p.s[p.pos]
	if c == '(' {
		p.pos++
		v := p.parseExpr()
		p.skipSpace()
		if p.pos < len(p.s) && p.s[p.pos] == ')' {
			p.pos++
		}
		return v
	}
	if c == '-' {
		p.pos++
		return -p.parseFactor()
	}
	if c == '+' {
		p.pos++
		return p.parseFactor()
	}
	// number literal
	if (c >= '0' && c <= '9') || c == '.' {
		start := p.pos
		for p.pos < len(p.s) && ((p.s[p.pos] >= '0' && p.s[p.pos] <= '9') || p.s[p.pos] == '.') {
			p.pos++
		}
		f, _ := strconv.ParseFloat(p.s[start:p.pos], 64)
		return f
	}
	// identifier
	start := p.pos
	for p.pos < len(p.s) {
		ch := p.s[p.pos]
		if (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9') || ch == '_' {
			p.pos++
		} else {
			break
		}
	}
	name := p.s[start:p.pos]
	if name == "" {
		p.pos++ // skip unknown char to avoid infinite loop
		return 0
	}
	return toFloat(p.row[name])
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
