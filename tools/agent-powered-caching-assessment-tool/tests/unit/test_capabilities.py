"""GET /capabilities. Spec: ux-api UX-16.

The point of the route is that a UX can trust it, so most of these tests compare it with
what the routes themselves enforce (read from the OpenAPI document).
"""
import boto3
import pytest
from fastapi.testclient import TestClient

from src.api import common
from src.api.main import app
from src.orchestrator.local_orchestrator import REGION_PATTERN
from src.tools.analysis.cache_sizing import SizingParameters
from src.version import __version__

PREFIX = "/api/v1"


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def caps(client) -> dict:
    r = client.get(f"{PREFIX}/capabilities")
    assert r.status_code == 200, r.text
    return r.json()


def _param(path: str, method: str, name: str) -> dict:
    op = app.openapi()["paths"][path][method]
    return next(p for p in op["parameters"] if p["name"] == name)["schema"]


def test_it_reports_the_release_and_the_authentication_mode(caps):
    assert caps["version"] == __version__
    assert "none" in caps["authentication"]
    assert caps["job_states"] == ["queued", "running", "complete", "failed", "cancelled"]


def test_the_phases_are_in_run_order_and_sizing_is_the_optional_one(caps):
    assert [p["phase"] for p in caps["phases"]] == ["collect", "analyze", "size", "report"]
    assert [p["phase"] for p in caps["phases"] if p["optional"]] == ["size"]
    assert all(p["description"] and p["label"] for p in caps["phases"])


def test_the_option_defaults_are_the_submit_defaults(caps):
    opts = {o["name"]: o["default"] for o in caps["options"]}
    assert opts == {"use_llm": False, "size": True, "price": True, "region": "us-east-1",
                    "model_id": caps["model"]["default"], "require_llm": False}
    # The same defaults the submit route declares.
    spec = app.openapi()["paths"][f"{PREFIX}/assessments"]["post"]
    for name in ("size", "price", "use_llm", "require_llm"):
        schema = next(p for p in spec["parameters"] if p["name"] == name)["schema"]
        assert schema["default"] is opts[name]


def test_the_region_rule_is_the_one_the_routes_enforce(caps):
    assert caps["region"]["pattern"] == REGION_PATTERN.pattern
    schema = _param(f"{PREFIX}/assessments", "post", "region")
    assert schema["pattern"] == caps["region"]["pattern"]
    assert schema["maxLength"] == caps["region"]["max_length"]
    assert "shape only" in caps["region"]["note"].lower()


def test_the_limits_are_the_ones_the_routes_enforce(caps):
    lim = caps["limits"]
    listing = _param(f"{PREFIX}/assessments", "get", "limit")
    assert (listing["default"], listing["maximum"]) == (
        lim["list_default_limit"], lim["list_max_limit"])
    for path in ("queries", "tables"):
        page = _param(f"{PREFIX}/assessments/{{job_id}}/report-data/{path}", "get", "limit")
        assert (page["default"], page["maximum"]) == (
            lim["report_page_default_limit"], lim["report_page_max_limit"])
    label = _param(f"{PREFIX}/assessments", "post", "label")
    assert any(b.get("maxLength") == lim["label_max_length"] for b in label["anyOf"])
    job_id = _param(f"{PREFIX}/assessments/{{job_id}}/status", "get", "job_id")
    assert job_id["pattern"] == lim["job_id_pattern"]
    assert job_id["maxLength"] == lim["job_id_max_length"]


def test_the_collection_limits_come_from_the_collector_contract(caps):
    lim = caps["limits"]
    assert (lim["collection_timeout_minutes_min"], lim["collection_timeout_minutes_max"]) == (10, 720)
    assert lim["query_pattern_limit_max"] == 5000
    assert lim["max_concurrent_preflights"] == common.MAX_CONCURRENT_PREFLIGHTS
    assert lim["max_concurrent_priced_what_ifs"] == common.MAX_CONCURRENT_PRICED_WHAT_IFS


def test_the_database_limit_is_the_one_the_store_enforces(caps):
    from src.orchestrator.database_store import MAX_DATABASES
    assert caps["limits"]["max_databases"] == MAX_DATABASES


def test_the_sizing_parameters_are_exactly_the_ones_what_if_accepts(caps):
    schema = app.openapi()["components"]["schemas"]["SizingOverrides"]["properties"]
    defaults = SizingParameters().as_dict()
    by_name = {p["name"]: p for p in caps["sizing_parameters"]}
    assert set(by_name) == set(schema) == set(defaults)
    for name, p in by_name.items():
        assert p["default"] == defaults[name]
        assert p["minimum"] <= p["default"] <= p["maximum"], name
        assert p["description"], name
        field = next(b for b in schema[name]["anyOf"] if b.get("type") != "null")
        assert field["minimum"] == p["minimum"] and field["maximum"] == p["maximum"], name
        assert field["type"] == p["type"], name


def test_every_optional_feature_is_listed(caps):
    features = dict(caps["features"])
    assert isinstance(features.pop("model_framework"), bool)
    assert isinstance(features.pop("secrets_manager"), bool)
    assert features == {"list_and_delete": True, "cancel": True, "preflight": True,
                        "what_if_sizing": True, "report_data": True, "rerun": True,
                        "purge": True, "component_runs": True}


def test_the_model_rule_is_the_one_the_routes_enforce(caps):
    from src.contracts.models import DEFAULT_MODEL_ID, MODEL_ID_PATTERN
    rule = caps["model"]
    assert rule["default"] == DEFAULT_MODEL_ID == rule["suggestions"][0]
    assert rule["pattern"] == MODEL_ID_PATTERN.pattern
    schema = _param(f"{PREFIX}/assessments", "post", "model_id")
    assert any(b.get("pattern") == rule["pattern"] for b in schema["anyOf"])


def test_the_notes_say_what_is_not_checked(caps):
    text = " ".join(caps["notes"]).lower()
    assert "bedrock" in text and "phase boundary" in text and "database host" in text


def test_it_makes_no_aws_call(client, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("capabilities must not call AWS")

    monkeypatch.setattr(boto3, "client", refuse)
    monkeypatch.setattr(boto3.Session, "client", refuse)
    assert client.get(f"{PREFIX}/capabilities").status_code == 200


def test_it_is_behind_the_loopback_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.7", 50000))
    assert remote.get(f"{PREFIX}/capabilities").status_code == 403
