"""KV size per token for ``simulate`` (L2 = all workers) and ``tiers`` (one worker)."""

import gzip
import json

import pytest

from kvtrace.cli import main
from kvtrace.models import (
    ModelConfigError,
    kv_bytes_per_token_all_workers,
    kv_bytes_per_token_per_rank,
    resolve_kv_dtype_bytes,
)

# Qwen3-235B-A22B config.json fields that set the KV size (94 layers, 4 KV heads, head_dim 128).
QWEN3_235B = {"num_hidden_layers": 94, "num_attention_heads": 64, "num_key_value_heads": 4,
              "head_dim": 128, "hidden_size": 4096, "torch_dtype": "bfloat16",
              "quantization_config": {"quant_method": "fp8"}}
FULL_BF16 = 2 * 94 * 4 * 128 * 2  # 192,512: the whole model's KV at bf16


def test_tp_sizing_from_one():
    # TP 1, 2 and 4 split the 4 KV heads; TP 8 copies each head to two ranks.
    per_rank = {tp: kv_bytes_per_token_per_rank(QWEN3_235B, tp, 2) for tp in (1, 2, 4, 8)}
    assert per_rank == {1: FULL_BF16, 2: FULL_BF16 // 2, 4: FULL_BF16 // 4, 8: FULL_BF16 // 4}
    totals = {tp: kv_bytes_per_token_all_workers(QWEN3_235B, tp, 2) for tp in (1, 2, 4, 8)}
    assert totals == {1: FULL_BF16, 2: FULL_BF16, 4: FULL_BF16, 8: 2 * FULL_BF16}


def test_tp_larger_than_kv_heads_with_fp8_kv():
    # The README example: TP 8, fp8 KV -> 24,064 B per worker, 192,512 B in L2.
    assert kv_bytes_per_token_per_rank(QWEN3_235B, 8, 1) == 24064
    assert kv_bytes_per_token_all_workers(QWEN3_235B, 8, 1) == 192512


def test_pipeline_parallel_splits_layers_per_worker():
    llama = {"num_hidden_layers": 32, "num_attention_heads": 32, "num_key_value_heads": 8,
             "hidden_size": 4096}
    assert kv_bytes_per_token_per_rank(llama, 2, 2, pp=1) == 65536
    assert kv_bytes_per_token_per_rank(llama, 2, 2, pp=4) == 16384
    # PP stages hold different layers, so the L2 total does not change.
    assert kv_bytes_per_token_all_workers(llama, 2, 2, pp=4) == 131072
    # 94 layers over 4 stages: the mean share (23.5 layers).
    assert kv_bytes_per_token_per_rank(QWEN3_235B, 8, 1, pp=4) == 6016
    with pytest.raises(ModelConfigError):
        kv_bytes_per_token_per_rank(llama, 1, 2, pp=0)


def test_resolve_kv_dtype_bytes_precedence():
    assert resolve_kv_dtype_bytes(QWEN3_235B) == (2, "torch_dtype", True)
    assert resolve_kv_dtype_bytes(QWEN3_235B, kv_cache_dtype="auto") == (2, "torch_dtype", True)
    assert resolve_kv_dtype_bytes(QWEN3_235B, kv_cache_dtype="fp8") == (1, "--kv-cache-dtype fp8", False)
    assert resolve_kv_dtype_bytes(QWEN3_235B, kv_dtype_bytes=4) == (4, "--kv-dtype-bytes", False)
    assert resolve_kv_dtype_bytes(None) == (2, "default; config names no dtype", True)
    scheme = dict(QWEN3_235B, quantization_config={"kv_cache_scheme": {"num_bits": 8}})
    assert resolve_kv_dtype_bytes(scheme)[0::2] == (1, False)
    with pytest.raises(ModelConfigError):
        resolve_kv_dtype_bytes(QWEN3_235B, kv_dtype_bytes=1, kv_cache_dtype="fp8")


def _trace(tmp_path):
    rec = {"t": "S", "ts": 1.0, "node": "A", "rank": 0, "model": "m", "ns": "", "medium": "GPU",
           "bs": 16, "h": ["a1", "a2"], "p": None, "n": 32}
    path = tmp_path / "t.jsonl.gz"
    with gzip.open(path, "wt") as fh:
        fh.write(json.dumps(rec) + "\n")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(QWEN3_235B))
    return path, cfg


