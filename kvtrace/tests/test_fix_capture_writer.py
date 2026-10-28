"""Recorder identity (rid, default node id), one writer per node directory, orphan close."""

import errno
import gzip
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys

import pytest
from conftest import make_publisher, read_segment, run_recorders

from kvtrace.cli import main, summarize_trace
from kvtrace.recorder import Recorder, RecorderConfig, anon_key_fingerprint
from kvtrace.segments import (
    PART_SUFFIX,
    SEGMENT_SUFFIX,
    NodeInUseError,
    SegmentWriter,
    iter_records,
    iter_segment_files,
)
from kvtrace.sinks import Sink

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
IDLE = "tcp://127.0.0.1:1"


def idle_cfg(out, **kwargs):
    kwargs.setdefault("node_id", "n1")
    return RecorderConfig(endpoint=IDLE, model="m", out_dir=str(out), duration=0.3,
                          require_events_within=0, health_interval=0, **kwargs)


class RecordingSink(Sink):
    def __init__(self):
        self.closed_segments = []
        self.closed = False

    def on_segment_closed(self, path):
        self.closed_segments.append(path)

    def close(self):
        self.closed = True


def test_rid_and_header_fields_on_every_recorder_record(tmp_path):
    pub = make_publisher(engine="vllm", max_batches=20)
    cfg = RecorderConfig(endpoint=pub.endpoint, replay_endpoint=pub.replay_endpoint, model="m",
                         out_dir=str(tmp_path), node_id="n1", duration=3.0, rotate_bytes=1,
                         anonymize_key=b"k", health_interval=0.5,
                         extra_metrics_urls=("http://user:pw@127.0.0.1:1/metrics?x=1",),
                         metrics_url="http://127.0.0.1:1/metrics", metrics_interval=0.2)
    (rec,) = run_recorders([pub], [cfg])
    records = [r for p in rec.writer.closed_paths for r in read_segment(p)]
    assert len(rec.rid) == 12 and int(rec.rid, 16) >= 0
    headers = [r for r in records if r["t"] == "H"]
    assert headers and all(h["rid"] == rec.rid for h in headers)
    assert headers[0]["metrics_urls"] == ["http://127.0.0.1:1/metrics"]
    assert headers[0]["anon_key_fp"] == anon_key_fingerprint(b"k") == \
        hashlib.sha256(b"k").hexdigest()[:16]
    for kind in ("P", "T"):
        assert [r for r in records if r["t"] == kind]
        assert all(r["rid"] == rec.rid for r in records if r["t"] == kind)


def test_header_without_key_or_extras(tmp_path):
    rec = Recorder(idle_cfg(tmp_path))
    rec.writer.write({"t": "C", "ts": 1.0})
    rec.writer.close()
    header = read_segment(rec.writer.closed_paths[0])[0]
    assert header["metrics_urls"] == [] and header["anon_key_fp"] is None


def test_two_recorders_get_different_rids(tmp_path):
    assert Recorder(idle_cfg(tmp_path / "a")).rid != Recorder(idle_cfg(tmp_path / "b")).rid


def test_default_node_id_is_hostname_and_endpoint_port():
    cfg = RecorderConfig(endpoint="tcp://127.0.0.1:5557", model="m", out_dir="/tmp/x")
    assert cfg.node_id == f"{socket.gethostname()}-5557"
    assert RecorderConfig(endpoint="tcp://127.0.0.1:5557", model="m", out_dir="/tmp/x",
                          node_id="mine").node_id == "mine"


def test_cli_default_node_id_names_the_segment_directory(tmp_path, capsys):
    rc = main(["record", "--endpoint", "tcp://127.0.0.1:5999", "--model", "m", "--out",
               str(tmp_path), "--duration", "0.3", "--require-events-within", "0"])
    assert rc == 0
    assert (tmp_path / f"{socket.gethostname()}-5999").is_dir()


def test_second_recorder_with_same_node_id_is_refused(tmp_path):
    first = SegmentWriter(str(tmp_path), "n1", {"node": "n1"})
    first.claim()
    sink = RecordingSink()
    with pytest.raises(NodeInUseError, match="--node-id"):
        Recorder(idle_cfg(tmp_path), sink=sink).run()
    assert sink.closed
    first.release()
    assert Recorder(idle_cfg(tmp_path)).run().batches == 0


def test_cli_refuses_a_second_recorder_and_names_node_id(tmp_path, capsys):
    holder = SegmentWriter(str(tmp_path), "n1", {"node": "n1"})
    holder.claim()
    try:
        rc = main(["record", "--endpoint", IDLE, "--model", "m", "--out", str(tmp_path),
                   "--node-id", "n1", "--duration", "0.3", "--require-events-within", "0"])
    finally:
        holder.release()
    assert rc == 2 and "--node-id" in capsys.readouterr().err


