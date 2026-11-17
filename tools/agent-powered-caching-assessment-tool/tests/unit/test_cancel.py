"""Cancel. Spec: ux-api UX-17 to UX-20.

A queued job is cancelled on the spot. A running job is asked to stop and does so
between phases. A phase that has started always runs to its end.
"""
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import common
from src.api.main import app
from src.api.routes import assessments
from src.contracts.api_models import JobRecord
from src.orchestrator.job_store import CANCEL_MARKER, LocalJobStore
from src.orchestrator.local_orchestrator import JobCancelled, LocalOrchestrator

FIXTURES = Path(__file__).parent.parent / "fixtures"
PREFIX = "/api/v1"
CREATED = "2026-10-14T10:00:00+00:00"


@pytest.fixture
def collection() -> dict:
    return json.loads((FIXTURES / "strong_candidate_collection.json").read_text())


@pytest.fixture
def store(tmp_path) -> LocalJobStore:
    return LocalJobStore(tmp_path)


@pytest.fixture
def orch(tmp_path) -> LocalOrchestrator:
    return LocalOrchestrator(str(tmp_path), price=False)


def _queued(store: LocalJobStore, job_id: str = "job-q") -> str:
    store.create(JobRecord(job_id=job_id, source="collection", created_at=CREATED))
    return job_id


def _running(store: LocalJobStore, job_id: str = "job-r") -> str:
    _queued(store, job_id)
    st = store.read_state(job_id)
    st.update(status="running", current_phase="analyze", started_at=CREATED,
              phases=[{"phase": "analyze", "status": "running", "started_at": CREATED,
                       "ended_at": None, "error": None}])
    store.write_state(job_id, st)
    return job_id


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def test_a_queued_job_is_cancelled_on_the_spot(store, tmp_path):
    job = _queued(store)
    assert store.request_cancel(job) == "cancelled"
    st = store.read_state(job)
    assert st["status"] == "cancelled" and st["error"] is None
    assert st["note"] == "Cancelled before it started."
    assert st["completed_at"] and st["duration_seconds"] is None  # it never started
    assert store.cancel_requested(job) and (tmp_path / job / CANCEL_MARKER).is_file()


def test_a_running_job_is_only_asked(store):
    job = _running(store)
    assert store.request_cancel(job) == "requested"
    st = store.read_state(job)
    assert st["status"] == "running"  # it stops itself, at the next phase boundary
    assert store.cancel_requested(job)


def test_cancelling_twice_changes_nothing(store):
    job = _queued(store)
    store.request_cancel(job)
    first = store.read_state(job)
    assert store.request_cancel(job) == "already_cancelled"
    assert store.read_state(job) == first


@pytest.mark.parametrize("final", ["complete", "failed"])
def test_a_finished_job_cannot_be_cancelled(store, final):
    job = _queued(store)
    st = store.read_state(job)
    st["status"] = final
    store.write_state(job, st)
    assert store.request_cancel(job) == "finished"
    assert not store.cancel_requested(job)


@pytest.mark.parametrize("job_id", ["nope", "..", ".hidden", "a..b", "x" * 65])
def test_an_unknown_or_bad_job_is_not_found(store, tmp_path, job_id):
    assert store.request_cancel(job_id) == "not_found"
    assert store.cancel_requested(job_id) is False
    assert list(tmp_path.iterdir()) == []


def test_clear_cancel_forgets_the_request(store):
    job = _running(store)
    store.request_cancel(job)
    store.clear_cancel(job)
    assert not store.cancel_requested(job)
    store.clear_cancel("..")  # a bad id is ignored
    store.clear_cancel("nope")


def test_a_cancelled_job_is_listed_and_can_be_deleted(store):
    job = _queued(store)
    store.request_cancel(job)
    total, items = store.list(status="cancelled")
    assert total == 1 and items[0].job_id == job and items[0].status == "cancelled"
    assert items[0].note == "Cancelled before it started." and items[0].error is None
    assert store.delete(job) == "deleted"


