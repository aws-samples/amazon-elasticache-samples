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
