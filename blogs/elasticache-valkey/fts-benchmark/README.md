# Valkey Full-Text Search & Vector Search — Benchmark Dataset & Scripts

This repository contains the dataset and scripts used in the **Valkey FTS on Amazon ElastiCache** blog post.

## Dataset

The dataset is based on the [Amazon ESCI (Shopping Queries)](https://github.com/amazon-science/esci-data) product catalog — **137,042 products** with rich metadata.

Download from [Releases](https://github.com/ebalan/valkey-FTS-blog/releases/tag/v1.0):

| File | Size | Description |
|------|------|-------------|
| `products_esci_vec.jsonl.gz` | 24 MB | 137K products — text, tags, numerics (no vectors) |
| `products_esci_vec_64d.jsonl.gz` | 110 MB | 137K products — all fields + 64-dim vector embeddings |

### Fields per record

| Field | Type | Description |
|-------|------|-------------|
| `title` | TEXT | Product title (full-text indexed) |
| `description` | TEXT | Product description |
| `brand` | TAG | Brand name |
| `color` | TAG | Product color |
| `price` | NUMERIC | Price in cents |
| `rating` | NUMERIC | Rating (0-5) |
| `stock` | NUMERIC | Stock level (0-999) |
| `embedding` | VECTOR | 64-dim FLOAT32 (BAAI/bge-small-en-v1.5, truncated) — only in `_64d` file |

## Scripts

### `load_products.py` — Load text/tag/numeric data (fast)

Bulk-loads the non-vector dataset using `valkey-cli --pipe` (RESP protocol streaming). Loads 137K products in ~2 minutes.

```bash
# Requirements: Python 3.8+, valkey-cli in PATH
python3 load_products.py --host your-cluster.cache.amazonaws.com
```

**What it does:**
1. Downloads the dataset from GitHub (24 MB)
2. Creates `products_index` with TEXT, TAG, and NUMERIC fields
3. Streams HSET commands via `valkey-cli --pipe`

### `load_vectors.py` — Load with vector embeddings (fast)

Bulk-loads the full dataset including 64-dim vector embeddings using `valkey-cli --pipe` with binary RESP encoding.

```bash
# Requirements: Python 3.8+, valkey-cli in PATH
# Place products_esci_vec_64d.jsonl.gz in same directory
python3 load_vectors.py your-cluster.cache.amazonaws.com
```

**What it does:**
1. Creates `products_vec_index` with TEXT, TAG, NUMERIC, and VECTOR FLAT (COSINE, 64-dim) fields
2. Encodes each embedding as a raw FLOAT32 binary blob (256 bytes per vector)
3. Streams everything via `valkey-cli --pipe`

### `load_products_blog.py` — Simple Python client loader (blog snippet)

A readable, blog-friendly version using the `valkey` Python client directly. Slower (~17 docs/sec) but simpler code.

```bash
pip install valkey
python3 load_products_blog.py
```

**What it does:**
1. Downloads the 110 MB vector dataset from GitHub Releases
2. Creates the index using the `valkey-py` search API
3. Loads products using pipelined HSET with `struct.pack` for vector encoding

### `query_examples.py` — All query types demonstrated

Shows every search pattern: full-text, prefix, fuzzy, tag, numeric, combined, KNN vector, and hybrid (text + vector).

```bash
pip install valkey
python3 query_examples.py --host your-cluster.cache.amazonaws.com
```

## Performance Note

| Method | Speed | Use case |
|--------|-------|----------|
| `valkey-cli --pipe` | ~1,100 docs/sec | Production bulk loads |
| Python client pipeline | ~17 docs/sec | Simple scripts, small datasets |

The Python client is slow because Valkey blocks on TEXT field tokenization during each pipeline batch. For large datasets, always use the RESP protocol pipe method.

## Index Schemas

### `products_index` (non-vector)
```
FT.CREATE products_index ON HASH PREFIX 1 product:
  SCHEMA title TEXT description TEXT
  brand TAG SEPARATOR , color TAG SEPARATOR ,
  price NUMERIC rating NUMERIC stock NUMERIC
```

### `products_vec_index` (with vectors)
```
FT.CREATE products_vec_index ON HASH PREFIX 1 pv:
  SCHEMA title TEXT description TEXT
  brand TAG SEPARATOR , color TAG SEPARATOR ,
  price NUMERIC rating NUMERIC stock NUMERIC
  embedding VECTOR FLAT 6 TYPE FLOAT32 DIM 64 DISTANCE_METRIC COSINE
```

## License

Dataset derived from [Amazon ESCI](https://github.com/amazon-science/esci-data) under Apache 2.0.
