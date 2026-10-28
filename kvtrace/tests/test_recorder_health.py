"""Bind mode, the no-event watchdog and the P (health) / W (warning) records."""

import json
import logging
import socket
import threading

import pytest
from conftest import make_publisher, read_segment, run_recorders

from kvtrace.cli import main, summarize_trace
from kvtrace.health import ProcessSampler
from kvtrace.recorder import EXIT_NO_EVENTS, Recorder, RecorderConfig, check_bind_endpoint
from kvtrace.simulator import SimParams, parse_capacities, simulate


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def records_of(recorder):
    return [r for p in recorder.writer.closed_paths for r in read_segment(p)]


def config(tmp_path, endpoint, **kwargs):
    kwargs.setdefault("duration", 3.0)
    return RecorderConfig(endpoint=endpoint, model="m", out_dir=str(tmp_path), node_id="n1", **kwargs)


def test_both_sides_connecting_records_nothing_and_fails(tmp_path, caplog):
    # vLLM with tcp://127.0.0.1:PORT connects, and so does a default recorder, so
    # nothing listens and no event can arrive.
    endpoint = f"tcp://127.0.0.1:{free_port()}"
    pub = make_publisher(bind=endpoint, connect=True, wait_subscribers=0, start_delay=0.0,
                         duration=3.0, topic="kv-events")
    recorder = Recorder(config(tmp_path, endpoint, topic="kv-events", require_events_within=2.0,
                               duration=5.0))
    pub_thread = pub.start_thread()
    with caplog.at_level(logging.ERROR, logger="kvtrace.recorder"):
        recorder.run()
    pub_thread.join(timeout=10)
    assert pub.sent > 0 and recorder.stats.batches == 0
    assert recorder.exit_code == EXIT_NO_EVENTS
    (error,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert endpoint in error.getMessage() and "'kv-events'" in error.getMessage()
    records = records_of(recorder)
    (warning,) = [r for r in records if r["t"] == "W"]
    assert warning["code"] == "no_events" and warning["endpoints"] == [endpoint]
    assert warning["topic"] == "kv-events" and warning["endpoint_mode"] == "connect"
    # The final health record is written even when the recorder gives up.
    assert [r["final"] for r in records if r["t"] == "P"] == [True]


def test_subscriber_on_forwarder_xsub_port_records_nothing_and_fails(tmp_path):
    # A real failure: vLLM connected its PUB to a forwarder's XSUB (input) port and
    # the recorder's SUB connected to that same port. SUB and XSUB never complete the
    # ZMTP handshake, so the recorder got nothing; only the XPUB (output) port works.
    import zmq

    ctx = zmq.Context.instance()
    xsub, xpub = ctx.socket(zmq.XSUB), ctx.socket(zmq.XPUB)
    xsub_port = xsub.bind_to_random_port("tcp://127.0.0.1")
    xpub_port = xpub.bind_to_random_port("tcp://127.0.0.1")
    proxy = threading.Thread(target=lambda: _proxy(xsub, xpub), daemon=True)
    proxy.start()
    pub = make_publisher(bind=f"tcp://127.0.0.1:{xsub_port}", connect=True, wait_subscribers=0,
                         start_delay=0.5, duration=3.0, rate=50.0, topic="kv-events")
    wrong = Recorder(config(tmp_path / "xsub", f"tcp://127.0.0.1:{xsub_port}", topic="kv-events",
                            require_events_within=1.5, duration=4.0))
    right = Recorder(config(tmp_path / "xpub", f"tcp://127.0.0.1:{xpub_port}", topic="kv-events",
                            require_events_within=1.5, duration=4.0))
    threads = [threading.Thread(target=r.run, daemon=True) for r in (wrong, right)]
    for t in threads:
        t.start()
    pub_thread = pub.start_thread()
    for t in threads + [pub_thread]:
        t.join(timeout=20)
        assert not t.is_alive()
    xsub.close(linger=0)
    xpub.close(linger=0)
    assert wrong.stats.batches == 0 and wrong.exit_code == EXIT_NO_EVENTS
    assert [r["code"] for r in records_of(wrong) if r["t"] == "W"] == ["no_events"]
    assert right.stats.batches > 0 and right.exit_code == 0


def _proxy(xsub, xpub):
    import zmq

    try:
        zmq.proxy(xsub, xpub)
    except zmq.ZMQError:
        pass  # sockets closed at the end of the test


def test_bind_mode_ipv6_loopback(tmp_path):
    recorder = Recorder(config(tmp_path, "tcp://[::1]:0", endpoint_mode="bind"))
    try:
        recorder.open()
        (endpoint,) = recorder.endpoints
        assert endpoint.startswith("tcp://[::1]:")
    finally:
        recorder._close_sockets()


def test_ipv6_loopback_bind_mode_with_gap_replay(tmp_path):
    # Both the bound SUB and the connecting replay DEALER need IPv6 enabled for [::1].
    recorder = Recorder(config(tmp_path, "tcp://[::1]:0", endpoint_mode="bind", duration=4.0))
    recorder.open()
    (endpoint,) = recorder.endpoints
    pub = make_publisher(bind=endpoint, connect=True, replay_bind="tcp://[::1]:0", max_batches=26,
                         drop_every=5)
    recorder.cfg.replay_endpoint = pub.replay_endpoint
    recorder._ranks[0].replay_endpoint = pub.replay_endpoint
    thread = threading.Thread(target=recorder.run, daemon=True)
    thread.start()
    pub_thread = pub.start_thread()
    for t in (pub_thread, thread):
        t.join(timeout=30)
        assert not t.is_alive()
    assert recorder.stats.batches == 26 and pub.dropped == 5
    assert recorder.stats.recovered_batches == 5 and recorder.stats.unrecovered_gap_batches == 0


def test_bind_mode_port_zero_needs_one_rank(tmp_path):
    with pytest.raises(ValueError, match="fixed port"):
        config(tmp_path, "tcp://127.0.0.1:0", endpoint_mode="bind", dp_ranks=2).validate()


def test_no_event_error_with_batches_points_at_decoding(tmp_path, caplog):
    clock = _Clock()
    recorder = Recorder(config(tmp_path, "tcp://127.0.0.1:1", require_events_within=10.0),
                        clock=clock)
    recorder._started = clock.now
    recorder.stats.batches, recorder.stats.decode_errors = 7, 3
    with caplog.at_level(logging.ERROR, logger="kvtrace.recorder"):
        recorder.check_events(clock.now + 11)
    recorder.writer.close()
    (error,) = caplog.records
    assert "7 batches arrived" in error.getMessage() and "--engine" in error.getMessage()
    assert "--endpoint-mode bind" not in error.getMessage()
    (warning,) = [r for r in records_of(recorder) if r["t"] == "W"]
    assert warning["batches"] == 7 and warning["decode_errors"] == 3
    assert recorder.exit_code == EXIT_NO_EVENTS


def test_bind_mode_records_vllm_framing_from_a_connecting_publisher(tmp_path):
    recorder = Recorder(config(tmp_path, "tcp://127.0.0.1:0", endpoint_mode="bind",
                               topic="kv-events", require_events_within=2.0))
    recorder.open()
    (endpoint,) = recorder.endpoints
    assert endpoint.startswith("tcp://127.0.0.1:") and not endpoint.endswith(":0")
    # vLLM framing: multipart [b"kv-events", seq, payload] from a PUB that connects.
    pub = make_publisher(bind=endpoint, connect=True, max_batches=30, topic="kv-events")
    thread = threading.Thread(target=recorder.run, daemon=True)
    thread.start()
    pub_thread = pub.start_thread()
    for t in (pub_thread, thread):
        t.join(timeout=30)
        assert not t.is_alive()
    assert recorder.stats.batches == 30 and recorder.exit_code == 0
    assert not [r for r in records_of(recorder) if r["t"] == "W"]


def test_topic_mismatch_is_reported(tmp_path):
    pub = make_publisher(max_batches=20, topic="kv-events", wait_subscribers=1)
    (recorder,) = run_recorders([pub], [config(tmp_path, pub.endpoint, topic="other",
                                               require_events_within=2.0,
                                               fail_on_no_events=False, duration=3.0)])
    assert recorder.stats.batches == 0 and recorder.exit_code == 0
    assert [r["code"] for r in records_of(recorder) if r["t"] == "W"] == ["no_events"]


@pytest.mark.parametrize("endpoint", ["tcp://*:5557", "tcp://0.0.0.0:5557", "tcp://10.1.2.3:5557",
                                      "tcp://[::]:5557", "tcp://example.com:5557"])
def test_bind_mode_refuses_non_loopback(endpoint, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolves_to("93.184.216.34"))
    with pytest.raises(ValueError, match="loopback"):
        check_bind_endpoint(endpoint)


def test_bind_mode_accepts_loopback_and_ipc():
    for endpoint in ("tcp://127.0.0.1:5557", "tcp://[::1]:5557", "tcp://localhost:5557",
                     "ipc:///tmp/kv.sock"):
        check_bind_endpoint(endpoint)


def _resolves_to(*addresses):
    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM,
                 socket.IPPROTO_TCP, "", (a, 0)) for a in addresses]
    return getaddrinfo


