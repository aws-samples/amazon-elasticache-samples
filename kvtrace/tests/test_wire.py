import json

import msgpack
import pytest

from kvtrace.normalize import (
    Anonymizer,
    Normalizer,
    _digest,
    namespace_of,
    normalize_hash,
    normalize_medium,
)
from kvtrace.wire import (
    AllBlocksCleared,
    BlockRemoved,
    BlockStored,
    DecodeError,
    decode_batch,
    decode_seq,
    encode_seq,
)


def pack(obj):
    return msgpack.packb(obj, use_bin_type=True)


def test_vllm_array_like_int_hashes_with_rank():
    batch = decode_batch(pack([12.5, [
        ["BlockStored", [1, 2], None, list(range(32)), 16, None, "GPU"],
        ["BlockRemoved", [1], "GPU"],
        ["AllBlocksCleared"],
    ], 3]), "vllm")
    assert batch.ts == 12.5 and batch.dp_rank == 3
    stored, removed, cleared = batch.events
    assert isinstance(stored, BlockStored) and stored.block_hashes == [1, 2]
    assert stored.num_tokens == 32 and stored.block_size == 16 and stored.medium == "GPU"
    assert isinstance(removed, BlockRemoved) and removed.medium == "GPU"
    assert isinstance(cleared, AllBlocksCleared)


def test_vllm_bytes_hashes_and_missing_trailing_fields_and_rank():
    batch = decode_batch(pack([1.0, [["BlockStored", [b"\x01\x02"], b"\xff", [5] * 16, 16]]]))
    (event,) = batch.events
    assert batch.dp_rank is None
    assert event.block_hashes == [b"\x01\x02"] and event.parent_block_hash == b"\xff"
    assert event.lora_id is None and event.medium is None


def test_vllm_extra_trailing_fields_are_ignored_or_named():
    event = ["BlockStored", [7], None, [1] * 16, 16, 2, "GPU", "my-lora", [["img", 3]], 1,
             "future-field", {"x": 1}]
    (stored,) = decode_batch(pack([1.0, [event]]), "vllm").events
    assert stored.lora_name == "my-lora" and stored.extra_keys == [["img", 3]]
    assert stored.group_idx == 1


def test_sglang_style_with_cache_salt_and_medium():
    event = ["BlockStored", [-5, 9], None, [1, 2], 1, None, "CPU_PINNED", "salt-1", "sess-9", "lora-a"]
    (stored,) = decode_batch(pack([2.0, [event], 0]), "auto").events
    assert stored.cache_salt == "salt-1" and stored.lora_name == "lora-a"
    assert stored.medium == "CPU_PINNED"
    assert "salt=" in namespace_of(stored) and "sess-9" not in namespace_of(stored)


def test_dict_form_events_and_batch():
    payload = pack({"ts": 3.0, "events": [
        {"type": "BlockStored", "block_hashes": [1], "parent_block_hash": None,
         "token_ids": [1] * 16, "block_size": 16, "medium": "GPU", "lora_name": "x"},
        {"_type": "BlockRemoved", "block_hashes": [1]},
        {"type": "AllBlocksCleared"},
    ], "data_parallel_rank": 1})
    batch = decode_batch(payload)
    assert [type(e).__name__ for e in batch.events] == ["BlockStored", "BlockRemoved", "AllBlocksCleared"]
    assert batch.events[0].lora_name == "x" and batch.dp_rank == 1


def test_unknown_and_malformed_events_are_counted_not_raised():
    batch = decode_batch(pack([1.0, [["NewEventType", 1], ["BlockStored", "bad"], 42,
                                     ["BlockRemoved", [1]]]]))
    assert batch.unknown_events == 1 and batch.bad_events == 2 and len(batch.events) == 1


@pytest.mark.parametrize("payload", [b"\xc1garbage", pack("str"), pack([1.0]), pack(["x", []]),
                                     pack([1.0, "notalist"]), b""])
def test_garbage_payload_raises_decode_error(payload):
    with pytest.raises(DecodeError):
        decode_batch(payload)


def test_seq_roundtrip():
    assert decode_seq(encode_seq(-1)) == -1
    assert decode_seq(encode_seq(2**40)) == 2**40
    with pytest.raises(DecodeError):
        decode_seq(b"\x00")


def test_hash_normalization():
    assert normalize_hash(-1) == "ffffffffffffffff"
    assert normalize_hash(2**64 - 1) == "ffffffffffffffff"  # int64 and uint64 views agree
    assert normalize_hash(1) == "0000000000000001"
    assert normalize_hash(-(2**63)) == "8000000000000000"
    assert normalize_hash(2**70) == format(2**70, "x")
    assert normalize_hash(b"\x00\xab") == "00ab"
    assert normalize_hash("ABCD") == "abcd"
    with pytest.raises(TypeError):
        normalize_hash(True)


def test_medium_normalization():
    assert normalize_medium(None) == "GPU"
    assert normalize_medium("cpu_pinned") == "CPU_PINNED"
    assert normalize_medium("EXTERNAL") == "EXTERNAL"


def test_normalizer_drops_token_ids_and_keeps_counts():
    batch = decode_batch(pack([1.0, [["BlockStored", [1, 2], 0, [987654321] * 32, 16, None, None]]]))
    (rec,) = Normalizer("n", "m").records(batch, 0)
    assert rec["n"] == 32 and rec["medium"] == "GPU" and rec["p"] == "0000000000000000"
    assert "987654321" not in str(rec) and "token_ids" not in rec


def test_anonymizer_is_keyed_and_stable():
    a, b = Anonymizer(b"k1"), Anonymizer(b"k2")
    assert a("00ff") == Anonymizer(b"k1")("00ff") and len(a("00ff")) == 32
    assert a("00ff") != b("00ff")


