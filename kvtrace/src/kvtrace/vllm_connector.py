"""Log-only vLLM KV connector: one JSON line per request with its L0 and external hits.

Import this module only inside a vLLM process; it needs ``vllm`` (written against the
v0.30.0 ``KVConnectorBase_V1`` API). Chain it after LMCache with vLLM's MultiConnector;
MultiConnector gives the load to the first child that reports matched tokens, and this
connector always reports 0, so LMCache keeps every load::

    --kv-transfer-config '{"kv_connector": "MultiConnector", "kv_role": "kv_both",
      "kv_connector_extra_config": {"connectors": [
        {"kv_connector": "LMCacheConnectorV1", "kv_role": "kv_both"},
        {"kv_connector": "KVTraceLogConnector", "kv_role": "kv_both",
         "kv_connector_module_path": "kvtrace.vllm_connector",
         "kv_connector_extra_config": {"path": "/var/tmp/kvtrace/requests-{pid}.jsonl"}}]}}'

``{pid}`` gives each data-parallel engine process its own file; a path without it gets
``-<pid>`` added before the extension. See :mod:`kvtrace.reqlog` for the record format.
"""

from __future__ import annotations

try:
    from vllm.distributed.kv_transfer.kv_connector.v1.base import (
        KVConnectorBase_V1,
        KVConnectorMetadata,
        KVConnectorRole,
    )
except ImportError as exc:  # pragma: no cover - exercised only without vllm
    raise ImportError("kvtrace.vllm_connector needs vllm (pip install vllm); "
                      "it is loaded by vLLM through kv_connector_module_path") from exc

try:
    from vllm.distributed.kv_transfer.kv_connector.v1.base import SupportsHMA
except ImportError:  # pragma: no cover - older vLLM without the hybrid allocator API
    SupportsHMA = None  # type: ignore[assignment, misc]

from .reqlog import make_log_connector

KVTraceLogConnector = make_log_connector(KVConnectorBase_V1, KVConnectorMetadata,
                                         KVConnectorRole.SCHEDULER, SupportsHMA)

__all__ = ["KVTraceLogConnector"]