@pytest.mark.parametrize("extra, expected", [
    ([], FULL_BF16),  # TP 1, KV dtype = model dtype (bf16): unchanged from before
    (["--tp", "8"], 2 * FULL_BF16),  # each of 8 workers saves one (copied) head
    (["--tp", "8", "--kv-cache-dtype", "fp8"], 192512),
    (["--tp", "8", "--kv-dtype-bytes", "1"], 192512),
    (["--tp", "8", "--pp", "2", "--kv-cache-dtype", "fp8"], 192512),
    (["--tp", "4", "--kv-cache-dtype", "fp8_e4m3"], FULL_BF16 // 2),
])
def test_cli_simulate_l2_kv_size_counts_every_worker(tmp_path, extra, expected):
    trace, cfg = _trace(tmp_path)
    out = tmp_path / "sim.json"
    assert main(["simulate", str(trace), "--model-config", str(cfg), "--chunk-tokens", "32",
                 "--capacity", "1GiB", "-o", str(out)] + extra) == 0
    sim = json.loads(out.read_text())
    assert sim["inputs"]["params"]["kv_bytes_per_token"] == expected
    assert "per worker x" in sim["inputs"]["kv_size_source"]


def test_cli_simulate_explicit_kv_bytes_wins(tmp_path):
    trace, _cfg = _trace(tmp_path)
    out = tmp_path / "sim.json"
    assert main(["simulate", str(trace), "--kv-bytes-per-token", "1000", "--tp", "8",
                 "--kv-cache-dtype", "fp8", "--chunk-tokens", "32", "--capacity", "1GiB",
                 "-o", str(out)]) == 0
    sim = json.loads(out.read_text())
    assert sim["inputs"]["params"]["kv_bytes_per_token"] == 1000
    assert sim["inputs"]["kv_size_source"] == "--kv-bytes-per-token"


def test_cli_simulate_kv_dtype_options_conflict(tmp_path, capsys):
    trace, cfg = _trace(tmp_path)
    assert main(["simulate", str(trace), "--model-config", str(cfg), "--kv-dtype-bytes", "1",
                 "--kv-cache-dtype", "fp8", "-o", str(tmp_path / "s.json")]) == 2
    assert "not both" in capsys.readouterr().err


def _tiers_trace(tmp_path):
    from test_tiers import lmcache_samples, m_records, write_segment
    write_segment(tmp_path / "A1-1-00000.kvt.jsonl.gz", m_records(lmcache_samples()))
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(QWEN3_235B))
    return cfg


def test_cli_tiers_kv_cache_dtype_and_default_note(tmp_path, capsys):
    cfg = _tiers_trace(tmp_path)
    out = tmp_path / "t.json"
    base = ["tiers", str(tmp_path), "-o", str(out), "--model-config", str(cfg), "--tp", "8"]
    assert main(base) == 0
    result = json.loads(out.read_text())
    assert result["inputs"]["worker_kv_bytes_per_token"] == 48128
    assert any("assumes KV dtype = model dtype" in n for n in result["notes"])
    assert "assumes KV dtype = model dtype" in capsys.readouterr().err
    assert main(base + ["--kv-cache-dtype", "fp8"]) == 0
    result = json.loads(out.read_text())
    assert result["inputs"]["worker_kv_bytes_per_token"] == 24064
    assert "(--kv-cache-dtype fp8)" in result["inputs"]["worker_kv_source"]
    assert not any("assumes KV dtype" in n for n in result["notes"])


def test_cli_tiers_pp_divides_the_worker_size(tmp_path):
    cfg = _tiers_trace(tmp_path)
    out = tmp_path / "t.json"
    assert main(["tiers", str(tmp_path), "-o", str(out), "--model-config", str(cfg), "--tp", "8",
                 "--pp", "2", "--kv-cache-dtype", "fp8"]) == 0
    result = json.loads(out.read_text())
    assert result["inputs"]["worker_kv_bytes_per_token"] == 12032
    assert "--pp 2" in result["inputs"]["worker_kv_source"]
