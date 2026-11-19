"""One error body, declared in OpenAPI. Spec: ux-api UX-30.

Every error is {"code": ..., "detail": ...}. These tests trigger each kind of error on the
real app and check the body, and check that the OpenAPI document declares the status for
the operation that returned it, so a generated client knows the case.
"""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import common
from src.api.errors import STATUS_CODES, ApiErrorBody, error_responses
from src.api.main import app
from src.api.routes import assessments, preflight, results, whatif
from src.orchestrator.job_store import LocalJobStore

FIXTURES = Path(__file__).parent.parent / "fixtures"
PREFIX = "/api/v1"
LIVE_CONFIG = {"ssh_target": "ec2-user@db-host.example.internal", "db_user": "apcat_ro",
               "password_file": "/home/ec2-user/.apcat-db-password"}  # nosec B105 - test value, not a password


def live(**over) -> dict:
    body = {"job_id": "live-1", "cluster_endpoint": "db.example.internal", "database_name": "appdb",
            "mode": "live", "live_config": LIVE_CONFIG,
            "collection_options": {"window_minutes": 15}}
    body.update(over)
    return body


@pytest.fixture
def collection() -> dict:
    return json.loads((FIXTURES / "strong_candidate_collection.json").read_text())


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


class _Idle:
    """Accepts a job and never starts it."""

    def get_state(self, job_id):
        return LocalJobStore(common.ARTIFACT_ROOT).read_state(job_id)

    def run_job(self, *_a):
        pass

    run_from_collection = run_job


@pytest.fixture
def idle(monkeypatch):
    monkeypatch.setattr(assessments, "orchestrator", lambda *a, **k: _Idle())


def declared(method: str, path: str) -> dict:
    """The responses OpenAPI declares for the operation a concrete path belongs to."""
    for template, ops in app.openapi()["paths"].items():
        if method.lower() in ops and re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", template), path):
            return ops[method.lower()]["responses"]
    raise AssertionError(f"no operation for {method} {path}")


def check(client, method, path, status, code, **kw):
    r = client.request(method, path, **kw)
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"code", "detail"}, body
    assert body["code"] == code
    assert str(status) in declared(method, path.split("?")[0]), (
        f"{method} {path} returned {status} but the API document does not declare it")
    return r


# ---------------------------------------------------------------------------
# Every kind of error, on the real app
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,status,code", [
    ("GET", "/assessments/nope-1/status", 404, "job_not_found"),
    ("DELETE", "/assessments/nope-1", 404, "job_not_found"),
    ("POST", "/assessments/nope-1/cancel", 404, "job_not_found"),
    ("GET", "/assessments/nope-1/analysis", 404, "artifact_not_available"),
    ("GET", "/assessments/nope-1/caching-assessment", 404, "artifact_not_available"),
    ("GET", "/assessments/nope-1/sizing", 404, "artifact_not_available"),
    ("GET", "/assessments/nope-1/collection", 404, "artifact_not_available"),
    ("GET", "/assessments/nope-1/report", 404, "artifact_not_available"),
    ("GET", "/assessments/nope-1/summary", 404, "artifact_not_available"),
    ("GET", "/assessments/nope-1/report-data", 404, "artifact_not_available"),
    ("GET", "/assessments/nope-1/report-data/overview", 404, "artifact_not_available"),
    ("GET", "/assessments/nope-1/report-data/queries", 404, "artifact_not_available"),
    ("GET", "/assessments/nope-1/report-data/tables/x", 404, "artifact_not_available"),
    ("POST", "/assessments/nope-1/sizing", 404, "artifact_not_available"),
])
def test_a_missing_job_or_artifact(client, method, path, status, code):
    kw = {"json": {}} if method == "POST" and path.endswith("sizing") else {}
    check(client, method, PREFIX + path, status, code, **kw)


def test_an_item_that_is_not_in_the_report(client, collection):
    job = client.post(f"{PREFIX}/assessments/from-collection",
                      json={"collector_output": collection, "price": False}).json()["job_id"]
    for kind in ("queries", "tables"):
        r = check(client, "GET", f"{PREFIX}/assessments/{job}/report-data/{kind}/nope",
                  404, "item_not_found")
        assert "nope" in r.json()["detail"]


