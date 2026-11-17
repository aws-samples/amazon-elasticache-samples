"""Running one component at a time. Spec: .kiro/specs/component-runs.

A job builds up phase by phase on one id. Each run starts fresh: it deletes the artifact of
its first phase and of every later phase, and keeps the earlier ones as input.
"""
import json
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import common, limits
from src.api.main import app
from src.api.routes import phases as phase_routes
from src.contracts.api_models import JobRecord
from src.orchestrator.job_store import JobBusy, JobNotFound, LocalJobStore
from src.orchestrator.local_orchestrator import (
    InvalidPhases,
    JobCancelled,
    LocalOrchestrator,
    MissingInput,
    Phase,
    normalize_phases,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
PREFIX = "/api/v1"
LIVE_CONFIG = {"ssh_target": "ec2-user@db-host.example.internal", "db_user": "apcat_ro",
               "password_file": "/home/ec2-user/.apcat-db-password"}  # nosec B105 - test value, not a password
SSH_VALUES = ("ec2-user", "db-host.example.internal", "apcat_ro", ".apcat-db-password")
ALL = ["collect", "analyze", "size", "report"]


@pytest.fixture
def coll() -> dict:
    return json.loads((FIXTURES / "strong_candidate_collection.json").read_text())


@pytest.fixture
def orch(tmp_path) -> LocalOrchestrator:
    return LocalOrchestrator(str(tmp_path), price=False)


@pytest.fixture
def live_collector(monkeypatch, coll):
    """Stands in for the collector, which would need a host to connect to and a database."""
    calls: list = []

    def fake(payload, **_kwargs):
        calls.append(payload)
        return coll

    monkeypatch.setattr("src.agents.collector.handler.run_collector", fake)
    return calls


def live_payload(job_id="live-1") -> dict:
    return {"job_id": job_id, "cluster_endpoint": "db.example.internal", "database_name": "appdb",
            "mode": "live", "live_config": LIVE_CONFIG,
            "collection_options": {"window_minutes": 15}}


def available(orch, job) -> list[str]:
    return orch.store.available_phases(job)


def history(orch, job) -> list[tuple[str, int]]:
    return [(p["phase"], p["run"]) for p in orch.get_state(job)["phases"]]


# ---------------------------------------------------------------------------
# Choosing phases
# ---------------------------------------------------------------------------


def test_phases_are_put_in_pipeline_order_whatever_order_they_are_given():
    assert normalize_phases(["report", "collect", "size"]) == [
        Phase.COLLECT, Phase.SIZE, Phase.REPORT]
    assert normalize_phases([Phase.ANALYZE, "collect"]) == [Phase.COLLECT, Phase.ANALYZE]


@pytest.mark.parametrize("bad", [[], ["nope"], ["analyze", "analyze"], ["Analyze"], [""], [None]])
def test_a_bad_list_is_refused(bad):
    with pytest.raises(InvalidPhases):
        normalize_phases(bad)


def test_a_new_live_job_must_start_with_collect(orch, tmp_path):
    with pytest.raises(InvalidPhases, match="starts with collect"):
        orch.run_job("x", live_payload(), phases=["analyze"])
    assert not (tmp_path / "x").exists()  # nothing was created


def test_a_job_built_from_a_collection_must_start_with_analyze(orch, coll, tmp_path):
    for bad in (["collect"], ["size"], ["report"], ["size", "report"]):
        with pytest.raises(InvalidPhases, match="starts with analyze"):
            orch.run_from_collection("x", coll, phases=bad)
    assert not (tmp_path / "x").exists()


def test_a_list_with_a_gap_is_refused_before_a_job_is_made(orch, tmp_path):
    with pytest.raises(MissingInput, match="size needs analyze/analysis.json"):
        orch.run_job("x", live_payload(), phases=["collect", "size"])
    assert not (tmp_path / "x").exists()


def test_the_usual_set_is_unchanged(orch, coll):
    assert orch.default_phases() == [Phase.COLLECT, Phase.ANALYZE, Phase.SIZE, Phase.REPORT]
    assert LocalOrchestrator(str(orch.root), size=False).default_phases(from_collection=True) == [
        Phase.ANALYZE, Phase.REPORT]
    orch.run_from_collection("whole", coll)
    st = orch.get_state("whole")
    assert st["requested"] == ["analyze", "size", "report"] and st["run"] == 1
    assert available(orch, "whole") == ALL


# ---------------------------------------------------------------------------
# One phase after another
# ---------------------------------------------------------------------------


def test_a_job_is_built_a_phase_at_a_time_on_one_id(orch, coll):
    orch.run_from_collection("j", coll, phases=["analyze"])
    st = orch.get_state("j")
    assert st["status"] == "complete" and st["requested"] == ["analyze"] and st["run"] == 1
    assert available(orch, "j") == ["collect", "analyze"]
    assert history(orch, "j") == [("analyze", 1)]

    orch.run_phases("j", ["size"])
    st = orch.get_state("j")
    assert st["status"] == "complete" and st["requested"] == ["size"] and st["run"] == 2
    assert st["current_phase"] == "size"
    assert available(orch, "j") == ["collect", "analyze", "size"]
    assert history(orch, "j") == [("analyze", 1), ("size", 2)]

    orch.run_phases("j", [Phase.REPORT])
    assert orch.get_state("j")["run"] == 3 and available(orch, "j") == ALL
    assert history(orch, "j") == [("analyze", 1), ("size", 2), ("report", 3)]
    assert orch.get_state("j")["duration_seconds"] >= 0


def test_the_pieces_add_up_to_the_same_result_as_one_run(orch, coll):
    orch.run_from_collection("whole", coll)
    orch.run_from_collection("parts", coll, phases=["analyze"])
    orch.run_phases("parts", ["size"])
    orch.run_phases("parts", ["report"])

    def verdicts(job):
        a = orch.load_analysis(job)
        return {"tables": [(t["table_id"], t["verdict"]) for t in a["table_recommendations"]],
                "queries": [(q["queryid"], q["verdict"]) for q in a["query_recommendations"]],
                "sizing": {k: v for k, v in a["sizing"].items() if k != "generated_at"}}

    assert verdicts("parts") == verdicts("whole")
    assert orch.store.get("parts").sizing_status == orch.store.get("whole").sizing_status
    assert "How scoring works" in orch.load_text("parts", Phase.REPORT, "report.html")


def test_a_later_phase_leaves_the_earlier_artifacts_alone(orch, coll, tmp_path):
    orch.run_from_collection("j", coll, phases=["analyze"])
    files = {p: (tmp_path / "j" / p).read_bytes() for p in ("collect/output.json", "analyze/analysis.json")}
    orch.run_phases("j", ["size"])
    orch.run_phases("j", ["report"])
    for rel, data in files.items():
        assert (tmp_path / "j" / rel).read_bytes() == data


def test_several_phases_can_be_asked_for_at_once(orch, coll):
    orch.run_from_collection("j", coll, phases=["analyze"])
    orch.run_phases("j", ["report", "size"])        # order does not matter
    st = orch.get_state("j")
    assert st["requested"] == ["size", "report"] and available(orch, "j") == ALL
    assert history(orch, "j")[-2:] == [("size", 2), ("report", 2)]


def test_a_live_job_can_stop_after_collect_and_carry_on(orch, coll, live_collector):
    orch.run_job("live-1", live_payload(), phases=["collect"])
    st = orch.get_state("live-1")
    assert st["status"] == "complete" and st["requested"] == ["collect"]
    assert available(orch, "live-1") == ["collect"]
    assert orch.store.get("live-1").database_name  # learned from the collection
    assert not (orch.root / "live-1" / "analyze").exists()

    orch.run_phases("live-1", ["analyze", "size", "report"])
    assert available(orch, "live-1") == ALL
    assert len(live_collector) == 1  # the database was asked once


def test_the_job_record_follows_its_artifacts(orch, coll):
    orch.run_from_collection("j", coll)
    rec = orch.store.get("j")
    assert rec.workload_verdict is not None or rec.interpretation_mode == "deterministic_fallback"
    assert rec.sizing_status and rec.database_name
    orch.run_phases("j", ["analyze"])           # clears sizing, so the record forgets it
    rec = orch.store.get("j")
    assert rec.sizing_status is None and rec.interpretation_mode == "deterministic_fallback"
    orch.run_phases("j", ["size"])
    assert orch.store.get("j").sizing_status


# ---------------------------------------------------------------------------
# Starting fresh
# ---------------------------------------------------------------------------


def test_running_a_phase_again_deletes_it_and_everything_after(orch, coll, tmp_path):
    orch.run_from_collection("j", coll)
    assert orch.would_clear("j", Phase.ANALYZE) == ["analyze", "size", "report"]
    assert orch.would_clear("j", Phase.REPORT) == ["report"]
    assert orch.would_clear("j", Phase.COLLECT) == ALL
    collect_bytes = (tmp_path / "j" / "collect" / "output.json").read_bytes()

    orch.run_phases("j", ["analyze"])

    assert available(orch, "j") == ["collect", "analyze"]
    assert not (tmp_path / "j" / "size").exists() and not (tmp_path / "j" / "report").exists()
    assert (tmp_path / "j" / "collect" / "output.json").read_bytes() == collect_bytes


def test_sizing_again_leaves_a_report_that_must_be_rebuilt(orch, coll, tmp_path):
    orch.run_from_collection("j", coll)
    orch.run_phases("j", ["size"])
    assert available(orch, "j") == ["collect", "analyze", "size"]      # the old report is gone
    assert not (tmp_path / "j" / "report").exists()
    orch.run_phases("j", ["report"])
    assert available(orch, "j") == ALL


def test_analyze_and_report_without_size_drops_sizing_and_the_report_says_so(orch, coll):
    orch.run_from_collection("j", coll)
    orch.run_phases("j", ["analyze", "report"])
    assert available(orch, "j") == ["collect", "analyze", "report"]
    assert "Sizing was not run" in orch.load_text("j", Phase.REPORT, "report.html")


def test_a_run_that_fails_leaves_no_stale_later_artifact(orch, coll, monkeypatch):
    orch.run_from_collection("j", coll)

    def boom(*_a):
        raise RuntimeError("model offline")

    monkeypatch.setattr(orch, "_run_analyze", boom)
    with pytest.raises(RuntimeError):
        orch.run_phases("j", ["analyze", "size", "report"])
    st = orch.get_state("j")
    assert st["status"] == "failed" and "model offline" in st["error"]
    assert available(orch, "j") == ["collect"]            # nothing from the old run remains


def test_collecting_again_starts_everything_over(orch, coll, live_collector):
    orch.run_from_collection("j", coll)
    orch.run_phases("j", ["collect"], collector_input=live_payload("j"))
    assert available(orch, "j") == ["collect"]
    assert orch.store.get("j").input.cluster_endpoint == "db.example.internal"
    assert "secret" not in orch.store.get("j").model_dump_json().lower()


# ---------------------------------------------------------------------------
# Refused before anything changes
# ---------------------------------------------------------------------------


def test_a_phase_that_lacks_its_input_names_the_file_and_the_phase_that_makes_it(orch, coll):
    import shutil
    orch.run_from_collection("j", coll, phases=["analyze"])
    shutil.rmtree(orch.root / "j" / "analyze")            # take the analysis away
    before = orch.get_state("j")
    with pytest.raises(MissingInput) as err:
        orch.run_phases("j", ["size"])
    assert err.value.phase is Phase.SIZE and err.value.made_by is Phase.ANALYZE
    assert "analyze/analysis.json" in str(err.value) and "Run analyze first" in str(err.value)
    after = orch.get_state("j")
    assert (after["status"], after["run"], after["phases"]) == (
        before["status"], before["run"], before["phases"])   # nothing was claimed or run


def test_collect_needs_the_collector_input(orch, coll):
    orch.run_from_collection("j", coll)
    with pytest.raises(MissingInput, match="collector input"):
        orch.run_phases("j", ["collect"])
    assert available(orch, "j") == ALL


def test_an_unknown_job_is_not_found(orch):
    with pytest.raises(JobNotFound):
        orch.run_phases("nope", ["analyze"])


def test_an_artifact_the_run_would_clear_does_not_count_as_present(orch, coll):
    orch.run_from_collection("j", coll)
    # collect would delete the analysis, so sizing in the same request has nothing to read.
    with pytest.raises(MissingInput, match="size needs analyze/analysis.json"):
        orch.check_inputs("j", [Phase.COLLECT, Phase.SIZE], has_collector_input=True)
    orch.check_inputs("j", [Phase.COLLECT, Phase.ANALYZE, Phase.SIZE], has_collector_input=True)


def test_a_bad_job_id_never_reaches_the_disk(orch, tmp_path):
    for bad in ("..", ".x", "a..b", "x" * 65):
        with pytest.raises(ValueError):
            orch.run_phases(bad, ["analyze"])
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Busy jobs and claims
# ---------------------------------------------------------------------------


def test_a_job_that_is_queued_or_running_cannot_start_another_run(orch, coll):
    orch.run_from_collection("j", coll)
    run = orch.store.claim_run("j", ["analyze"])
    assert run == 2 and orch.get_state("j")["status"] == "queued"
    with pytest.raises(JobBusy):
        orch.store.claim_run("j", ["size"])
    with pytest.raises(JobBusy):
        orch.run_phases("j", ["size"])

    st = orch.get_state("j")
    st["status"] = "running"
    orch.store.write_state("j", st)
    with pytest.raises(JobBusy):
        orch.run_phases("j", ["report"])


def test_two_requests_cannot_both_take_the_same_job(orch, coll):
    orch.run_from_collection("j", coll)
    outcomes, gate = [], threading.Barrier(10)

    def take():
        gate.wait()
        try:
            orch.store.claim_run("j", ["analyze"])
            outcomes.append("claimed")
        except JobBusy:
            outcomes.append("busy")

    threads = [threading.Thread(target=take) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert outcomes.count("claimed") == 1 and outcomes.count("busy") == 9


def test_a_claim_takes_nothing_off_the_job(orch, coll):
    orch.run_from_collection("j", coll)
    orch.store.claim_run("j", ["analyze"])
    assert available(orch, "j") == ALL            # cleared only when the run starts


def test_a_finished_job_can_be_claimed_again_and_a_cancelled_one_too(orch, coll):
    orch.run_from_collection("j", coll)
    assert orch.store.claim_run("j", ["report"]) == 2
    assert orch.store.request_cancel("j") == "cancelled"
    assert orch.store.claim_run("j", ["report"]) == 3          # cancelled is not busy
    assert not orch.store.cancel_requested("j")


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_a_run_cancelled_in_the_queue_changes_nothing(orch, coll):
    orch.run_from_collection("j", coll)
    run = orch.store.claim_run("j", ["analyze"])
    assert orch.store.request_cancel("j") == "cancelled"
    with pytest.raises(JobCancelled):
        orch.run_phases("j", ["analyze"], claimed_run=run)
    assert available(orch, "j") == ALL                    # nothing was deleted
    assert orch.get_state("j")["status"] == "cancelled"


def test_a_run_stops_between_its_phases(orch, coll, monkeypatch):
    orch.run_from_collection("j", coll, phases=["analyze"])
    real = orch._run_size
    monkeypatch.setattr(orch, "_run_size", lambda j, c, a: (
        real(j, c, a), orch.store.request_cancel(j))[0])
    with pytest.raises(JobCancelled):
        orch.run_phases("j", ["size", "report"])
    st = orch.get_state("j")
    assert st["status"] == "cancelled" and st["note"] == "Cancelled before the report phase."
    assert available(orch, "j") == ["collect", "analyze", "size"]


def test_an_old_claim_is_not_run_after_a_newer_one(orch, coll):
    orch.run_from_collection("j", coll)
    old = orch.store.claim_run("j", ["analyze"])
    orch.store.request_cancel("j")
    new = orch.store.claim_run("j", ["size"])
    assert new == old + 1
    with pytest.raises(JobCancelled):
        orch.run_phases("j", ["analyze"], claimed_run=old)
    st = orch.get_state("j")
    assert st["status"] == "queued" and st["run"] == new and available(orch, "j") == ALL


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def test_sizing_alone_that_fails_fails_the_run(orch, coll, monkeypatch):
    orch.run_from_collection("j", coll, phases=["analyze"])
    monkeypatch.setattr(orch, "_run_size", lambda *a: (_ for _ in ()).throw(RuntimeError("pricing")))
    with pytest.raises(RuntimeError):
        orch.run_phases("j", ["size"])
    st = orch.get_state("j")
    assert st["status"] == "failed" and "pricing" in st["error"]
    assert [p["status"] for p in st["phases"] if p["run"] == 2] == ["failed"]


def test_sizing_followed_by_the_report_is_only_a_warning(orch, coll, monkeypatch):
    orch.run_from_collection("j", coll, phases=["analyze"])
    monkeypatch.setattr(orch, "_run_size", lambda *a: (_ for _ in ()).throw(RuntimeError("pricing")))
    orch.run_phases("j", ["size", "report"])
    st = orch.get_state("j")
    assert st["status"] == "complete" and any("pricing" in w for w in st["warnings"])
    assert available(orch, "j") == ["collect", "analyze", "report"]
    assert "Sizing was not run" in orch.load_text("j", Phase.REPORT, "report.html")


def test_a_failure_before_any_phase_does_not_leave_the_job_running(orch, coll, monkeypatch):
    orch.run_from_collection("j", coll)
    monkeypatch.setattr(orch, "_clear_from", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        orch.run_phases("j", ["analyze"])
    st = orch.get_state("j")
    assert st["status"] == "failed" and "disk" in st["error"] and st["completed_at"]


def test_a_claimed_run_whose_input_vanished_frees_the_job(orch, coll):
    import shutil
    orch.run_from_collection("j", coll)
    run = orch.store.claim_run("j", ["analyze"])
    shutil.rmtree(orch.root / "j" / "collect")
    with pytest.raises(MissingInput):
        orch.run_phases("j", ["analyze"], claimed_run=run)
    st = orch.get_state("j")
    assert st["status"] == "failed" and "collect/output.json" in st["error"]
    assert orch.store.claim_run("j", ["report"]) == run + 1        # not stuck queued


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


def start(client, coll, phases=None, **body) -> dict:
    payload = {"collector_output": coll, "price": False, **body}
    if phases is not None:
        payload["phases"] = phases
    r = client.post(f"{PREFIX}/assessments/from-collection", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def status(client, job) -> dict:
    return client.get(f"{PREFIX}/assessments/{job}/status").json()


def run(client, job, phase, **body):
    return client.post(f"{PREFIX}/assessments/{job}/{phase}", json=body or None)


def test_a_job_is_built_over_the_api_one_call_at_a_time(client, coll):
    job = start(client, coll, ["analyze"])["job_id"]
    s = status(client, job)
    assert s["status"] == "complete" and s["requested"] == ["analyze"] and s["run"] == 1
    assert s["available"] == ["collect", "analyze"]
    assert client.get(f"{PREFIX}/assessments/{job}/analysis").status_code == 200
    for missing in ("sizing", "report"):
        r = client.get(f"{PREFIX}/assessments/{job}/{missing}")
        assert r.status_code == 404 and r.json()["code"] == "artifact_not_available", missing

    r = run(client, job, "size", price=False)
    assert r.status_code == 200
    assert r.json() == {"job_id": job, "status": "complete", "phase": "size", "run": 2,
                        "replaces": []}
    assert status(client, job)["available"] == ["collect", "analyze", "size"]
    assert client.get(f"{PREFIX}/assessments/{job}/sizing").status_code == 200

    r = run(client, job, "report")
    assert r.json()["phase"] == "report" and r.json()["run"] == 3
    s = status(client, job)
    assert s["available"] == ALL and s["status"] == "complete" and s["current_phase"] == "report"
    assert [(p["phase"], p["run"]) for p in s["phases"]] == [
        ("analyze", 1), ("size", 2), ("report", 3)]
    assert client.get(f"{PREFIX}/assessments/{job}/report").status_code == 200
    assert client.get(f"{PREFIX}/assessments/{job}/report-data").status_code == 200
    assert client.get(f"{PREFIX}/assessments/{job}/summary").json()["sizing"] is not None


def test_the_result_equals_a_whole_run(client, coll):
    whole = start(client, coll)["job_id"]
    parts = start(client, coll, ["analyze"])["job_id"]
    run(client, parts, "size", price=False)
    run(client, parts, "report")

    def view(job):
        sizing = client.get(f"{PREFIX}/assessments/{job}/sizing").json()
        sizing.pop("generated_at")
        tables = client.get(f"{PREFIX}/assessments/{job}/report-data/tables").json()["items"]
        return sizing, [(t["table_id"], t["verdict"]) for t in tables]

    assert view(parts) == view(whole)


def test_a_new_run_says_what_it_replaces_and_does_it(client, coll):
    job = start(client, coll)["job_id"]
    assert status(client, job)["available"] == ALL
    r = run(client, job, "analyze")
    assert r.json()["replaces"] == ["analyze", "size", "report"] and r.json()["run"] == 2
    assert status(client, job)["available"] == ["collect", "analyze"]
    for gone in ("sizing", "report"):
        assert client.get(f"{PREFIX}/assessments/{job}/{gone}").status_code == 404
    assert run(client, job, "size", price=False).json()["replaces"] == []
    assert run(client, job, "size", price=False).json()["replaces"] == ["size"]    # again
    assert run(client, job, "report").json()["replaces"] == []


def test_a_live_job_stops_after_collect_and_carries_on(client, live_collector):
    r = client.post(f"{PREFIX}/assessments", params={"phases": "collect", "label": "stage"},
                    json=live_payload("live-1"))
    assert r.status_code == 200 and r.json()["status"] == "queued"
    s = status(client, "live-1")
    assert s["status"] == "complete" and s["requested"] == ["collect"]
    assert s["available"] == ["collect"]
    item = client.get(f"{PREFIX}/assessments").json()["items"][0]
    assert item["available"] == ["collect"] and item["status"] == "complete"
    assert client.get(f"{PREFIX}/assessments/live-1/analysis").status_code == 404

    assert run(client, "live-1", "analyze").json()["status"] == "complete"
    assert run(client, "live-1", "size", price=False).status_code == 200
    assert run(client, "live-1", "report").status_code == 200
    assert status(client, "live-1")["available"] == ALL
    assert len(live_collector) == 1


def test_collect_on_an_existing_job_replaces_everything(client, coll, live_collector):
    job = start(client, coll)["job_id"]
    r = client.post(f"{PREFIX}/assessments/{job}/collect", json=live_payload("ignored"))
    assert r.status_code == 200
    assert r.json()["status"] == "queued" and r.json()["replaces"] == ALL
    assert live_collector[0]["job_id"] == job            # the path decides the job, not the body
    s = status(client, job)
    assert s["status"] == "complete" and s["available"] == ["collect"]
    listing = client.get(f"{PREFIX}/assessments").text
    assert not any(v in listing for v in SSH_VALUES)


def test_collect_checks_the_target_like_a_submit(client, coll, tmp_path):
    job = start(client, coll)["job_id"]
    bad = live_payload()
    bad["cluster_endpoint"] = "db; id"
    r = client.post(f"{PREFIX}/assessments/{job}/collect", json=bad)
    assert r.status_code == 422 and r.json()["code"] == "invalid_target"
    assert status(client, job)["available"] == ALL and status(client, job)["run"] == 1


@pytest.mark.parametrize("phase", ["collect", "analyze", "size", "report"])
def test_an_unknown_job_is_404(client, phase):
    body = live_payload() if phase == "collect" else {}
    r = client.post(f"{PREFIX}/assessments/nope-1/{phase}", json=body)
    assert r.status_code == 404 and r.json()["code"] == "job_not_found"


def test_a_phase_with_no_input_says_which_to_run_first(client, coll):
    import shutil
    job = start(client, coll, ["analyze"])["job_id"]
    shutil.rmtree(Path(common.ARTIFACT_ROOT) / job / "analyze")
    for phase in ("size", "report"):
        r = run(client, job, phase)
        assert r.status_code == 404 and r.json()["code"] == "artifact_not_available"
        assert "analyze/analysis.json" in r.json()["detail"] and "Run analyze first" in r.json()["detail"]
    assert status(client, job)["run"] == 1        # not claimed by the refused calls


def test_a_busy_job_is_a_409(client, coll, tmp_path):
    job = start(client, coll)["job_id"]
    LocalJobStore(tmp_path).claim_run(job, ["analyze"])            # now queued
    for phase in ("analyze", "size", "report"):
        r = run(client, job, phase)
        assert r.status_code == 409 and r.json()["code"] == "job_busy"
    r = client.post(f"{PREFIX}/assessments/{job}/collect", json=live_payload())
    assert r.status_code == 409 and r.json()["code"] == "job_busy"


def test_a_run_that_fails_in_the_request_is_a_400_and_the_job_says_so(client, coll, tmp_path):
    job = start(client, coll)["job_id"]
    (tmp_path / job / "analyze" / "analysis.json").write_text("{ not json")
    r = run(client, job, "size", price=False)
    assert r.status_code == 400 and r.json()["code"] == "phase_failed"
    s = status(client, job)
    assert s["status"] == "failed" and s["error"] and s["available"] == ["collect", "analyze"]


@pytest.mark.parametrize("path,body", [
    ("analyze", {"use_llm": "maybe"}), ("analyze", {"surprise": 1}),
    ("size", {"region": "US-EAST-1"}), ("size", {"region": "us-east-1; id"}),
    ("size", {"price": 7}), ("size", {"other": True}),
])
def test_bad_options_are_422_and_claim_nothing(client, coll, path, body):
    job = start(client, coll)["job_id"]
    r = client.post(f"{PREFIX}/assessments/{job}/{path}", json=body)
    assert r.status_code == 422
    assert status(client, job)["run"] == 1 and status(client, job)["available"] == ALL


@pytest.fixture
def held(monkeypatch) -> list:
    """Records background phase runs instead of running them, so the job stays queued."""
    calls: list = []
    monkeypatch.setattr(phase_routes, "_run_phase_job", lambda *a: calls.append(a))
    return calls


def test_a_phase_that_prices_is_queued_and_can_be_cancelled(client, coll, held):
    job = start(client, coll)["job_id"]
    r = run(client, job, "size", price=True)
    assert r.json()["status"] == "queued" and r.json()["replaces"] == ["size", "report"]
    s = status(client, job)
    assert s["status"] == "queued" and s["requested"] == ["size"] and s["run"] == 2
    assert s["available"] == ALL                      # nothing is deleted until it starts
    assert len(held) == 1 and held[0][:2] == (job, Phase.SIZE)

    assert client.post(f"{PREFIX}/assessments/{job}/cancel").json()["status"] == "cancelled"
    assert status(client, job)["available"] == ALL
    assert run(client, job, "report").status_code == 200       # a cancelled job can be run again


def test_the_model_makes_analyze_a_queued_run(client, coll, held):
    job = start(client, coll)["job_id"]
    r = run(client, job, "analyze", use_llm=True)
    assert r.json()["status"] == "queued" and len(held) == 1
    assert held[0][3].use_llm is True               # the options the run will use


def test_collect_is_always_queued(client, coll, held):
    job = start(client, coll)["job_id"]
    r = client.post(f"{PREFIX}/assessments/{job}/collect", json=live_payload())
    assert r.json()["status"] == "queued" and len(held) == 1


def test_phase_runs_count_against_the_job_limits(client, coll, held, monkeypatch, tmp_path):
    job = start(client, coll)["job_id"]
    monkeypatch.setattr(limits, "MAX_RUNNING_JOBS", 1)
    monkeypatch.setattr(limits, "MAX_QUEUED_JOBS", 0)
    LocalJobStore(tmp_path).create(JobRecord(job_id="other", created_at="2026-10-14T10:00:00+00:00"))

    r = run(client, job, "size", price=True)            # would wait, and the server is full
    assert r.status_code == 429 and r.json()["code"] == "queue_full"
    assert status(client, job)["run"] == 1 and held == []        # not claimed

    assert run(client, job, "report").status_code == 200         # runs in its request: allowed
    assert run(client, job, "size", price=False).status_code == 200


def test_a_phase_run_is_audited_without_secrets(client, coll, held):
    job = start(client, coll)["job_id"]
    client.post(f"{PREFIX}/assessments/{job}/collect", json=live_payload())
    rows = [json.loads(line) for line in Path(os.environ["AUDIT_LOG_PATH"]).read_text().splitlines()]
    runs = [r for r in rows if r["event"] == "phase_run"]
    assert [(r["phase"], r["run"]) for r in runs] == [("collect", 2)]
    assert runs[0]["job_id"] == job and runs[0]["replaces"] == ALL and runs[0]["peer"] == "127.0.0.1"
    text = Path(os.environ["AUDIT_LOG_PATH"]).read_text()
    assert not any(v in text for v in SSH_VALUES)


def test_the_submit_routes_check_their_phases(client, coll):
    live = live_payload()
    for phases, code in ((["analyze"], "invalid_phases"), (["collect", "size"], "invalid_phases"),
                         (["collect", "collect"], "invalid_phases")):
        r = client.post(f"{PREFIX}/assessments", params={"phases": phases}, json=live)
        assert r.status_code == 422 and r.json()["code"] == code, phases
    r = client.post(f"{PREFIX}/assessments", params={"phases": "nonsense"}, json=live)
    assert r.status_code == 422
    for phases in ([], ["collect"], ["size"], ["report"], ["size", "report"], ["analyze", "analyze"]):
        r = client.post(f"{PREFIX}/assessments/from-collection",
                        json={"collector_output": coll, "price": False, "phases": phases})
        assert r.status_code == 422, phases
    assert client.get(f"{PREFIX}/assessments").json()["total"] == 0        # no job was made


def test_explicit_phases_decide_whether_sizing_runs(client, coll):
    job = start(client, coll, ["analyze", "report"], size=True)["job_id"]
    s = status(client, job)
    assert s["available"] == ["collect", "analyze", "report"]            # `size` was ignored
    job2 = start(client, coll, ["analyze", "size"], size=False)["job_id"]
    assert status(client, job2)["available"] == ["collect", "analyze", "size"]
    item = {i["job_id"]: i for i in client.get(f"{PREFIX}/assessments").json()["items"]}
    assert item[job]["options"]["size"] is False and item[job2]["options"]["size"] is True


def test_the_list_and_status_say_what_each_job_holds(client, coll):
    a = start(client, coll, ["analyze"])["job_id"]
    b = start(client, coll)["job_id"]
    items = {i["job_id"]: i for i in client.get(f"{PREFIX}/assessments").json()["items"]}
    assert items[a]["available"] == ["collect", "analyze"] and items[b]["available"] == ALL
    assert status(client, a)["available"] == ["collect", "analyze"]


def test_capabilities_describe_each_phase(client):
    caps = client.get(f"{PREFIX}/capabilities").json()
    by = {p["phase"]: p for p in caps["phases"]}
    assert by["collect"]["needs"] == [] and by["collect"]["makes"] == "collect/output.json"
    assert by["analyze"]["needs"] == ["collect"] and by["analyze"]["makes"] == "analyze/analysis.json"
    assert by["size"]["needs"] == ["collect", "analyze"] and by["size"]["makes"] == "size/sizing.json"
    assert by["report"]["needs"] == ["collect", "analyze"] and by["report"]["makes"] == "report/report.html"
    assert caps["features"]["component_runs"] is True


def test_the_routes_are_documented_and_behind_the_gate(tmp_path, monkeypatch):
    paths = app.openapi()["paths"]
    for phase, op in (("collect", "run_collect_phase"), ("analyze", "run_analyze_phase"),
                      ("size", "run_size_phase"), ("report", "run_report_phase")):
        post = paths[f"{PREFIX}/assessments/{{job_id}}/{phase}"]["post"]
        assert post["operationId"] == op
        assert post["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
            "/PhaseRunResponse")
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.7", 50000))
    assert remote.post(f"{PREFIX}/assessments/x/report").status_code == 403
