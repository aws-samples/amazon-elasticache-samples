#!/usr/bin/env python3
"""Tool-neutral chunk view of a Mooncake trace, for the simulator cross-check.

Writes one JSON line per request, in replay order ``(timestamp, line)``:
``{"line", "ts_ms", "input_length", "chunks": [...]}``. ``chunks`` holds the keys
of the request's full ``--chunk-tokens`` chunks. A 512-token Mooncake block
``h`` becomes the chunks ``"h:0"`` and ``"h:1"`` (only full chunks; a partial
last chunk is never cached). Positions past the end of ``hash_ids`` become
request-unique ``"pad:<line>:<j>"`` chunks, as in ``synth_from_mooncake.py``.
The keys are not chained: ``--check`` verifies that every hash id has one
parent and one position in the whole trace, so the id alone names its prefix.

Variants: ``full`` (every request), ``k32`` (prompt + output <= 32768, the
``ground_truth.py`` default), ``bench`` (the lines of a ``bench-requests.jsonl``
index, with prompts clamped to ``--max-model-len``). ``--trace-out`` writes the
selected requests as a Mooncake trace; run every tool on that file so line
numbers agree.

Standard library only.

    python examples/xcheck/prepare.py conversation_trace.jsonl --variant full \\
        -o out/xcheck/full.chunks.jsonl --check out/xcheck/trace-check.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mooncake_replay import BLOCK_TOKENS, Request, load_trace, select  # noqa: E402


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for part in iter(lambda: fh.read(1 << 20), b""):
            h.update(part)
    return h.hexdigest()


def prompt_length(req: Request, max_model_len: Optional[int]) -> int:
    """Same clamp as ``synth_from_mooncake.prompt_length``."""
    if not max_model_len:
        return req.input_length
    return min(req.input_length, max(1, max_model_len - max(1, req.output_length)))


def chunk_keys(req: Request, chunk: int, max_model_len: Optional[int] = None) -> List[str]:
    per_block = BLOCK_TOKENS // chunk
    keys = []
    for j in range(prompt_length(req, max_model_len) // chunk):
        idx = j // per_block
        if idx < len(req.hash_ids):
            keys.append(f"{req.hash_ids[idx]}:{j % per_block}")
        else:
            keys.append(f"pad:{req.index}:{j}")
    return keys


def select_variant(requests: Sequence[Request], variant: str,
                   bench_index: Optional[str]) -> List[Request]:
    if variant == "full":
        kept = list(requests)
    elif variant == "k32":
        kept, _ = select(requests, 32768)
    elif variant == "bench":
        if not bench_index:
            raise SystemExit("--variant bench needs --bench-index")
        with open(bench_index) as fh:
            lines = {int(json.loads(row)["line"]) for row in fh if row.strip()}
        kept = [r for r in requests if r.index in lines]
    else:
        raise SystemExit(f"unknown variant {variant!r}")
    return sorted(kept, key=lambda r: (r.timestamp_ms, r.index))


def check_trace(requests: Sequence[Request]) -> Dict[str, Any]:
    """Facts the chunk mapping depends on."""
    parent: Dict[int, Any] = {}
    position: Dict[int, int] = {}
    multi_parent = multi_position = 0
    for r in requests:
        prev: Any = None
        for pos, h in enumerate(r.hash_ids):
            if parent.setdefault(h, prev) != prev:
                multi_parent += 1
            if position.setdefault(h, pos) != pos:
                multi_position += 1
            prev = h
    # A partial last block: its id must not appear as a full block elsewhere, or a
    # 512-token ground truth would credit tokens the partial request never had.
    full_uses: Dict[int, int] = {}
    partial_last: Dict[int, int] = {}
    for r in requests:
        n_full = min(len(r.hash_ids), r.input_length // BLOCK_TOKENS)
        for h in r.hash_ids[:n_full]:
            full_uses[h] = full_uses.get(h, 0) + 1
        if r.input_length % BLOCK_TOKENS and len(r.hash_ids) > n_full:
            h = r.hash_ids[n_full]
            partial_last[h] = partial_last.get(h, 0) + 1
    shared_partial = sum(1 for h in partial_last if h in full_uses or partial_last[h] > 1)
    pad = sum(1 for r in requests if r.input_length > len(r.hash_ids) * BLOCK_TOKENS)
    extra = sum(1 for r in requests if len(r.hash_ids) > -(-r.input_length // BLOCK_TOKENS))
    return {
        "requests": len(requests),
        "hash_ids_with_two_parents": multi_parent,
        "hash_ids_at_two_positions": multi_position,
        "partial_last_blocks": sum(partial_last.values()),
        "partial_last_block_ids_shared": shared_partial,
        "requests_needing_pad_blocks": pad,
        "requests_with_extra_hash_ids": extra,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("trace")
    p.add_argument("--variant", choices=("full", "k32", "bench"), default="full")
    p.add_argument("--bench-index", help="bench-requests.jsonl (lines of the benchmark's OK requests)")
    p.add_argument("--max-model-len", type=int, help="clamp prompts like synth_from_mooncake.py")
    p.add_argument("--chunk-tokens", type=int, default=256)
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--check", help="also write trace facts (sha256, id uniqueness) here")
    p.add_argument("--trace-out", help="also write the selected requests as a Mooncake trace")
    a = p.parse_args(argv)
    if BLOCK_TOKENS % a.chunk_tokens:
        p.error(f"--chunk-tokens must divide {BLOCK_TOKENS}")
    raw = load_trace(a.trace)
    kept = select_variant(raw, a.variant, a.bench_index)
    if a.trace_out:
        with open(a.trace_out, "w") as fh:
            for r in sorted(kept, key=lambda r: r.index):
                fh.write(json.dumps({"timestamp": r.timestamp_ms, "input_length": r.input_length,
                                     "output_length": r.output_length, "hash_ids": r.hash_ids},
                                    separators=(",", ":")) + "\n")
    n_tok = n_chunks = 0
    with open(a.out, "w") as fh:
        for r in kept:
            keys = chunk_keys(r, a.chunk_tokens, a.max_model_len)
            n_in = prompt_length(r, a.max_model_len)
            n_tok += n_in
            n_chunks += len(keys)
            fh.write(json.dumps({"line": r.index, "ts_ms": r.timestamp_ms, "input_length": n_in,
                                 "chunks": keys}, separators=(",", ":")) + "\n")
    info: Dict[str, Any] = {"variant": a.variant, "requests": len(kept), "input_tokens": n_tok,
                            "chunk_tokens": a.chunk_tokens, "chunks": n_chunks,
                            "unique_chunks": len({k for r in kept
                                                  for k in chunk_keys(r, a.chunk_tokens, a.max_model_len)})}
    if a.check:
        info["trace_sha256"] = sha256(a.trace)
        info["check_raw"] = check_trace(raw)
        info["check_selected"] = check_trace(kept)
        with open(a.check, "w") as fh:
            json.dump(info, fh, indent=2)
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
