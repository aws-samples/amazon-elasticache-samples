"""Limits on load. Spec: ux-api UX-32 to UX-34.

Request size, jobs running at once, jobs waiting. Nothing here starts a real job: the
background runner is replaced by a spy so jobs stay queued.
"""
import asyncio
import json
import threading
from pathlib import Path

import anyio.to_thread
import pytest
from fastapi.testclient import TestClient

from src.api import common, limits
from src.api.main import REQUEST_THREADS, app, lifespan
from src.api.routes import assessments
from src.contracts.api_models import JobRecord
from src.orchestrator.job_store import LocalJobStore

FIXTURES = Path(__file__).parent.parent / "fixtures"
PREFIX = "/api/v1"
LIVE_CONFIG = {"ssh_target": "ec2-user@db-host.example.internal", "db_user": "apcat_ro",
               "password_file": "/home/ec2-user/.apcat-db-password"}  # nosec B105 - test value, not a password


@pytest.fixture
def collection() -> dict:
    return json.loads((FIXTURES / "strong_candidate_collection.json").read_text())


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def started(monkeypatch) -> list:
    """Records the jobs that would start. They never run, so they stay queued."""
    calls: list = []
    monkeypatch.setattr(assessments, "_run_from_collection", lambda *a: calls.append(a))
    monkeypatch.setattr(assessments, "_run_job", lambda *a: calls.append(a))
    return calls


def limit(monkeypatch, running=1, queued=1, body=None):
    monkeypatch.setattr(limits, "MAX_RUNNING_JOBS", running)
    monkeypatch.setattr(limits, "MAX_QUEUED_JOBS", queued)
    monkeypatch.setattr(limits, "running_slots", threading.BoundedSemaphore(running))
    if body is not None:
        monkeypatch.setattr(limits, "MAX_REQUEST_BYTES", body)


def priced(client, collection, **kw):
    return client.post(f"{PREFIX}/assessments/from-collection",
                       json={"collector_output": collection, **kw})


def live(job_id="live-1") -> dict:
    return {"job_id": job_id, "cluster_endpoint": "db.example.internal",
            "database_name": "appdb", "mode": "live", "live_config": LIVE_CONFIG,
            "collection_options": {"window_minutes": 15}}


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------


def test_the_defaults():
    assert limits.env_int("APCAT_TEST_UNSET", 7) == 7
    assert limits.MAX_REQUEST_BYTES == 64 * 1024 * 1024
    assert (limits.MAX_RUNNING_JOBS, limits.MAX_QUEUED_JOBS) == (4, 20)


def test_a_value_from_the_environment(monkeypatch):
    monkeypatch.setenv("APCAT_TEST_LIMIT", " 12 ")
    assert limits.env_int("APCAT_TEST_LIMIT", 3) == 12
    monkeypatch.setenv("APCAT_TEST_LIMIT", "")
    assert limits.env_int("APCAT_TEST_LIMIT", 3) == 3
    monkeypatch.setenv("APCAT_TEST_LIMIT", "0")
    assert limits.env_int("APCAT_TEST_LIMIT", 3, minimum=0) == 0


@pytest.mark.parametrize("raw", ["abc", "1.5", "-1", "0", "4 jobs", "0x10"])
def test_a_bad_value_stops_the_server_and_names_the_variable(monkeypatch, raw):
    monkeypatch.setenv("MAX_RUNNING_JOBS", raw)
    with pytest.raises(SystemExit, match="MAX_RUNNING_JOBS"):
        limits.env_int("MAX_RUNNING_JOBS", 4)


def test_capabilities_publishes_the_three_limits(client):
    lim = client.get(f"{PREFIX}/capabilities").json()["limits"]
    assert lim["max_request_bytes"] == limits.MAX_REQUEST_BYTES
    assert lim["max_running_jobs"] == limits.MAX_RUNNING_JOBS
    assert lim["max_queued_jobs"] == limits.MAX_QUEUED_JOBS


# ---------------------------------------------------------------------------
# Request size
# ---------------------------------------------------------------------------


