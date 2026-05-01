#!/usr/bin/env python3
"""
Load the Amazon ESCI product dataset into Valkey with a full-text search index.

Downloads the dataset from GitHub, creates a search index with TEXT, TAG,
and NUMERIC fields, and bulk-loads 137K products using valkey-cli --pipe.

Usage:
    python3 load_products.py --host <valkey-host>

Requirements:
    - Python 3.8+
    - valkey-cli in PATH
"""
import argparse
import gzip
import json
import os
import subprocess
import sys
import time
import urllib.request

DATASET_URL = "https://github.com/ebalan/valkey-FTS-blog/raw/main/products_esci_vec.jsonl.gz"
DATASET_FILE = "products_esci_vec.jsonl.gz"

INDEX_NAME = "products_index"
PREFIX = "product:"
FIELDS = ("title", "description", "brand", "color", "rating", "price", "stock")


def download_dataset(url=DATASET_URL, dest=DATASET_FILE):
    """Download the gzipped dataset from GitHub."""
    if os.path.exists(dest):
        print(f"Dataset already exists: {dest}")
        return dest
    print(f"Downloading dataset from {url} ...")
    start = time.time()
    urllib.request.urlretrieve(url, dest)
    mb = os.path.getsize(dest) / 1024 / 1024
    print(f"Downloaded {mb:.1f} MB in {time.time() - start:.1f}s")
    return dest


def redis_proto(*args):
    """Encode a single command in RESP (Redis Serialization Protocol)."""
    tokens = [f"*{len(args)}\r\n"]
    for a in args:
        s = str(a).encode()
        tokens.append(f"${len(s)}\r\n")
        tokens.append(s.decode() + "\r\n")
    return "".join(tokens)


def generate_protocol(dataset_path):
    """Yield RESP-encoded commands: DROP + CREATE index, then HSET per product."""

    # 1. Drop any existing index (will error-reply if absent — that's fine)
    yield redis_proto("FT.DROPINDEX", INDEX_NAME)

    # 2. Create the search index
    yield redis_proto(
        "FT.CREATE", INDEX_NAME,
        "ON", "HASH", "PREFIX", "1", PREFIX,
        "SCHEMA",
        "title",       "TEXT",
        "description", "TEXT",
        "brand",       "TAG", "SEPARATOR", ",",
        "color",       "TAG", "SEPARATOR", ",",
        "price",       "NUMERIC",
        "rating",      "NUMERIC",
        "stock",       "NUMERIC",
    )

    # 3. HSET each product
    opener = gzip.open if dataset_path.endswith(".gz") else open
    count = 0
    with opener(dataset_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            pid = rec.get("key", "").split(":")[-1] or str(count)
            args = ["HSET", f"{PREFIX}{pid}"]
            for field in FIELDS:
                if field in rec:
                    args.extend([field, rec[field]])
            if len(args) > 2:
                yield redis_proto(*args)
                count += 1
                if count % 25_000 == 0:
                    print(f"  Generated {count:,} commands ...", file=sys.stderr)

    print(f"  Total: {count:,} HSET commands", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Bulk-load products into Valkey")
    ap.add_argument("--host", required=True, help="Valkey hostname")
    ap.add_argument("--port", type=int, default=6379)
    ap.add_argument("--dataset", default=DATASET_FILE, help="Local dataset path")
    ap.add_argument("--skip-download", action="store_true")
    args = ap.parse_args()

    # Step 1 — download
    if not args.skip_download:
        dataset = download_dataset(dest=args.dataset)
    else:
        dataset = args.dataset
        if not os.path.exists(dataset):
            sys.exit(f"Dataset not found: {dataset}")

    # Step 2 — stream RESP into valkey-cli --pipe
    print(f"Loading into {args.host}:{args.port} ...")
    start = time.time()

    pipe_cmd = ["valkey-cli", "-h", args.host, "-p", str(args.port), "--pipe"]
    proc = subprocess.Popen(pipe_cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    for chunk in generate_protocol(dataset):
        proc.stdin.write(chunk.encode())
    proc.stdin.close()

    output = proc.communicate()[0].decode()
    elapsed = time.time() - start

    print(output)
    print(f"\nCompleted in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
