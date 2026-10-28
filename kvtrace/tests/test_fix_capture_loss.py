"""Lost data reaches the trace and the exit status: scrape failures, sink losses, disk errors."""

import errno
import gzip
import http.server
import json
import threading

import pytest
from conftest import make_publisher, read_segment, run_recorders

import kvtrace.cli as cli
from kvtrace.metrics import source_label
from kvtrace.recorder import (
    EXIT_SINK_LOST,
    EXIT_WRITE_FAILED,
    Recorder,
    RecorderConfig,
)
from kvtrace.sinks import S3Sink, Sink, ValkeySink

IDLE = "tcp://127.0.0.1:1"
DOWN = "http://127.0.0.1:1/metrics"


class LosingSink(Sink):
    def __init__(self, lost=1):
        self._lost = lost
        self.closed = False

    def close(self):
        self.closed = True

    def stats(self):
        return {"test_failed": self._lost}

    @property
    def lost(self):
        return self._lost


@pytest.fixture
def metrics_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"vllm:num_requests_running 0\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/metrics"
    server.shutdown()
    server.server_close()


def idle_cfg(out, **kwargs):
    kwargs.setdefault("node_id", "n1")
    kwargs.setdefault("duration", 0.3)
    return RecorderConfig(endpoint=IDLE, model="m", out_dir=str(out), require_events_within=0,
                          health_interval=0, **kwargs)


def final_health(rec):
    records = [r for p in rec.writer.closed_paths for r in read_segment(p)]
    return [r for r in records if r["t"] == "P"][-1]


def test_failed_engine_scrapes_are_counted_in_stats_and_p(tmp_path):
    rec = Recorder(idle_cfg(tmp_path, metrics_url=DOWN, metrics_interval=0.05, duration=1.0))
    stats = rec.run()
    assert stats.metrics_failures > 0
    assert final_health(rec)["metrics_failures"] == stats.metrics_failures


def test_failed_extra_url_is_counted_per_label(tmp_path, metrics_server):
    bad = "http://user:secret@127.0.0.1:1/metrics"
    rec = Recorder(idle_cfg(tmp_path, metrics_url=metrics_server, metrics_interval=0.05,
                            duration=1.0, extra_metrics_urls=(bad,),
                            extra_metrics_timeout=0.2))
    stats = rec.run()
    label = source_label(bad)
    assert stats.metrics_failures == 0
    assert stats.extra_metrics_failures[label] > 0
    health = final_health(rec)
    assert health["extra_metrics_failures"] == {label: stats.extra_metrics_failures[label]}
    assert "secret" not in json.dumps(health)


def test_sink_counters_are_in_p_records_and_lost_data_gives_its_exit_status(tmp_path):
    sink = LosingSink()
    rec = Recorder(idle_cfg(tmp_path), sink=sink)
    rec.run()
    assert final_health(rec)["sink"] == {"test_failed": 1}
    assert rec.exit_code == EXIT_SINK_LOST and sink.closed


def test_sink_without_loss_exits_zero(tmp_path):
    rec = Recorder(idle_cfg(tmp_path), sink=LosingSink(lost=0))
    rec.run()
    assert rec.exit_code == 0


def test_failing_s3_uploads_set_exit_status(tmp_path):
    class DownS3:
        def upload_file(self, path, bucket, key):
            raise ConnectionError("S3 down")

    sink = S3Sink("s3://b/p", node="n1", client=DownS3(), backoff_seconds=0)
    rec = Recorder(idle_cfg(tmp_path), sink=sink)
    rec.run()
    assert sink.stats() == {"s3_uploaded": 0, "s3_failed": 1} and sink.lost == 1
    assert rec.exit_code == EXIT_SINK_LOST


def test_s3_backoff_is_longer_than_three_seconds_and_bounded():
    class OkS3:
        def upload_file(self, path, bucket, key):
            pass

    sink = S3Sink("s3://b/p", node="n1", client=OkS3())
    delays = [sink.backoff_delay(a) for a in range(1, sink.retries)]
    sink.close()
    assert delays == [1.0, 2.0, 4.0, 8.0] and sum(delays) > 3
    assert sink.backoff_delay(20) == sink.max_backoff_seconds


def test_valkey_failures_and_drops_are_reported():
    class DownValkey:
        def pipeline(self, transaction=False):
            raise ConnectionError("valkey down")

    sink = ValkeySink("valkey://127.0.0.1:1/s", client=DownValkey(), flush_seconds=0.01)
    sink.on_record({"t": "C"})
    sink.close()
    assert sink.stats() == {"valkey_added": 0, "valkey_failed": 1, "valkey_dropped": 0}
    assert sink.lost == 1


def test_cli_record_prints_sink_counters_and_exits_with_sink_status(tmp_path, monkeypatch,
                                                                     capsys):
    monkeypatch.setattr(cli, "make_sink", lambda spec, node: LosingSink())
    rc = cli.main(["record", "--endpoint", IDLE, "--model", "m", "--out", str(tmp_path),
                   "--node-id", "n1", "--duration", "0.3", "--require-events-within", "0"])
    assert rc == EXIT_SINK_LOST
    assert json.loads(capsys.readouterr().out)["sink"] == {"test_failed": 1}


def test_record_help_documents_exit_statuses(capsys):
    with pytest.raises(SystemExit):
        cli.main(["record", "--help"])
    out = capsys.readouterr().out
    assert f"{EXIT_SINK_LOST}  the --sink lost" in out and f"{EXIT_WRITE_FAILED}  writing" in out


def test_disk_full_still_closes_writer_and_sink_and_exits_non_zero(tmp_path, monkeypatch):
    real_write = gzip.GzipFile.write
    calls = {"n": 0}

    def write(self, data):
        calls["n"] += 1
        if calls["n"] > 60:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(self, data)

    pub = make_publisher(engine="vllm", max_batches=40)
    cfg = RecorderConfig(endpoint=pub.endpoint, replay_endpoint=pub.replay_endpoint, model="m",
                         out_dir=str(tmp_path), node_id="n1", duration=3.0, rotate_bytes=1)
    sink = LosingSink(lost=0)
    monkeypatch.setattr(gzip.GzipFile, "write", write)
    (rec,) = run_recorders([pub], [cfg], sinks=[sink])
    monkeypatch.undo()
    assert rec.exit_code == EXIT_WRITE_FAILED and sink.closed
    assert rec.writer.closed_paths
    for path in rec.writer.closed_paths:
        assert read_segment(path)[0]["t"] == "H"
