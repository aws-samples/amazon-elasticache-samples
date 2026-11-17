"""Retention and purge. Spec: ux-api UX-35, UX-36.

Deleting data on its own is opt-in. A purge is a dry run unless asked otherwise. A queued
or running job is never deleted.
"""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import common, retention
from src.api.main import app, lifespan
from src.contracts.api_models import JobRecord
from src.orchestrator.job_store import LocalJobStore

FIXTURES = Path(__file__).parent.parent / "fixtures"
PREFIX = "/api/v1"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def collection() -> dict:
    return json.loads((FIXTURES / "strong_candidate_collection.json").read_text())


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.delenv("AUDIT_LOG_PATH", raising=False)
    monkeypatch.delenv("JOB_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("RETENTION_SWEEP_SECONDS", raising=False)
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def store(tmp_path, monkeypatch) -> LocalJobStore:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.delenv("AUDIT_LOG_PATH", raising=False)
    monkeypatch.delenv("JOB_RETENTION_DAYS", raising=False)
    return LocalJobStore(tmp_path)


def make(store: LocalJobStore, job_id: str, status: str, days_ago: float | None,
         field: str = "completed_at", when: str | None = None) -> str:
    """A job in a given state that finished `days_ago` days before NOW."""
    store.create(JobRecord(job_id=job_id, source="collection", created_at=NOW.isoformat()))
    st = store.read_state(job_id)
    st["status"] = status
    if when is not None:
        st[field] = when
    elif days_ago is not None:
        st[field] = (NOW - timedelta(days=days_ago)).isoformat()
    store.write_state(job_id, st)
    return job_id


def audit_events(tmp_path, name) -> list[dict]:
    path = tmp_path / "audit.log"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if r["event"] == name]


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def test_only_finished_jobs_older_than_the_cutoff_are_listed(store):
    make(store, "old-complete", "complete", 40)
    make(store, "old-failed", "failed", 40)
    make(store, "old-cancelled", "cancelled", 40)
    make(store, "young", "complete", 5)
    make(store, "old-queued", "queued", 400)
    make(store, "old-running", "running", 400)
    assert store.expired(NOW - timedelta(days=30)) == ["old-cancelled", "old-complete", "old-failed"]


def test_a_status_can_be_asked_for(store):
    make(store, "a", "complete", 40)
    make(store, "b", "failed", 40)
    assert store.expired(NOW - timedelta(days=30), "failed") == ["b"]
    assert store.expired(NOW - timedelta(days=30), "cancelled") == []


def test_the_cutoff_itself_is_kept(store):
    make(store, "edge", "complete", 30)
    assert store.expired(NOW - timedelta(days=30)) == []
    assert store.expired(NOW - timedelta(days=30) + timedelta(seconds=1)) == ["edge"]


def test_age_falls_back_to_the_last_update_then_the_creation_time(store):
    make(store, "updated", "failed", None)
    st = store.read_state("updated")
    st["updated_at"] = (NOW - timedelta(days=50)).isoformat()
    store.write_state("updated", st)
    make(store, "created", "failed", None)  # only created_at, which is NOW
    assert store.expired(NOW - timedelta(days=30)) == ["updated"]


def test_a_job_whose_age_is_unknown_is_kept(store):
    make(store, "garbled", "complete", None, when="yesterday-ish")
    make(store, "none", "complete", None)
    st = store.read_state("none")
    st.pop("completed_at", None)
    st["updated_at"] = st["created_at"] = None
    store.write_state("none", st)
    assert store.expired(NOW + timedelta(days=3650)) == []


def test_a_time_without_a_zone_is_taken_as_utc(store):
    make(store, "naive", "complete", None, when="2026-08-01T00:00:00")
    assert store.expired(NOW - timedelta(days=30)) == ["naive"]


# ---------------------------------------------------------------------------
# purge()
# ---------------------------------------------------------------------------


def test_a_dry_run_lists_and_deletes_nothing(store, tmp_path):
    make(store, "old", "complete", 40)
    assert retention.purge(store, 30, dry_run=True, now=NOW) == ["old"]
    assert (tmp_path / "old").is_dir()


def test_a_real_purge_deletes_the_job_and_its_artifacts(store, tmp_path):
    make(store, "old", "complete", 40)
    (tmp_path / "old" / "report").mkdir()
    (tmp_path / "old" / "report" / "report.html").write_text("x")
    make(store, "young", "complete", 3)
    assert retention.purge(store, 30, now=NOW) == ["old"]
    assert not (tmp_path / "old").exists() and (tmp_path / "young").is_dir()


