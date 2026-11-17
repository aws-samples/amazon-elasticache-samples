"""Rerun. Spec: ux-api UX-31.

A rerun starts a new job on the collection a job saved, so the UX does not download the
collection and post it back. The original job is never changed.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import common
from src.api.main import app
from src.api.routes import assessments
from src.contracts.api_models import JobRecord
from src.orchestrator.job_store import LocalJobStore

FIXTURES = Path(__file__).parent.parent / "fixtures"
PREFIX = "/api/v1"


@pytest.fixture
def collection() -> dict:
    return json.loads((FIXTURES / "strong_candidate_collection.json").read_text())


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


def submit(client, collection, **body) -> str:
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "price": False, **body})
    assert r.status_code == 200 and r.json()["status"] == "complete", r.text
    return r.json()["job_id"]


@pytest.fixture
def job(client, collection) -> str:
    return submit(client, collection, label="Orders DB")


@pytest.fixture
def queued(monkeypatch) -> list:
    """Records the jobs the API queues instead of running them."""
    calls: list = []
    monkeypatch.setattr(assessments, "_run_from_collection",
                        lambda *args: calls.append(args))
    return calls


def rerun(client, job_id, **body):
    r = client.post(f"{PREFIX}/assessments/{job_id}/rerun", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def record(client, job_id) -> dict:
    items = client.get(f"{PREFIX}/assessments", params={"limit": 100}).json()["items"]
    return next(i for i in items if i["job_id"] == job_id)


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------
# What a rerun is
# ---------------------------------------------------------------------------


def test_a_rerun_is_a_new_job_that_points_back(client, job):
    out = rerun(client, job)
    new = out["job_id"]
    assert new != job and new.startswith("rerun-")
    assert out["status"] == "complete"  # the original did not price, so this runs in the request

    rec = record(client, new)
    assert rec["rerun_of"] == job and rec["source"] == "collection" and rec["status"] == "complete"
    assert record(client, job)["rerun_of"] is None


def test_the_new_job_analysed_the_same_collection(client, job, collection):
    new = rerun(client, job)["job_id"]
    a = client.get(f"{PREFIX}/assessments/{job}/summary").json()
    b = client.get(f"{PREFIX}/assessments/{new}/summary").json()
    for key in ("verdict", "queries_recommended", "tables_recommended", "bundles",
                "database_name", "collected_at", "sizing"):
        assert a[key] == b[key], key
    assert client.get(f"{PREFIX}/assessments/{new}/collection").json() == \
        client.get(f"{PREFIX}/assessments/{job}/collection").json()
    assert client.get(f"{PREFIX}/assessments/{new}/report").status_code == 200


def test_the_original_job_is_left_exactly_as_it_was(client, job, tmp_path):
    before = {k: v for k, v in snapshot(tmp_path).items() if k.startswith(f"{job}/")}
    rerun(client, job)
    rerun(client, job, size=False)
    after = {k: v for k, v in snapshot(tmp_path).items() if k.startswith(f"{job}/")}
    assert after == before


def test_a_rerun_can_be_rerun(client, job):
    second = rerun(client, rerun(client, job)["job_id"])["job_id"]
    first = record(client, second)["rerun_of"]
    assert first.startswith("rerun-") and record(client, first)["rerun_of"] == job


def test_two_reruns_are_two_jobs(client, job):
    assert rerun(client, job)["job_id"] != rerun(client, job)["job_id"]
    assert client.get(f"{PREFIX}/assessments").json()["total"] == 3


# ---------------------------------------------------------------------------
# Options and label
# ---------------------------------------------------------------------------


def test_options_default_to_what_the_original_used(client, collection):
    job = submit(client, collection, region="ap-south-1", size=False)
    new = rerun(client, job)["job_id"]
    assert record(client, new)["options"] == record(client, job)["options"] == {
        "use_llm": False, "size": False, "price": False, "region": "ap-south-1",
        "model_id": None, "require_llm": False}


def test_pricing_queues_the_job_and_passes_the_saved_collection(client, job, queued):
    out = rerun(client, job, price=True, region="eu-west-1")
    assert out["status"] == "queued"
    (new, collection, use_llm, size, price, region), = [(a[0], a[1], *a[2:]) for a in queued]
    assert new == out["job_id"] and use_llm is False and size is True and price is True
    assert region == "eu-west-1"
    assert collection == client.get(f"{PREFIX}/assessments/{job}/collection").json()
    assert record(client, new)["status"] == "queued"
    assert record(client, new)["options"]["price"] is True


def test_running_the_model_queues_the_job(client, job, queued):
    out = rerun(client, job, use_llm=True)
    assert out["status"] == "queued"
    assert queued[0][2] is True  # use_llm
    assert record(client, out["job_id"])["options"]["use_llm"] is True


def test_the_database_and_collection_time_show_while_it_is_still_queued(client, job, queued):
    new = rerun(client, job, price=True)["job_id"]
    rec = record(client, new)
    original = record(client, job)
    assert rec["status"] == "queued"
    assert rec["database_name"] and rec["database_name"] == original["database_name"]
    assert rec["collected_at"] == original["collected_at"]


def test_a_rerun_without_a_label_takes_the_originals(client, job, collection):
    assert record(client, rerun(client, job)["job_id"])["label"] == "Orders DB (rerun)"
    unnamed = submit(client, collection)
    assert record(client, rerun(client, unnamed)["job_id"])["label"] is None


def test_a_long_label_stays_within_the_limit(client, collection):
    job = submit(client, collection, label="x" * 100)
    label = record(client, rerun(client, job)["job_id"])["label"]
    assert len(label) == 100 and label.startswith("xxx")


def test_a_label_in_the_request_wins(client, job):
    assert record(client, rerun(client, job, label="  After tuning ")["job_id"])["label"] == \
        "After tuning"


# ---------------------------------------------------------------------------
# Missing and refused
# ---------------------------------------------------------------------------


def test_no_body_is_the_same_as_an_empty_one(client, job):
    r = client.post(f"{PREFIX}/assessments/{job}/rerun")
    assert r.status_code == 200 and r.json()["status"] == "complete"


def test_an_unknown_job_is_404(client):
    r = client.post(f"{PREFIX}/assessments/nope-1/rerun", json={})
    assert r.status_code == 404 and r.json()["code"] == "job_not_found"


def test_a_job_with_no_collection_is_404(client, tmp_path):
    LocalJobStore(tmp_path).create(JobRecord(
        job_id="job-early", source="live", created_at="2026-10-14T10:00:00+00:00"))
    r = client.post(f"{PREFIX}/assessments/job-early/rerun", json={})
    assert r.status_code == 404 and r.json()["code"] == "artifact_not_available"
    assert "no saved collection" in r.json()["detail"]
    assert client.get(f"{PREFIX}/assessments").json()["total"] == 1  # no job was created


def test_a_deleted_collection_is_404(client, job, tmp_path):
    (tmp_path / job / "collect" / "output.json").unlink()
    assert client.post(f"{PREFIX}/assessments/{job}/rerun", json={}).status_code == 404


@pytest.mark.parametrize("body", [
    {"region": "US-EAST-1"}, {"region": "us-east-1; id"}, {"region": ""},
    {"use_llm": "maybe"}, {"price": 7}, {"surprise": 1}, {"label": "x" * 101},
])
def test_bad_settings_are_422_and_create_no_job(client, job, body):
    r = client.post(f"{PREFIX}/assessments/{job}/rerun", json=body)
    assert r.status_code == 422
    assert client.get(f"{PREFIX}/assessments").json()["total"] == 1


def test_a_label_with_a_control_character_is_refused(client, job):
    r = client.post(f"{PREFIX}/assessments/{job}/rerun", json={"label": "a\nb"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_label"
    assert client.get(f"{PREFIX}/assessments").json()["total"] == 1


@pytest.mark.parametrize("job_id", ["..", ".hidden", "a..b", "x" * 65])
def test_a_bad_job_id_never_reaches_the_disk(client, tmp_path, job_id):
    assert client.post(f"{PREFIX}/assessments/{job_id}/rerun", json={}).status_code in (404, 405, 422)
    assert list(tmp_path.iterdir()) == []


def test_the_route_is_typed_documented_and_behind_the_gate(tmp_path, monkeypatch):
    op = app.openapi()["paths"][f"{PREFIX}/assessments/{{job_id}}/rerun"]["post"]
    assert op["operationId"] == "rerun_assessment"
    assert op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/SubmitResponse")
    assert {"404", "413", "429", "403", "422", "500"} <= set(op["responses"])
    assert op["requestBody"].get("required") is not True
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.7", 50000))
    assert remote.post(f"{PREFIX}/assessments/x/rerun", json={}).status_code == 403
