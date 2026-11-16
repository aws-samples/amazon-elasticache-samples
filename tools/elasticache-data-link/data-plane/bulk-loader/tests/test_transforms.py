#!/usr/bin/env python3
"""Validate the Python transform engine against the shared golden fixtures
(data-plane/transforms-fixtures.json) — the same file the Go sink tests.

Run: python3 test_transforms.py   (exit 0 = all pass)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOADER = os.path.dirname(HERE)          # data-plane/bulk-loader (import transforms)
DP = os.path.dirname(LOADER)            # data-plane (transforms.json + fixtures)
sys.path.insert(0, LOADER)

import transforms  # noqa: E402


def main():
    spec = transforms.load_spec(os.path.join(DP, "transforms.json"))
    fx = json.load(open(os.path.join(DP, "transforms-fixtures.json")))
    failures = 0
    for c in fx["cases"]:
        out, keep = transforms.apply(spec, c["table"], c["row"])
        key = f'{c["table"]}:{c["row"][c["pk"]]}'
        if keep != c["keep"]:
            print(f'FAIL {c["name"]}: keep={keep} want {c["keep"]}'); failures += 1; continue
        if key != c["key"]:
            print(f'FAIL {c["name"]}: key={key} want {c["key"]}'); failures += 1
        if keep and out != c["fields"]:
            print(f'FAIL {c["name"]}: fields={out} want {c["fields"]}'); failures += 1
    if failures:
        print(f"\n{failures} failure(s)"); sys.exit(1)
    print(f'OK — {len(fx["cases"])} fixtures passed')


if __name__ == "__main__":
    main()
