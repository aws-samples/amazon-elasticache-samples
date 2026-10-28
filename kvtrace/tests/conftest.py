"""Shared test helpers. All sockets bind to 127.0.0.1 on OS-assigned ports."""

from __future__ import annotations

import gzip
import json
import threading
from typing import Any, Dict, List, Optional

import msgpack
import pytest
import zmq

from kvtrace.fake import FakePublisher, Workload
from kvtrace.normalize import Normalizer
from kvtrace.recorder import Recorder, RecorderConfig
from kvtrace.wire import decode_batch

LOCAL = "tcp://127.0.0.1:0"


def make_publisher(**kwargs: Any) -> FakePublisher:
    kwargs.setdefault("bind", LOCAL)
    kwargs.setdefault("replay_bind", LOCAL)
    kwargs.setdefault("rate", 200.0)
    kwargs.setdefault("wait_subscribers", 1)
    kwargs.setdefault("linger", 1.0)
    return FakePublisher(**kwargs)


def make_rank_pair(**kwargs: Any) -> List[FakePublisher]:
    """Two publishers on consecutive ports (rank 0 on p, rank 1 on p+1)."""
    for _ in range(50):
        first = make_publisher(dp_rank=0, **kwargs)
        port = int(first.endpoint.rsplit(":", 1)[1])
        rport = int(first.replay_endpoint.rsplit(":", 1)[1])
        try:
            second = make_publisher(dp_rank=1, bind=f"tcp://127.0.0.1:{port + 1}",
                                    replay_bind=f"tcp://127.0.0.1:{rport + 1}", **kwargs)
        except zmq.ZMQError:
            first.close()
            continue
        return [first, second]
    raise RuntimeError("could not bind consecutive ports")


def run_recorders(publishers: List[FakePublisher], configs: List[RecorderConfig],
                  sinks: Optional[List[Any]] = None) -> List[Recorder]:
    recorders = [Recorder(cfg, sink=(sinks[i] if sinks else None)) for i, cfg in enumerate(configs)]
    threads = [threading.Thread(target=r.run, daemon=True) for r in recorders]
    for t in threads:
        t.start()
    pub_threads = [p.start_thread() for p in publishers]
    for t in pub_threads + threads:
        t.join(timeout=30)
        assert not t.is_alive(), "thread did not finish"
    return recorders


def read_segment(path: str) -> List[Dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def workload_records(nodes: int = 2, requests: int = 200, engine: str = "vllm",
                     model: str = "m", **kwargs: Any) -> List[Dict[str, Any]]:
    """Generate normalized records without sockets: workload -> msgpack -> decode -> normalize."""
    records: List[Dict[str, Any]] = []
    for n in range(nodes):
        wl = Workload(engine=engine, seed=n + 1, prefix_seed=7, **kwargs)
        norm = Normalizer(f"node{n}", model)
        for i in range(requests):
            payload = msgpack.packb([1000.0 + i * 0.5 + n * 0.01, wl.next_request()],
                                    use_bin_type=True)
            records.extend(norm.records(decode_batch(payload, engine), 0))
    return records


@pytest.fixture
def sim_json(tmp_path):
    from kvtrace.simulator import SimParams, parse_capacities, simulate

    recs = workload_records(nodes=2, requests=150, gpu_tokens=4096)
    for rec in recs:
        rec["model"] = "<script>alert(1)</script>"
    params = SimParams(chunk_tokens=64, capacities=parse_capacities("1MiB,16MiB,inf"),
                       kv_bytes_per_token=1024)
    sim = simulate(recs, params, sources=["fixture"])
    sim["metrics"] = {"per_node": {}, "fleet": {"counters": {
        "vllm:prompt_tokens_total": 1_000_000.0,
        "vllm:request_prefill_time_seconds_sum": 100.0,
        "vllm:request_decode_time_seconds_sum": 300.0,
    }, "gauges": {}}}
    path = tmp_path / "sim.json"
    path.write_text(json.dumps(sim))
    return path
