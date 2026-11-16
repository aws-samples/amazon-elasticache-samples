package main

import "strings"

// keyPrefix is the link's advanced.key_prefix, normalised to end in ":" (or
// empty). Every row key is keyPrefix + <table> + ":" + <pk>, the same shape the
// bulk loader writes, so a loaded row and a CDC-written row share one key.
var keyPrefix string

func normalizeKeyPrefix(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	return strings.TrimRight(raw, ":") + ":"
}

func rowKey(table, pk string) string {
	return keyPrefix + table + ":" + pk
}
