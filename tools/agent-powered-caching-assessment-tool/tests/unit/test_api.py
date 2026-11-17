"""The API: v2 output, and the loopback-only gate. Spec: v2-consolidation VC-4."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import common
from src.api.main import app, is_loopback, resolve_bind

FIXTURES = Path(__file__).parent.parent / "fixtures"
PREFIX = "/api/v1"


@pytest.fixture
def collection() -> dict:
    return json.loads((FIXTURES / "strong_candidate_collection.json").read_text())


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


def _run(client: TestClient, collection: dict) -> str:
    """Run a job in the request. Pricing is on by default and queues the job, so a
    test that wants the result at once passes price=False."""
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "price": False})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "complete"
    return r.json()["job_id"]


# ---------------------------------------------------------------------------
# Routes return v2 output
# ---------------------------------------------------------------------------


def test_from_collection_then_every_artifact_route(client, collection):
    job = _run(client, collection)

    assert client.get(f"{PREFIX}/assessments/{job}/status").json()["status"] == "complete"

    analysis = client.get(f"{PREFIX}/assessments/{job}/analysis").json()
    assert analysis["contract_version"] == "2.7"
    assert analysis["query_recommendations"] and analysis["table_recommendations"]
    assert client.get(f"{PREFIX}/assessments/{job}/caching-assessment").json() == analysis
    assert client.get(f"{PREFIX}/assessments/{job}/collection").status_code == 200


def test_summary_is_built_from_the_contract_accessors(client, collection):
    job = _run(client, collection)
    s = client.get(f"{PREFIX}/assessments/{job}/summary").json()
    assert s["contract_version"] == "2.7"
    assert s["interpretation_mode"] == "deterministic_fallback"
    assert s["verdict"] is None  # no model ran, so no workload verdict of record
    assert s["tables_recommended"] >= 1
    assert isinstance(s["bundles"], int)
    assert "monthly_cost_usd" not in s and "suggested_node_type" not in s


def test_report_route_serves_html_with_a_locked_down_policy(client, collection):
    job = _run(client, collection)
    r = client.get(f"{PREFIX}/assessments/{job}/report")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "How scoring works" in r.text


def test_unknown_job_is_404(client):
    for path in ("status", "analysis", "report", "summary", "collection"):
        assert client.get(f"{PREFIX}/assessments/nope-123/{path}").status_code == 404


def test_a_bad_collection_is_a_400_not_a_500(client):
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": {"queries": "nope"}, "price": False})
    assert r.status_code == 400


def test_health_reports_the_release_and_no_authentication(client):
    from src.version import __version__
    body = client.get("/health").json()
    assert body["version"] == __version__
    assert "none" in body["authentication"]


# ---------------------------------------------------------------------------
# Sizing & Cost. Spec: sizing-orchestrator SO-5 to SO-8.
# ---------------------------------------------------------------------------


def test_sizing_is_attached_to_every_analysis_route(client, collection):
    job = _run(client, collection)

    analysis = client.get(f"{PREFIX}/assessments/{job}/analysis").json()
    assert analysis["sizing"]["contract_version"] == "1.0"
    assert client.get(f"{PREFIX}/assessments/{job}/caching-assessment").json() == analysis

    sizing = client.get(f"{PREFIX}/assessments/{job}/sizing")
    assert sizing.status_code == 200
    assert sizing.json() == analysis["sizing"]
    # _run asked for price=False, and the block says cost is unavailable.
    assert sizing.json()["cost"]["available"] is False
    assert sizing.json()["cost"]["unavailable_reason"] == "pricing skipped by caller"


def test_summary_carries_a_short_sizing_object(client, collection):
    job = _run(client, collection)
    s = client.get(f"{PREFIX}/assessments/{job}/summary").json()["sizing"]
    assert set(s) == {"status", "items_sized", "items_not_sized", "memory_needed_gb",
                      "instance_type", "total_nodes", "cost_available", "monthly_total"}
    assert s["cost_available"] is False and s["monthly_total"] is None


def test_report_has_the_sizing_section(client, collection):
    job = _run(client, collection)
    assert 'id="sizing"' in client.get(f"{PREFIX}/assessments/{job}/report").text


def test_size_false_leaves_no_sizing(client, collection):
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "size": False})
    assert r.status_code == 200
    assert r.json()["status"] == "complete"  # nothing to price, so it runs in the request
    job = r.json()["job_id"]
    assert client.get(f"{PREFIX}/assessments/{job}/analysis").json().get("sizing") is None
    assert client.get(f"{PREFIX}/assessments/{job}/sizing").status_code == 404
    assert client.get(f"{PREFIX}/assessments/{job}/summary").json()["sizing"] is None


def test_sizing_route_for_an_unknown_job_is_404(client):
    assert client.get(f"{PREFIX}/assessments/nope-123/sizing").status_code == 404


@pytest.mark.parametrize("region", ["", "US-EAST-1", "us-east-1; x", "../x", "a b"])
def test_a_bad_region_is_a_422_before_anything_runs(client, collection, tmp_path, region):
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "region": region})
    assert r.status_code == 422
    assert list(tmp_path.iterdir()) == []
    r = client.post(f"{PREFIX}/assessments", params={"region": region}, json=_live_payload())
    assert r.status_code == 422
    assert "region" in r.text
    assert list(tmp_path.iterdir()) == []


def _live_payload() -> dict:
    """A valid CollectorInput for a live run. Nothing connects to it in these tests."""
    return {
        "job_id": "live-1",
        "cluster_endpoint": "db.example.internal",
        "database_name": "appdb",
        "mode": "live",
        "live_config": {"ssh_target": "ec2-user@db-host.example.internal",
                        "db_user": "apcat_ro",
                        "password_file": "/home/ec2-user/.apcat-db-password"},  # nosec B105 - test value, not a password
        "collection_options": {"window_minutes": 15},
    }


class _FakeOrchestrator:
    """Records how the API builds the orchestrator and what it is asked to run."""

    calls: list = []

    def __init__(self, **kw):
        self.kw = kw

    def get_state(self, job_id):
        # Reads the state the API wrote at submit; the fake never advances it.
        from src.orchestrator.job_store import LocalJobStore
        return LocalJobStore(common.ARTIFACT_ROOT).read_state(job_id)

    def run_from_collection(self, job_id, collector_output):
        _FakeOrchestrator.calls.append(("from_collection", job_id, self.kw))

    def run_job(self, job_id, payload):
        _FakeOrchestrator.calls.append(("job", job_id, self.kw))


@pytest.fixture
def fake_orch(monkeypatch):
    from src.api.routes import assessments
    _FakeOrchestrator.calls = []
    monkeypatch.setattr(
        assessments, "orchestrator",
        lambda use_llm=False, size=True, price=True, region="us-east-1":
        _FakeOrchestrator(use_llm=use_llm, size=size, price=price, region=region))
    return _FakeOrchestrator


def test_a_default_request_prices_and_is_queued(client, collection, fake_orch):
    r = client.post(f"{PREFIX}/assessments/from-collection", json={"collector_output": collection})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    (kind, _job, kw), = fake_orch.calls
    assert kind == "from_collection"
    assert kw == {"use_llm": False, "size": True, "price": True, "region": "us-east-1"}


def test_a_pricing_request_with_a_region_is_queued(client, collection, fake_orch):
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "price": True, "region": "eu-west-1"})
    assert r.json()["status"] == "queued"
    (_kind, _job, kw), = fake_orch.calls
    assert kw == {"use_llm": False, "size": True, "price": True, "region": "eu-west-1"}


def test_price_false_runs_in_the_request_without_pricing(client, collection, fake_orch):
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "price": False})
    assert r.json()["status"] == "complete"
    (_kind, _job, kw), = fake_orch.calls
    assert kw == {"use_llm": False, "size": True, "price": False, "region": "us-east-1"}


def test_a_default_live_submission_prices(client, fake_orch):
    r = client.post(f"{PREFIX}/assessments", json=_live_payload())
    assert r.status_code == 200 and r.json()["status"] == "queued"
    (_kind, _job, kw), = fake_orch.calls
    assert kw["price"] is True and kw["size"] is True and kw["region"] == "us-east-1"


def test_live_submission_passes_the_sizing_switches_through(client, fake_orch):
    r = client.post(f"{PREFIX}/assessments",
                    params={"price": "true", "size": "true", "region": "us-east-2"},
                    json=_live_payload())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    (_kind, job, kw), = fake_orch.calls
    assert job == "live-1"
    assert kw["price"] is True and kw["region"] == "us-east-2"


# ---------------------------------------------------------------------------
# Typed responses. Spec: ux-api UX-1.
# ---------------------------------------------------------------------------


def test_every_json_route_declares_a_response_schema():
    spec = app.openapi()
    untyped, html, streams = [], [], []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            ok = op["responses"].get("200") or op["responses"].get("201")
            if ok is None:  # a 204, which has no body
                assert "204" in op["responses"], (method, path)
                continue
            content = ok.get("content", {})
            if "text/html" in content:
                html.append(path)
            elif "text/event-stream" in content:
                # Typed too: each event is one AgentEvent.
                assert content["text/event-stream"]["schema"] == {
                    "$ref": "#/components/schemas/AgentEvent"}, path
                streams.append(path)
            elif content.get("application/json", {}).get("schema", {}) == {}:
                untyped.append(f"{method.upper()} {path}")
    assert untyped == []
    assert html == ["/api/v1/assessments/{job_id}/report"]
    assert streams == ["/api/v1/agent/chat"]


def test_operation_ids_are_the_function_names():
    ids = {op["operationId"] for ops in app.openapi()["paths"].values() for op in ops.values()}
    assert {"create_assessment", "create_from_collection", "list_assessments",
            "delete_assessment", "get_status", "get_analysis", "get_sizing",
            "get_summary", "health"} <= ids
    assert all(" " not in i and "/" not in i for i in ids)


def test_health_is_typed_and_unchanged(client):
    from src.version import __version__
    assert client.get("/health").json() == {
        "status": "ok", "service": "apcat", "version": __version__,
        "authentication": "none: loopback only"}


# ---------------------------------------------------------------------------
# Jobs: queued at once, status history, list, delete. Spec: ux-api UX-2 to UX-9.
# ---------------------------------------------------------------------------


def test_status_reports_the_phase_history_of_a_finished_job(client, collection):
    job = _run(client, collection)
    s = client.get(f"{PREFIX}/assessments/{job}/status").json()
    assert s["status"] == "complete" and s["current_phase"] == "report"
    assert [p["phase"] for p in s["phases"]] == ["analyze", "size", "report"]
    assert all(p["status"] == "complete" and p["ended_at"] for p in s["phases"])
    assert s["created_at"] and s["started_at"] and s["completed_at"]
    assert s["duration_seconds"] >= 0 and s["error"] is None and s["warnings"] == []


def test_a_queued_job_has_a_status_the_moment_it_is_accepted(client, collection, fake_orch):
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection})
    job = r.json()["job_id"]
    s = client.get(f"{PREFIX}/assessments/{job}/status")
    assert s.status_code == 200  # never a 404 for an accepted job
    # The fake orchestrator never starts the job, so it is still queued.
    assert s.json()["status"] == "queued" and s.json()["phases"] == []


def test_the_list_shows_label_database_verdict_and_status(client, collection):
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "price": False,
                          "label": "  Strong fixture "})
    job = r.json()["job_id"]
    body = client.get(f"{PREFIX}/assessments").json()
    assert body["total"] == 1 and body["offset"] == 0 and body["limit"] == 25
    item = body["items"][0]
    assert item["job_id"] == job and item["label"] == "Strong fixture"
    assert item["status"] == "complete" and item["source"] == "collection"
    assert item["interpretation_mode"] == "deterministic_fallback"
    assert item["database_name"] and item["collected_at"]
    assert item["options"]["price"] is False and item["owner"] is None


def test_the_list_pages_filters_and_searches(client, collection):
    ids = []
    for label in ("Marketplace 1x", "Retail", "Marketplace 5x"):
        r = client.post(f"{PREFIX}/assessments/from-collection",
                        json={"collector_output": collection, "price": False, "label": label})
        ids.append(r.json()["job_id"])
    base = f"{PREFIX}/assessments"
    assert [i["job_id"] for i in client.get(base).json()["items"]] == ids[::-1]
    page = client.get(base, params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 3 and [i["job_id"] for i in page["items"]] == [ids[0]]
    assert client.get(base, params={"q": "marketplace"}).json()["total"] == 2
    assert client.get(base, params={"status": "failed"}).json()["total"] == 0
    assert client.get(base, params={"status": "complete"}).json()["total"] == 3


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "weird"}, {"q": "x" * 101},
])
def test_bad_list_parameters_are_422(client, params):
    assert client.get(f"{PREFIX}/assessments", params=params).status_code == 422


@pytest.mark.parametrize("label,status", [
    ("x" * 100, 200), ("x" * 101, 422), ("a\nb", 422), ("tab\there", 422), ("", 200),
])
def test_labels_are_checked(client, collection, label, status):
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "price": False, "label": label})
    assert r.status_code == status


def test_a_live_submission_keeps_the_label_and_a_sanitized_input(client, fake_orch):
    r = client.post(f"{PREFIX}/assessments", params={"label": "Prod orders"},
                    json=_live_payload())
    assert r.status_code == 200
    listing = client.get(f"{PREFIX}/assessments")
    item = listing.json()["items"][0]
    assert item["label"] == "Prod orders" and item["source"] == "live"
    assert item["input"]["cluster_endpoint"] == "db.example.internal"
    assert item["input"]["database_name"] == "appdb" and "aws_region" not in item["input"]
    text = listing.text
    for secret in ("ec2-user", "db-host.example.internal", "apcat_ro", ".apcat-db-password"):
        assert secret not in text


def test_a_submit_with_the_removed_fields_is_a_422(client, fake_orch):
    """A client still sending the old access fields is told so, not silently ignored."""
    body = _live_payload()
    body["live_config"].update({"secret_arn": "x", "automation_instance_id": "y"})  # nosec B105 - test value, not a password
    assert client.post(f"{PREFIX}/assessments", json=body).status_code == 422
    body = _live_payload()
    body["aws_config"] = {"region": "us-east-1"}
    assert client.post(f"{PREFIX}/assessments", json=body).status_code == 422
    assert fake_orch.calls == []


def test_reusing_a_live_job_id_is_a_409(client, fake_orch):
    assert client.post(f"{PREFIX}/assessments", json=_live_payload()).status_code == 200
    again = client.post(f"{PREFIX}/assessments", json=_live_payload())
    assert again.status_code == 409 and "already exists" in again.json()["detail"]
    assert len(fake_orch.calls) == 1  # the second job was never queued


def test_delete_removes_a_finished_job(client, collection, tmp_path):
    job = _run(client, collection)
    assert (tmp_path / job).is_dir()
    assert client.delete(f"{PREFIX}/assessments/{job}").status_code == 204
    assert not (tmp_path / job).exists()
    assert client.get(f"{PREFIX}/assessments/{job}/status").status_code == 404
    assert client.get(f"{PREFIX}/assessments/{job}/analysis").status_code == 404
    assert client.get(f"{PREFIX}/assessments").json()["total"] == 0
    assert client.delete(f"{PREFIX}/assessments/{job}").status_code == 404


def test_delete_refuses_a_job_that_is_still_going(client, collection, fake_orch, tmp_path):
    job = client.post(f"{PREFIX}/assessments/from-collection",
                      json={"collector_output": collection}).json()["job_id"]
    r = client.delete(f"{PREFIX}/assessments/{job}")
    assert r.status_code == 409 and "still queued or running" in r.json()["detail"]
    assert (tmp_path / job).is_dir()


@pytest.mark.parametrize("job_id", ["..", ".hidden", "a..b", "x" * 65])
def test_delete_of_a_bad_id_touches_nothing(client, tmp_path, job_id):
    (tmp_path / "keep").mkdir()
    assert client.delete(f"{PREFIX}/assessments/{job_id}").status_code in (404, 405, 422)
    assert [p.name for p in tmp_path.iterdir()] == ["keep"]


def test_cors_allows_delete(client):
    r = client.options(f"{PREFIX}/assessments/x", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "DELETE"})
    assert r.status_code == 200
    assert "DELETE" in r.headers["access-control-allow-methods"]


def test_a_server_start_fails_jobs_the_last_run_left_unfinished(client, collection, tmp_path):
    from src.api.main import sweep_unfinished_jobs
    from src.contracts.api_models import JobRecord
    from src.orchestrator.job_store import LocalJobStore

    done = _run(client, collection)  # a real, finished job
    job = "left-queued"
    LocalJobStore(tmp_path).create(JobRecord(
        job_id=job, source="collection", created_at="2026-10-14T10:00:00+00:00"))

    assert sweep_unfinished_jobs() == [job]

    s = client.get(f"{PREFIX}/assessments/{job}/status").json()
    assert s["status"] == "failed" and "server stopped" in s["error"]
    assert client.get(f"{PREFIX}/assessments/{done}/status").json()["status"] == "complete"
    # Failed, so it can now be deleted.
    assert client.delete(f"{PREFIX}/assessments/{job}").status_code == 204


def test_the_sweep_runs_when_the_app_starts(collection, tmp_path, monkeypatch):
    from src.contracts.api_models import JobRecord
    from src.orchestrator.job_store import LocalJobStore
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    LocalJobStore(tmp_path).create(JobRecord(job_id="left-behind", source="collection",
                                             created_at="2026-10-14T10:00:00+00:00"))
    with TestClient(app, client=("127.0.0.1", 50000)) as started:  # runs the lifespan
        s = started.get(f"{PREFIX}/assessments/left-behind/status").json()
    assert s["status"] == "failed"


def test_the_summary_carries_the_label_and_the_job_facts(client, collection):
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "price": False, "label": "Tile"})
    s = client.get(f"{PREFIX}/assessments/{r.json()['job_id']}/summary").json()
    assert s["label"] == "Tile" and s["database_name"] and s["collected_at"]
    assert "executive_summary" in s


@pytest.mark.parametrize("method,path", [
    ("get", f"{PREFIX}/assessments"), ("delete", f"{PREFIX}/assessments/x"),
])
def test_the_new_routes_are_behind_the_loopback_gate(tmp_path, monkeypatch, method, path):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.7", 50000))
    assert getattr(remote, method)(path).status_code == 403


# ---------------------------------------------------------------------------
# The report as data. Spec: ux-api UX-11 to UX-15.
# ---------------------------------------------------------------------------


def _labelled_run(client, collection, label="Marketplace 1x") -> str:
    r = client.post(f"{PREFIX}/assessments/from-collection",
                    json={"collector_output": collection, "price": False, "label": label})
    return r.json()["job_id"]


def test_report_data_is_the_whole_report_as_json(client, collection):
    job = _labelled_run(client, collection)
    r = client.get(f"{PREFIX}/assessments/{job}/report-data")
    assert r.status_code == 200
    vm = r.json()
    assert vm["meta"]["job_id"] == job and vm["meta"]["label"] == "Marketplace 1x"
    assert vm["meta"]["view_model_version"] == "1.0"
    assert vm["headline"]["title"] and vm["at_a_glance"] and vm["glossary"]
    assert vm["queries"] and vm["tables"] and vm["scoring"]["pillars"]
    assert vm["sizing"]["contract_version"] == "1.0"  # the SIZE phase ran
    assert vm["counts"]["tables"] == len(vm["tables"])
    # The same facts the summary tile and the analysis give.
    summary = client.get(f"{PREFIX}/assessments/{job}/summary").json()
    assert vm["counts"]["tables_recommended"] == summary["tables_recommended"]
    assert vm["counts"]["queries_recommended"] == summary["queries_recommended"]


def test_the_overview_leaves_out_the_long_lists(client, collection):
    job = _run(client, collection)
    ov = client.get(f"{PREFIX}/assessments/{job}/report-data/overview").json()
    assert "queries" not in ov and "tables" not in ov
    assert ov["headline"] and ov["workload"] and ov["counts"]["queries"] > 0


def test_report_queries_are_paged_and_filtered(client, collection):
    job = _run(client, collection)
    base = f"{PREFIX}/assessments/{job}/report-data/queries"
    page = client.get(base).json()
    assert page["total"] >= 1 and page["offset"] == 0 and page["limit"] == 25
    assert [q["rank"] for q in page["items"]] == list(range(1, len(page["items"]) + 1))
    one = client.get(base, params={"limit": 1}).json()
    assert len(one["items"]) == 1 and one["total"] == page["total"]
    rec = client.get(base, params={"recommended": "true"}).json()
    assert all(q["recommended"] for q in rec["items"])
    assert client.get(base, params={"cache_type": "UNKNOWN"}).json()["total"] == 0
    for bad in ({"verdict": "MAYBE"}, {"cache_type": "x"}, {"limit": 0}, {"limit": 201},
                {"offset": -1}):
        assert client.get(base, params=bad).status_code == 422


def test_one_report_query_by_id(client, collection):
    job = _run(client, collection)
    first = client.get(f"{PREFIX}/assessments/{job}/report-data/queries").json()["items"][0]
    got = client.get(f"{PREFIX}/assessments/{job}/report-data/queries/{first['queryid']}")
    assert got.status_code == 200 and got.json() == first
    missing = client.get(f"{PREFIX}/assessments/{job}/report-data/queries/nope")
    assert missing.status_code == 404 and "nope" in missing.json()["detail"]


def test_report_tables_are_paged_filtered_and_found_by_id(client, collection):
    job = _run(client, collection)
    base = f"{PREFIX}/assessments/{job}/report-data/tables"
    page = client.get(base).json()
    ids = [t["table_id"] for t in page["items"]]
    assert page["total"] == len(ids) >= 2
    ruled = client.get(base, params={"ruled_out": "true"}).json()
    assert ruled["total"] == sum(t["ruled_out"] for t in page["items"])
    assert all(t["ruled_out"] for t in ruled["items"])
    weighed = client.get(base, params={"ruled_out": "false"}).json()
    assert weighed["total"] + ruled["total"] == page["total"]
    # Weighed tables come before ruled-out ones.
    flags = [t["ruled_out"] for t in page["items"]]
    assert flags == sorted(flags)
    first = page["items"][0]
    got = client.get(f"{base}/{first['table_id']}")  # ids hold a dot, as in public.product
    assert got.status_code == 200 and got.json() == first
    assert client.get(f"{base}/nope.nothing").status_code == 404


def test_report_data_routes_404_for_unknown_and_unfinished_jobs(client, collection, fake_orch):
    for tail in ("", "/overview", "/queries", "/tables", "/queries/x", "/tables/x"):
        assert client.get(f"{PREFIX}/assessments/nope-1/report-data{tail}").status_code == 404
    queued = client.post(f"{PREFIX}/assessments/from-collection",
                         json={"collector_output": collection}).json()["job_id"]
    assert client.get(f"{PREFIX}/assessments/{queued}/report-data").status_code == 404


def test_report_data_is_behind_the_loopback_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.7", 50000))
    assert remote.get(f"{PREFIX}/assessments/x/report-data").status_code == 403


def test_report_data_and_the_html_report_agree_on_the_query_order(client, collection):
    import re
    job = _run(client, collection)
    html = client.get(f"{PREFIX}/assessments/{job}/report").text
    sec = html.split("<h2>Where the database spends its time</h2>")[1].split("<h2>")[0]
    in_html = re.findall(r'href="#(q-[^"]+)"', sec)
    items = client.get(f"{PREFIX}/assessments/{job}/report-data/queries",
                       params={"limit": 200}).json()["items"]
    assert in_html == [q["anchor"] for q in items if q["is_candidate"]]


# ---------------------------------------------------------------------------
# Job id handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job_id", ["..", ".hidden", "a..b", "x" * 65, "a%2Fb", "a%5Cb"])
def test_bad_job_ids_never_reach_the_filesystem(client, tmp_path, job_id):
    r = client.get(f"{PREFIX}/assessments/{job_id}/analysis")
    assert r.status_code in (404, 422)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Loopback-only gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("peer", ["10.0.0.5", "192.168.1.9", "203.0.113.7", "::2", "testclient"])
def test_non_local_peers_get_403_on_every_route(tmp_path, monkeypatch, peer):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=(peer, 50000))
    for method, path in (("get", "/health"), ("get", f"{PREFIX}/assessments/x/status"),
                         ("get", "/docs"), ("get", "/openapi.json"),
                         ("post", f"{PREFIX}/assessments/from-collection")):
        r = getattr(remote, method)(path)
        assert r.status_code == 403, (peer, path)
        assert "no authentication" in r.json()["detail"]
    assert list(tmp_path.iterdir()) == []


def test_a_request_with_no_identifiable_peer_is_refused(tmp_path, monkeypatch):
    import asyncio

    from fastapi import Request

    from src.api.main import local_only_gate

    async def go():
        scope = {"type": "http", "method": "GET", "path": "/health", "headers": [],
                 "query_string": b"", "client": None}
        called = False

        async def call_next(_req):
            nonlocal called
            called = True

        resp = await local_only_gate(Request(scope), call_next)
        return resp.status_code, called

    status, called = asyncio.run(go())
    assert status == 403 and not called


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("127.5.5.5", True), ("::1", True), ("::ffff:127.0.0.1", True),
    ("0.0.0.0", False), ("::", False), ("10.1.1.1", False), ("", False), (None, False),  # nosec B104 - test input, nothing binds
    ("localhost", False), ("not-an-ip", False),
])
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected


def test_default_bind_is_loopback(monkeypatch):
    monkeypatch.delenv("API_HOST", raising=False)
    monkeypatch.delenv("API_PORT", raising=False)
    assert resolve_bind() == ("127.0.0.1", 8000)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.4", "example.com"])  # nosec B104 - test input, nothing binds
def test_non_loopback_bind_is_refused(monkeypatch, host):
    monkeypatch.setenv("API_HOST", host)
    with pytest.raises(SystemExit, match="Refusing to listen"):
        resolve_bind()


@pytest.mark.parametrize("port", ["0", "70000", "abc", "-1"])
def test_bad_port_is_refused(monkeypatch, port):
    monkeypatch.delenv("API_HOST", raising=False)
    monkeypatch.setenv("API_PORT", port)
    with pytest.raises(SystemExit):
        resolve_bind()


def test_cors_allows_one_origin_and_no_credentials(client):
    r = client.options(f"{PREFIX}/assessments/x/status", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert "access-control-allow-credentials" not in r.headers
    other = client.options(f"{PREFIX}/assessments/x/status", headers={
        "Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in other.headers