def test_a_job_that_started_again_meanwhile_is_skipped(store, tmp_path, monkeypatch):
    make(store, "a", "complete", 40)
    make(store, "b", "complete", 40)
    real = store.delete
    monkeypatch.setattr(store, "delete", lambda j: "busy" if j == "a" else real(j))
    assert retention.purge(store, 30, now=NOW) == ["b"]
    assert (tmp_path / "a").is_dir()


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def aged(client, store, collection, days: int, status: str | None = None) -> str:
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "price": False})
    job = r.json()["job_id"]
    st = store.read_state(job)
    st["completed_at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    if status:
        st["status"] = status
    store.write_state(job, st)
    return job


def test_the_default_is_a_dry_run(client, store, collection):
    old = aged(client, store, collection, 40)
    r = client.post(f"{PREFIX}/assessments/purge", json={"older_than_days": 30})
    assert r.status_code == 200
    assert r.json() == {"dry_run": True, "older_than_days": 30, "status": None,
                        "count": 1, "job_ids": [old]}
    assert client.get(f"{PREFIX}/assessments/{old}/status").status_code == 200  # still there


def test_dry_run_false_deletes_old_finished_jobs_only(client, store, collection):
    old = aged(client, store, collection, 40)
    young = aged(client, store, collection, 3)
    running = aged(client, store, collection, 400, status="running")
    queued = aged(client, store, collection, 400, status="queued")

    r = client.post(f"{PREFIX}/assessments/purge",
                    json={"older_than_days": 30, "dry_run": False})
    assert r.json()["job_ids"] == [old] and r.json()["dry_run"] is False
    assert client.get(f"{PREFIX}/assessments/{old}/status").status_code == 404
    for kept in (young, running, queued):
        assert client.get(f"{PREFIX}/assessments/{kept}/status").status_code == 200


def test_a_status_narrows_it(client, store, collection):
    done = aged(client, store, collection, 40)
    failed = aged(client, store, collection, 40, status="failed")
    r = client.post(f"{PREFIX}/assessments/purge",
                    json={"older_than_days": 30, "status": "failed", "dry_run": False})
    assert r.json()["job_ids"] == [failed] and r.json()["status"] == "failed"
    assert client.get(f"{PREFIX}/assessments/{done}/status").status_code == 200


def test_nothing_to_purge_is_an_empty_answer(client):
    r = client.post(f"{PREFIX}/assessments/purge", json={"older_than_days": 1, "dry_run": False})
    assert r.status_code == 200 and r.json()["count"] == 0 and r.json()["job_ids"] == []


@pytest.mark.parametrize("body", [
    {}, {"older_than_days": 0}, {"older_than_days": -5}, {"older_than_days": 3651},
    {"older_than_days": "30"}, {"older_than_days": 1.5}, {"older_than_days": 30, "status": "running"},
    {"older_than_days": 30, "status": "queued"}, {"older_than_days": 30, "dry_run": "later"},
    {"older_than_days": 30, "everything": True}, {"older_than_days": 30.0},
    {"older_than_days": 30, "dry_run": "false"}, {"older_than_days": 30, "dry_run": "no"},
    {"older_than_days": 30, "dry_run": 0}, {"older_than_days": 30, "dry_run": "0"},
])
def test_a_bad_request_deletes_nothing(client, store, collection, body):
    old = aged(client, store, collection, 400)
    r = client.post(f"{PREFIX}/assessments/purge", json=body)
    assert r.status_code == 422 and r.json()["code"] == "invalid_request"
    assert client.get(f"{PREFIX}/assessments/{old}/status").status_code == 200


def test_a_purge_is_audited_dry_or_not(client, store, collection, tmp_path):
    old = aged(client, store, collection, 40)
    client.post(f"{PREFIX}/assessments/purge", json={"older_than_days": 30})
    client.post(f"{PREFIX}/assessments/purge", json={"older_than_days": 30, "dry_run": False})
    first, second = audit_events(tmp_path, "jobs_purged")
    assert (first["dry_run"], second["dry_run"]) == (True, False)
    assert first["job_ids"] == second["job_ids"] == [old] and second["count"] == 1
    assert second["older_than_days"] == 30 and second["peer"] == "127.0.0.1"


def test_the_route_is_documented_and_behind_the_gate(tmp_path, monkeypatch):
    op = app.openapi()["paths"][f"{PREFIX}/assessments/purge"]["post"]
    assert op["operationId"] == "purge_assessments"
    assert {"413", "403", "422", "500"} <= set(op["responses"])
    body = app.openapi()["components"]["schemas"]["PurgeRequest"]
    assert body["properties"]["dry_run"]["default"] is True
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.7", 50000))
    assert remote.post(f"{PREFIX}/assessments/purge", json={"older_than_days": 1}).status_code == 403


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_retention_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("JOB_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("RETENTION_SWEEP_SECONDS", raising=False)
    assert retention.settings() == (None, 3600)
    monkeypatch.setenv("JOB_RETENTION_DAYS", "0")
    assert retention.settings()[0] is None
    monkeypatch.setenv("JOB_RETENTION_DAYS", "30")
    monkeypatch.setenv("RETENTION_SWEEP_SECONDS", "120")
    assert retention.settings() == (30, 120)


@pytest.mark.parametrize("name,value", [
    ("JOB_RETENTION_DAYS", "-1"), ("JOB_RETENTION_DAYS", "a month"),
    ("RETENTION_SWEEP_SECONDS", "30"), ("RETENTION_SWEEP_SECONDS", "0"),
])
def test_a_bad_setting_stops_the_server_and_names_the_variable(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match=name):
        retention.settings()


def test_a_sweep_with_retention_off_deletes_nothing(store, tmp_path):
    make(store, "ancient", "complete", 4000)
    assert retention.sweep() == []
    assert (tmp_path / "ancient").is_dir()
    assert audit_events(tmp_path, "retention_sweep") == []


def test_a_sweep_deletes_old_finished_jobs_and_nothing_else(store, tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RETENTION_DAYS", "30")
    now = datetime.now(UTC)
    for name, status, days in (("old", "complete", 40), ("old-failed", "failed", 90),
                               ("young", "complete", 5), ("busy", "running", 400),
                               ("waiting", "queued", 400)):
        make(store, name, status, None)
        st = store.read_state(name)
        st["completed_at"] = (now - timedelta(days=days)).isoformat()
        store.write_state(name, st)

    assert retention.sweep() == ["old", "old-failed"]
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_dir()) == ["busy", "waiting", "young"]
    (e,) = audit_events(tmp_path, "retention_sweep")
    assert e["count"] == 2 and e["job_ids"] == ["old", "old-failed"] and e["older_than_days"] == 30


def test_a_sweep_that_finds_nothing_writes_no_line(store, tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RETENTION_DAYS", "30")
    # sweep() uses the real clock, so the job is a day old by that clock, not by NOW.
    make(store, "young", "complete", None, when=(datetime.now(UTC) - timedelta(days=1)).isoformat())
    assert retention.sweep() == []
    assert audit_events(tmp_path, "retention_sweep") == []


def test_a_failing_sweep_is_logged_and_does_not_raise(store, monkeypatch, caplog):
    monkeypatch.setenv("JOB_RETENTION_DAYS", "30")
    monkeypatch.setattr(retention, "purge", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert retention.sweep() == []
    assert "Retention sweep failed" in caplog.text


def test_start_up_sweeps_once_and_keeps_sweeping_until_shutdown(store, tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RETENTION_DAYS", "30")
    make(store, "old", "complete", None)
    st = store.read_state("old")
    st["completed_at"] = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    store.write_state("old", st)

    async def run() -> int:
        before = set(asyncio.all_tasks())
        async with lifespan(app):
            assert not (tmp_path / "old").exists()  # the start-up sweep has run
            assert len(asyncio.all_tasks() - before) == 1  # and the timer is running
        return len([t for t in asyncio.all_tasks() - before if not t.done()])

    assert asyncio.run(run()) == 0  # the timer was stopped with the server


def test_the_timer_sweeps_again_and_again(monkeypatch):
    calls = []
    real_sleep = asyncio.sleep

    async def quick(_seconds):
        await real_sleep(0.01)

    monkeypatch.setattr(retention, "sweep", lambda: calls.append(1) or [])
    monkeypatch.setattr(retention.asyncio, "sleep", quick)

    async def run():
        task = asyncio.create_task(retention.run_forever(60))
        await real_sleep(0.3)
        task.cancel()

    asyncio.run(run())
    assert len(calls) >= 3


def test_no_timer_when_retention_is_off(store, monkeypatch):
    monkeypatch.delenv("JOB_RETENTION_DAYS", raising=False)
    make(store, "ancient", "complete", 4000)

    async def run() -> int:
        before = set(asyncio.all_tasks())
        async with lifespan(app):
            return len(asyncio.all_tasks() - before)

    assert asyncio.run(run()) == 0
    assert (store.root / "ancient").is_dir()


def test_capabilities_says_whether_retention_is_on(client, monkeypatch):
    assert client.get(f"{PREFIX}/capabilities").json()["limits"]["job_retention_days"] is None
    monkeypatch.setenv("JOB_RETENTION_DAYS", "45")
    assert client.get(f"{PREFIX}/capabilities").json()["limits"]["job_retention_days"] == 45