def test_a_running_job_cannot_be_deleted_even_after_a_cancel_request(store):
    job = _running(store)
    store.request_cancel(job)
    assert store.delete(job) == "busy"


def test_the_sweep_fails_a_running_job_that_was_asked_to_cancel(store):
    job = _running(store)
    store.request_cancel(job)
    assert store.interrupt_unfinished() == [job]
    assert store.read_state(job)["status"] == "failed"


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


def test_a_job_cancelled_while_it_analyses_stops_before_sizing(orch, collection, tmp_path, monkeypatch):
    real = orch._run_analyze

    def analyse_then_cancel(job_id, co):
        out = real(job_id, co)
        orch.store.request_cancel(job_id)  # arrives while ANALYZE is running
        return out

    monkeypatch.setattr(orch, "_run_analyze", analyse_then_cancel)
    with pytest.raises(JobCancelled):
        orch.run_from_collection("job-c", collection)

    st = orch.get_state("job-c")
    assert st["status"] == "cancelled" and st["error"] is None
    assert st["note"] == "Cancelled before the size phase."
    assert [(p["phase"], p["status"]) for p in st["phases"]] == [("analyze", "complete")]
    assert st["completed_at"] and st["duration_seconds"] >= 0
    # The phase that had started ran to its end and its artifact is kept.
    job = tmp_path / "job-c"
    assert (job / "analyze" / "analysis.json").is_file()
    assert not (job / "size").exists() and not (job / "report").exists()


def test_a_job_cancelled_while_it_sizes_stops_before_the_report(orch, collection, tmp_path, monkeypatch):
    real = orch._run_size

    def size_then_cancel(job_id, co, analysis):
        out = real(job_id, co, analysis)
        orch.store.request_cancel(job_id)
        return out

    monkeypatch.setattr(orch, "_run_size", size_then_cancel)
    with pytest.raises(JobCancelled):
        orch.run_from_collection("job-s", collection)

    st = orch.get_state("job-s")
    assert st["status"] == "cancelled" and st["note"] == "Cancelled before the report phase."
    assert [p["phase"] for p in st["phases"]] == ["analyze", "size"]
    assert (tmp_path / "job-s" / "size" / "sizing.json").is_file()
    assert not (tmp_path / "job-s" / "report").exists()


def test_a_cancel_during_the_last_phase_has_no_effect(orch, collection, monkeypatch):
    real = orch._run_report

    def report_then_cancel(job_id, co, analysis):
        out = real(job_id, co, analysis)
        orch.store.request_cancel(job_id)
        return out

    monkeypatch.setattr(orch, "_run_report", report_then_cancel)
    orch.run_from_collection("job-l", collection)  # does not raise
    assert orch.get_state("job-l")["status"] == "complete"


def test_a_cancel_with_sizing_off_stops_before_the_report(tmp_path, collection, monkeypatch):
    orch = LocalOrchestrator(str(tmp_path), price=False, size=False)
    real = orch._run_analyze

    def analyse_then_cancel(job_id, co):
        out = real(job_id, co)
        orch.store.request_cancel(job_id)
        return out

    monkeypatch.setattr(orch, "_run_analyze", analyse_then_cancel)
    with pytest.raises(JobCancelled):
        orch.run_from_collection("job-n", collection)
    assert orch.get_state("job-n")["note"] == "Cancelled before the report phase."


def test_a_job_cancelled_in_the_queue_never_runs(orch, store, collection, tmp_path):
    job = _queued(store, "job-never")
    store.request_cancel(job)
    with pytest.raises(JobCancelled):
        orch.run_from_collection(job, collection)
    st = store.read_state(job)
    assert st["status"] == "cancelled" and st["phases"] == []
    # Nothing was collected, analysed or written.
    assert sorted(p.name for p in (tmp_path / job).iterdir()) == [
        CANCEL_MARKER, "job.json", "state.json"]


