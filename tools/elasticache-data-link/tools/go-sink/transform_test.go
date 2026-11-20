package main

import (
	"os"
	"reflect"
	"testing"

	json "github.com/goccy/go-json"
)

// Validates the Go transform engine against the shared golden fixtures
// (data-plane/transforms-fixtures.json), the same file the Python loader tests.
func TestTransformFixtures(t *testing.T) {
	specBytes, err := os.ReadFile("../../data-plane/transforms.json")
	if err != nil {
		t.Fatalf("read spec: %v", err)
	}
	spec, err := LoadTransformSpec(specBytes)
	if err != nil {
		t.Fatalf("load spec: %v", err)
	}

	fxBytes, err := os.ReadFile("../../data-plane/transforms-fixtures.json")
	if err != nil {
		t.Fatalf("read fixtures: %v", err)
	}
	var fx struct {
		Cases []struct {
			Name   string            `json:"name"`
			Table  string            `json:"table"`
			PK     string            `json:"pk"`
			Row    map[string]string `json:"row"`
			Keep   bool              `json:"keep"`
			Key    string            `json:"key"`
			Fields map[string]string `json:"fields"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(fxBytes, &fx); err != nil {
		t.Fatalf("parse fixtures: %v", err)
	}

	for _, c := range fx.Cases {
		out, keep := spec.Apply(c.Table, c.Row)
		if keep != c.Keep {
			t.Errorf("%s: keep=%v want %v", c.Name, keep, c.Keep)
			continue
		}
		gotKey := c.Table + ":" + c.Row[c.PK]
		if gotKey != c.Key {
			t.Errorf("%s: key=%s want %s", c.Name, gotKey, c.Key)
		}
		if !keep {
			continue
		}
		if !reflect.DeepEqual(out, c.Fields) {
			t.Errorf("%s: fields=%v want %v", c.Name, out, c.Fields)
		}
	}
}

func TestLoadSpecFromEnvFailsClosed(t *testing.T) {
	t.Setenv("TRANSFORMS_FILE", "")
	t.Setenv("TRANSFORMS_JSON", "{not json")
	if s, err := loadSpecFromEnv(); err == nil || s != nil {
		t.Fatalf("bad inline spec: s=%v err=%v (must not run as passthrough)", s, err)
	}
	t.Setenv("TRANSFORMS_JSON", "")
	t.Setenv("TRANSFORMS_FILE", t.TempDir()+"/missing.json")
	if _, err := loadSpecFromEnv(); err == nil {
		t.Fatal("missing spec file accepted")
	}
	bad := t.TempDir() + "/bad.json"
	if err := os.WriteFile(bad, []byte(`{"tables":`), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("TRANSFORMS_FILE", bad)
	if _, err := loadSpecFromEnv(); err == nil {
		t.Fatal("unparseable spec file accepted")
	}
	t.Setenv("TRANSFORMS_FILE", "")
	if s, err := loadSpecFromEnv(); err != nil || s != nil {
		t.Fatalf("no spec: s=%v err=%v", s, err)
	}
	t.Setenv("TRANSFORMS_JSON", `{"tables":{"orders":{"columns":["id"]}}}`)
	if s, err := loadSpecFromEnv(); err != nil || s == nil || len(s.Tables) != 1 {
		t.Fatalf("good spec: s=%v err=%v", s, err)
	}
}

func TestCheckSpecTables(t *testing.T) {
	specs := []streamSpec{{key: "data:x.Inventory.orders", table: "orders"}, {key: "data:x.Inventory.users", table: "users"}}
	for js, ok := range map[string]bool{
		`{"tables":{"orders":{"columns":["id"]}}}`: true,
		`{"tables":{}}`: true,
		`{"tables":{"public.users":{"columns":["id"]}}}`: false,
		`{"tables":{"Users":{"columns":["id"]}}}`:        false,
		`{"tables":{"payments":{"columns":["id"]}}}`:     false,
	} {
		s, err := LoadTransformSpec([]byte(js))
		if err != nil {
			t.Fatal(err)
		}
		if err := checkSpecTables(s, specs); (err == nil) != ok {
			t.Errorf("%s: err=%v, want ok=%v", js, err, ok)
		}
	}
	if err := checkSpecTables(nil, specs); err != nil {
		t.Errorf("nil spec: %v", err)
	}
}
