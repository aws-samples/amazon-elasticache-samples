#!/usr/bin/env python3
"""Load 137K products with vector embeddings into Valkey."""
import gzip
import json
import struct
import urllib.request
import valkey
from valkey.commands.search.field import TextField, TagField, NumericField, VectorField
from valkey.commands.search.indexDefinition import IndexDefinition, IndexType

# <Input required>: Insert your ElastiCache cluster's endpoint
VALKEY_HOST = "mycluster.cnxa6h.clustercfg.use1.cache.amazonaws.com"

client = valkey.Valkey(host=VALKEY_HOST, port=6379, decode_responses=False)

# Create the search index with text, tag, numeric, and vector fields
try:
    client.execute_command("FT.DROPINDEX", "products_vec_index")
except:
    pass

client.ft("products_vec_index").create_index(
    fields=[
        TextField("title"),
        TextField("description"),
        TagField("brand", separator=","),
        TagField("color", separator=","),
        NumericField("price"),
        NumericField("rating"),
        NumericField("stock"),
        VectorField("embedding", "FLAT", {
            "TYPE": "FLOAT32",
            "DIM": 64,
            "DISTANCE_METRIC": "COSINE",
        }),
    ],
    definition=IndexDefinition(prefix=["pv:"], index_type=IndexType.HASH),
)

# Download and load the product dataset with 64-dim embeddings
DATASET_URL = "https://github.com/ebalan/valkey-FTS-blog/releases/download/v1.0/products_esci_vec_64d.jsonl.gz"
print("Downloading dataset...")
urllib.request.urlretrieve(DATASET_URL, "products.jsonl.gz")

print("Loading products...")
count = 0
pipe = client.pipeline(transaction=False)

with gzip.open("products.jsonl.gz", "rt") as f:
    for line in f:
        rec = json.loads(line)
        key = f"pv:{rec['key'].split(':')[-1]}"

        # Convert embedding list to binary FLOAT32 blob
        embedding = struct.pack(f"{len(rec['embedding'])}f", *rec["embedding"])

        pipe.hset(key, mapping={
            "title": rec.get("title", ""),
            "description": rec.get("description", ""),
            "brand": rec.get("brand", ""),
            "color": rec.get("color", ""),
            "price": rec.get("price", 0),
            "rating": rec.get("rating", 0),
            "stock": rec.get("stock", 0),
            "embedding": embedding,
        })

        count += 1
        if count % 1000 == 0:
            pipe.execute()
            pipe = client.pipeline(transaction=False)
            print(f"  {count:,} products loaded...")

    pipe.execute()

print(f"Done! Loaded {count:,} products into products_vec_index")
