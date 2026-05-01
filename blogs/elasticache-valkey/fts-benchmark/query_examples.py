#!/usr/bin/env python3
"""
Query examples for Valkey Full-Text Search + Vector Search.

Demonstrates:
1. Text search (full-text)
2. Prefix search (autocomplete-style)
3. Fuzzy search (typo tolerance)
4. Tag filter
5. Numeric range filter
6. Combined text + numeric filter
7. KNN vector search (pure)
8. Hybrid search (text filter + vector KNN)

Usage:
    pip install valkey
    python3 query_examples.py --host <valkey-host>
"""
import argparse
import struct
import valkey
from valkey.commands.search.query import Query

def main():
    ap = argparse.ArgumentParser(description="Valkey FTS query examples")
    ap.add_argument("--host", required=True, help="Valkey host")
    ap.add_argument("--port", type=int, default=6379)
    args = ap.parse_args()

    # Text-mode client for standard queries
    client = valkey.Valkey(host=args.host, port=args.port, decode_responses=True)
    # Binary-mode client for vector queries
    client_bin = valkey.Valkey(host=args.host, port=args.port, decode_responses=False)

    INDEX = "products_vec_index"
    print("=" * 70)

    # ─── 1. Full-Text Search ───────────────────────────────────────────────
    print("\n1. FULL-TEXT SEARCH: 'wireless headphones'")
    print("-" * 50)
    results = client.ft(INDEX).search(
        Query("wireless headphones")
        .return_fields("title", "brand", "price")
        .paging(0, 5)
    )
    print(f"   Total matches: {results.total}")
    for doc in results.docs:
        print(f"   {doc.id}: {doc.title[:60]} | {doc.brand} | ${doc.price}")

    # ─── 2. Prefix Search ─────────────────────────────────────────────────
    print("\n2. PREFIX SEARCH: 'wire*' (autocomplete)")
    print("-" * 50)
    results = client.ft(INDEX).search(
        Query("wire*")
        .return_fields("title", "brand")
        .paging(0, 5)
    )
    print(f"   Total matches: {results.total}")
    for doc in results.docs:
        print(f"   {doc.id}: {doc.title[:60]} | {doc.brand}")

    # ─── 3. Fuzzy Search ──────────────────────────────────────────────────
    print("\n3. FUZZY SEARCH: '%%headphnes%%' (typo tolerant)")
    print("-" * 50)
    results = client.ft(INDEX).search(
        Query("%%headphnes%%")
        .return_fields("title", "brand")
        .paging(0, 5)
    )
    print(f"   Total matches: {results.total}")
    for doc in results.docs:
        print(f"   {doc.id}: {doc.title[:60]} | {doc.brand}")

    # ─── 4. Tag Filter ────────────────────────────────────────────────────
    print("\n4. TAG FILTER: @brand:{Sony}")
    print("-" * 50)
    results = client.ft(INDEX).search(
        Query("@brand:{Sony}")
        .return_fields("title", "brand", "price")
        .paging(0, 5)
    )
    print(f"   Total matches: {results.total}")
    for doc in results.docs:
        print(f"   {doc.id}: {doc.title[:60]} | ${doc.price}")

    # ─── 5. Numeric Range ─────────────────────────────────────────────────
    print("\n5. NUMERIC RANGE: @price:[100 500]")
    print("-" * 50)
    results = client.ft(INDEX).search(
        Query("@price:[100 500]")
        .return_fields("title", "price")
        .paging(0, 5)
    )
    print(f"   Total matches: {results.total}")
    for doc in results.docs:
        print(f"   {doc.id}: {doc.title[:60]} | ${doc.price}")

    # ─── 6. Combined: Text + Numeric ──────────────────────────────────────
    print("\n6. COMBINED: 'headphones @price:[50 200]'")
    print("-" * 50)
    results = client.ft(INDEX).search(
        Query("headphones @price:[50 200]")
        .return_fields("title", "brand", "price")
        .paging(0, 5)
    )
    print(f"   Total matches: {results.total}")
    for doc in results.docs:
        print(f"   {doc.id}: {doc.title[:60]} | {doc.brand} | ${doc.price}")

    # ─── 7. KNN Vector Search (pure) ──────────────────────────────────────
    print("\n7. KNN VECTOR SEARCH: Top 5 similar to 'American Audio HP-550'")
    print("-" * 50)
    # Get embedding from a known product
    product_embedding = client_bin.hget("pv:B001O4JX5O", "embedding")
    if product_embedding:
        results = client_bin.execute_command(
            "FT.SEARCH", INDEX,
            "*=>[KNN 5 @embedding $vec AS score]",
            "PARAMS", "2", "vec", product_embedding,
            "RETURN", "3", "title", "score", "brand",
            "SORTBY", "score",
            "DIALECT", "2",
        )
        print(f"   Top 5 nearest neighbors:")
        for i in range(1, len(results), 2):
            doc_id = results[i].decode()
            fields = results[i + 1]
            fdict = {}
            for j in range(0, len(fields), 2):
                fdict[fields[j].decode()] = fields[j + 1].decode()
            print(f"   {doc_id}: score={fdict.get('score','?')[:8]} | "
                  f"{fdict.get('title','')[:50]} | {fdict.get('brand','')}")
    else:
        print("   (product pv:B001O4JX5O not found — skip)")

    # ─── 8. Hybrid Search: Text + Vector KNN ──────────────────────────────
    print("\n8. HYBRID SEARCH: text 'headphones' + KNN vector ranking")
    print("-" * 50)
    if product_embedding:
        results = client_bin.execute_command(
            "FT.SEARCH", INDEX,
            "@title:headphones =>[KNN 5 @embedding $vec AS score]",
            "PARAMS", "2", "vec", product_embedding,
            "RETURN", "3", "title", "score", "brand",
            "DIALECT", "2",
        )
        print(f"   Pre-filter: @title:headphones")
        print(f"   Then rank by vector similarity to 'American Audio HP-550':\n")
        for i in range(1, len(results), 2):
            doc_id = results[i].decode()
            fields = results[i + 1]
            fdict = {}
            for j in range(0, len(fields), 2):
                fdict[fields[j].decode()] = fields[j + 1].decode()
            print(f"   {doc_id}: score={fdict.get('score','?')[:8]} | "
                  f"{fdict.get('title','')[:50]} | {fdict.get('brand','')}")
    else:
        print("   (product pv:B001O4JX5O not found — skip)")

    print("\n" + "=" * 70)
    print("Done!")


if __name__ == "__main__":
    main()