def test_conflicts_on_a_job_that_is_not_finished(client, idle):
    assert client.post(f"{PREFIX}/assessments", json=live()).status_code == 200
    check(client, "POST", f"{PREFIX}/assessments", 409, "job_exists", json=live())
    check(client, "DELETE", f"{PREFIX}/assessments/live-1", 409, "job_busy")


def test_cancelling_a_finished_job(client, collection):
    done = client.post(f"{PREFIX}/assessments/from-collection",
                       json={"collector_output": collection, "price": False}).json()["job_id"]
    check(client, "POST", f"{PREFIX}/assessments/{done}/cancel", 409, "job_finished")


def test_a_value_that_is_refused(client, idle, collection):
    check(client, "POST", f"{PREFIX}/assessments", 422, "invalid_target",
          json=live(cluster_endpoint="db; id"))
    check(client, "POST", f"{PREFIX}/assessments", 422, "invalid_job_id",
          json=live(job_id="a..b"))
    check(client, "POST", f"{PREFIX}/assessments", 422, "invalid_label",
          params={"label": "a\nb"}, json=live())
    check(client, "POST", f"{PREFIX}/assessments/preflight", 422, "invalid_target",
          json=live(database_name="app db"))
    check(client, "POST", f"{PREFIX}/assessments/from-collection", 422, "invalid_label",
          json={"collector_output": collection, "label": "a\tb", "price": False})


def test_a_validation_error_keeps_the_list_and_gains_a_code(client):
    r = check(client, "GET", f"{PREFIX}/assessments?limit=0", 422, "invalid_request")
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail[0]["loc"][-1] == "limit"
    r = check(client, "POST", f"{PREFIX}/assessments/nope-1/sizing", 422, "invalid_request",
              json={"parameters": {"peak_factor": 99}})
    assert "peak_factor" in json.dumps(r.json()["detail"])


def test_a_collection_that_cannot_be_analysed(client):
    check(client, "POST", f"{PREFIX}/assessments/from-collection", 400, "assessment_failed",
          json={"collector_output": {"queries": "nope"}, "price": False})


def test_the_frameworks_own_errors_use_the_same_body(client):
    r = client.get("/no/such/route")
    assert r.status_code == 404 and r.json() == {"code": "not_found", "detail": "Not Found"}
    r = client.put(f"{PREFIX}/assessments/x/status")
    assert r.status_code == 405 and r.json()["code"] == "method_not_allowed"


def test_the_loopback_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.7", 50000))
    r = remote.get(f"{PREFIX}/capabilities")
    assert r.status_code == 403 and r.json()["code"] == "local_only"
    assert "no authentication" in r.json()["detail"]
    assert "403" in declared("GET", f"{PREFIX}/capabilities")


def test_too_many_requests_keep_retry_after(client):
    held = [preflight._slots.acquire(blocking=False) for _ in range(common.MAX_CONCURRENT_PREFLIGHTS)]
    try:
        r = check(client, "POST", f"{PREFIX}/assessments/preflight", 429, "preflight_busy",
                  json=live())
        assert r.headers["retry-after"] == "10"
    finally:
        for ok in held:
            if ok:
                preflight._slots.release()


def test_what_if_busy(client, collection):
    job = client.post(f"{PREFIX}/assessments/from-collection",
                      json={"collector_output": collection, "price": False}).json()["job_id"]
    held = [whatif._priced_slots.acquire(blocking=False)
            for _ in range(common.MAX_CONCURRENT_PRICED_WHAT_IFS)]
    try:
        check(client, "POST", f"{PREFIX}/assessments/{job}/sizing", 429, "what_if_busy",
              json={"price": True})
    finally:
        for ok in held:
            if ok:
                whatif._priced_slots.release()


def test_stored_data_that_is_not_valid_is_a_500_with_a_code(client, collection, tmp_path):
    job = client.post(f"{PREFIX}/assessments/from-collection",
                      json={"collector_output": collection, "price": False}).json()["job_id"]
    path = tmp_path / job / "analyze" / "analysis.json"
    data = json.loads(path.read_text())
    data["interpretation_mode"] = "not a mode"
    path.write_text(json.dumps(data))
    check(client, "GET", f"{PREFIX}/assessments/{job}/summary", 500, "stored_data_invalid")


