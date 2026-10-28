# Bounded S3 close, no orphan finalize without the directory lock, and exit statuses
# for write and node conflicts during the run.
import errno
import gzip
import json
import logging
import os
import threading
import time

import pytest
from conftest import make_publisher, run_recorders

import kvtrace.cli as cli
import kvtrace.segments as segments
from kvtrace.recorder import (
    EXIT_NODE_IN_USE,
    EXIT_SINK_LOST,
    EXIT_WRITE_FAILED,
    Recorder,
    RecorderConfig,
)
from kvtrace.segments import PART_SUFFIX, SEGMENT_SUFFIX, SegmentWriter
from kvtrace.sinks import S3Sink, Sink

IDLE = "tcp://127.0.0.1:1"
README = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")


class RecordingSink(Sink):
    def __init__(self, lost=0):
        self.closed_segments = []
        self.closed = False
        self._lost = lost

    def on_segment_closed(self, path):
        self.closed_segments.append(path)

    def close(self):
        self.closed = True

    @property
    def lost(self):
        return self._lost


class DownS3:
    def __init__(self):
        self.attempts = 0

    def upload_file(self, path, bucket, key):
        self.attempts += 1
        raise ConnectionError("S3 down")


class HangingS3:
    def __init__(self):
        self.release = threading.Event()

    def upload_file(self, path, bucket, key):
        self.release.wait(10)


def idle_cfg(out, **kwargs):
    kwargs.setdefault("node_id", "n1")
    return RecorderConfig(endpoint=IDLE, model="m", out_dir=str(out), duration=0.3,
                          require_events_within=0, health_interval=0, **kwargs)


def idle_argv(out):
    return ["record", "--endpoint", IDLE, "--model", "m", "--out", str(out), "--node-id", "n1",
            "--duration", "0.3", "--require-events-within", "0"]


def write_orphan(out):
    node = out / "n1"
    node.mkdir(parents=True, exist_ok=True)
    part = node / ("n1-1-00000" + SEGMENT_SUFFIX + ".part")
    with gzip.open(part, "wt") as fh:
        fh.write(json.dumps({"t": "H", "node": "n1"}) + "\n")
        fh.write(json.dumps({"t": "C", "ts": 1.0, "node": "n1", "rank": 0}) + "\n")
    return part


def held_part_locks(monkeypatch):
    """Make every .part lock look held by another process; the directory lock still works."""
    real_flock = segments.fcntl.flock

    def flock(fd, op):
        if os.readlink(f"/proc/self/fd/{fd}").endswith(PART_SUFFIX):
            raise OSError(errno.EWOULDBLOCK, "Resource temporarily unavailable")
        return real_flock(fd, op)

    monkeypatch.setattr(segments.fcntl, "flock", flock)


