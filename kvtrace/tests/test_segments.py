import errno
import gzip
import logging
import os
import signal
import subprocess
import sys

import pytest

from kvtrace import segments
from kvtrace.segments import (
    PART_SUFFIX,
    SEGMENT_SUFFIX,
    SegmentWriter,
    TracePaths,
    iter_records,
    iter_segment_files,
)


class FakeS3:
    def __init__(self, keys):
        self.keys = keys
        self.downloaded = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        keys = self.keys

        class Pager:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in keys if k.startswith(Prefix)]}

        return Pager()

    def download_file(self, bucket, key, dest):
        self.downloaded.append(key)
        with gzip.open(dest, "wt") as fh:
            fh.write('{"t":"H","model":"m"}\n{"t":"C","ts":1,"node":"n","rank":0}\n')


def test_writer_header_rotation_and_atomic_rename(tmp_path):
    now = [100.0]
    closed = []
    writer = SegmentWriter(str(tmp_path), "node/1", {"node": "node/1", "model": "m"},
                           rotate_seconds=10, on_closed=closed.append, clock=lambda: now[0])
    writer.write({"t": "C", "ts": 1})
    assert list(tmp_path.rglob("*.part"))
    now[0] += 11
    writer.write({"t": "C", "ts": 2})
    writer.close()
    assert len(closed) == 2 and not list(tmp_path.rglob("*.part"))
    assert all(os.path.basename(os.path.dirname(p)) == "node_1" for p in closed)
    recs = list(iter_records(closed))
    assert [r["t"] for r in recs] == ["H", "C", "H", "C"] and recs[1]["model"] == "m"


def test_corrupt_segments_are_counted_not_fatal(tmp_path):
    good = tmp_path / f"a{SEGMENT_SUFFIX}"
    with gzip.open(good, "wb") as fh:
        fh.write(b'{"t":"C","ts":1}\n\xff\xfe not json\n[1,2]\n{"t":"C","ts":2}\n')
    bad = tmp_path / f"b{SEGMENT_SUFFIX}"
    bad.write_bytes(b"\x1f\x8b\x08\x00garbage-not-deflate")
    errors = {}
    recs = list(iter_records([str(good), str(bad)], errors))
    assert [r["ts"] for r in recs] == [1, 2]
    assert errors == {"bad_lines": 2, "damaged_files": 1}


def test_s3_download_matches_prefix_components_and_stays_inside(tmp_path):
    client = FakeS3([
        f"traces/n1/a{SEGMENT_SUFFIX}",
        f"traces2/n1/other{SEGMENT_SUFFIX}",
        f"traces/../../escape{SEGMENT_SUFFIX}",
        "traces/n1/readme.txt",
    ])
    with TracePaths(["s3://bkt/traces"], client=client) as files:
        assert len(files) == 1 and files[0].endswith(f"n1/a{SEGMENT_SUFFIX}")
        tmp_dir = os.path.dirname(os.path.dirname(os.path.dirname(files[0])))
        assert os.path.isdir(tmp_dir)
    assert client.downloaded == [f"traces/n1/a{SEGMENT_SUFFIX}"]
    assert not os.path.exists(tmp_dir)


def test_failed_s3_download_cleans_temp_dir(monkeypatch):
    import tempfile

    import pytest

    created = []
    real = tempfile.TemporaryDirectory

    def tracking(*args, **kwargs):
        tmp = real(*args, **kwargs)
        created.append(tmp.name)
        return tmp

    class BrokenS3(FakeS3):
        def download_file(self, bucket, key, dest):
            raise OSError("denied")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", tracking)
    with pytest.raises(OSError):
        with TracePaths(["s3://bkt/traces"], client=BrokenS3([f"traces/a{SEGMENT_SUFFIX}"])):
            pass
    assert created and not os.path.exists(created[0])


_KILLED_WRITER = """
import os, signal, sys
from kvtrace.segments import SegmentWriter
now = [100.0]
writer = SegmentWriter(sys.argv[1], "n1", {"node": "n1", "model": "m"}, flush_seconds=10,
                       clock=lambda: now[0])
for i in range(3):
    writer.write({"t": "C", "ts": 100.0 + i, "node": "n1", "rank": 0})
now[0] += 10
writer.maybe_flush()  # the timer flush: these three records are now readable
writer.write({"t": "C", "ts": 200.0, "node": "n1", "rank": 0})  # after the last flush
os.kill(os.getpid(), signal.SIGKILL)
"""