def test_an_unexpected_error_is_a_fixed_500_and_the_traceback_stays_in_the_log(
        tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))

    def boom(*_a, **_k):
        raise RuntimeError("secret internal detail: /srv/app/db.password")

    monkeypatch.setattr(results, "orchestrator", boom)
    quiet = TestClient(app, client=("127.0.0.1", 50000), raise_server_exceptions=False)
    r = quiet.get(f"{PREFIX}/assessments/x/analysis")
    assert r.status_code == 500
    assert r.json() == {"code": "internal_error", "detail": "Internal server error"}
    assert "secret internal detail" not in r.text
    assert "secret internal detail" in caplog.text
    assert "500" in declared("GET", f"{PREFIX}/assessments/x/analysis")


# ---------------------------------------------------------------------------
# The API document
# ---------------------------------------------------------------------------


# The statuses each operation must declare besides the ones every operation has.
EXPECTED = {
    "create_assessment": {409, 413, 429},
    "create_from_collection": {400, 413, 429},
    "preflight": {413, 429},
    "delete_assessment": {404, 409},
    "cancel_assessment": {404, 409},
    "get_status": {404},
    "get_analysis": {404}, "get_caching_assessment": {404}, "get_sizing": {404},
    "get_collection": {404}, "get_report": {404}, "get_summary": {404},
    "get_report_data": {404}, "get_report_overview": {404},
    "list_report_queries": {404}, "get_report_query": {404},
    "list_report_tables": {404}, "get_report_table": {404},
    "what_if_sizing": {404, 413, 429},
    "rerun_assessment": {404, 413, 429},
    "purge_assessments": {413},
    "run_collect_phase": {404, 409, 413, 429},
    "run_analyze_phase": {400, 404, 409, 413, 429},
    "run_size_phase": {400, 404, 409, 413, 429},
    "run_report_phase": {400, 404, 409},
    "agent_chat": {413, 429},
    "discover_databases": {413, 429},
    "probe_host": {413, 429},
    "trust_host_key": {413, 429},
    "create_database": {409, 413},
    "create_role_route": {413, 429},
    "get_database": {404},
    "update_database": {404, 409, 413},
    "delete_database": {404, 409},
    "check_database": {404, 413, 429},
    "run_database_assessment": {404, 409, 413, 429},
}
EVERYWHERE = {403, 422, 500}


def operations() -> dict[str, dict]:
    return {op["operationId"]: op for ops in app.openapi()["paths"].values() for op in ops.values()}


def test_every_operation_declares_the_errors_it_can_return():
    ops = operations()
    missing = {}
    for name, op in ops.items():
        want = EVERYWHERE | EXPECTED.get(name, set())
        have = {int(s) for s in op["responses"] if s.isdigit()}
        if not want <= have:
            missing[name] = sorted(want - have)
    assert missing == {}


def test_every_operation_with_a_job_in_its_path_declares_404():
    app_spec = app.openapi()
    for path, ops in app_spec["paths"].items():
        if "{job_id}" not in path:
            continue
        for method, op in ops.items():
            assert "404" in op["responses"], f"{method.upper()} {path}"


def test_every_error_response_uses_the_one_body():
    schema_ref = "#/components/schemas/ApiErrorBody"
    for name, op in operations().items():
        for status, resp in op["responses"].items():
            if status.isdigit() and int(status) >= 400:
                ref = resp["content"]["application/json"]["schema"]["$ref"]
                assert ref == schema_ref, f"{name} {status}"
    body = app.openapi()["components"]["schemas"]["ApiErrorBody"]
    assert set(body["properties"]) == {"code", "detail"} and set(body["required"]) == {"code", "detail"}
    assert "HTTPValidationError" not in app.openapi()["components"]["schemas"]


def test_every_documented_status_has_a_description_and_a_code():
    assert set(error_responses(400, 403, 404, 409, 413, 422, 429, 500)) == {
        400, 403, 404, 409, 413, 422, 429, 500}
    assert all(error_responses(s)[s]["description"] for s in STATUS_CODES if s in
               (400, 403, 404, 409, 413, 422, 429, 500))
    assert ApiErrorBody(code="x", detail=["a"]).detail == ["a"]


def test_the_codes_a_client_can_switch_on_are_stable_words():
    for code in STATUS_CODES.values():
        assert re.fullmatch(r"[a-z]+(_[a-z]+)*", code)

