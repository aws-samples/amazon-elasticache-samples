"""Model choice, the model-not-used warning, require_llm, and Secrets Manager passwords."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.agents.analysis import handler_v2
from src.agents.collector import preflight as pf
from src.agents.collector.target_checks import target_problems
from src.api import common
from src.api.main import app
from src.contracts.collector_input import CollectorInput, LiveConnectionConfig
from src.orchestrator.local_orchestrator import LocalOrchestrator, ModelRequired
from src.tools.aws import secrets

FIXTURES = Path(__file__).parent.parent / "fixtures"
ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:apcat/retail-AbCdEf"


@pytest.fixture
def collection() -> dict:
    return json.loads((FIXTURES / "strong_candidate_collection.json").read_text())


@pytest.fixture
def model_down(monkeypatch):
    """The analysis as it comes back when the model was asked for and could not be used."""
    real = handler_v2.run_analysis_v2

    def fake(collector_output, **kw):
        out, trace = real(collector_output, **{**kw, "use_llm": False})
        out["fallback_reason"] = "Bedrock call failed: AccessDeniedException"
        return out, trace

    monkeypatch.setattr(handler_v2, "run_analysis_v2", fake)


# --- The model-not-used warning and require_llm ------------------------------------------


def test_a_model_requested_but_not_used_completes_with_a_warning(tmp_path, collection, model_down):
    orch = LocalOrchestrator(str(tmp_path), use_llm=True, size=False, price=False)
    orch.run_from_collection("j1", collection)
    st = orch.get_state("j1")
    assert st["status"] == "complete"
    assert st["warnings"] == ["analyze: the model was requested but not used: "
                              "Bedrock call failed: AccessDeniedException"]


def test_turning_the_model_off_is_not_a_warning(tmp_path, collection):
    orch = LocalOrchestrator(str(tmp_path), use_llm=False, size=False, price=False)
    orch.run_from_collection("j2", collection)
    st = orch.get_state("j2")
    assert st["status"] == "complete" and not st.get("warnings")


def test_require_llm_fails_the_job_and_saves_no_analysis(tmp_path, collection, model_down):
    orch = LocalOrchestrator(str(tmp_path), use_llm=True, require_llm=True, size=False,
                             price=False)
    with pytest.raises(ModelRequired):
        orch.run_from_collection("j3", collection)
    st = orch.get_state("j3")
    assert st["status"] == "failed"
    assert "the model was required but not used" in st["error"]
    assert not (tmp_path / "j3" / "analyze" / "analysis.json").exists()


def test_the_record_keeps_the_model_choice(tmp_path, collection):
    orch = LocalOrchestrator(str(tmp_path), use_llm=False, size=False, price=False,
                             model_id="global.anthropic.claude-sonnet-5")
    orch.run_from_collection("j4", collection)
    assert orch.store.get("j4").options.model_id == "global.anthropic.claude-sonnet-5"


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


def test_require_llm_without_use_llm_is_refused(client, collection):
    r = client.post("/api/v1/assessments/from-collection",
                    json={"collector_output": collection, "require_llm": True, "price": False})
    assert r.status_code == 422 and r.json()["code"] == "invalid_options"


def test_a_model_id_must_be_shaped_like_one(client, collection):
    r = client.post("/api/v1/assessments/from-collection",
                    json={"collector_output": collection, "model_id": "rm -rf /; x", "price": False})
    assert r.status_code == 422


def test_the_model_choice_reaches_the_record(client, collection):
    r = client.post("/api/v1/assessments/from-collection",
                    json={"collector_output": collection, "price": False,
                          "model_id": "global.anthropic.claude-sonnet-4-6"})
    assert r.status_code == 200
    job = r.json()["job_id"]
    rec = client.get("/api/v1/assessments", params={"q": job}).json()["items"][0]
    assert rec["options"]["model_id"] == "global.anthropic.claude-sonnet-4-6"


# --- Secrets Manager ---------------------------------------------------------------------


def live(**over) -> dict:
    cfg = {"ssh_target": "db-host", "db_user": "retail_app", **over}
    return {"job_id": "x", "cluster_endpoint": "127.0.0.1", "database_name": "retail",
            "live_config": cfg}


def test_exactly_one_password_source():
    LiveConnectionConfig(ssh_target="h", db_user="u", password_file="/p")  # nosec B106 - test value, not a password
    LiveConnectionConfig(ssh_target="h", db_user="u", password_secret_arn=ARN)
    for extra in ({}, {"password_file": "/p", "password_secret_arn": ARN}):  # nosec B105 - test value, not a password
        with pytest.raises(ValidationError):
            LiveConnectionConfig(ssh_target="h", db_user="u", **extra)


def test_a_secret_is_refused_unless_the_operator_allows_it(monkeypatch):
    monkeypatch.delenv("SECRET_ARN_ALLOWED_PREFIXES", raising=False)
    inp = CollectorInput.model_validate(live(password_secret_arn=ARN))
    assert any("does not read database passwords" in p for p in target_problems(inp))
    monkeypatch.setenv("SECRET_ARN_ALLOWED_PREFIXES", "arn:aws:secretsmanager:us-east-1:999999999999:secret:")
    assert any("may not read that secret" in p for p in target_problems(inp))
    monkeypatch.setenv("SECRET_ARN_ALLOWED_PREFIXES", "arn:aws:secretsmanager:us-east-1:123456789012:secret:apcat/")
    assert target_problems(inp) == []


def test_a_secret_arn_must_be_shaped_like_one(monkeypatch):
    monkeypatch.setenv("SECRET_ARN_ALLOWED_PREFIXES", "arn:")
    inp = CollectorInput.model_validate(live(password_secret_arn="arn:aws:s3:::bucket"))  # nosec B106 - test value, not a password
    assert "live_config.password_secret_arn is not in an accepted form" in target_problems(inp)


class FakeSecrets:
    def __init__(self, value=None, fail=False):
        self.value, self.fail, self.asked = value, fail, []

    def __call__(self, region):
        self.region = region
        return self

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3's own argument name
        self.asked.append(SecretId)
        if self.fail:
            raise RuntimeError("AccessDenied for arn:aws:iam::123456789012:role/Secret")
        return {"SecretString": self.value}


@pytest.mark.parametrize("stored, password", [
    ("plain-pw", "plain-pw"),
    (json.dumps({"username": "u", "password": "json-pw"}), "json-pw"),  # nosec B105 - test value, not a password
])
def test_reading_the_password(monkeypatch, stored, password):
    monkeypatch.setenv("SECRET_ARN_ALLOWED_PREFIXES", "arn:aws:secretsmanager:us-east-1:123456789012:secret:")
    fake = FakeSecrets(stored)
    assert secrets.read_password(ARN, client_for=fake) == password
    assert fake.region == "us-east-1" and fake.asked == [ARN]


@pytest.mark.parametrize("fake, kind", [
    (FakeSecrets(fail=True), "unreadable"),
    (FakeSecrets(json.dumps({"username": "u"})), "no_password"),
    (FakeSecrets("two\nlines"), "no_password"),
    (FakeSecrets(""), "no_password"),
])
def test_a_secret_that_cannot_be_used(monkeypatch, fake, kind):
    monkeypatch.setenv("SECRET_ARN_ALLOWED_PREFIXES", "arn:")
    with pytest.raises(secrets.SecretError) as e:
        secrets.read_password(ARN, client_for=fake)
    assert e.value.kind == kind
    assert "123456789012:role" not in str(e.value)


def test_no_aws_call_for_a_secret_that_is_not_allowed(monkeypatch):
    monkeypatch.delenv("SECRET_ARN_ALLOWED_PREFIXES", raising=False)
    fake = FakeSecrets("pw")
    with pytest.raises(secrets.SecretError):
        secrets.read_password(ARN, client_for=fake)
    assert fake.asked == []


def test_preflight_reports_the_secret_first_in_fixed_text(monkeypatch):
    monkeypatch.setenv("SECRET_ARN_ALLOWED_PREFIXES", "arn:")
    monkeypatch.setattr(secrets, "_client", FakeSecrets(fail=True))
    result = pf.run_preflight(CollectorInput.model_validate(live(password_secret_arn=ARN)))
    assert not result.ok
    first = result.checks[0]
    assert (first.name, first.status) == ("secret", "fail")
    assert first.message == secrets.MESSAGES["unreadable"][0]
    assert [c.status for c in result.checks[1:]] == ["skipped"] * 4
    assert "123456789012:role" not in result.model_dump_json()


def test_preflight_hands_the_password_to_the_executor(monkeypatch):
    monkeypatch.setenv("SECRET_ARN_ALLOWED_PREFIXES", "arn:")
    monkeypatch.setattr(secrets, "_client", FakeSecrets("s3cret"))
    seen = {}

    class Stop(Exception):
        pass

    def built(inp, password=None):
        seen["password"] = password
        raise Stop("ssh: connect to host db-host port 22: Connection refused")

    monkeypatch.setattr(pf, "executor_for", built)
    result = pf.run_preflight(CollectorInput.model_validate(live(password_secret_arn=ARN)))
    assert seen["password"] == "s3cret"
    assert [c.name for c in result.checks][:2] == ["secret", "ssh"]
    assert result.checks[0].status == "pass"
    assert "s3cret" not in result.model_dump_json()


def test_the_secret_arn_is_never_echoed_in_a_422(client, monkeypatch):
    monkeypatch.delenv("SECRET_ARN_ALLOWED_PREFIXES", raising=False)
    r = client.post("/api/v1/assessments/preflight", json=live(password_secret_arn=ARN))
    assert r.status_code == 422 and r.json()["code"] == "invalid_target"
    assert ARN not in r.text


@pytest.mark.parametrize("model, takes", [
    ("global.anthropic.claude-sonnet-5-5", False), ("global.anthropic.claude-sonnet-5", False),
    ("us.anthropic.claude-opus-5", False), ("global.anthropic.claude-sonnet-4-6", True),
    ("anthropic.claude-3-5-sonnet-20241022-v2:0", True),
])
def test_temperature_is_sent_only_to_models_that_take_it(model, takes):
    from src.contracts.models import takes_temperature
    assert takes_temperature(model) is takes


def test_models_that_reject_a_forced_tool_choice_get_auto():
    import asyncio

    from src.agents.analysis.cache_candidacy_agent_v2 import _auto_choice_model
    from src.contracts.models import forces_tool_choice
    assert not forces_tool_choice("global.anthropic.claude-sonnet-5-5")
    assert forces_tool_choice("global.anthropic.claude-sonnet-4-6")

    seen = {}

    class Base:
        def stream(self, messages, tool_specs=None, system_prompt=None, *, tool_choice=None, **kw):
            seen["choice"] = tool_choice
            return iter(())

    model = _auto_choice_model(Base)()
    model.stream([], [], None, tool_choice={"any": {}})
    assert seen["choice"] == {"auto": {}}
    model.stream([], [], None, tool_choice=None)
    assert seen["choice"] is None
    assert asyncio  # imported for parity with async callers


def test_nested_objects_sent_as_json_text_are_decoded():
    from pydantic import BaseModel

    from src.agents.analysis.cache_candidacy_agent_v2 import _lenient

    class Inner(BaseModel):
        a: int

    class Outer(BaseModel):
        inner: Inner
        items: list[Inner]
        note: str

    m = _lenient(Outer).model_validate({"inner": '{"a": 1}', "items": '[{"a": 2}]', "note": "{not json"})
    assert m.inner.a == 1 and m.items[0].a == 2 and m.note == "{not json"
    assert isinstance(m, Outer)
