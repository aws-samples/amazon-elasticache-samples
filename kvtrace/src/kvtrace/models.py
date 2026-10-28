"""KV-cache size per token from Hugging Face model configs, plus convenience presets.

Standard (MHA/GQA) attention stores one K and one V vector per KV head per layer::

    bytes/token = 2 * num_hidden_layers * num_key_value_heads * head_dim * dtype_bytes

MLA models (DeepSeek V2/V3; config has ``kv_lora_rank``) store one compressed
latent plus the decoupled RoPE key per layer::

    bytes/token = (kv_lora_rank + qk_rope_head_dim) * num_hidden_layers * dtype_bytes
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

# Values copied from the public Hugging Face config.json of each model.
# These are a convenience only; pass --model-config for the exact model you serve.
PRESETS: Dict[str, Dict[str, Any]] = {
    "llama-3.1-8b": {
        "num_hidden_layers": 32, "num_attention_heads": 32, "num_key_value_heads": 8,
        "hidden_size": 4096,
    },
    "llama-3.1-70b": {
        "num_hidden_layers": 80, "num_attention_heads": 64, "num_key_value_heads": 8,
        "hidden_size": 8192,
    },
    "qwen2.5-72b": {
        "num_hidden_layers": 80, "num_attention_heads": 64, "num_key_value_heads": 8,
        "hidden_size": 8192,
    },
    "qwen3-32b": {
        "num_hidden_layers": 64, "num_attention_heads": 64, "num_key_value_heads": 8,
        "hidden_size": 5120, "head_dim": 128,
    },
    "deepseek-v3": {
        "num_hidden_layers": 61, "num_attention_heads": 128, "hidden_size": 7168,
        "kv_lora_rank": 512, "qk_rope_head_dim": 64,
    },
}


class ModelConfigError(ValueError):
    pass


def _text_config(config: Dict[str, Any]) -> Dict[str, Any]:
    # Multimodal configs nest the language model under text_config.
    inner = config.get("text_config")
    if isinstance(inner, dict) and "num_hidden_layers" in inner:
        return inner
    return config


def kv_bytes_per_token(config: Dict[str, Any], dtype_bytes: int = 2) -> int:
    cfg = _text_config(config)
    try:
        layers = int(cfg["num_hidden_layers"])
        if cfg.get("kv_lora_rank"):
            return (int(cfg["kv_lora_rank"]) + int(cfg.get("qk_rope_head_dim", 0))) * layers * dtype_bytes
        heads = int(cfg["num_attention_heads"])
        kv_heads = int(cfg.get("num_key_value_heads") or heads)
        head_dim = cfg.get("head_dim") or int(cfg["hidden_size"]) // heads
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelConfigError(f"model config lacks a required field: {exc}") from exc
    return 2 * layers * kv_heads * int(head_dim) * dtype_bytes


def load_model_config(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


_DTYPE_BYTES = {"float32": 4, "float": 4, "float16": 2, "half": 2, "bfloat16": 2,
                "float8_e4m3fn": 1, "float8_e5m2": 1, "fp8": 1}


def config_kv_dtype_bytes(config: Dict[str, Any]) -> Optional[Tuple[int, str]]:
    """KV element size a model config implies, with the field it came from, or None.

    A ``quantization_config.kv_cache_scheme`` (compressed-tensors) sets the KV cache dtype.
    Otherwise vLLM's ``--kv-cache-dtype auto`` uses the model dtype (``torch_dtype`` or
    ``dtype``). FP8 *weights* do not make an FP8 KV cache: ``--kv-cache-dtype fp8`` is an
    engine flag that no config file records, so give ``--kv-dtype-bytes 1`` for it.
    """
    quant = config.get("quantization_config")
    scheme = quant.get("kv_cache_scheme") if isinstance(quant, dict) else None
    if isinstance(scheme, dict) and isinstance(scheme.get("num_bits"), int) and scheme["num_bits"] > 0:
        return max(1, scheme["num_bits"] // 8), "quantization_config.kv_cache_scheme.num_bits"
    for cfg in (_text_config(config), config):
        for field_name in ("torch_dtype", "dtype"):
            value = cfg.get(field_name)
            if isinstance(value, str) and value.replace("torch.", "") in _DTYPE_BYTES:
                return _DTYPE_BYTES[value.replace("torch.", "")], field_name
    return None


# vLLM --kv-cache-dtype values and their bytes per KV element. ``auto`` means the
# model dtype (see config_kv_dtype_bytes).
KV_CACHE_DTYPE_BYTES = {"bf16": 2, "fp16": 2, "fp8": 1, "fp8_e4m3": 1, "fp8_e5m2": 1}
KV_CACHE_DTYPES = ("auto",) + tuple(KV_CACHE_DTYPE_BYTES)


def resolve_kv_dtype_bytes(config: Optional[Dict[str, Any]], kv_dtype_bytes: Optional[int] = None,
                           kv_cache_dtype: Optional[str] = None,
                           default: int = 2) -> Tuple[int, str, bool]:
    """Bytes per KV element, the text that says where it came from, and a flag.

    Order: ``--kv-dtype-bytes``, then ``--kv-cache-dtype`` (not ``auto``), then the
    model config, then ``default``. The flag is True when the value only assumes
    that the KV dtype is the model dtype (no flag and no KV scheme in the config).
    """
    explicit_dtype = kv_cache_dtype not in (None, "auto")
    if kv_dtype_bytes and explicit_dtype:
        raise ModelConfigError("give --kv-dtype-bytes or --kv-cache-dtype, not both")
    if kv_dtype_bytes:
        if kv_dtype_bytes < 1:
            raise ModelConfigError("--kv-dtype-bytes must be >= 1")
        return int(kv_dtype_bytes), "--kv-dtype-bytes", False
    if explicit_dtype:
        if kv_cache_dtype not in KV_CACHE_DTYPE_BYTES:
            raise ModelConfigError(f"unknown --kv-cache-dtype {kv_cache_dtype!r}; choose from "
                                   f"{', '.join(KV_CACHE_DTYPES)}")
        return KV_CACHE_DTYPE_BYTES[kv_cache_dtype], f"--kv-cache-dtype {kv_cache_dtype}", False
    found = config_kv_dtype_bytes(config) if config else None
    if found:
        return found[0], found[1], found[1] in ("torch_dtype", "dtype")
    return default, "default; config names no dtype", True


def kv_bytes_per_token_per_rank(config: Dict[str, Any], tp: int, dtype_bytes: int = 2,
                                pp: int = 1) -> int:
    """KV bytes per token that one worker (one TP rank of one PP stage) stores.

    vLLM splits KV heads over TP ranks and copies a head to several ranks when TP is
    larger than the number of KV heads, so each rank holds ``max(1, kv_heads // tp)``
    heads. MLA keeps the whole latent on every rank. Pipeline parallelism splits the
    layers over ``pp`` stages, so one worker holds ``layers / pp`` of them (the mean
    share, rounded, when the layers do not divide evenly).
    """
    if tp < 1:
        raise ModelConfigError("--tp must be >= 1")
    if pp < 1:
        raise ModelConfigError("--pp must be >= 1")
    cfg = _text_config(config)
    if cfg.get("kv_lora_rank"):
        return round(kv_bytes_per_token(config, dtype_bytes) / pp)
    try:
        heads = int(cfg["num_attention_heads"])
        kv_heads = int(cfg.get("num_key_value_heads") or heads)
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelConfigError(f"model config lacks a required field: {exc}") from exc
    per_tp_rank = kv_bytes_per_token(config, dtype_bytes) * max(1, kv_heads // tp) // kv_heads
    return round(per_tp_rank / pp)


def kv_bytes_per_token_all_workers(config: Dict[str, Any], tp: int, dtype_bytes: int = 2,
                                   pp: int = 1) -> int:
    """KV bytes per token that all ``tp * pp`` workers of one replica store together.

    This is what an LMCache-style L2 holds per token: every worker saves its own
    part. It is the per-worker size times the number of workers. The PP stages
    split the layers without overlap, so the sum over stages is exact.
    """
    if pp < 1:
        raise ModelConfigError("--pp must be >= 1")
    return kv_bytes_per_token_per_rank(config, tp, dtype_bytes) * tp


def resolve_config(model_config: Optional[str] = None, preset: Optional[str] = None
                   ) -> Optional[Dict[str, Any]]:
    if model_config:
        return load_model_config(model_config)
    if preset:
        if preset not in PRESETS:
            raise ModelConfigError(f"unknown preset {preset!r}; choose from {', '.join(PRESETS)}")
        return PRESETS[preset]
    return None


def resolve_kv_bytes(kv_bytes: Optional[int] = None, model_config: Optional[str] = None,
                     preset: Optional[str] = None, dtype_bytes: int = 2) -> Optional[int]:
    """Pick the KV size from the first given source: explicit bytes, config file, preset."""
    if kv_bytes:
        return int(kv_bytes)
    if model_config:
        return kv_bytes_per_token(load_model_config(model_config), dtype_bytes)
    if preset:
        if preset not in PRESETS:
            raise ModelConfigError(f"unknown preset {preset!r}; choose from {', '.join(PRESETS)}")
        return kv_bytes_per_token(PRESETS[preset], dtype_bytes)
    return None
