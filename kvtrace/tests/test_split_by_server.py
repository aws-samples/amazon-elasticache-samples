"""Tests for examples/split_by_server/split_by_server.py."""

from __future__ import annotations

import gzip
import importlib.util
import json
import os

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "examples", "split_by_server", "split_by_server.py")
_SPEC = importlib.util.spec_from_file_location("split_by_server", _PATH)
assert _SPEC and _SPEC.loader
sbs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sbs)


def _write_inputs(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "".join(
            json.dumps({"timestamp": 1000 * i, "input_length": 10 + i, "output_length": 1, "hash_ids": [i]})
            + "\n"
            for i in range(5)
        )
    )
    reqs = tmp_path / "requests.jsonl.gz"
    rows = [
        {"line": 0, "server": "a:8000", "pass": 0},
        {"line": 1, "server": "b:8000", "pass": 0},
        {"line": 3, "server": "a:8000", "pass": 0},
        {"line": 4, "server": "b:8000", "pass": 0},
        {"line": 2, "server": "a:8000", "pass": 1},
    ]
    with gzip.open(reqs, "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return trace, reqs, rows


def test_server_lines_filters_server_and_pass(tmp_path):
    _, _, rows = _write_inputs(tmp_path)
    assert sbs.server_lines(rows, "a:8000") == {0, 3}
    assert sbs.server_lines(rows, "a:8000", pass_no=1) == {2}
    assert sbs.server_lines(rows, "c:8000") == set()


def test_main_keeps_trace_order_and_timestamps(tmp_path):
    trace, reqs, _ = _write_inputs(tmp_path)
    out = tmp_path / "out.jsonl"
    rc = sbs.main(["--trace", str(trace), "--requests", str(reqs), "--server", "a:8000", "--out", str(out)])
    assert rc == 0
    got = [json.loads(x) for x in out.read_text().splitlines()]
    assert [r["timestamp"] for r in got] == [0, 3000]
    assert [r["input_length"] for r in got] == [10, 13]


def test_main_writes_to_stdout(tmp_path, capsys):
    trace, reqs, _ = _write_inputs(tmp_path)
    assert sbs.main(["--trace", str(trace), "--requests", str(reqs), "--server", "b:8000"]) == 0
    got = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [r["timestamp"] for r in got] == [1000, 4000]


def test_main_fails_for_unknown_server_and_lists_servers_per_pass(tmp_path, capsys):
    trace, reqs, _ = _write_inputs(tmp_path)
    assert sbs.main(["--trace", str(trace), "--requests", str(reqs), "--server", "z:1"]) == 1
    capsys.readouterr()
    assert sbs.main(["--trace", str(trace), "--requests", str(reqs)]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert listed == ["a:8000\tpass 0\t2", "b:8000\tpass 0\t2", "a:8000\tpass 1\t1"]


def test_main_fails_when_trace_is_shorter_than_log(tmp_path):
    trace, reqs, _ = _write_inputs(tmp_path)
    trace.write_text(trace.read_text().splitlines(keepends=True)[0])
    assert sbs.main(["--trace", str(trace), "--requests", str(reqs), "--server", "a:8000", "--out",
                     str(tmp_path / "o.jsonl")]) == 1
