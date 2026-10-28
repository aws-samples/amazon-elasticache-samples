import json
import threading

import msgpack
import pytest
from conftest import make_publisher, make_rank_pair, read_segment, run_recorders

from kvtrace.fake import CANARY_TOKEN
from kvtrace.normalize import Anonymizer, normalize_hash
from kvtrace.recorder import Recorder, RecorderConfig, _Rank, endpoint_for_rank
from kvtrace.simulator import sort_records
from kvtrace.sinks import S3Sink
from kvtrace.wire import encode_seq


def cfg(pub, out, **kwargs):
    kwargs.setdefault("duration", 3.0)
    return RecorderConfig(endpoint=pub.endpoint, replay_endpoint=pub.replay_endpoint,
                          model="test-model", out_dir=str(out), node_id=kwargs.pop("node_id", "n1"),
                          **kwargs)


def stored_hashes(paths):
    found = set()
    for path in paths:
        for rec in read_segment(path):
            if rec["t"] == "S":
                found.update(rec["h"])
    return found


def test_endpoint_for_rank():
    assert endpoint_for_rank("tcp://10.0.0.1:5557", 0) == "tcp://10.0.0.1:5557"
    assert endpoint_for_rank("tcp://10.0.0.1:5557", 2) == "tcp://10.0.0.1:5559"


def test_vllm_record_end_to_end_without_token_ids(tmp_path):
    pub = make_publisher(engine="vllm", max_batches=40)
    (rec,) = run_recorders([pub], [cfg(pub, tmp_path)])
    assert rec.stats.batches == 40 and rec.stats.decode_errors == 0
    assert CANARY_TOKEN in pub.workload.published_tokens
    paths = rec.writer.closed_paths
    assert paths and all(p.endswith(".kvt.jsonl.gz") for p in paths)
    assert not list(tmp_path.rglob("*.part"))
    records = [r for p in paths for r in read_segment(p)]
    assert records[0]["t"] == "H" and records[0]["model"] == "test-model"
    assert records[0]["anonymized"] is False
    assert any(r["t"] == "S" for r in records) and records[-1]["t"] == "T"
    raw = "".join(json.dumps(r) for r in records)
    assert str(CANARY_TOKEN) not in raw
    assert all("token_ids" not in r for r in records)


def test_sglang_hicache_record(tmp_path):
    pub = make_publisher(engine="sglang", max_batches=60, gpu_tokens=2048, host_tokens=8192,
                         block_size=1, turn_tokens=(32, 128), system_prompt_tokens=256)
    (rec,) = run_recorders([pub], [cfg(pub, tmp_path, engine="sglang")])
    records = [r for p in rec.writer.closed_paths for r in read_segment(p)]
    media = {r["medium"] for r in records if r["t"] in ("S", "R")}
    assert {"GPU", "CPU_PINNED"} <= media
    assert rec.stats.batches == 60
    assert str(CANARY_TOKEN) not in json.dumps(records)


def test_anonymization_consistent_across_recorders(tmp_path):
    key = b"shared-secret"
    pub = make_publisher(engine="vllm", max_batches=20, wait_subscribers=3)
    recorders = run_recorders([pub], [
        cfg(pub, tmp_path / "a", anonymize_key=key, node_id="a"),
        cfg(pub, tmp_path / "b", anonymize_key=key, node_id="b"),
        cfg(pub, tmp_path / "plain", node_id="plain"),
    ])
    anon_a, anon_b, plain = (stored_hashes(r.writer.closed_paths) for r in recorders)
    assert anon_a and anon_a == anon_b
    assert not (anon_a & plain)
    anonymize = Anonymizer(key)
    assert {anonymize(h) for h in plain} == anon_a