def no_flock(monkeypatch):
    def flock(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(segments.fcntl, "flock", flock)


# ---- 1: S3Sink.close() is bounded ------------------------------------------------------
def test_s3_close_tries_each_queued_segment_once_without_backoff(tmp_path):
    client = DownS3()
    sink = S3Sink("s3://b/p", node="n1", client=client, backoff_seconds=0.5,
                  max_backoff_seconds=0.5)
    for i in range(3):
        sink.on_segment_closed(str(tmp_path / f"s{i}"))
    started = time.monotonic()
    sink.close()
    assert time.monotonic() - started < 1.0
    assert client.attempts == 3
    assert sink.stats() == {"s3_uploaded": 0, "s3_failed": 3} and sink.lost == 3


def test_s3_close_deadline_counts_unfinished_uploads_as_failed(tmp_path):
    client = HangingS3()
    sink = S3Sink("s3://b/p", node="n1", client=client, close_deadline_seconds=0.3)
    sink.on_segment_closed(str(tmp_path / "a"))
    sink.on_segment_closed(str(tmp_path / "b"))
    started = time.monotonic()
    sink.close()
    assert time.monotonic() - started < 2.0
    assert sink.stats() == {"s3_uploaded": 0, "s3_failed": 2} and sink.lost == 2
    client.release.set()
    time.sleep(0.1)
    # A late upload does not change the counters that close() reported.
    assert sink.stats() == {"s3_uploaded": 0, "s3_failed": 2}


def test_record_with_hanging_s3_ends_with_sink_lost_status(tmp_path):
    sink = S3Sink("s3://b/p", node="n1", client=HangingS3(), close_deadline_seconds=0.3)
    rec = Recorder(idle_cfg(tmp_path), sink=sink)
    rec.run()
    assert sink.stats()["s3_failed"] == 1 and rec.exit_code == EXIT_SINK_LOST


# ---- 2: no directory lock, no orphan finalize -----------------------------------------
def test_claim_without_directory_lock_leaves_a_live_writers_part(tmp_path, monkeypatch, caplog):
    no_flock(monkeypatch)
    live = SegmentWriter(str(tmp_path), "n1", {"node": "n1"})
    live.write({"t": "C", "ts": 1.0})
    live.flush()
    (part,) = (tmp_path / "n1").glob("*" + PART_SUFFIX)
    with caplog.at_level(logging.WARNING, logger="kvtrace.segments"):
        assert SegmentWriter(str(tmp_path), "n1", {"node": "n1"}).claim() == []
    assert part.exists()
    assert "stay as .part" in caplog.text
    # The live writer still closes its own segment.
    assert live.close() == str(part)[: -len(".part")]


# ---- 3: write failure and node conflict keep shutdown and the stats JSON ---------------
def test_node_conflict_during_the_run_closes_sink_and_sets_status(tmp_path, monkeypatch):
    held_part_locks(monkeypatch)
    pub = make_publisher(engine="vllm", max_batches=5)
    cfg = RecorderConfig(endpoint=pub.endpoint, replay_endpoint=pub.replay_endpoint, model="m",
                         out_dir=str(tmp_path), node_id="n1", duration=3.0)
    sink = RecordingSink()
    (rec,) = run_recorders([pub], [cfg], sinks=[sink])
    assert rec.exit_code == EXIT_NODE_IN_USE and sink.closed


def test_node_conflict_status_loses_to_a_higher_one(tmp_path, monkeypatch):
    held_part_locks(monkeypatch)
    sink = RecordingSink(lost=1)
    rec = Recorder(idle_cfg(tmp_path), sink=sink)
    rec.run()
    assert rec.exit_code == EXIT_SINK_LOST and sink.closed


def test_cli_node_conflict_during_the_run_prints_stats(tmp_path, monkeypatch, capsys):
    held_part_locks(monkeypatch)
    rc = cli.main(idle_argv(tmp_path))
    assert rc == EXIT_NODE_IN_USE
    assert "stats" in json.loads(capsys.readouterr().out)


def test_failed_orphan_close_exits_write_failed_and_prints_stats(tmp_path, monkeypatch, capsys):
    write_orphan(tmp_path)

    def full(path):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(segments, "finalize_orphan", full)
    sink = RecordingSink()
    rec = Recorder(idle_cfg(tmp_path), sink=sink)
    rec.run()
    assert rec.exit_code == EXIT_WRITE_FAILED and sink.closed
    rc = cli.main(idle_argv(tmp_path))
    assert rc == EXIT_WRITE_FAILED
    assert "stats" in json.loads(capsys.readouterr().out)


def test_record_help_and_readme_document_node_conflict_during_the_run(capsys):
    with pytest.raises(SystemExit):
        cli.main(["record", "--help"])
    assert "during the run" in capsys.readouterr().out
    with open(README, encoding="utf-8") as fh:
        readme = fh.read()
    assert "during the run" in readme and "NFSv4" in readme and "lease expires" in readme


# ---- 4: finalized orphans are in closed_paths -----------------------------------------
def test_finalized_orphan_is_in_closed_paths_and_record_json(tmp_path, capsys):
    part = write_orphan(tmp_path)
    final = str(part)[: -len(".part")]
    writer = SegmentWriter(str(tmp_path), "n1", {"node": "n1"})
    assert writer.claim() == [final] and writer.closed_paths == [final]
    writer.release()
    write_orphan(tmp_path / "again")
    assert cli.main(idle_argv(tmp_path / "again")) == 0
    segments_out = json.loads(capsys.readouterr().out)["segments"]
    assert str(tmp_path / "again" / "n1" / os.path.basename(final)) in segments_out
