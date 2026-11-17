"""Saved databases: the store, the routes, readiness and the run gate. No SSH, no AWS."""

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from src.api import common
from src.api.main import app
from src.api.routes import assessments, databases
from src.contracts.api_models import JobInput, JobRecord
from src.contracts.databases import DatabaseConnection, DatabaseRecord, readiness_from
from src.contracts.preflight import PreflightCheck, PreflightResult
from src.orchestrator import database_store as ds
from src.orchestrator.job_store import LocalJobStore

P = "/api/v1/databases"
CONN = {"ssh_target": "ec2-user@10.0.0.5", "identity_file": "apcat_live",
        "db_user": "apcat_reader", "password_file": "/home/ec2-user/.apcat-reader-password",  # nosec B105 - test value, not a password
        "database_name": "marketplace"}


def result(*statuses: tuple[str, str], pss: bool | None = True) -> PreflightResult:
    return PreflightResult(ok=not any(s == "fail" for _n, s in statuses),
                           checks=[PreflightCheck(name=n, status=s, message="m")
                                   for n, s in statuses],
                           database_version="PostgreSQL 16.15",
                           pg_stat_statements_available=pss, duration_seconds=0.1)


READY = result(("ssh", "pass"), ("database", "pass"), ("database_host", "pass"),
               ("pg_stat_statements", "pass"))
DEGRADED = result(("ssh", "pass"), ("database", "pass"), ("database_host", "pass"),
                  ("pg_stat_statements", "warn"), pss=False)