def test_live_part_of_an_old_recorder_is_refused(tmp_path):
    # A writer that only locks its .part (no directory claim) is still detected.
    other = SegmentWriter(str(tmp_path), "n1", {"node": "n1"})
    other.write({"t": "C", "ts": 1.0})
    try:
        with pytest.raises(NodeInUseError):
            SegmentWriter(str(tmp_path), "n1", {"node": "n1"}).claim()
    finally:
        other.close()


def test_same_name_segment_is_not_truncated(tmp_path):
    clock = lambda: 100.0  # noqa: E731
    a = SegmentWriter(str(tmp_path), "n1", {"node": "n1"}, clock=clock)
    b = SegmentWriter(str(tmp_path), "n1", {"node": "n1"}, clock=clock)
    for _ in range(1000):
        a.write({"t": "C", "ts": 1.0, "rank": 0})
        b.write({"t": "C", "ts": 1.0, "rank": 1})
    pa, pb = a.close(), b.close()
    assert pa != pb
    for path, rank in ((pa, 0), (pb, 1)):
        recs = read_segment(path)
        assert [r["rank"] for r in recs if r["t"] == "C"] == [rank] * 1000


def test_held_lock_on_a_new_part_is_a_conflict_not_missing_flock(tmp_path, monkeypatch):
    import kvtrace.segments as segments

    def held(fd, op):
        raise OSError(errno.EWOULDBLOCK, "Resource temporarily unavailable")

    monkeypatch.setattr(segments.fcntl, "flock", held)
    writer = SegmentWriter(str(tmp_path), "n1", {"node": "n1"})
    with pytest.raises(NodeInUseError):
        writer.write({"t": "C", "ts": 1.0})


_KILLED_WRITER = """
import os, signal, sys
from kvtrace.segments import SegmentWriter
now = [100.0]
writer = SegmentWriter(sys.argv[1], "n1", {"node": "n1", "model": "m"}, flush_seconds=10,
                       clock=lambda: now[0])
for i in range(3):
    writer.write({"t": "C", "ts": 100.0 + i, "node": "n1", "rank": 0})
now[0] += 10
writer.maybe_flush()
writer.write({"t": "C", "ts": 200.0, "node": "n1", "rank": 0})
os.kill(os.getpid(), signal.SIGKILL)
"""


def _killed_part(out):
    proc = subprocess.run([sys.executable, "-c", _KILLED_WRITER, str(out)],
                          env={**os.environ, "PYTHONPATH": SRC}, check=False)
    assert proc.returncode == -signal.SIGKILL
    (part,) = (out / "n1").glob("*" + PART_SUFFIX)
    return part


def test_orphaned_part_is_closed_marked_and_given_to_the_sink(tmp_path):
    part = _killed_part(tmp_path)
    sink = RecordingSink()
    Recorder(idle_cfg(tmp_path), sink=sink).run()
    final = str(part)[: -len(".part")]
    assert not part.exists() and os.path.exists(final)
    assert sink.closed_segments[0] == final
    recs = list(iter_records([final], errors := {}))
    assert [r["t"] for r in recs] == ["H", "C", "C", "C", "G"]
    assert recs[-1] == {"t": "G", "ts": 102.0, "node": "n1", "rank": 0, "missing": 0,
                        "recovered": 0, "truncated": part.name, "model": "m"}
    assert errors == {}
    assert summarize_trace(iter_records(iter_segment_files([str(tmp_path)])))[
        "truncated_segments"] == [part.name]


def test_complete_orphan_gets_no_truncation_marker(tmp_path):
    node = tmp_path / "n1"
    node.mkdir()
    part = node / ("n1-1-00000" + SEGMENT_SUFFIX + ".part")
    with gzip.open(part, "wt") as fh:
        fh.write(json.dumps({"t": "H", "node": "n1"}) + "\n")
        fh.write(json.dumps({"t": "C", "ts": 1.0, "node": "n1", "rank": 0}) + "\n")
    sink = RecordingSink()
    Recorder(idle_cfg(tmp_path), sink=sink).run()
    (final,) = [p for p in sink.closed_segments if p.endswith("n1-1-00000" + SEGMENT_SUFFIX)]
    assert [r["t"] for r in read_segment(final)] == ["H", "C"]


def test_unreadable_orphan_is_left_in_place(tmp_path):
    node = tmp_path / "n1"
    node.mkdir()
    part = node / ("n1-1-00000" + SEGMENT_SUFFIX + ".part")
    part.write_bytes(b"")
    sink = RecordingSink()
    rec = Recorder(idle_cfg(tmp_path), sink=sink)
    rec.run()
    # Only the new recorder's own segment reaches the sink.
    assert part.exists() and sink.closed_segments == rec.writer.closed_paths
