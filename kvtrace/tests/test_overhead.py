"""Tests for examples/overhead/: the process sampler and the A/B compare script."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "overhead")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_DIR, f"{name}.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mo = _load("measure_overhead")
ab = _load("compare_ab")

PIDSTAT = """Linux 6.8.0 (host)  10/20/26  _x86_64_  (192 CPU)

# Time  UID  PID  %usr %system  %guest  %wait  %CPU  CPU  minflt/s  majflt/s  VSZ  RSS  %MEM  Command
03:40:05  1000  12345  3.00  1.00  0.00  0.00  4.00  7  0.00  0.00  812345  65536  0.01  kvtrace
03:40:05  1000  777  101.00  2.50  0.00  0.00  103.50  12  1.00  0.00  999999  1048576  0.50  VLLM::EngineCor
"""


def test_parse_pidstat_aligns_fields_after_the_hash_header():
    rows = list(mo.parse_pidstat(PIDSTAT.splitlines(), now=lambda: 10.0))
    assert [(r["pid"], r["cpu_pct"], r["rss_bytes"], r["comm"]) for r in rows] == [
        (12345, 4.0, 65536 * 1024, "kvtrace"), (777, 103.5, 1048576 * 1024, "VLLM::EngineCor")]
    assert all(r["source"] == "pidstat" and r["ts"] == 10.0 for r in rows)


def test_percentile_nearest_rank():
    values = list(range(1, 101))
    assert mo.percentile(values, 95) == 95 and mo.percentile(values, 50) == 50
    assert mo.percentile([7.0], 95) == 7.0 and mo.percentile([], 95) is None


def test_summarize_per_process():
    rows = [{"pid": 1, "cpu_pct": c, "rss_bytes": r} for c, r in ((10.0, 100), (30.0, 300), (20.0, 200))]
    summary = mo.summarize(rows, {1: "kvtrace"}, {1: {"write_bytes": 0, "wchar": 10}},
                           {1: {"write_bytes": 4096, "wchar": 110}})
    proc = summary["processes"]["1"]
    assert proc["cpu_pct_mean"] == 20.0 and proc["cpu_pct_max"] == 30.0 and proc["cpu_pct_p95"] == 30.0
    assert proc["rss_max_bytes"] == 300 and proc["write_bytes"] == 4096 and proc["wchar_bytes"] == 100


@pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="needs Linux /proc")
def test_sampler_smoke_on_fake_publisher_and_recorder(tmp_path):
    # A real `kvtrace record` (bind mode) fed by `kvtrace fake-publisher --connect`.
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    kvtrace = [sys.executable, "-m", "kvtrace.cli"]
    trace = tmp_path / "trace"
    rec = subprocess.Popen(kvtrace + ["record", "--endpoint-mode", "bind", "--endpoint",
                                      f"tcp://127.0.0.1:{port}", "--model", "m", "--node-id", "smoke",
                                      "--out", str(trace), "--duration", "6", "--health-interval", "1",
                                      "--rotate-seconds", "1"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    pub = subprocess.Popen(kvtrace + ["fake-publisher", "--connect", "--bind", f"tcp://127.0.0.1:{port}",
                                      "--replay-bind", "tcp://127.0.0.1:0", "--rate", "200",
                                      "--duration", "7"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.5)
        out = tmp_path / "overhead"
        assert mo.main(["--pid", str(rec.pid), "--pid", str(pub.pid), "--interval", "0.5",
                        "--method", "proc", "--bytes-dir", str(trace), "--duration", "4",
                        "--out", str(out)]) == 0
    finally:
        rec.wait(timeout=20)
        pub.wait(timeout=20)
    summary = json.loads((out / "summary.json").read_text())
    assert set(summary["processes"]) == {str(rec.pid), str(pub.pid)}
    recorder = summary["processes"][str(rec.pid)]
    assert recorder["samples"] >= 3 and recorder["rss_max_bytes"] > 0
    assert recorder["cpu_pct_mean"] is not None and recorder["cpu_pct_max"] >= recorder["cpu_pct_mean"]
    assert summary["bytes_dir"]["grew_bytes"] > 0
    rows = [json.loads(line) for line in (out / "samples.jsonl").read_text().splitlines()]
    assert rows and all(r["source"] == "proc" for r in rows)
    assert rec.returncode == 0
    stats = json.loads(rec.stdout.read())["stats"]
    assert stats["batches"] > 0


@pytest.mark.parametrize("stat", ["", "123 (x) S 1 2", "123 (x) S " + " ".join(["z"] * 20)])
def test_read_cpu_rss_malformed_stat_is_none(monkeypatch, stat):
    import builtins
    import io

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/4242/stat":
            return io.StringIO(stat)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert mo.read_cpu_rss(4242) is None


def test_find_pids_never_matches_itself():
    assert os.getpid() not in mo.find_pids([r"pytest|python"])


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _rows(ttfts, tpot=0.1, errors=0):
    rows = [{"phase": "measure", "status": "ok", "ttft_s": t, "tpot_s": tpot} for t in ttfts]
    rows += [{"phase": "measure", "status": "error", "ttft_s": None, "tpot_s": None}] * errors
    rows.append({"phase": "warmup", "status": "ok", "ttft_s": 99.0, "tpot_s": 9.0})
    return rows


def test_compare_ab_pass_and_fail(tmp_path, capsys):
    base = [1.0 + i / 100 for i in range(100)]
    _write(tmp_path / "off" / "requests.jsonl", _rows(base))
    _write(tmp_path / "on" / "requests.jsonl", _rows([t * 1.005 for t in base], errors=2))
    _write(tmp_path / "slow" / "requests.jsonl", _rows([t * 1.05 for t in base]))
    out = tmp_path / "ab.json"
    assert ab.main([str(tmp_path / "off"), str(tmp_path / "on"), "-o", str(out)]) == 1
    assert json.loads(out.read_text())["fail_reasons"] == ["err +2 > 0"]
    assert ab.main([str(tmp_path / "off"), str(tmp_path / "on"), "--max-err-delta", "2",
                    "-o", str(out)]) == 0
    result = json.loads(out.read_text())
    assert result["a"]["ok"] == 100 and result["a"]["err"] == 0 and result["b"]["err"] == 2
    assert result["b"]["status"] == {"error": 2, "ok": 100}
    assert result["a"]["ttft_p50_s"] == 1.49 and result["a"]["tpot_p50_s"] == 0.1
    assert result["delta_b_minus_a"]["ttft_p50_pct"] == pytest.approx(0.5)
    assert result["delta_b_minus_a"]["err"] == 2 and result["verdict"] == "pass"
    assert ab.main([str(tmp_path / "off"), str(tmp_path / "slow")]) == 1
    capsys.readouterr()
    assert ab.main([str(tmp_path / "off"), str(tmp_path / "slow"),
                    "--max-ttft-p50-delta-pct", "6"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"
    assert ab.main([str(tmp_path / "off"), str(tmp_path / "on"), "--max-err-delta", "-1"]) == 0


def test_sampler_restores_signal_handlers(tmp_path):
    import signal

    before = signal.getsignal(signal.SIGINT)
    assert mo.main(["--pid", str(os.getpid()), "--interval", "0.1", "--duration", "0.3",
                    "--method", "proc", "--out", str(tmp_path)]) == 0
    assert signal.getsignal(signal.SIGINT) is before


def test_compare_ab_no_ok_rows(tmp_path):
    _write(tmp_path / "a.jsonl", _rows([1.0]))
    _write(tmp_path / "b.jsonl", [{"phase": "measure", "status": "error"}])
    assert ab.main([str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")]) == 2
