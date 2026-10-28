"""Convert decoded event batches into normalized, privacy-safe trace records.

Record types (one JSON object per line in a segment):

- ``S`` BlockStored, ``R`` BlockRemoved, ``C`` AllBlocksCleared
- ``M`` metrics sample, ``G`` sequence gap, ``X`` publisher restart
- ``T`` per-segment recorder tallies, ``H`` segment header

Only hashes, counts, sizes and metadata are kept. Token ids never reach here
(``wire`` reduces them to a count).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Dict, List, Optional

from .wire import AllBlocksCleared, BlockRemoved, BlockStored, Event, EventBatch

GPU = "GPU"
_U64_MASK = (1 << 64) - 1
_HEX = set("0123456789abcdef")


def normalize_hash(value: Any) -> str:
    """Return a canonical lowercase hex string for an engine block hash.

    - Ints in the signed/unsigned 64-bit range are written as their 64-bit
      two's-complement value with 16 hex digits. SGLang (int64) and vLLM
      (uint64) therefore agree on the same bit pattern.
    - Larger ints use minimal hex (negative values get a leading ``-``).
    - Bytes (newer vLLM) become plain hex.
    """
    if isinstance(value, bool):
        raise TypeError("bool is not a block hash")
    if isinstance(value, int):
        if -(1 << 63) <= value <= _U64_MASK:
            return format(value & _U64_MASK, "016x")
        return format(value, "x")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, str):
        lowered = value.lower()
        if lowered and set(lowered) <= _HEX:
            return lowered
        return value.encode("utf-8").hex()
    raise TypeError(f"unsupported block hash type {type(value).__name__}")


def normalize_medium(medium: Optional[str]) -> str:
    """None means the GPU (L1) tier. Other values are kept as upper-case tier names."""
    if medium is None or medium == "":
        return GPU
    return str(medium).upper()


def _digest(value: Any, key: Optional[bytes] = None) -> str:
    blob = json.dumps(value, sort_keys=True, default=repr, separators=(",", ":")).encode("utf-8")
    if key:
        return hmac.new(key, blob, hashlib.sha256).hexdigest()[:16]
    return hashlib.sha256(blob).hexdigest()[:16]


def namespace_of(event: BlockStored, key: Optional[bytes] = None) -> str:
    """Build the cache namespace of a stored block.

    Blocks in different namespaces can never share KV data (different LoRA
    adapter or cache salt), so the simulator keys L2 entries by namespace. The
    salt is digested (keyed with the anonymization key when one is given).

    vLLM ``extra_keys`` is a per-block list (``[None] * n`` for plain text) whose
    content is already folded into each chained block hash, so it is not part of
    the namespace: including it would split one prefix across namespaces.
    """
    parts = []
    if event.lora_name:
        parts.append(f"lora={event.lora_name}")
    elif event.lora_id not in (None, 0):
        parts.append(f"lora={event.lora_id}")
    if event.cache_salt:
        parts.append(f"salt={_digest(event.cache_salt, key)}")
    group = group_namespace(event.group_idx)
    if group:
        parts.append(group)
    return "|".join(parts)


def group_namespace(group_idx: Optional[int]) -> str:
    """Return the namespace part of a vLLM KV cache group: ``g=N``, or "" for group 0/None.

    ``S`` and ``R`` records use the same rule, so a removal can be tied to the store
    of its group. Hybrid-attention models reuse one block hash in each group.
    """
    if group_idx in (None, 0):
        return ""
    return f"g={group_idx}"


class Anonymizer:
    """Keyed HMAC-SHA256 of the canonical hex hash, truncated to 16 bytes.

    The same key must be used on every node, or cross-node reuse is lost.
    """

    def __init__(self, key: bytes):
        if not key:
            raise ValueError("anonymization key is empty")
        self._key = key

    @classmethod
    def from_file(cls, path: str) -> Anonymizer:
        with open(path, "rb") as fh:
            return cls(fh.read().strip())

    def __call__(self, hex_hash: str) -> str:
        # UTF-8 equals ASCII for hex hashes; it also accepts any request id string.
        return hmac.new(self._key, hex_hash.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    @property
    def key(self) -> bytes:
        return self._key


class Normalizer:
    """Turn ``EventBatch`` objects into record dicts for one node."""

    def __init__(self, node: str, model: str, anonymizer: Optional[Anonymizer] = None):
        self.node = node
        self.model = model
        self.anonymizer = anonymizer
        self.bad_events = 0

    def _h(self, value: Any) -> str:
        hexed = normalize_hash(value)
        return self.anonymizer(hexed) if self.anonymizer else hexed

    def _record(self, event: Event, ts: float, rank: int) -> Optional[Dict[str, Any]]:
        if isinstance(event, BlockStored):
            hashes = [self._h(h) for h in event.block_hashes]
            parent = event.parent_block_hash
            key = self.anonymizer.key if self.anonymizer else None
            return {
                "t": "S",
                "ts": ts,
                "node": self.node,
                "rank": rank,
                "model": self.model,
                "ns": namespace_of(event, key),
                "medium": normalize_medium(event.medium),
                "bs": event.block_size,
                "h": hashes,
                "p": None if parent is None else self._h(parent),
                "n": event.num_tokens if event.num_tokens else len(hashes) * event.block_size,
            }
        if isinstance(event, BlockRemoved):
            record = {
                "t": "R",
                "ts": ts,
                "node": self.node,
                "rank": rank,
                "medium": normalize_medium(event.medium),
                "h": [self._h(h) for h in event.block_hashes],
            }
            # A removal carries no LoRA or salt, so its ns is only the group part of
            # the store's ns. Group 0 adds no key, which keeps such records as before.
            group = group_namespace(event.group_idx)
            if group:
                record["ns"] = group
            return record
        if isinstance(event, AllBlocksCleared):
            return {"t": "C", "ts": ts, "node": self.node, "rank": rank}
        return None

    def records(self, batch: EventBatch, rank: int) -> List[Dict[str, Any]]:
        """Normalize one batch. An event with an unusable hash is counted in
        ``bad_events`` and skipped; the rest of the batch is kept."""
        out: List[Dict[str, Any]] = []
        for event in batch.events:
            try:
                record = self._record(event, batch.ts, rank)
            except (TypeError, ValueError):
                self.bad_events += 1
                continue
            if record is not None:
                out.append(record)
        return out
