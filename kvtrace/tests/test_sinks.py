import sys
import threading
import time

import pytest

from kvtrace.sinks import FileSink, S3Sink, SinkError, ValkeySink, make_sink


class FlakyS3:
    def __init__(self, failures):
        self.failures = failures
        self.calls = []

    def upload_file(self, path, bucket, key):
        self.calls.append((path, bucket, key))
        if self.failures:
            self.failures -= 1
            raise OSError("boom")


class FakeValkey:
    def __init__(self, fail=False):
        self.entries = []
        self.fail = fail
        self.closed = False
        self.executes = 0

    def pipeline(self, transaction=True):
        assert transaction is False
        client = self

        class Pipe:
            def __init__(self):
                self.ops = []

            def xadd(self, stream, fields, maxlen=None, approximate=True):
                self.ops.append((stream, fields, maxlen, approximate))

            def execute(self):
                client.executes += 1
                if client.fail:
                    raise ConnectionError("down")
                client.entries.extend(self.ops)

        return Pipe()

    def close(self):
        self.closed = True


def test_s3_sink_retries_then_succeeds(tmp_path):
    seg = tmp_path / "n1-1-00000.kvt.jsonl.gz"
    seg.write_bytes(b"x")
    client = FlakyS3(failures=2)
    sink = S3Sink("s3://bkt/some/prefix", node="n1", client=client, backoff_seconds=0)
    sink.on_segment_closed(str(seg))
    # Retries happen while recording; close() would try only once more.
    deadline = time.monotonic() + 5
    while sink.uploaded == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    sink.close()
    assert len(client.calls) == 3 and sink.uploaded == 1
    assert client.calls[-1][1:] == ("bkt", "some/prefix/n1/n1-1-00000.kvt.jsonl.gz")


def test_s3_upload_does_not_block_the_caller(tmp_path):
    gate = threading.Event()

    class SlowS3:
        def upload_file(self, path, bucket, key):
            gate.wait(5)

    sink = S3Sink("s3://bkt", node="n1", client=SlowS3())
    start = time.monotonic()
    sink.on_segment_closed(str(tmp_path / "a.kvt.jsonl.gz"))
    assert time.monotonic() - start < 0.5
    gate.set()
    sink.close()
    assert sink.uploaded == 1


def test_s3_sink_gives_up_without_raising(tmp_path):
    sink = S3Sink("s3://bkt", node="n1", client=FlakyS3(failures=10), backoff_seconds=0)
    assert sink.upload(str(tmp_path / "f.kvt.jsonl.gz")) is False
    sink.close()
    assert sink.failed == 1 and sink.key_for("/x/f.gz") == "n1/f.gz"


def test_valkey_sink_pipelines_records():
    client = FakeValkey()
    sink = ValkeySink("valkey://localhost:6379/kvstream", client=client, batch_size=2,
                      flush_seconds=3600)
    for i in range(3):
        sink.on_record({"t": "S", "h": [str(i)]})
    deadline = time.monotonic() + 5
    while len(client.entries) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(client.entries) == 2 and client.executes == 1
    sink.close()
    assert len(client.entries) == 3 and client.closed
    stream, fields, maxlen, approximate = client.entries[0]
    assert stream == "kvstream" and fields["r"] == '{"t":"S","h":["0"]}'
    assert maxlen == 1_000_000 and approximate is True


def test_valkey_sink_failures_are_counted():
    sink = ValkeySink("valkey://h:1/s", client=FakeValkey(fail=True))
    sink.on_record({"t": "C"})
    sink.close()
    assert sink.failed == 1


def test_valkey_sink_never_blocks_the_caller():
    gate = threading.Event()

    class HungValkey(FakeValkey):
        def pipeline(self, transaction=True):
            gate.wait(5)
            return super().pipeline(transaction)

    sink = ValkeySink("valkey://h:1/s", client=HungValkey(), batch_size=1, queue_size=2)
    start = time.monotonic()
    for _ in range(10):
        sink.on_record({"t": "C"})
    assert time.monotonic() - start < 0.5 and sink.dropped >= 7
    gate.set()
    sink.close()


def test_make_sink_builds_valkey_sink():
    assert isinstance(make_sink("valkey://localhost:6379/s", node="n1", client=FakeValkey()),
                      ValkeySink)


def test_valkey_url_credentials_reach_the_client(monkeypatch):
    seen = {}

    class FakeModule:
        class Valkey:
            @staticmethod
            def from_url(url, **kwargs):
                seen["url"], seen["kwargs"] = url, kwargs
                return FakeValkey()

    monkeypatch.setitem(sys.modules, "valkey", FakeModule)
    sink = ValkeySink("valkeys://user:pw@cache.example:6380/mystream")
    assert sink.stream == "mystream"
    assert seen["url"] == "valkeys://user:pw@cache.example:6380"
    assert seen["kwargs"]["socket_timeout"] == 2.0


def test_make_sink_variants_and_errors(monkeypatch):
    assert isinstance(make_sink("file", "n"), FileSink)
    assert isinstance(make_sink(None, "n"), FileSink)
    with pytest.raises(SinkError):
        make_sink("ftp://x", "n")
    with pytest.raises(SinkError):
        make_sink("s3://", "n", client=object())
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(SinkError, match=r"\[s3\]"):
        make_sink("s3://bucket/p", "n")
    monkeypatch.setitem(sys.modules, "valkey", None)
    with pytest.raises(SinkError, match=r"\[valkey\]"):
        make_sink("valkey://h:1/s", "n")
