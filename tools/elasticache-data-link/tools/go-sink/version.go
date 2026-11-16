package main

import (
	"fmt"
	"runtime"
)

// Build identity, stamped at link time by release.sh:
//
//	-ldflags "-X main.version=0.3.0 -X main.commit=abc1234 -X main.buildDate=..."
//
// The defaults are what you get from a plain `go build`, and they say so rather
// than pretending to be a release. A binary that cannot tell you which build it
// is makes it impossible to check whether a fix is deployed -- and the
// images this replaces were all tagged :latest, so the tag could not answer it
// either.
var (
	version   = "dev"
	commit    = "unknown"
	buildDate = "unknown"
)

// Version is the release tag, or "dev" for an unstamped local build.
func Version() string { return version }

// buildInfo is the one-line startup banner.
func buildInfo() string {
	return fmt.Sprintf("ecdl-sink %s (commit %s, built %s, %s/%s, %s)",
		version, commit, buildDate, runtime.GOOS, runtime.GOARCH, runtime.Version())
}

// buildJSON is the same identity as a JSON object, for /health. Built as a string
// rather than marshalled: these are known-safe values fixed at build time, and
// /health must not be able to fail on a serialisation error.
func buildJSON() string {
	return fmt.Sprintf(`{"version":%q,"commit":%q,"built":%q,"go":%q,"platform":"%s/%s"}`,
		version, commit, buildDate, runtime.Version(), runtime.GOOS, runtime.GOARCH)
}
