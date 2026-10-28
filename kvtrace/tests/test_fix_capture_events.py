"""A BlockRemoved keeps its KV cache group, so its R record matches the group's S record."""

import msgpack
import pytest

from kvtrace.normalize import Normalizer
from kvtrace.wire import decode_batch

HASH = 0x1234


def _records(events, engine="auto"):
    batch = decode_batch(msgpack.packb([1.0, events, 0]), engine)
    assert batch.bad_events == 0 and batch.unknown_events == 0
    return Normalizer("n1", "m").records(batch, 0)


def _map_stored(group_idx):
    return {"type": "BlockStored", "block_hashes": [HASH], "parent_block_hash": None,
            "token_ids": [1] * 16, "block_size": 16, "lora_id": None, "medium": "GPU",
            "lora_name": None, "extra_keys": None, "group_idx": group_idx}


def _array_stored(group_idx):
    return ["BlockStored", [HASH], None, [1] * 16, 16, None, "GPU", None, None, group_idx]


@pytest.mark.parametrize("form", ["map", "array"])
def test_removal_of_group_1_carries_the_ns_of_its_store(form):
    if form == "map":
        events = [_map_stored(1),
                  {"type": "BlockRemoved", "block_hashes": [HASH], "medium": "GPU",
                   "group_idx": 1, "locality": None, "ownership": None}]
    else:
        events = [_array_stored(1), ["BlockRemoved", [HASH], "GPU", 1, None, None]]
    stored, removed = _records(events, engine="vllm" if form == "array" else "auto")
    assert stored["t"] == "S" and stored["ns"] == "g=1"
    assert removed == {"t": "R", "ts": 1.0, "node": "n1", "rank": 0, "medium": "GPU",
                       "h": [stored["h"][0]], "ns": stored["ns"]}


@pytest.mark.parametrize("form", ["map", "array"])
@pytest.mark.parametrize("group_idx", [0, None])
def test_removal_without_a_group_has_no_ns_key(form, group_idx):
    if form == "map":
        removal = {"type": "BlockRemoved", "block_hashes": [HASH], "medium": None,
                   "group_idx": group_idx}
    else:
        removal = ["BlockRemoved", [HASH], None, group_idx]
    (removed,) = _records([removal])
    assert removed == {"t": "R", "ts": 1.0, "node": "n1", "rank": 0, "medium": "GPU",
                       "h": ["0000000000001234"]}


def test_sglang_removal_never_reads_a_group():
    (removed,) = _records([["BlockRemoved", [HASH], None, 7]], engine="sglang")
    assert "ns" not in removed