def test_gap_replay_recovers_dropped_batches(tmp_path):
    pub = make_publisher(engine="vllm", max_batches=40, drop_every=5)
    (rec,) = run_recorders([pub], [cfg(pub, tmp_path)])
    # seq 4, 9, ..., 34 are detected (seq 39 is the last one, so no later seq exposes it).
    assert rec.stats.gaps == 7 and rec.stats.gap_batches == 7
    assert rec.stats.recovered_batches == 7 and rec.stats.unrecovered_gap_batches == 0
    assert rec.stats.batches == 39
    gaps = [r for p in rec.writer.closed_paths for r in read_segment(p) if r["t"] == "G"]
    assert sum(g["recovered"] for g in gaps) == 7


def test_gap_replay_accepts_legacy_answer_without_topic(tmp_path):
    pub = make_publisher(engine="vllm", max_batches=40, drop_every=5, legacy_replay=True)
    (rec,) = run_recorders([pub], [cfg(pub, tmp_path)])
    assert rec.stats.recovered_batches == 7 and rec.stats.unrecovered_gap_batches == 0
    assert rec.stats.decode_errors == 0


def test_gap_with_tiny_replay_buffer_is_unrecovered(tmp_path):
    pub = make_publisher(engine="vllm", max_batches=40, drop_every=5, replay_buffer=1)
    (rec,) = run_recorders([pub], [cfg(pub, tmp_path)])
    assert rec.stats.gap_batches == 7 and rec.stats.unrecovered_gap_batches == 7
    assert rec.stats.batches == 32  # 40 published minus 8 dropped, none recovered


def test_multi_rank_subscribe(tmp_path):
    pubs = make_rank_pair(engine="vllm", max_batches=15)
    (rec,) = run_recorders(pubs, [cfg(pubs[0], tmp_path, dp_ranks=2)])
    assert rec.stats.batches == 30
    ranks = {r["rank"] for p in rec.writer.closed_paths for r in read_segment(p) if r["t"] == "S"}
    assert ranks == {0, 1}


def test_rotation_and_s3_upload_on_close(tmp_path):
    class StubS3:
        def __init__(self):
            self.calls = []

        def upload_file(self, path, bucket, key):
            self.calls.append((path, bucket, key))

    stub = StubS3()
    pub = make_publisher(engine="vllm", max_batches=30)
    sink = S3Sink("s3://bucket/traces", node="n1", client=stub)
    (rec,) = run_recorders([pub], [cfg(pub, tmp_path, rotate_bytes=1)], sinks=[sink])
    assert len(rec.writer.closed_paths) > 1
    assert [c[0] for c in stub.calls] == rec.writer.closed_paths
    assert all(c[1] == "bucket" and c[2].startswith("traces/n1/") for c in stub.calls)


def test_publisher_restart_writes_marker_with_engine_rank(tmp_path):
    first = make_publisher(engine="vllm", max_batches=10, dp_rank=3, linger=0.3)
    endpoint = first.endpoint
    config = RecorderConfig(endpoint=endpoint, model="m", out_dir=str(tmp_path), node_id="n1",
                            duration=8.0)
    recorder = Recorder(config)
    thread = threading.Thread(target=recorder.run, daemon=True)
    thread.start()
    first.run()  # seq 0..9, then the socket closes: the engine "stops"
    # The engine comes back on the same port with a fresh sequence starting at 0.
    second = make_publisher(engine="vllm", max_batches=5, dp_rank=3, bind=endpoint,
                            replay_bind=None, linger=0.3)
    second.run()
    recorder.stop()
    thread.join(timeout=15)
    assert recorder.stats.restarts == 1 and recorder.stats.batches == 15
    records = [r for p in recorder.writer.closed_paths for r in read_segment(p)]
    (marker,) = [r for r in records if r["t"] == "X"]
    assert marker["rank"] == 3 and marker["from_seq"] == 9 and marker["to_seq"] == 0


