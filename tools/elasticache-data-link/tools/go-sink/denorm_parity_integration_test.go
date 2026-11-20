package main

// Jira story F198: a child embeds the same parent values whichever side wrote last.
// The cases are testdata/denorm_cases.json, which the bulk loader's tests
// also read (data-plane/bulk-loader/tests/test_denorm_parity.py).

import (
	"encoding/json"
	"fmt"
	"os"
	"testing"

	"github.com/redis/go-redis/v9"
)

type denormCase struct {
	Name   string                 `json:"name"`
	Spec   json.RawMessage        `json:"spec"`
	Parent map[string]interface{} `json:"parent"`
	Child  map[string]interface{} `json:"child"`
	Embed  map[string]string      `json:"embed"`
}

func readDenormCases(t *testing.T) []denormCase {
	t.Helper()
	raw, err := os.ReadFile("testdata/denorm_cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Cases []denormCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Cases) < 5 {
		t.Fatalf("only %d cases in testdata/denorm_cases.json", len(doc.Cases))
	}
	return doc.Cases
}

func intOf(v interface{}) int {
	f, _ := v.(float64)
	return int(f)
}

func TestIntegrationDenormParentUpdateComparesTransformedRows(t *testing.T) { // Jira story F198
	spec := `{"tables":{"orders":{"denormalize":[{"parent_table":"customers","join_field":"cid",
 "fields":["total","name"],"prefix":"c_"}]},
 "customers":{"rename":{"name":"label"},"computed":[{"name":"total","expr":"a + b"}]}}}`
	eachTarget(t, func(t *testing.T, c redis.UniversalClient) {
		d := setupDenormOn(t, c, "v2", spec)
		d.apply(evMsg("100-0", "c", "customers", 7, nil, row("id", 7, "a", "1", "b", "2", "name", "Ann")), "customers")
		d.order("200-0", "c", 1, 7, 5, nil)
		d.want("orders", 1, "c_total", "3")
		d.want("orders", 1, "c_name", "Ann")
		// Only an input of the computed field changes: the raw row has no
		// "total" before or after, but the embedded value changes.
		d.apply(evMsg("300-0", "u", "customers", 7, row("id", 7, "a", "1", "b", "2", "name", "Ann"),
			row("id", 7, "a", "5", "b", "2", "name", "Ann")), "customers")
		d.want("orders", 1, "c_total", "7")
		d.want("orders", 1, "_ecdl_pv:customers:cid", "300-0")
		// Nothing embedded changes: no fan-out, the child keeps its parent version.
		d.apply(evMsg("400-0", "u", "customers", 7, row("id", 7, "a", "5", "b", "2", "name", "Ann"),
			row("id", 7, "a", "4", "b", "3", "name", "Ann")), "customers")
		d.want("orders", 1, "_ecdl_pv:customers:cid", "300-0")
		// A renamed embedded column changes.
		d.apply(evMsg("500-0", "u", "customers", 7, row("id", 7, "a", "4", "b", "3", "name", "Ann"),
			row("id", 7, "a", "4", "b", "3", "name", "Anna")), "customers")
		d.want("orders", 1, "c_name", "Anna")
		// A placeholder for the renamed column leaves the children's value.
		d.apply(evMsg("600-0", "u", "customers", 7, nil,
			row("id", 7, "a", "4", "b", "4", "name", unavailableText)), "customers")
		d.want("customers", 7, "label", "Anna")
		d.want("orders", 1, "c_name", "Anna")
		d.want("orders", 1, "c_total", "8")
		// A before-image with only the key (REPLICA IDENTITY USING INDEX) cannot
		// show the old value, so the change fans out even when the new value is
		// the empty string a missing column also reads as.
		d.apply(evMsg("700-0", "u", "customers", 7, row("id", 7),
			row("id", 7, "a", "0", "b", "0", "name", nil)), "customers")
		d.want("orders", 1, "c_name", "")
		d.want("orders", 1, "c_total", "0")
	})
}

func TestIntegrationDenormSharedCases(t *testing.T) { // Jira story F198
	cases := readDenormCases(t)
	eachTarget(t, func(t *testing.T, c redis.UniversalClient) {
		for _, tc := range cases {
			for _, order := range []string{"parent first", "child first", "child first, parent updated"} {
				for _, prefix := range []string{"", "v2"} {
					t.Run(fmt.Sprintf("%s/%s/prefix=%s", tc.Name, order, prefix), func(t *testing.T) {
						d := setupDenormOn(t, c, prefix, string(tc.Spec))
						ppk, cpk := intOf(tc.Parent["id"]), intOf(tc.Child["id"])
						parent := evMsg("100-0", "c", "customers", ppk, nil, tc.Parent)
						child := evMsg("200-0", "c", "orders", cpk, nil, tc.Child)
						switch order {
						case "parent first":
							d.apply(parent, "customers")
							d.apply(child, "orders")
						case "child first":
							parent.ID = "300-0"
							d.apply(child, "orders")
							d.apply(parent, "customers")
						default:
							// The child first sees an older parent value, then an
							// update (with a before-image) fans out the case's row.
							old := map[string]interface{}{}
							for k, v := range tc.Parent {
								old[k] = v
							}
							for k, v := range old {
								if s, ok := v.(string); ok {
									old[k] = s + "-old"
								}
							}
							d.apply(evMsg("100-0", "c", "customers", ppk, nil, old), "customers")
							d.apply(child, "orders")
							d.apply(evMsg("300-0", "u", "customers", ppk, old, tc.Parent), "customers")
						}
						got := c.HGetAll(d.ctx, rowKey("orders", fmt.Sprint(cpk))).Val()
						for f, want := range tc.Embed {
							if v, ok := got[f]; !ok || v != want {
								t.Errorf("%s = %q (present %v), want %q; child %v", f, v, ok, want, got)
							}
						}
					})
				}
			}
		}
	})
}
