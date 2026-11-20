package main

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/redis/go-redis/v9"
)

// TestPkFromMsgMatchesSharedCases checks pkFromMsg against
// testdata/sink_pk_cases.json. The bulk loader's tests read the same file
// (data-plane/bulk-loader/tests/test_keys.py), so a loaded row and a
// CDC-written row of the same source row share one key.
func TestPkFromMsgMatchesSharedCases(t *testing.T) {
	raw, err := os.ReadFile("testdata/sink_pk_cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Cases []struct {
			Name    string `json:"name"`
			KeyJSON string `json:"key_json"`
			PK      string `json:"pk"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("no cases in testdata/sink_pk_cases.json")
	}
	for _, c := range doc.Cases {
		msg := redis.XMessage{Values: map[string]interface{}{"key": c.KeyJSON}}
		if got := pkFromMsg(msg, nil); got != c.PK {
			t.Errorf("%s: pkFromMsg(%s) = %q, want %q", c.Name, c.KeyJSON, got, c.PK)
		}
	}
}