def test_gap_marker_uses_engine_rank(tmp_path):
    pub = make_publisher(engine="vllm", max_batches=12, drop_every=5, dp_rank=2)
    (rec,) = run_recorders([pub], [cfg(pub, tmp_path)])
    gaps = [r for p in rec.writer.closed_paths for r in read_segment(p) if r["t"] == "G"]
    assert gaps and all(g["rank"] == 2 for g in gaps)


def test_engine_detected_from_metrics_is_recorded_in_the_open_segment(tmp_path):
    config = RecorderConfig(endpoint="tcp://127.0.0.1:1", model="m", out_dir=str(tmp_path),
                            node_id="n1")
    recorder = Recorder(config, clock=lambda: 100.0)
    # Events opened the segment (and wrote its header) before the first metrics scrape.
    recorder._emit({"t": "C", "ts": 100.0, "node": "n1", "rank": 0})
    recorder._metrics_q.put((100.0, {"vllm:prompt_tokens_total": 5.0}, {}))
    recorder._drain_metrics()
    recorder.writer.close()
    records = [r for p in recorder.writer.closed_paths for r in read_segment(p)]
    # The header was written before detection; the E record names the engine.
    assert records[0]["t"] == "H" and records[0]["engine"] == "auto"
    (engine,) = [r for r in records if r["t"] == "E"]
    assert engine["engine"] == "vllm" and engine["node"] == "n1"
    assert recorder.writer.header["engine"] == "vllm"


def test_short_replay_answer_counts_a_decode_error(tmp_path):
    from kvtrace.recorder import _Rank
    from kvtrace.wire import SEQ_END_MARKER, encode_seq

    class FakeDealer:
        def __init__(self, answers):
            self.answers = list(answers)

        def send_multipart(self, frames):
            pass

        def poll(self, timeout):
            return 1

        def recv_multipart(self):
            return self.answers.pop(0)

    config = RecorderConfig(endpoint="tcp://127.0.0.1:1", model="m", out_dir=str(tmp_path),
                            node_id="n1")
    recorder = Recorder(config)
    rank = _Rank(0, None, "tcp://127.0.0.1:2")
    rank.replay = FakeDealer([[b""], [b"", b"", encode_seq(SEQ_END_MARKER), b""]])
    assert recorder._request_replay(rank, 1, 2) == {}
    assert recorder.stats.decode_errors == 1


def _stored(ts, h):
    return msgpack.packb([ts, [["BlockStored", [h], None, [1] * 16, 16, None, "GPU"]]],
                         use_bin_type=True)


def _restart_run(tmp_path, replayed=None):
    """Old stream seq 0..5 (ts 90..95); new stream first seen at seq 2 (ts 100).

    The recorder clock (500) is later than every engine ts."""
    config = RecorderConfig(endpoint="tcp://127.0.0.1:1", model="m", out_dir=str(tmp_path),
                            node_id="n1")
    recorder = Recorder(config, clock=lambda: 500.0)
    rank = _Rank(0, None, None)
    if replayed is not None:
        rank.replay_endpoint = "tcp://127.0.0.1:2"
        recorder._request_replay = lambda r, first, last: {
            s: p for s, p in replayed.items() if first <= s <= last}
    for seq in range(6):
        recorder._on_live(rank, [b"kv-events", encode_seq(seq), _stored(90.0 + seq, seq)])
    recorder._on_live(rank, [b"kv-events", encode_seq(2), _stored(100.0, 100)])
    recorder.writer.close()
    return recorder, [r for p in recorder.writer.closed_paths for r in read_segment(p)]


def test_restart_marker_sorts_before_the_new_stream(tmp_path):
    recorder, records = _restart_run(tmp_path)
    (marker,) = [r for r in records if r["t"] == "X"]
    assert marker["ts"] == 100.0  # min(recorder clock 500, first new batch 100)
    ordered = [r for r in sort_records(records) if r["t"] in ("S", "X")]
    assert [r["t"] for r in ordered][-2:] == ["X", "S"]
    # The simulator keeps the new stream's block after the reset.
    assert ordered[-1]["h"] == [normalize_hash(100)]