def test_a_body_over_the_limit_is_refused_before_anything_is_created(client, monkeypatch, tmp_path):
    limit(monkeypatch, body=2000)
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": {"pad": "x" * 3000}})
    assert r.status_code == 413
    assert r.json() == {"code": "payload_too_large",
                        "detail": "The request body is larger than the 2000 bytes this server accepts."}
    assert list(tmp_path.iterdir()) == []


def test_a_body_with_no_length_is_counted_as_it_arrives(client, monkeypatch, tmp_path):
    limit(monkeypatch, body=2000)

    def chunks():
        for _ in range(10):
            yield b"x" * 500

    r = client.post(f"{PREFIX}/assessments/from-collection", content=chunks(),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"
    assert list(tmp_path.iterdir()) == []


def test_a_length_that_understates_the_body_does_not_get_round_the_limit(client, monkeypatch):
    limit(monkeypatch, body=2000)
    r = client.post(f"{PREFIX}/assessments/from-collection", content=b"x" * 5000,
                    headers={"Content-Type": "application/json", "Content-Length": "10"})
    assert r.status_code == 413


def test_a_body_within_the_limit_goes_through(client, monkeypatch):
    limit(monkeypatch, body=2000)
    r = client.post(f"{PREFIX}/assessments/from-collection", content=b"x" * 2000,
                    headers={"Content-Type": "application/json"})
    assert r.status_code != 413  # it is not valid JSON, so the route says so instead
    assert client.get(f"{PREFIX}/capabilities").status_code == 200


def test_the_refusal_carries_the_cors_headers_a_browser_needs(client, monkeypatch):
    limit(monkeypatch, body=2000)
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": {"pad": "x" * 3000}},
                    headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 413
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_every_route_that_takes_a_body_enforces_it(client, monkeypatch):
    limit(monkeypatch, body=500)
    big = {"pad": "x" * 2000}
    for path in ("/assessments", "/assessments/preflight", "/assessments/x/rerun",
                 "/assessments/x/sizing", "/assessments/from-collection"):
        assert client.post(PREFIX + path, json=big).status_code == 413, path


# ---------------------------------------------------------------------------
# How many jobs
# ---------------------------------------------------------------------------


def test_the_job_after_the_last_place_is_refused_and_not_created(client, collection, monkeypatch, started):
    limit(monkeypatch, running=1, queued=1)
    assert priced(client, collection).json()["status"] == "queued"
    assert priced(client, collection).json()["status"] == "queued"

    r = priced(client, collection)
    assert r.status_code == 429
    assert r.json()["code"] == "queue_full" and "limit" in r.json()["detail"]
    assert r.headers["retry-after"] == str(limits.RETRY_AFTER_SECONDS)
    assert client.get(f"{PREFIX}/assessments").json()["total"] == 2
    assert len(started) == 2


def test_live_submits_and_reruns_count_too(client, collection, monkeypatch, started):
    limit(monkeypatch, running=1, queued=1)
    done = priced(client, collection, price=False).json()["job_id"]  # finishes in the request
    assert client.post(f"{PREFIX}/assessments", json=live("live-1")).status_code == 200
    assert client.post(f"{PREFIX}/assessments/{done}/rerun", json={"price": True}).status_code == 200

    assert client.post(f"{PREFIX}/assessments", json=live("live-2")).status_code == 429
    r = client.post(f"{PREFIX}/assessments/{done}/rerun", json={"price": True})
    assert r.status_code == 429 and r.json()["code"] == "queue_full"
    assert client.get(f"{PREFIX}/assessments").json()["total"] == 3  # done, live-1, one rerun


def test_a_job_that_runs_in_its_own_request_is_never_refused(client, collection, monkeypatch, started):
    limit(monkeypatch, running=1, queued=0)
    assert priced(client, collection).json()["status"] == "queued"
    assert priced(client, collection).status_code == 429  # full
    r = priced(client, collection, price=False)  # takes seconds, runs here
    assert r.status_code == 200 and r.json()["status"] == "complete"


def test_finished_and_cancelled_jobs_free_their_place(client, collection, monkeypatch, started):
    limit(monkeypatch, running=1, queued=0)
    first = priced(client, collection).json()["job_id"]
    assert priced(client, collection).status_code == 429

    assert client.post(f"{PREFIX}/assessments/{first}/cancel").json()["status"] == "cancelled"
    second = priced(client, collection)
    assert second.status_code == 200  # the cancelled job no longer counts

    state = LocalJobStore(common.ARTIFACT_ROOT)
    st = state.read_state(second.json()["job_id"])
    st["status"] = "complete"
    state.write_state(second.json()["job_id"], st)
    assert priced(client, collection).status_code == 200


def test_a_running_job_takes_a_place(client, collection, monkeypatch, started):
    limit(monkeypatch, running=1, queued=0)
    job = priced(client, collection).json()["job_id"]
    store = LocalJobStore(common.ARTIFACT_ROOT)
    st = store.read_state(job)
    st["status"] = "running"
    store.write_state(job, st)
    assert store.count_unfinished() == 1
    assert priced(client, collection).status_code == 429


def test_no_waiting_room_at_all_is_allowed(client, collection, monkeypatch, started):
    limit(monkeypatch, running=2, queued=0)
    assert priced(client, collection).status_code == 200
    assert priced(client, collection).status_code == 200
    assert priced(client, collection).status_code == 429


def test_the_last_place_goes_to_one_submit_when_many_arrive_together(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    limit(monkeypatch, running=1, queued=2)
    results, gate = [], threading.Barrier(12)

    def submit(n):
        gate.wait()
        try:
            assessments._queue(JobRecord(job_id=f"job-{n}", created_at="2026-10-14T10:00:00+00:00"),
                               background=True)
            results.append("ok")
        except Exception as e:  # noqa: BLE001 - the refusals are the point
            results.append(getattr(e, "code", repr(e)))

    threads = [threading.Thread(target=submit, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert results.count("ok") == 3 and results.count("queue_full") == 9
    assert LocalJobStore(tmp_path).count_unfinished() == 3


# ---------------------------------------------------------------------------
# Running at once
# ---------------------------------------------------------------------------


class _Blocking:
    """An orchestrator whose job waits to be released, and records who is inside."""

    def __init__(self):
        self.inside = []
        self.release = {}
        self.entered = {}

    def factory(self, *_args):
        outer = self

        class Orch:
            def run_from_collection(self, job_id, _collection):
                outer.inside.append(job_id)
                outer.entered[job_id].set()
                outer.release[job_id].wait(timeout=10)
                outer.inside.remove(job_id)

        return Orch()


def test_only_the_running_limit_of_jobs_are_inside_at_once(monkeypatch):
    limit(monkeypatch, running=1, queued=5)
    fake = _Blocking()
    for j in ("a", "b"):
        fake.release[j], fake.entered[j] = threading.Event(), threading.Event()
    monkeypatch.setattr(assessments, "orchestrator", fake.factory)

    args = lambda j: (j, {}, False, True, False, "us-east-1")  # noqa: E731
    first = threading.Thread(target=assessments._run_from_collection, args=args("a"))
    second = threading.Thread(target=assessments._run_from_collection, args=args("b"))
    first.start()
    assert fake.entered["a"].wait(timeout=5)
    second.start()
    assert not fake.entered["b"].wait(timeout=0.4)  # b waits for a slot
    assert fake.inside == ["a"]

    fake.release["a"].set()
    assert fake.entered["b"].wait(timeout=5)  # a slot is free, so b starts
    fake.release["b"].set()
    first.join(timeout=5)
    second.join(timeout=5)


def test_a_slot_is_given_back_when_a_job_fails(monkeypatch):
    limit(monkeypatch, running=1, queued=5)
    ran = []

    class Failing:
        def run_from_collection(self, job_id, _c):
            ran.append(job_id)
            raise RuntimeError("boom")

    monkeypatch.setattr(assessments, "orchestrator", lambda *_a: Failing())
    for j in ("a", "b", "c"):
        assessments._run_from_collection(j, {}, False, True, False, "us-east-1")
    assert ran == ["a", "b", "c"]
    assert limits.running_slots.acquire(blocking=False)
    limits.running_slots.release()


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


def test_start_up_leaves_request_handling_its_own_threads(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    limit(monkeypatch, running=4, queued=100)

    async def run() -> int:
        async with lifespan(app):
            return anyio.to_thread.current_default_thread_limiter().total_tokens

    assert asyncio.run(run()) >= REQUEST_THREADS + 4 + 100
