package main

// Jira story F077: the Go transform engine against testdata/transform_cases.json, which
// the bulk loader (tests/test_transform_cases.py) and the link checker
// (ecdl_link/tests/test_exprcheck.py) also read.

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"
)

type transformCases struct {
	Expressions []struct {
		Expr string            `json:"expr"`
		Row  map[string]string `json:"row"`
		Want string            `json:"want"`
	} `json:"expressions"`
	InvalidExpressions []string `json:"invalid_expressions"`
	Filters            []struct {
		Filter FilterSpec        `json:"filter"`
		Row    map[string]string `json:"row"`
		Keep   bool              `json:"keep"`
	} `json:"filters"`
	Apply []struct {
		Name   string            `json:"name"`
		Spec   json.RawMessage   `json:"spec"`
		Row    map[string]string `json:"row"`
		Keep   bool              `json:"keep"`
		Fields map[string]string `json:"fields"`
	} `json:"apply"`
	InvalidSpecs []struct {
		Name string          `json:"name"`
		Spec json.RawMessage `json:"spec"`
	} `json:"invalid_specs"`
}

func readTransformCases(t *testing.T) transformCases {
	t.Helper()
	raw, err := os.ReadFile("testdata/transform_cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var tc transformCases
	if err := json.Unmarshal(raw, &tc); err != nil {
		t.Fatal(err)
	}
	if len(tc.Expressions) < 50 || len(tc.InvalidExpressions) < 20 || len(tc.Filters) < 10 ||
		len(tc.Apply) < 5 || len(tc.InvalidSpecs) < 5 {
		t.Fatal("testdata/transform_cases.json lost cases")
	}
	return tc
}

func TestTransformCasesExpressions(t *testing.T) {
	for _, c := range readTransformCases(t).Expressions {
		if err := checkExpr(c.Expr); err != nil {
			t.Errorf("%q refused: %v", c.Expr, err)
			continue
		}
		// Through the spec, as a computed field is applied.
		s, err := LoadTransformSpec([]byte(`{"tables":{"t":{"computed":[{"name":"out","expr":` + jsonString(c.Expr) + `}]}}}`))
		if err != nil {
			t.Errorf("%q: spec refused: %v", c.Expr, err)
			continue
		}
		out, _ := s.Apply("t", c.Row)
		if out["out"] != c.Want {
			t.Errorf("%q over %v = %q, want %q", c.Expr, c.Row, out["out"], c.Want)
		}
	}
}

func TestTransformCasesInvalidExpressions(t *testing.T) {
	for _, e := range readTransformCases(t).InvalidExpressions {
		if err := checkExpr(e); err == nil {
			t.Errorf("%q accepted", e)
		}
		_, err := LoadTransformSpec([]byte(`{"tables":{"t":{"computed":[{"name":"c","expr":` + jsonString(e) + `}]}}}`))
		if err == nil || !strings.Contains(err.Error(), "computed[0] (c)") {
			t.Errorf("spec with %q: err=%v, want it refused naming the field", e, err)
		}
	}
}

func TestTransformCasesFilters(t *testing.T) {
	for _, c := range readTransformCases(t).Filters {
		f := c.Filter
		if got := evalFilter(&f, c.Row); got != c.Keep {
			t.Errorf("%s %s %q over %v: keep=%v, want %v", f.Field, f.Op, f.Value, c.Row, got, c.Keep)
		}
	}
}

func TestTransformCasesApply(t *testing.T) {
	for _, c := range readTransformCases(t).Apply {
		s, err := LoadTransformSpec([]byte(`{"tables":{"t":` + string(c.Spec) + `}}`))
		if err != nil {
			t.Fatalf("%s: %v", c.Name, err)
		}
		for i := 0; i < 20; i++ { // map order must not matter
			out, keep := s.Apply("t", c.Row)
			if keep != c.Keep || (keep && !reflect.DeepEqual(out, c.Fields)) {
				t.Fatalf("%s: keep=%v fields=%v, want keep=%v fields=%v", c.Name, keep, out, c.Keep, c.Fields)
			}
		}
	}
}

func TestTransformCasesInvalidSpecs(t *testing.T) {
	for _, c := range readTransformCases(t).InvalidSpecs {
		if s, err := LoadTransformSpec(c.Spec); err == nil {
			t.Errorf("%s: loaded %+v", c.Name, s)
		}
	}
}

func TestParseNumberRejectsWhatPythonAndGoReadDifferently(t *testing.T) {
	for _, s := range []string{"1_000", "0x1p3", "0x10", "١٢", "\u00a012", "1e", "--1", "+-1", "nanx", "in f"} {
		if f, ok := parseNumber(s); ok {
			t.Errorf("parseNumber(%q) = %v, want not a number", s, f)
		}
	}
}

func jsonString(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}