def test_a_request_that_beat_the_state_write_also_stops_a_job_at_start(orch, store, collection):
    """The marker is written first; a task that starts before the state flips still stops."""
    job = _queued(store, "job-race")
    (store.root / job / CANCEL_MARKER).write_text(CREATED)  # marker, state still queued
    with pytest.raises(JobCancelled):
        orch.run_from_collection(job, collection)
    st = store.read_state(job)
    assert st["status"] == "cancelled" and st["note"] == "Cancelled before it started."


def test_a_live_job_cancelled_in_the_queue_does_not_collect(orch, store):
    job = _queued(store, "job-live")
    store.request_cancel(job)
    with pytest.raises(JobCancelled):
        orch.run_job(job, {"cluster_endpoint": "db.example.internal"})
    assert not (store.root / job / "collect").exists()


def test_a_cancelled_job_id_is_not_reused_by_a_direct_call(orch, store, collection):
    job = _queued(store, "job-reuse")
    store.request_cancel(job)
    with pytest.raises(JobCancelled):
        orch.run_from_collection(job, collection)
    with pytest.raises(JobCancelled):  # delete the job first to reuse the id
        orch.run_from_collection(job, collection)
    assert store.delete(job) == "deleted"
    assert orch.run_from_collection(job, collection)["contract_version"]


def test_a_stale_request_on_a_finished_job_does_not_cancel_the_next_run(orch, collection):
    orch.run_from_collection("job-stale", collection)
    # A cancel that lost the race to the end of the run left its marker behind.
    (orch.store.root / "job-stale" / CANCEL_MARKER).write_text(CREATED)
    orch.run_from_collection("job-stale", collection)
    st = orch.get_state("job-stale")
    assert st["status"] == "complete" and not orch.store.cancel_requested("job-stale")


def test_a_failure_is_still_a_failure_not_a_cancel(orch, collection):
    with pytest.raises(Exception) as err:
        orch.run_from_collection("job-bad", {"queries": "nope"})
    assert not isinstance(err.value, JobCancelled)
    assert orch.get_state("job-bad")["status"] == "failed"


def test_the_phases_in_a_cancelled_job_are_never_left_running(orch, collection, monkeypatch):
    real = orch._run_analyze
    monkeypatch.setattr(orch, "_run_analyze", lambda j, c: (
        real(j, c), orch.store.request_cancel(j))[0])
    with pytest.raises(JobCancelled):
        orch.run_from_collection("job-p", collection)
    assert all(p["status"] != "running" for p in orch.get_state("job-p")["phases"])


def test_the_background_wrappers_log_a_cancel_quietly(store, collection, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    job = _queued(store, "job-bg")
    store.request_cancel(job)
    with caplog.at_level(logging.INFO):
        assessments._run_from_collection(job, collection, False, True, False, "us-east-1")
        assessments._run_job(job, {"cluster_endpoint": "x"}, False, True, False, "us-east-1")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    api_lines = [r for r in caplog.records
                 if r.name == "src.api.routes.assessments" and "was cancelled" in r.getMessage()]
    assert len(api_lines) == 2


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


class _Idle:
    """An orchestrator that accepts a job and never starts it, so it stays queued."""

    def get_state(self, job_id):
        return LocalJobStore(common.ARTIFACT_ROOT).read_state(job_id)

    def run_from_collection(self, job_id, collector_output):
        pass


@pytest.fixture
def idle(monkeypatch):
    monkeypatch.setattr(assessments, "orchestrator", lambda *a, **k: _Idle())


def _submit(client, collection) -> str:
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection})
    assert r.json()["status"] == "queued"
    return r.json()["job_id"]


def test_cancelling_a_queued_job_over_the_api(client, collection, idle):
    job = _submit(client, collection)
    r = client.post(f"{PREFIX}/assessments/{job}/cancel")
    assert r.status_code == 200
    assert r.json() == {"job_id": job, "status": "cancelled", "cancel_requested": False,
                        "message": "The job was cancelled."}

    s = client.get(f"{PREFIX}/assessments/{job}/status").json()
    assert s["status"] == "cancelled" and s["error"] is None
    assert s["note"] == "Cancelled before it started." and s["cancel_requested"] is False
    assert client.get(f"{PREFIX}/assessments", params={"status": "cancelled"}).json()["total"] == 1
    assert client.get(f"{PREFIX}/assessments", params={"status": "failed"}).json()["total"] == 0
    assert client.delete(f"{PREFIX}/assessments/{job}").status_code == 204