def test_killed_recorder_part_is_readable_to_last_flush_with_gap(tmp_path, caplog):
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    proc = subprocess.run([sys.executable, "-c", _KILLED_WRITER, str(tmp_path)],
                          env={**os.environ, "PYTHONPATH": src}, check=False)
    assert proc.returncode == -signal.SIGKILL
    files = iter_segment_files([str(tmp_path)])
    assert len(files) == 1 and files[0].endswith(PART_SUFFIX)
    errors = {}
    with caplog.at_level(logging.WARNING):
        recs = list(iter_records(files, errors))
    assert [r["t"] for r in recs] == ["H", "C", "C", "C", "G"]
    gap = recs[-1]
    assert gap["ts"] == 102.0 and gap["node"] == "n1" and gap["rank"] == 0
    assert gap["truncated"] == os.path.basename(files[0]) and gap["missing"] == 0
    assert errors == {"truncated_parts": 1}
    assert os.path.basename(files[0]) in caplog.text


def test_without_timer_flush_a_killed_part_has_no_records(tmp_path):
    now = [100.0]
    writer = SegmentWriter(str(tmp_path), "n1", {"node": "n1"}, flush_seconds=0,
                           clock=lambda: now[0])
    writer.write({"t": "C", "ts": 1})
    now[0] += 60
    writer.maybe_rotate()
    (part,) = tmp_path.rglob("*.part")
    assert part.stat().st_size == 0  # nothing reached the file: the timer is off
    writer.close()


def test_live_part_is_skipped_and_closed_segments_are_kept(tmp_path):
    writer = SegmentWriter(str(tmp_path), "n1", {"node": "n1"}, rotate_seconds=1e9)
    writer.write({"t": "C", "ts": 1})
    writer.flush()
    (part,) = tmp_path.rglob("*.part")
    assert iter_segment_files([str(tmp_path)]) == []
    assert iter_segment_files([str(part)]) == []
    closed = writer.close()
    assert iter_segment_files([str(tmp_path)]) == [closed]


def _flock_raising(err):
    def flock(fd, op):
        raise OSError(err, os.strerror(err))
    return flock


@pytest.mark.parametrize("err", [errno.ENOLCK, errno.EOPNOTSUPP])
def test_writer_without_flock_support_logs_and_keeps_writing(tmp_path, monkeypatch, caplog,
                                                              err):
    monkeypatch.setattr(segments.fcntl, "flock", _flock_raising(err))
    writer = SegmentWriter(str(tmp_path), "n1", {"node": "n1"})
    with caplog.at_level(logging.WARNING, logger="kvtrace.segments"):
        writer.write({"t": "C", "ts": 1})
    assert "cannot lock" in caplog.text
    closed = writer.close()
    assert [r["t"] for r in iter_records([closed])] == ["H", "C"]


@pytest.mark.parametrize("err,live", [(errno.EWOULDBLOCK, True), (errno.EAGAIN, True),
                                      (errno.ENOLCK, False), (errno.EOPNOTSUPP, False)])
def test_only_a_would_block_lock_error_marks_a_part_as_live(tmp_path, monkeypatch, err, live):
    part = tmp_path / f"a{PART_SUFFIX}"
    with gzip.open(part, "wt") as fh:
        fh.write('{"t":"C","ts":1}\n')
    monkeypatch.setattr(segments.fcntl, "flock", _flock_raising(err))
    assert segments._is_live_part(str(part)) is live
    assert iter_segment_files([str(tmp_path)]) == ([] if live else [str(part)])


def test_part_renamed_after_listing_is_read_under_its_final_name(tmp_path):
    writer = SegmentWriter(str(tmp_path), "n1", {"node": "n1", "model": "m"})
    writer.write({"t": "C", "ts": 1.0, "node": "n1", "rank": 0})
    (part,) = tmp_path.rglob("*.part")
    gone = tmp_path / f"gone{PART_SUFFIX}"
    writer.close()  # renames the .part that was "listed" above
    errors = {}
    recs = list(iter_records([str(part), str(gone)], errors))
    assert [r["t"] for r in recs] == ["H", "C"] and recs[1]["model"] == "m"
    assert errors == {}