def test_bind_mode_refuses_name_resolving_to_non_loopback(monkeypatch):
    # A hosts file can map "localhost" to a network address; bind mode must not trust the name.
    monkeypatch.setattr(socket, "getaddrinfo", _resolves_to("10.1.2.3"))
    with pytest.raises(ValueError, match="loopback"):
        check_bind_endpoint("tcp://localhost:5557")
    monkeypatch.setattr(socket, "getaddrinfo", _resolves_to("127.0.0.1", "10.1.2.3"))
    with pytest.raises(ValueError, match="loopback"):
        check_bind_endpoint("tcp://localhost:5557")
    monkeypatch.setattr(socket, "getaddrinfo", _resolves_to("127.0.0.1", "::1"))
    check_bind_endpoint("tcp://localhost:5557")


def test_bind_mode_refuses_unresolvable_name(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(ValueError, match="cannot resolve"):
        check_bind_endpoint("tcp://no-such-host:5557")


def test_cli_bind_mode_refuses_wildcard(tmp_path, capsys):
    assert main(["record", "--endpoint", "tcp://*:5557", "--endpoint-mode", "bind", "--model", "m",
                 "--out", str(tmp_path), "--duration", "0.1"]) == 2
    assert "loopback" in capsys.readouterr().err


def test_cli_record_exits_3_without_events(tmp_path, capsys):
    rc = main(["record", "--endpoint", f"tcp://127.0.0.1:{free_port()}", "--model", "m",
               "--out", str(tmp_path / "t"), "--require-events-within", "2", "--duration", "5"])
    assert rc == EXIT_NO_EVENTS
    capsys.readouterr()
    assert main(["inspect", str(tmp_path / "t")]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["warnings"][0]["code"] == "no_events"
    (health,) = summary["health"].values()
    assert health["final"] is True and health["batches"] == 0


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_stall_warning_needs_active_requests(tmp_path):
    clock = _Clock()
    recorder = Recorder(config(tmp_path, "tcp://127.0.0.1:1", require_events_within=60.0),
                        clock=clock)
    recorder._started = clock.now
    recorder.stats.events["S"] = 5
    recorder.check_events(clock.now)  # events arrived
    clock.now += 120
    recorder.check_events(clock.now)  # no metrics: an idle engine is not a stall
    assert recorder.stats.records == 0
    recorder._metrics_q.put((clock.now - 10, {"vllm:num_requests_running": 0.0,
                                              "vllm:prompt_tokens_total": 100.0}, {}))
    recorder._metrics_q.put((clock.now, {"vllm:num_requests_running": 0.0,
                                         "vllm:prompt_tokens_total": 900.0}, {}))
    recorder._drain_metrics()
    recorder.check_events(clock.now)
    recorder.check_events(clock.now + 5)  # reported once per stall
    recorder.writer.close()
    warnings = [r for r in records_of(recorder) if r["t"] == "W"]
    assert [w["code"] for w in warnings] == ["events_stalled"] and warnings[0]["seconds"] == 120.0
    assert recorder.exit_code == 0


def test_periodic_health_records_and_log_line(tmp_path, caplog):
    # 55 batches: the last one is sent, so every dropped batch shows up as a gap.
    pub = make_publisher(max_batches=55, rate=40.0, drop_every=10)
    with caplog.at_level(logging.INFO, logger="kvtrace.recorder"):
        (recorder,) = run_recorders([pub], [config(tmp_path, pub.endpoint,
                                                   replay_endpoint=pub.replay_endpoint,
                                                   health_interval=0.5, duration=3.0)])
    health = [r for r in records_of(recorder) if r["t"] == "P"]
    assert len(health) >= 3 and health[-1]["final"] is True
    assert not any(h["final"] for h in health[:-1])
    last = health[-1]
    assert last["batches"] == 55 and last["events"]["S"] == recorder.stats.events["S"]
    assert last["replay_requests"] >= 1 and last["recovered_batches"] == pub.dropped
    assert last["bytes_written"] > 0 and last["rss_bytes"] > 0 and last["cpu_seconds"] >= 0
    assert any(h["events_per_s"] for h in health)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("health ")]
    assert json.loads(lines[-1][len("health "):])["final"] is True


def test_simulate_and_inspect_accept_p_and_w_records():
    base = {"node": "n1", "model": "m", "rank": 0}
    records = [
        {"t": "S", "ts": 1.0, "ns": "", "medium": "GPU", "bs": 16, "h": ["aa"], "p": None, "n": 16,
         **base},
        {"t": "P", "ts": 2.0, "node": "n1", "final": False, "batches": 1},
        {"t": "W", "ts": 2.5, "node": "n1", "code": "events_stalled", "message": "x"},
        {"t": "P", "ts": 3.0, "node": "n1", "final": True, "batches": 2},
    ]
    sim = simulate(list(records), SimParams(chunk_tokens=16, capacities=parse_capacities("inf")),
                   sources=["t"])
    assert "unknown_record" not in json.dumps(sim["trace"])
    summary = summarize_trace(records)
    assert summary["health"]["n1"]["batches"] == 2 and summary["health"]["n1"]["final"] is True
    assert summary["warnings"][0]["code"] == "events_stalled"
    assert summary["window"]["end"] == 1.0


def test_process_sampler_cpu_percent():
    wall, cpu = iter([0.0, 10.0, 20.0]), iter([0.0, 5.0, 15.0])
    sampler = ProcessSampler(wall=lambda: next(wall), cpu=lambda: next(cpu))
    assert sampler.sample()["cpu_pct"] == 50.0
    second = sampler.sample()
    assert second["cpu_pct"] == 100.0 and second["cpu_pct_avg"] == 75.0


def _metrics_recorder(tmp_path, clock, **kwargs):
    recorder = Recorder(config(tmp_path, "tcp://127.0.0.1:1", require_events_within=60.0,
                               metrics_url="http://127.0.0.1:1/metrics", **kwargs), clock=clock)
    recorder._started = clock.now
    return recorder


def _metrics(recorder, ts, running):
    recorder._metrics_q.put((ts, {"vllm:num_requests_running": running}, {}))
    recorder._drain_metrics()


def test_watchdog_with_metrics_waits_for_traffic(tmp_path):
    # A loading or idle engine sends no events: with metrics that is not a failure.
    clock = _Clock()
    recorder = _metrics_recorder(tmp_path, clock)
    recorder.check_events(clock.now + 600)  # no metrics sample yet (still loading)
    _metrics(recorder, clock.now + 600, 0.0)
    recorder.check_events(clock.now + 1200)  # healthy but idle
    recorder.writer.close()
    assert recorder.exit_code == 0
    assert not [r for r in records_of(recorder) if r["t"] == "W"]


def test_watchdog_with_metrics_fails_when_traffic_has_no_events(tmp_path, caplog):
    # The same case: the engine serves requests but no KV event reaches the recorder.
    clock = _Clock()
    recorder = _metrics_recorder(tmp_path, clock)
    _metrics(recorder, clock.now + 300, 4.0)
    recorder.check_events(clock.now + 300)  # traffic starts the window
    recorder.check_events(clock.now + 359)
    assert recorder.exit_code == 0
    with caplog.at_level(logging.ERROR, logger="kvtrace.recorder"):
        recorder.check_events(clock.now + 360)
    recorder.writer.close()
    assert recorder.exit_code == EXIT_NO_EVENTS
    (error,) = caplog.records
    assert "since metrics showed requests" in error.getMessage()
    (warning,) = [r for r in records_of(recorder) if r["t"] == "W"]
    assert warning["code"] == "no_events" and warning["seconds"] == 60.0