def test_restart_counts_batches_lost_before_the_first_new_seq(tmp_path):
    recorder, records = _restart_run(tmp_path)
    s = recorder.stats
    assert s.restarts == 1 and s.gaps == 1 and s.gap_batches == 2
    assert s.unrecovered_gap_batches == 2 and s.recovered_batches == 0
    (gap,) = [r for r in records if r["t"] == "G"]
    assert gap["missing"] == 2 and gap["recovered"] == 0


def test_restart_replays_the_lost_head_and_marks_before_it(tmp_path):
    replayed = {0: _stored(98.0, 200), 1: _stored(99.0, 201)}
    recorder, records = _restart_run(tmp_path, replayed)
    assert recorder.stats.recovered_batches == 2 and recorder.stats.unrecovered_gap_batches == 0
    (marker,) = [r for r in records if r["t"] == "X"]
    assert marker["ts"] == 98.0
    tail = [r for r in sort_records(records) if r["t"] in ("S", "X")][-4:]
    assert [r["t"] for r in tail] == ["X", "S", "S", "S"]
    assert [r["h"][0] for r in tail[1:]] == [normalize_hash(h) for h in (200, 201, 100)]


def test_restart_at_seq_zero_has_no_gap(tmp_path):
    config = RecorderConfig(endpoint="tcp://127.0.0.1:1", model="m", out_dir=str(tmp_path),
                            node_id="n1")
    recorder = Recorder(config, clock=lambda: 500.0)
    rank = _Rank(0, None, None)
    recorder._on_live(rank, [b"kv-events", encode_seq(3), _stored(90.0, 1)])
    recorder._on_live(rank, [b"kv-events", encode_seq(0), _stored(100.0, 2)])
    assert recorder.stats.restarts == 1 and recorder.stats.gaps == 0


def test_segment_flush_seconds_must_not_be_negative():
    config = RecorderConfig(endpoint="tcp://127.0.0.1:1", model="m", out_dir="/tmp",
                            segment_flush_seconds=-1)
    with pytest.raises(ValueError, match="segment-flush-seconds"):
        config.validate()


def test_extra_metrics_urls_must_have_distinct_source_labels():
    config = RecorderConfig(
        endpoint="tcp://127.0.0.1:1", model="m", out_dir="/tmp",
        metrics_url="http://h:8000/metrics",
        extra_metrics_urls=("http://user:secret@h:9000/metrics?token=abc",
                            "http://h:9000/metrics#w1", "http://h:9001/metrics"))
    with pytest.raises(ValueError, match="duplicate labels: http://h:9000/metrics$") as err:
        config.validate()
    assert "secret" not in str(err.value) and "token" not in str(err.value)
    RecorderConfig(endpoint="tcp://127.0.0.1:1", model="m", out_dir="/tmp",
                   metrics_url="http://h:9000/metrics",
                   extra_metrics_urls=("http://h:9000/metrics?w=0", "http://h:9001/metrics"),
                   ).validate()


def test_quiet_recorder_loop_still_flushes_the_open_segment(tmp_path):
    ticks = iter(range(1000))
    config = RecorderConfig(endpoint="tcp://127.0.0.1:1", model="m", out_dir=str(tmp_path),
                            node_id="n1", duration=20.0, segment_flush_seconds=5.0,
                            health_interval=0, require_events_within=0)
    # Each clock read is one second later, so the loop ends after a few polls.
    recorder = Recorder(config, clock=lambda: float(next(ticks)))
    recorder._emit({"t": "C", "ts": 0.0, "node": "n1", "rank": 0})
    flushes = []
    real_flush = recorder.writer.flush
    recorder.writer.flush = lambda: (flushes.append(1), real_flush())
    recorder.open()
    try:
        recorder._loop()  # no events arrive: only the loop can flush
    finally:
        recorder._close_sockets()
        recorder.writer.close()
    assert flushes