def test_cancelling_a_cancelled_job_is_a_200_that_changes_nothing(client, collection, idle):
    job = _submit(client, collection)
    client.post(f"{PREFIX}/assessments/{job}/cancel")
    before = client.get(f"{PREFIX}/assessments/{job}/status").json()
    again = client.post(f"{PREFIX}/assessments/{job}/cancel")
    assert again.status_code == 200 and again.json()["status"] == "cancelled"
    assert "already" in again.json()["message"]
    assert client.get(f"{PREFIX}/assessments/{job}/status").json() == before


def test_cancelling_a_running_job_says_it_will_stop_at_the_next_phase(client, tmp_path):
    job = _running(LocalJobStore(tmp_path))
    r = client.post(f"{PREFIX}/assessments/{job}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running" and body["cancel_requested"] is True
    assert "current phase" in body["message"]

    s = client.get(f"{PREFIX}/assessments/{job}/status").json()
    assert s["status"] == "running" and s["cancel_requested"] is True
    # It cannot be deleted until it has stopped.
    d = client.delete(f"{PREFIX}/assessments/{job}")
    assert d.status_code == 409 and "Cancel it first" in d.json()["detail"]


def test_cancel_requested_is_false_when_nothing_was_asked(client, tmp_path):
    job = _running(LocalJobStore(tmp_path))
    assert client.get(f"{PREFIX}/assessments/{job}/status").json()["cancel_requested"] is False


def test_a_marker_left_on_a_finished_job_is_not_shown(client, collection, tmp_path):
    job = client.post(f"{PREFIX}/assessments/from-collection",
                      json={"collector_output": collection, "price": False}).json()["job_id"]
    (tmp_path / job / CANCEL_MARKER).write_text(CREATED)
    s = client.get(f"{PREFIX}/assessments/{job}/status").json()
    assert s["status"] == "complete" and s["cancel_requested"] is False


@pytest.mark.parametrize("final", ["complete", "failed"])
def test_a_finished_job_cannot_be_cancelled_over_the_api(client, tmp_path, final):
    store = LocalJobStore(tmp_path)
    job = _queued(store, "job-done")
    st = store.read_state(job)
    st["status"] = final
    store.write_state(job, st)
    r = client.post(f"{PREFIX}/assessments/{job}/cancel")
    assert r.status_code == 409 and "already finished" in r.json()["detail"]
    assert not store.cancel_requested(job)


def test_an_unknown_job_is_404_and_a_bad_id_is_refused_before_the_disk(client, tmp_path):
    assert client.post(f"{PREFIX}/assessments/nope-1/cancel").status_code == 404
    for bad in ("..", ".hidden", "a..b", "x" * 65):
        assert client.post(f"{PREFIX}/assessments/{bad}/cancel").status_code in (404, 405, 422)
    assert list(tmp_path.iterdir()) == []


def test_the_schemas_publish_the_cancelled_state():
    schemas = app.openapi()["components"]["schemas"]
    assert "cancelled" in schemas["JobStatus"]["properties"]["status"]["enum"]
    assert "cancelled" in schemas["JobSummary"]["properties"]["status"]["enum"]
    assert set(schemas["CancelResponse"]["properties"]) == {
        "job_id", "status", "cancel_requested", "message"}
    ids = {op["operationId"] for ops in app.openapi()["paths"].values() for op in ops.values()}
    assert "cancel_assessment" in ids


def test_cancel_is_behind_the_loopback_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.7", 50000))
    assert remote.post(f"{PREFIX}/assessments/x/cancel").status_code == 403

