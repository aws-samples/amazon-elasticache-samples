import gzip
import json
import threading

from conftest import make_publisher

from kvtrace.cli import main
from kvtrace.fake import CANARY_TOKEN
from kvtrace.recorder import Recorder, RecorderConfig


def test_cli_record_simulate_report_end_to_end(tmp_path, capsys):
    out = tmp_path / "traces"
    pubs = [make_publisher(engine="vllm", max_batches=60, seed=s, prefix_seed=3, gpu_tokens=4096)
            for s in (1, 2)]
    # node1 is recorded in the background; node0 through the CLI in the main thread.
    other = Recorder(RecorderConfig(endpoint=pubs[1].endpoint, replay_endpoint=pubs[1].replay_endpoint,
                                    model="llama", out_dir=str(out), node_id="node1", duration=3.0))
    other_thread = threading.Thread(target=other.run, daemon=True)
    other_thread.start()
    threads = [p.start_thread() for p in pubs]
    rc = main(["record", "--endpoint", pubs[0].endpoint, "--replay-endpoint", pubs[0].replay_endpoint,
               "--model", "llama", "--node-id", "node0", "--out", str(out), "--duration", "3"])
    assert rc == 0
    for t in threads + [other_thread]:
        t.join(timeout=10)

    segs = list(out.rglob("*.kvt.jsonl.gz"))
    assert segs
    capsys.readouterr()
    assert main(["inspect", str(out)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["nodes"] == ["node0", "node1"] and summary["record_counts"]["S"] > 0

    sim_path = tmp_path / "sim.json"
    assert main(["simulate", str(out), "--preset", "llama-3.1-8b", "--chunk-tokens", "64",
                 "--capacity", "1GiB,inf", "-o", str(sim_path)]) == 0
    sim = json.loads(sim_path.read_text())
    assert [c["label"] for c in sim["capacities"]] == ["1GiB", "inf"]
    assert sim["inputs"]["params"]["kv_bytes_per_token"] == 131072
    assert sim["capacities"][-1]["saved_tokens"] > 0

    html, md = tmp_path / "r.html", tmp_path / "r.md"
    assert main(["report", str(sim_path), "-o", str(html), "--markdown", str(md),
                 "--prefill-tokens-per-sec", "20000", "--prefill-time-fraction", "0.3",
                 "--l2-cost-per-gb-hour", "0.01"]) == 0
    text = html.read_text()
    assert "<script" not in text and "Recommended capacity" in text
    assert "Net $ / month" in md.read_text()
    for seg in segs:
        assert str(CANARY_TOKEN) not in gzip.open(seg, "rt").read()


def test_cli_errors_are_reported(tmp_path, capsys):
    assert main(["simulate", str(tmp_path / "missing")]) == 2
    assert "error" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["simulate", str(empty)]) == 2
    assert main(["record", "--endpoint", "not-an-endpoint", "--model", "m",
                 "--out", str(tmp_path / "o"), "--duration", "0.1"]) == 2
    assert "kvtrace: error:" in capsys.readouterr().err


def test_report_cost_is_per_gpu(tmp_path, sim_json):
    out = tmp_path / "r.md"
    assert main(["report", str(sim_json), "-o", str(tmp_path / "r.html"), "--markdown", str(out),
                 "--prefill-tokens-per-sec", "10000", "--gpu-cost-per-hour", "6.88",
                 "--gpus-per-replica", "8"]) == 0
    assert "| Replica cost per hour | $55.04 ($6.88 per GPU-hour x 8 GPUs" in out.read_text()


def test_report_has_no_default_price(tmp_path, sim_json):
    out = tmp_path / "r.md"
    assert main(["report", str(sim_json), "-o", str(tmp_path / "r.html"), "--markdown", str(out),
                 "--prefill-tokens-per-sec", "10000"]) == 0
    text = out.read_text()
    assert "| Replica cost per hour | missing (no default price) |" in text
    assert "no default price; dollar figures omitted" in text
    assert "gross GPU savings" not in text and "replica-seconds of prefill saved" in text


def test_report_zero_replica_cost_is_used(tmp_path, sim_json):
    out = tmp_path / "r.md"
    assert main(["report", str(sim_json), "-o", str(tmp_path / "r.html"), "--markdown", str(out),
                 "--prefill-tokens-per-sec", "10000", "--replica-cost-per-hour", "0"]) == 0
    assert "| Replica cost per hour | $0.00 (input) |" in out.read_text()


def test_report_warns_when_gpus_disagree_with_trace_tp(tmp_path, sim_json, caplog):
    sim = json.loads(sim_json.read_text())
    sim["metrics"]["per_node"] = {"node0": {"counters": {
        f'lmcache:num_hit_tokens_total{{worker_id="{w}",role="worker"}}': 1.0 for w in range(2)},
        "gauges": {}}}
    sim_json.write_text(json.dumps(sim))
    out = tmp_path / "r.md"
    assert main(["report", str(sim_json), "-o", str(tmp_path / "r.html"), "--markdown", str(out),
                 "--prefill-tokens-per-sec", "10000", "--gpu-cost-per-hour", "6.88",
                 "--gpus-per-replica", "8"]) == 0
    assert "does not agree with the trace" in caplog.text
    assert "Warning: --gpus-per-replica 8 does not agree" in out.read_text()
