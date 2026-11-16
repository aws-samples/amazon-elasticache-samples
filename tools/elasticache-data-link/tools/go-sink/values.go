package main

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// nestedJSON renders a nested event value (a PostgreSQL array, a Debezium
// struct) as compact JSON with sorted keys and no HTML escaping, which is what
// the bulk loader writes for the same value (cdc_values.json_compact). Numbers
// decoded with UseNumber keep their exact text.
func nestedJSON(v interface{}) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return fmt.Sprintf("%v", v)
	}
	return string(bytes.TrimRight(buf.Bytes(), "\n"))
}
