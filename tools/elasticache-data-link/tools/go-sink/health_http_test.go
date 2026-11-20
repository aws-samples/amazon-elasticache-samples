package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// Every endpoint states its content type and turns off sniffing, so a browser
// pointed at the sink can never render one of these bodies as HTML.
func TestHealthEndpointsAreNeverHTML(t *testing.T) {
	mux := healthMux()
	cases := map[string]string{
		"/health":  "text/plain; charset=utf-8",
		"/version": "application/json",
		"/metrics": "application/json",
	}
	for path, wantType := range cases {
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		if got := rec.Header().Get("Content-Type"); got != wantType {
			t.Errorf("%s Content-Type = %q, want %q", path, got, wantType)
		}
		if got := rec.Header().Get("X-Content-Type-Options"); got != "nosniff" {
			t.Errorf("%s X-Content-Type-Options = %q, want nosniff", path, got)
		}
		if rec.Body.Len() == 0 {
			t.Errorf("%s returned an empty body", path)
		}
	}
}

func TestMetricsAndVersionAreValidJSON(t *testing.T) {
	mux := healthMux()
	for _, path := range []string{"/metrics", "/version"} {
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		var v map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &v); err != nil {
			t.Errorf("%s is not valid JSON: %v (%q)", path, err, rec.Body.String())
		}
		if rec.Code != http.StatusOK {
			t.Errorf("%s status = %d, want 200", path, rec.Code)
		}
	}
}
