"""Tolerant decoder for the vLLM / SGLang KV-event ZMQ wire format.

Both engines publish multipart messages ``[topic, seq, payload]`` on a PUB socket.
``seq`` is an 8-byte big-endian signed integer. ``payload`` is a msgpack-encoded
msgspec ``EventBatch`` that is normally ``array_like``::

    [ts, events, data_parallel_rank]          # rank can be absent (omit_defaults)

and each event is a tagged array whose first element is the type name::

    ["BlockStored", block_hashes, parent_block_hash, token_ids, block_size,
     lora_id, medium, ...engine/version specific trailing fields]
    ["BlockRemoved", block_hashes, medium, group_idx (vLLM), ...]
    ["AllBlocksCleared", ...]

The decoder reads core fields by position and ignores what it does not know.
It also accepts the map form (``{"type": "BlockStored", ...}``) defensively.

Privacy: ``token_ids`` are reduced to a count here, so raw prompt tokens never
leave this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Union

import msgpack

log = logging.getLogger(__name__)

SEQ_END_MARKER = -1

# Trailing (non-core) BlockStored fields, by engine. Positions after ``medium``.
# SGLang declares cache_salt, then session_id (python/sglang/srt/disaggregation/kv_events.py).
TRAILING_LAYOUTS = {
    "vllm": ("lora_name", "extra_keys", "group_idx"),
    "sglang": ("cache_salt", "session_id", "lora_name"),
}
_TAG_KEYS = ("type", "_type", "tag", "kind")


class DecodeError(ValueError):
    """The payload is not a recognizable KV event batch."""


@dataclass
class BlockStored:
    block_hashes: List[Any]
    parent_block_hash: Any
    num_tokens: Optional[int]
    block_size: int
    lora_id: Optional[int] = None
    medium: Optional[str] = None
    lora_name: Optional[str] = None
    cache_salt: Optional[str] = None
    extra_keys: Any = None
    group_idx: Optional[int] = None


@dataclass
class BlockRemoved:
    block_hashes: List[Any]
    medium: Optional[str] = None
    # vLLM KV cache group (hybrid attention); matches BlockStored.group_idx.
    group_idx: Optional[int] = None


@dataclass
class AllBlocksCleared:
    pass


Event = Union[BlockStored, BlockRemoved, AllBlocksCleared]


@dataclass
class EventBatch:
    ts: float
    events: List[Event] = field(default_factory=list)
    dp_rank: Optional[int] = None
    unknown_events: int = 0
    bad_events: int = 0


def decode_seq(frame: bytes) -> int:
    if len(frame) != 8:
        raise DecodeError(f"sequence frame has {len(frame)} bytes, expected 8")
    return int.from_bytes(frame, "big", signed=True)


def encode_seq(seq: int) -> bytes:
    return int(seq).to_bytes(8, "big", signed=True)


def _as_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _guess_layout(trailing: Sequence[Any]) -> str:
    """Pick a trailing-field layout when the engine is not known.

    SGLang puts ``session_id`` (a string) second and ``lora_name`` (a string)
    third, while vLLM puts ``extra_keys`` (a list or None) second and
    ``group_idx`` (an int) third. A string in either slot means SGLang; a list
    in the second slot or an int in the third means vLLM.

    Without such a sign the layout is ambiguous. msgspec omits trailing default
    fields, so an SGLang event with only ``cache_salt`` (``[salt]`` or
    ``[salt, None]``) looks like a vLLM event with only ``lora_name``. A string
    first is then read as an SGLang salt: the salt is digested, while the vLLM
    reading would store it raw as a LoRA name. A real vLLM LoRA name read this
    way still gets its own namespace, only digested.
    """
    second = trailing[1] if len(trailing) >= 2 else None
    third = trailing[2] if len(trailing) >= 3 else None
    if isinstance(second, (str, bytes)) or isinstance(third, (str, bytes)):
        return "sglang"
    if isinstance(second, (list, tuple)) or _as_int(third) is not None:
        return "vllm"
    if trailing and isinstance(trailing[0], (str, bytes)):
        return "sglang"
    return "vllm"


def _block_stored_from_values(core: Sequence[Any], named: dict) -> BlockStored:
    hashes, parent, token_ids, block_size = core[0], core[1], core[2], core[3]
    if not isinstance(hashes, (list, tuple)):
        raise DecodeError("BlockStored.block_hashes is not a list")
    if not isinstance(block_size, int) or isinstance(block_size, bool) or block_size <= 0:
        raise DecodeError("BlockStored.block_size is not a positive int")
    num_tokens = len(token_ids) if isinstance(token_ids, (list, tuple)) else None
    return BlockStored(
        block_hashes=list(hashes),
        parent_block_hash=parent,
        num_tokens=num_tokens,
        block_size=block_size,
        lora_id=_as_int(core[4]) if len(core) > 4 else None,
        medium=_as_str(core[5]) if len(core) > 5 else None,
        lora_name=_as_str(named.get("lora_name")),
        cache_salt=_as_str(named.get("cache_salt")),
        extra_keys=named.get("extra_keys"),
        group_idx=_as_int(named.get("group_idx")),
    )


def _decode_array_event(raw: Sequence[Any], engine: str) -> Optional[Event]:
    tag = _as_str(raw[0]) if raw else None
    body = list(raw[1:])
    if tag == "BlockStored":
        if len(body) < 4:
            raise DecodeError("BlockStored has too few fields")
        core = body[:6]
        trailing = body[6:]
        layout = TRAILING_LAYOUTS.get(engine) or TRAILING_LAYOUTS[_guess_layout(trailing)]
        named = dict(zip(layout, trailing))
        return _block_stored_from_values(core, named)
    if tag == "BlockRemoved":
        if not body or not isinstance(body[0], (list, tuple)):
            raise DecodeError("BlockRemoved.block_hashes is not a list")
        # vLLM appends group_idx after medium (then locality, ownership). SGLang has
        # no field there, so its removals never carry a group.
        group_idx = _as_int(body[2]) if len(body) > 2 and engine != "sglang" else None
        return BlockRemoved(list(body[0]), _as_str(body[1]) if len(body) > 1 else None,
                            group_idx)
    if tag == "AllBlocksCleared":
        return AllBlocksCleared()
    return None


def _decode_map_event(raw: dict) -> Optional[Event]:
    raw = {_as_str(k): v for k, v in raw.items()}
    tag = next((_as_str(raw[k]) for k in _TAG_KEYS if k in raw), None)
    if tag == "BlockStored":
        core = [
            raw.get("block_hashes"),
            raw.get("parent_block_hash"),
            raw.get("token_ids"),
            raw.get("block_size"),
            raw.get("lora_id"),
            raw.get("medium"),
        ]
        return _block_stored_from_values(core, raw)
    if tag == "BlockRemoved":
        hashes = raw.get("block_hashes")
        if not isinstance(hashes, (list, tuple)):
            raise DecodeError("BlockRemoved.block_hashes is not a list")
        return BlockRemoved(list(hashes), _as_str(raw.get("medium")),
                            _as_int(raw.get("group_idx")))
    if tag == "AllBlocksCleared":
        return AllBlocksCleared()
    return None


def decode_batch(payload: bytes, engine: str = "auto") -> EventBatch:
    """Decode one payload. Raises DecodeError when the batch itself is unusable.

    Individual malformed events are counted in ``bad_events`` and skipped;
    unknown event types are counted in ``unknown_events``.
    """
    try:
        obj = msgpack.unpackb(payload, raw=False, strict_map_key=False)
    except Exception as exc:  # msgpack raises several unrelated exception types
        raise DecodeError(f"msgpack decode failed: {exc}") from exc

    if isinstance(obj, (list, tuple)):
        if len(obj) < 2:
            raise DecodeError("batch array has fewer than 2 fields")
        ts, events = obj[0], obj[1]
        rank = obj[2] if len(obj) > 2 else None
    elif isinstance(obj, dict):
        obj = {_as_str(k): v for k, v in obj.items()}
        ts, events = obj.get("ts"), obj.get("events")
        rank = obj.get("data_parallel_rank")
    else:
        raise DecodeError(f"batch is a {type(obj).__name__}")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        raise DecodeError("batch ts is not a number")
    if not isinstance(events, (list, tuple)):
        raise DecodeError("batch events is not a list")

    batch = EventBatch(ts=float(ts), dp_rank=_as_int(rank))
    for raw in events:
        try:
            if isinstance(raw, (list, tuple)):
                event = _decode_array_event(raw, engine)
            elif isinstance(raw, dict):
                event = _decode_map_event(raw)
            else:
                raise DecodeError(f"event is a {type(raw).__name__}")
        except (DecodeError, TypeError, IndexError) as exc:
            log.debug("skip malformed event: %s", exc)
            batch.bad_events += 1
            continue
        if event is None:
            batch.unknown_events += 1
        else:
            batch.events.append(event)
    return batch