BLOCKED = result(("ssh", "fail"), pss=None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(databases, "run_preflight", lambda _inp: READY)
    ran: list[tuple] = []
    monkeypatch.setattr(assessments, "_run_job", lambda *a, **k: ran.append((a, k)))
    c = TestClient(app, client=("127.0.0.1", 50000))
    c.ran = ran  # type: ignore[attr-defined]
    return c


def create(client, **extra) -> dict:
    r = client.post(P, json={"connection": CONN, **extra})
    assert r.status_code == 201, r.text
    return r.json()


# --- store ---------------------------------------------------------------------


def test_store_files_are_private_and_ids_checked(tmp_path):
    store = ds.LocalDatabaseStore(tmp_path)
    rec = DatabaseRecord(database_id=ds.new_database_id(), label="x",
                         connection=DatabaseConnection(**CONN), created_at="t", updated_at="t")
    store.create(rec)
    assert stat.S_IMODE(os.stat(store.dir).st_mode) == 0o700
    f = store.dir / f"{rec.database_id}.json"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert store.get(rec.database_id) == rec
    assert store.get("../x") is None and store.get("db-zz") is None
    assert not store.delete("../../etc")
    with pytest.raises(ds.DatabaseExists):
        store.create(rec.model_copy(update={"database_id": ds.new_database_id()}))


def test_store_keeps_at_most_max_databases(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "MAX_DATABASES", 1)
    store = ds.LocalDatabaseStore(tmp_path)
    first = DatabaseRecord(database_id=ds.new_database_id(), label="x",
                           connection=DatabaseConnection(**CONN), created_at="t", updated_at="t")
    store.create(first)
    other = DatabaseConnection(**{**CONN, "database_name": "other"})
    with pytest.raises(ds.TooManyDatabases):
        store.create(first.model_copy(update={"database_id": ds.new_database_id(),
                                              "connection": other}))


def test_too_many_databases_is_409(client, monkeypatch):
    monkeypatch.setattr(ds, "MAX_DATABASES", 1)
    create(client)
    r = client.post(P, json={"connection": {**CONN, "database_name": "other"}})
    assert r.status_code == 409 and r.json()["code"] == "too_many_databases"


def test_discovered_from_is_a_known_source(client):
    d = create(client, discovered_from="host_probe")
    assert d["discovered_from"] == "host_probe"
    r = client.post(P, json={"connection": {**CONN, "database_name": "other"},
                             "discovered_from": "10.0.0.9"})
    assert r.status_code == 422


def test_no_password_field_can_be_stored():
    with pytest.raises(ValueError):
        DatabaseConnection(**CONN, password="hunter2")  # nosec B106 - test value, not a password
    with pytest.raises(ValueError):
        DatabaseConnection(**{**CONN, "password_secret_arn": "arn:aws:secretsmanager:x"})  # nosec B105 - test value, not a password


def test_readiness_from_preflight():
    assert readiness_from(READY, "t").status == "ready"
    assert readiness_from(DEGRADED, "t").status == "degraded"
    b = readiness_from(BLOCKED, "t")
    assert b.status == "blocked" and b.failed == ["ssh"]
    w = readiness_from(result(("ssh", "pass"), ("pg_stat_statements", "warn"), pss=None), "t")
    assert w.status == "warnings"


# --- routes --------------------------------------------------------------------


def test_create_list_get_patch_delete(client):
    d = create(client)
    assert d["database_id"].startswith("db-") and d["readiness"]["status"] == "unchecked"
    assert d["label"] == "marketplace on 10.0.0.5" and d["assessments"] == 0
    assert client.get(P).json()["total"] == 1
    assert client.get(f"{P}/{d['database_id']}").json()["label"] == d["label"]
    r = client.patch(f"{P}/{d['database_id']}", json={"label": "Prod marketplace"})
    assert r.status_code == 200 and r.json()["label"] == "Prod marketplace"
    assert client.delete(f"{P}/{d['database_id']}").status_code == 204
    assert client.get(f"{P}/{d['database_id']}").status_code == 404


def test_duplicate_and_bad_values(client):
    create(client)
    r = client.post(P, json={"connection": CONN})
    assert r.status_code == 409 and r.json()["code"] == "database_exists"
    bad = {**CONN, "ssh_target": "-oProxyCommand=sh", "database_name": "other"}
    r = client.post(P, json={"connection": bad})
    assert r.status_code == 422 and r.json()["code"] == "invalid_target"
    assert "ProxyCommand" not in r.text
    # DB-4: an id not in the stored form is 404 on every database route.
    bad = "not-an-id"
    for method, path, body in (("get", bad, None), ("patch", bad, {"label": "x"}),
                               ("delete", bad, None), ("post", f"{bad}/check", None),
                               ("post", f"{bad}/assessments", {"window_minutes": 15}),
                               ("get", "db-ABCDEF123456", None)):
        kwargs = {"json": body} if body is not None else {}
        r = getattr(client, method)(f"{P}/{path}", **kwargs)
        assert r.status_code == 404 and r.json()["code"] == "database_not_found", (method, path)
        assert bad not in r.text


def test_check_stores_readiness_and_a_new_connection_clears_it(client):
    d = create(client)
    r = client.post(f"{P}/{d['database_id']}/check")
    assert r.status_code == 200 and r.json()["readiness"]["status"] == "ready"
    assert client.get(f"{P}/{d['database_id']}").json()["readiness"]["status"] == "ready"
    r = client.patch(f"{P}/{d['database_id']}", json={"connection": {**CONN, "port": 5433}})
    assert r.json()["readiness"]["status"] == "unchecked"


def test_run_gate(client, monkeypatch):
    d = create(client)
    run = {"window_minutes": 15}
    url = f"{P}/{d['database_id']}/assessments"
    assert client.post(url, json=run).json()["code"] == "database_not_checked"
    monkeypatch.setattr(databases, "run_preflight", lambda _i: BLOCKED)
    client.post(f"{P}/{d['database_id']}/check")
    assert client.post(url, json=run).json()["code"] == "database_blocked"
    monkeypatch.setattr(databases, "run_preflight", lambda _i: DEGRADED)
    client.post(f"{P}/{d['database_id']}/check")
    r = client.post(url, json=run)
    assert r.status_code == 409 and r.json()["code"] == "degraded_not_allowed"
    r = client.post(url, json={**run, "allow_degraded": True})
    assert r.status_code == 200 and r.json()["status"] == "queued"


def test_run_queues_a_job_linked_to_the_database(client):
    d = create(client)
    client.post(f"{P}/{d['database_id']}/check")
    r = client.post(f"{P}/{d['database_id']}/assessments",
                    json={"window_minutes": 30, "use_llm": True, "price": False})
    assert r.status_code == 200, r.text
    job = r.json()["job_id"]
    rec = client.get(f"/api/v1/assessments?database_id={d['database_id']}").json()
    assert rec["total"] == 1 and rec["items"][0]["job_id"] == job
    assert rec["items"][0]["database_id"] == d["database_id"]
    (args, _kw), = client.ran
    payload = args[1]
    assert payload["live_config"]["ssh_target"] == CONN["ssh_target"]
    assert payload["collection_options"]["window_minutes"] == 30
    stored = json.loads((LocalJobStore(common.ARTIFACT_ROOT).job_dir(job) / "job.json").read_text())
    # The job record stays sanitized: no SSH target, not even in the default database label.
    for value in ("ssh_target", "10.0.0.5", "apcat_reader", "apcat_live",
                  ".apcat-reader-password"):
        assert value not in json.dumps(stored)
    assert stored["database_id"] == d["database_id"] and stored["label"] is None
    view = client.get(f"{P}/{d['database_id']}").json()
    assert view["assessments"] == 1 and view["running"] == 1 and view["latest"]["job_id"] == job
    r = client.delete(f"{P}/{d['database_id']}")
    assert r.status_code == 409 and r.json()["code"] == "database_busy"


def test_attach_earlier_is_opt_in(client):
    store = LocalJobStore(common.ARTIFACT_ROOT)
    for i, db in enumerate(["marketplace", "marketplace", "other"]):
        store.create(JobRecord(job_id=f"old-{i}", source="live", created_at=f"2026-10-0{i + 1}",
                               input=JobInput(cluster_endpoint="127.0.0.1", port=5432,
                                              database_name=db)))
    d = create(client)
    assert d["attached"] == 0 and d["assessments"] == 0
    client.delete(f"{P}/{d['database_id']}")
    d = create(client, attach_earlier=True)
    assert d["attached"] == 2 and d["assessments"] == 2


def test_audit_has_ids_not_targets(client, tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(log))
    d = create(client)
    client.post(f"{P}/{d['database_id']}/check")
    client.post(f"{P}/{d['database_id']}/assessments", json={"window_minutes": 5})
    text = log.read_text()
    for event in ("database_created", "database_check", "job_submitted"):
        assert f'"event":"{event}"' in text
    for secret in ("10.0.0.5", "apcat_reader", ".apcat-reader-password", "apcat_live"):
        assert secret not in text


def test_routes_are_loopback_only(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.9", 50000))
    assert remote.get(P).status_code == 403
    assert remote.post(P, json={"connection": CONN}).status_code == 403