def test_vllm_per_block_extra_keys_do_not_split_the_prefix():
    # Real vLLM shape: extra_keys is one entry per block, [None] * n for plain text.
    norm = Normalizer("n", "m")
    first = ["BlockStored", [1, 2], None, [1] * 32, 16, None, "GPU", None, [None, None], 0]
    second = ["BlockStored", [3, 4, 5], 2, [1] * 48, 16, None, "GPU", None, [None] * 3, 0]
    recs = norm.records(decode_batch(pack([1.0, [first, second]]), "vllm"), 0)
    assert {r["ns"] for r in recs} == {""}


def test_cache_salt_digest_is_keyed_when_anonymizing():
    event = ["BlockStored", [1], None, [1], 1, None, None, "low-entropy-salt"]
    batch = decode_batch(pack([1.0, [event]]), "sglang")
    plain = Normalizer("n", "m").records(batch, 0)[0]["ns"]
    keyed = Normalizer("n", "m", Anonymizer(b"k")).records(batch, 0)[0]["ns"]
    assert plain.startswith("salt=") and keyed.startswith("salt=") and plain != keyed


SALT = "secret-salt-77"


@pytest.mark.parametrize("trailing", [
    [SALT],
    [SALT, None],
    [SALT, "sess-1"],
    [SALT, "sess-1", "lora-a"],
    [SALT, None, None],
], ids=["salt", "salt-none", "salt-session", "sglang-full", "salt-none-none"])
@pytest.mark.parametrize("key", [None, b"k"], ids=["plain", "anonymized"])
def test_auto_reads_sglang_salt_as_salt_never_raw(trailing, key):
    event = ["BlockStored", [1], None, [1], 1, None, "GPU", *trailing]
    (stored,) = decode_batch(pack([1.0, [event]]), "auto").events
    assert stored.cache_salt == SALT
    assert stored.lora_name == (trailing[2] if len(trailing) > 2 else None)
    anonymizer = Anonymizer(key) if key else None
    (rec,) = Normalizer("n", "m", anonymizer).records(decode_batch(pack([1.0, [event]]), "auto"), 0)
    assert SALT not in json.dumps(rec)
    assert f"salt={_digest(SALT, key)}" in rec["ns"]


@pytest.mark.parametrize("trailing, lora, extra, group", [
    (["lora-v", [["img", 1]], 1], "lora-v", [["img", 1]], 1),
    (["lora-v", [None]], "lora-v", [None], None),
    (["lora-v", None, 2], "lora-v", None, 2),
    ([None, [None, None], 0], None, [None, None], 0),
    ([None], None, None, None),
], ids=["vllm-full", "lora-extra", "lora-group", "plain", "none"])
def test_auto_keeps_vllm_layout_when_a_vllm_field_shows(trailing, lora, extra, group):
    event = ["BlockStored", [1], None, [1] * 16, 16, None, "GPU", *trailing]
    (stored,) = decode_batch(pack([1.0, [event]]), "auto").events
    assert (stored.lora_name, stored.extra_keys, stored.group_idx) == (lora, extra, group)
    assert stored.cache_salt is None


def test_bad_hash_drops_only_that_event():
    batch = decode_batch(pack([1.0, [["BlockStored", [1.5], None, [1] * 16, 16],
                                     ["BlockRemoved", [7]]]]))
    norm = Normalizer("n", "m")
    recs = norm.records(batch, 0)
    assert [r["t"] for r in recs] == ["R"] and norm.bad_events == 1


def test_real_vllm_031_map_events():
    # One batch captured from vLLM 0.31.0, token_ids replaced by 0..N-1. Events are
    # tagged msgpack maps (not arrays), hashes are uint64 ints and the batch is
    # [ts, events, data_parallel_rank].
    import os
    path = os.path.join(os.path.dirname(__file__), "fixtures", "vllm031_batch.msgpack")
    with open(path, "rb") as fh:
        batch = decode_batch(fh.read())
    assert batch.dp_rank == 0 and batch.bad_events == 0 and batch.unknown_events == 0
    one, many = batch.events
    assert isinstance(one, BlockStored) and one.block_size == 16 and one.medium == "GPU"
    assert one.num_tokens == 16 and one.parent_block_hash is not None and one.group_idx == 0
    assert many.num_tokens == 2032 and len(many.block_hashes) == 127
    assert many.parent_block_hash is None
    records = Normalizer("n", "m").records(batch, 0)
    assert [r["ns"] for r in records] == ["", ""]
    assert all(len(h) == 16 for r in records for h in r["h"])


def test_real_sglang_0522_map_events():
    # One batch captured from SGLang 0.5.22 (--page-size 16), token_ids replaced by
    # 0..N-1: tagged maps with signed int64 hashes, a BlockRemoved between stores.
    import os
    path = os.path.join(os.path.dirname(__file__), "fixtures", "sglang0522_batch.msgpack")
    with open(path, "rb") as fh:
        batch = decode_batch(fh.read())
    assert batch.dp_rank == 0 and batch.bad_events == 0
    stored, removed, stored2 = batch.events
    assert isinstance(removed, BlockRemoved) and len(removed.block_hashes) == 60
    assert stored.block_size == 16 and stored.num_tokens == 32 and stored.medium == "GPU"
    assert any(h < 0 for h in stored2.block_hashes + removed.block_hashes)
    records = Normalizer("n", "m").records(batch, 0)
    assert [r["t"] for r in records] == ["S", "R", "S"]
    assert all(len(h) == 16 for r in records for h in r["h"])
