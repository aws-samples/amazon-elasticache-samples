#!/usr/bin/env python3
"""Load products with 64-dim vectors into Valkey using valkey-cli --pipe."""
import gzip
import json
import os
import struct
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT = os.path.join(SCRIPT_DIR, "products_esci_vec_64d.jsonl.gz")
HOST = sys.argv[1] if len(sys.argv) > 1 else "your-cluster.cache.amazonaws.com"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 6379
INDEX = "products_vec_index"
PREFIX = "pv:"
DIM = 64

def resp(*args):
    """Encode command as RESP protocol (binary-safe)."""
    parts = []
    parts.append(f"*{len(args)}\r\n".encode())
    for a in args:
        if isinstance(a, bytes):
            parts.append(f"${len(a)}\r\n".encode())
            parts.append(a)
            parts.append(b"\r\n")
        else:
            s = str(a).encode()
            parts.append(f"${len(s)}\r\n".encode())
            parts.append(s)
            parts.append(b"\r\n")
    return b"".join(parts)

import subprocess

# Drop existing index
print(f"Dropping index {INDEX} (if exists)...", flush=True)
subprocess.run(["valkey-cli", "-h", HOST, "-p", str(PORT), "FT.DROPINDEX", INDEX], capture_output=True)

# Create index with VECTOR field
print(f"Creating index {INDEX}...", flush=True)
create_cmd = [
    "valkey-cli", "-h", HOST, "-p", str(PORT),
    "FT.CREATE", INDEX,
    "ON", "HASH", "PREFIX", "1", PREFIX,
    "SCHEMA",
    "title", "TEXT",
    "description", "TEXT",
    "brand", "TAG", "SEPARATOR", ",",
    "color", "TAG", "SEPARATOR", ",",
    "price", "NUMERIC",
    "rating", "NUMERIC",
    "stock", "NUMERIC",
    "embedding", "VECTOR", "FLAT", "6",
    "TYPE", "FLOAT32",
    "DIM", str(DIM),
    "DISTANCE_METRIC", "COSINE",
]
result = subprocess.run(create_cmd, capture_output=True, text=True)
print(f"  Result: {result.stdout.strip()}", flush=True)
if result.returncode != 0:
    print(f"  Error: {result.stderr.strip()}", flush=True)

# Bulk load via pipe
print(f"Loading data from {INPUT}...", flush=True)
start = time.time()

pipe_cmd = ["valkey-cli", "-h", HOST, "-p", str(PORT), "--pipe"]
proc = subprocess.Popen(pipe_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

count = 0
with gzip.open(INPUT, "rt", encoding="utf-8") as f:
    for line in f:
        rec = json.loads(line)
        pid = rec.get("key", "").split(":")[-1] or str(count)
        
        # Build HSET args
        args = ["HSET", f"{PREFIX}{pid}"]
        for field in ("title", "description", "brand", "color", "price", "rating", "stock"):
            if field in rec:
                args.append(field)
                args.append(str(rec[field]))
        
        # Add vector as binary blob (FLOAT32)
        if "embedding" in rec:
            vec = rec["embedding"]
            blob = struct.pack(f"{len(vec)}f", *vec)
            args.append("embedding")
            args.append(blob)
        
        try:
            proc.stdin.write(resp(*args))
        except BrokenPipeError:
            print(f"Pipe broke at record {count}", flush=True)
            break
        count += 1
        if count % 25000 == 0:
            proc.stdin.flush()
            print(f"  Sent {count:,} ...", flush=True)

try:
    proc.stdin.close()
except:
    pass
output = proc.communicate()[0].decode()
elapsed = time.time() - start

print(output, flush=True)
print(f"\nLoaded {count:,} products with vectors in {elapsed:.1f}s", flush=True)
