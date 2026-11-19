"""The chat agent route and its turn loop, with a fake model. No test reaches AWS."""

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import agent_tools, common
from src.api.main import app
from src.api.routes import agent
from src.contracts.agent_chat import AgentChatRequest

FIXTURES = Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# A fake ConverseStream: each call pops the next scripted reply.
# ---------------------------------------------------------------------------


def text_reply(text: str) -> list[dict]:
    return [{"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": text[:5]}}},
            {"contentBlockDelta": {"delta": {"text": text[5:]}}},
            {"contentBlockStop": {}},
            {"messageStop": {"stopReason": "end_turn"}}]


def tool_reply(*calls: tuple[str, str, dict], text: str = "") -> list[dict]:
    events: list[dict] = [{"messageStart": {"role": "assistant"}}]
    if text:
        events += [{"contentBlockDelta": {"delta": {"text": text}}}, {"contentBlockStop": {}}]
    for use_id, name, args in calls:
        raw = json.dumps(args)
        events += [{"contentBlockStart": {"start": {"toolUse": {"toolUseId": use_id, "name": name}}}},
                   {"contentBlockDelta": {"delta": {"toolUse": {"input": raw[:7]}}}},
                   {"contentBlockDelta": {"delta": {"toolUse": {"input": raw[7:]}}}},
                   {"contentBlockStop": {}}]
    return events + [{"messageStop": {"stopReason": "tool_use"}}]


class FakeModel:
    def __init__(self, *replies: list[dict], fail: Exception | None = None):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.fail = fail

    def converse_stream(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if self.fail:
            raise self.fail
        return {"stream": iter(self.replies.pop(0))}


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def job(client) -> str:
    collection = json.loads((FIXTURES / "strong_candidate_collection.json").read_text())
    r = client.post("/api/v1/assessments/from-collection",
                    json={"collector_output": collection, "price": False})
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def use_model(monkeypatch, model: FakeModel) -> FakeModel:
    monkeypatch.setattr(agent, "bedrock_client", lambda: model)
    return model


def say(text: str, **extra) -> dict:
    return {"messages": [{"role": "user", "content": [{"type": "text", "text": text}]}], **extra}


def events(r) -> list[dict]:
    out = []
    for chunk in r.text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.splitlines() if ": " in line)
        if "data" in lines:
            e = json.loads(lines["data"])
            assert lines["event"] == e["type"]
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def test_a_text_reply_streams_start_text_message_done(client, monkeypatch):
    model = use_model(monkeypatch, FakeModel(text_reply("Hello there")))
    r = client.post("/api/v1/agent/chat", json=say("hi"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["X-Request-ID"]
    ev = events(r)
    assert [e["type"] for e in ev] == ["start", "text", "text", "message", "done"]
    assert "".join(e["text"] for e in ev if e["type"] == "text") == "Hello there"
    assert ev[-2]["message"] == {"role": "assistant",
                                 "content": [{"type": "text", "text": "Hello there"}]}
    assert ev[-1]["reason"] == "end_turn"
    sent = model.calls[0]
    assert {t["toolSpec"]["name"] for t in sent["toolConfig"]["tools"]} == set(agent_tools.BY_NAME)
    assert sent["messages"] == [{"role": "user", "content": [{"text": "hi"}]}]


def test_a_read_tool_runs_on_the_server_and_the_turn_goes_on(client, monkeypatch, job):
    model = use_model(monkeypatch, FakeModel(
        tool_reply(("t1", "get_status", {"job_id": job})), text_reply("It is complete.")))
    ev = events(client.post("/api/v1/agent/chat", json=say("how is it going?")))
    call = next(e for e in ev if e["type"] == "tool_call")
    assert call == {"type": "tool_call", "tool_use_id": "t1", "name": "get_status",
                    "input": {"job_id": job}, "executor": "server"}
    result = next(e for e in ev if e["type"] == "tool_result")
    assert result["status"] == "success" and result["content"]["status"] == "complete"
    # The same answer GET /status gives.
    assert result["content"]["job_id"] == job
    second = model.calls[1]["messages"]
    assert second[-1]["role"] == "user"
    assert second[-1]["content"][0]["toolResult"]["toolUseId"] == "t1"
    assert ev[-1]["reason"] == "end_turn"


def test_a_state_changing_tool_never_runs_on_the_server(client, monkeypatch, job):
    model = use_model(monkeypatch, FakeModel(
        tool_reply(("t1", "get_status", {"job_id": job}),
                   ("t2", "delete_assessment", {"job_id": job}), text="I will delete it.")))
    ev = events(client.post("/api/v1/agent/chat", json=say("delete it")))
    action = next(e for e in ev if e["type"] == "action_required")
    assert action == {"type": "action_required", "tool_use_id": "t2",
                      "name": "delete_assessment", "input": {"job_id": job}, "kind": "confirm"}
    assert ev[-1]["reason"] == "action_required"
    assert len(model.calls) == 1  # stopped: waits for the user
    # The job is still there.
    assert client.get(f"/api/v1/assessments/{job}/status").status_code == 200
    # The server tool's result is in the stream for the browser to send back.
    assert any(e["type"] == "tool_result" and e["tool_use_id"] == "t1" for e in ev)


def test_a_confirmed_result_comes_back_with_the_next_turn(client, monkeypatch, job):
    model = use_model(monkeypatch, FakeModel(text_reply("Deleted.")))
    body = {"messages": [
        {"role": "user", "content": [{"type": "text", "text": "delete it"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "tool_use_id": "t2", "name": "delete_assessment",
             "input": {"job_id": job}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "status": "declined", "content": {}}]},
    ]}
    ev = events(client.post("/api/v1/agent/chat", json=body))
    assert ev[-1]["reason"] == "end_turn"
    result = model.calls[0]["messages"][-1]["content"][0]["toolResult"]
    assert result["status"] == "error" and result["content"][0]["json"]["declined"] is True


def test_the_connection_form_is_a_client_tool(client, monkeypatch):
    use_model(monkeypatch, FakeModel(tool_reply(("t1", "request_connection_details", {}))))
    ev = events(client.post("/api/v1/agent/chat", json=say("assess my database")))
    assert next(e for e in ev if e["type"] == "action_required")["kind"] == "form"


def test_bad_tool_input_and_unknown_tools_go_back_to_the_model(client, monkeypatch):
    model = use_model(monkeypatch, FakeModel(
        tool_reply(("t1", "get_status", {"job_id": "../etc"}), ("t2", "drop_table", {})),
        text_reply("Sorry.")))
    ev = events(client.post("/api/v1/agent/chat", json=say("x")))
    res = {e["tool_use_id"]: e for e in ev if e["type"] == "tool_result"}
    assert res["t1"]["status"] == "error" and res["t1"]["content"]["code"] == "invalid_tool_input"
    assert res["t2"]["content"]["code"] == "unknown_tool"
    assert next(e for e in ev if e["type"] == "tool_call" and e["tool_use_id"] == "t2")["executor"] == "unknown"
    assert len(model.calls) == 2


def test_an_api_error_from_a_tool_is_its_code(client, monkeypatch):
    use_model(monkeypatch, FakeModel(tool_reply(("t1", "get_status", {"job_id": "nope"})),
                                     text_reply("No such job.")))
    ev = events(client.post("/api/v1/agent/chat", json=say("status of nope")))
    res = next(e for e in ev if e["type"] == "tool_result")
    assert res["content"]["code"] == "job_not_found"


def test_the_tool_rounds_are_capped(client, monkeypatch, job):
    from src.contracts.agent_chat import MAX_TOOL_ROUNDS
    replies = [tool_reply((f"t{i}", "get_status", {"job_id": job})) for i in range(MAX_TOOL_ROUNDS)]
    model = use_model(monkeypatch, FakeModel(*replies))
    ev = events(client.post("/api/v1/agent/chat", json=say("loop")))
    assert ev[-1]["reason"] == "max_tool_rounds"
    assert len(model.calls) == MAX_TOOL_ROUNDS


def test_a_model_failure_is_fixed_text(client, monkeypatch):
    use_model(monkeypatch, FakeModel(fail=RuntimeError("arn:aws:iam::123456789012:role/Secret")))
    r = client.post("/api/v1/agent/chat", json=say("hi"))
    assert r.status_code == 200
    ev = events(r)
    err = next(e for e in ev if e["type"] == "error")
    assert err["code"] == "model_unavailable"
    assert "123456789012" not in r.text and "Secret" not in r.text
    assert ev[-1] == {"type": "done", "reason": "error"}


def test_no_client_at_all_is_the_same_error(client, monkeypatch):
    def broken():
        raise RuntimeError("no credentials")
    monkeypatch.setattr(agent, "bedrock_client", broken)
    ev = events(client.post("/api/v1/agent/chat", json=say("hi")))
    assert [e["type"] for e in ev] == ["start", "error", "done"]


def test_the_conversation_must_end_with_the_user(client, monkeypatch):
    use_model(monkeypatch, FakeModel())
    body = {"messages": [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]}
    r = client.post("/api/v1/agent/chat", json=body)
    assert r.status_code == 422 and r.json()["code"] == "invalid_conversation"
    assert r.headers["X-Request-ID"]


def test_a_malformed_body_is_the_usual_422(client):
    r = client.post("/api/v1/agent/chat", json={"messages": []})
    assert r.status_code == 422 and r.json()["code"]


def test_too_many_turns_is_429_and_slots_come_back(client, monkeypatch):
    use_model(monkeypatch, FakeModel(*[text_reply("ok") for _ in range(3)]))
    held = [agent._slots.acquire(blocking=False) for _ in range(common.MAX_CONCURRENT_AGENT_TURNS)]
    try:
        r = client.post("/api/v1/agent/chat", json=say("hi"))
        assert r.status_code == 429 and r.json()["code"] == "agent_busy"
        assert r.headers["Retry-After"]
    finally:
        for ok in held:
            if ok:
                agent._slots.release()
    for _ in range(2):  # a finished stream gives its slot back
        assert client.post("/api/v1/agent/chat", json=say("hi")).status_code == 200


def test_the_audit_log_records_counts_only(client, monkeypatch, tmp_path):
    log = tmp_path / "audit.log"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(log))
    use_model(monkeypatch, FakeModel(text_reply("ok")))
    client.post("/api/v1/agent/chat", json=say("SELECT secret FROM t WHERE x = 'literal'"))
    text = log.read_text()
    assert '"event":"agent_turn"' in text and "literal" not in text


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


def test_every_client_tool_is_confirm_or_form_and_every_server_tool_is_read_or_render():
    for t in agent_tools.TOOLS:
        if t.executor == "client":
            assert t.kind in ("confirm", "form"), t.name
        else:
            assert t.kind in ("read", "render"), t.name
    changing = {"run_preflight", "start_assessment", "cancel_assessment", "rerun_assessment",
                "delete_assessment", "whatif_sizing", "check_database", "run_database_assessment"}
    assert {t.name for t in agent_tools.TOOLS if t.kind == "confirm"} == changing


def test_tool_specs_have_no_refs():
    assert "$ref" not in json.dumps(agent_tools.tool_specs())


def test_ask_report_carries_the_item_and_how_the_run_was_judged(client, job):
    q = client.get(f"/api/v1/assessments/{job}/report-data/queries?limit=1").json()["items"][0]
    status, pack = agent_tools.run_server_tool(
        agent_tools.BY_NAME["ask_report"], {"job_id": job, "kind": "query", "item_id": q["queryid"]})
    assert status == "success"
    assert pack["item"]["queryid"] == q["queryid"] and "sql" not in pack["item"]
    assert pack["item"]["metrics"] == q["metrics"]
    assert pack["run"]["interpretation_mode"] == "deterministic_fallback"
    assert pack["bands"]


def test_ask_report_for_a_table_and_the_workload(client, job):
    t = client.get(f"/api/v1/assessments/{job}/report-data/tables?limit=1").json()["items"][0]
    ask = agent_tools.BY_NAME["ask_report"]
    s, pack = agent_tools.run_server_tool(ask, {"job_id": job, "kind": "table", "item_id": t["table_id"]})
    assert s == "success" and pack["item"]["table_id"] == t["table_id"]
    s, pack = agent_tools.run_server_tool(ask, {"job_id": job, "kind": "workload"})
    assert s == "success" and pack["report"]["headline"]["title"]
    s, err = agent_tools.run_server_tool(ask, {"job_id": job, "kind": "query"})
    assert s == "error" and err["code"] == "invalid_tool_input"
    s, err = agent_tools.run_server_tool(ask, {"job_id": job, "kind": "table", "item_id": "x.y"})
    assert err["code"] == "item_not_found"


def test_the_read_tools_answer(client, job):
    run = agent_tools.run_server_tool
    by = agent_tools.BY_NAME
    s, jobs = run(by["list_assessments"], {})
    assert s == "success" and jobs["items"][0]["job_id"] == job
    s, rep = run(by["get_report"], {"job_id": job})
    assert s == "success" and rep["counts"]
    s, page = run(by["list_report_items"], {"job_id": job, "kind": "table", "limit": 2})
    assert s == "success" and len(page["items"]) <= 2
    s, caps = run(by["get_capabilities"], {})
    assert s == "success" and caps["job_states"]
    s, summ = run(by["get_summary"], {"job_id": job})
    assert s == "success" and summ["job_id"] == job
    s, shown = run(by["open_report"], {"job_id": job, "view": "html"})
    assert s == "success" and shown["view"] == "html"


def test_request_schema_round_trips():
    req = AgentChatRequest.model_validate(say("hi", assessment_id="job-1",
                                              focus={"kind": "table", "id": "shop.orders"}))
    assert req.focus and req.focus.id == "shop.orders"


# ---------------------------------------------------------------------------
# Readiness, model choice
# ---------------------------------------------------------------------------


def test_report_tools_say_not_ready_instead_of_failing(client, monkeypatch):
    from src.contracts.api_models import JobRecord
    from src.orchestrator.job_store import LocalJobStore
    store = LocalJobStore(common.ARTIFACT_ROOT)
    store.create(JobRecord(job_id="busy-1", created_at="2026-10-23T00:00:00+00:00"))
    st = store.read_state("busy-1")
    st.update(status="running", current_phase="analyze")
    store.write_state("busy-1", st)
    for name, args in (("get_report", {}), ("get_summary", {}), ("get_sizing", {}),
                       ("ask_report", {"kind": "workload"}),
                       ("list_report_items", {"kind": "table"}), ("open_report", {})):
        status, out = agent_tools.run_server_tool(agent_tools.BY_NAME[name], {"job_id": "busy-1", **args})
        assert status == "success", name
        assert out["ready"] is False and out["status"] == "running", name
        assert "analyze phase" in out["detail"], name


def test_a_finished_job_without_sizing_says_so(client, job):
    # The fixture job ran without pricing but with sizing; force a job that has no size.
    status, out = agent_tools.run_server_tool(agent_tools.BY_NAME["open_report"],
                                              {"job_id": job, "view": "full"})
    assert status == "success" and out.get("shown") is True


def test_the_chat_turn_uses_the_model_the_user_chose(client, monkeypatch):
    model = use_model(monkeypatch, FakeModel(text_reply("ok")))
    ev = events(client.post("/api/v1/agent/chat",
                            json=say("hi", model_id="global.anthropic.claude-sonnet-5")))
    assert ev[0]["model_id"] == "global.anthropic.claude-sonnet-5"
    assert model.calls[0]["modelId"] == "global.anthropic.claude-sonnet-5"


def test_the_default_chat_model_is_sonnet_5_5(client, monkeypatch):
    model = use_model(monkeypatch, FakeModel(text_reply("ok")))
    events(client.post("/api/v1/agent/chat", json=say("hi")))
    assert model.calls[0]["modelId"] == "global.anthropic.claude-sonnet-5-5"


def test_a_bad_model_id_is_422(client):
    r = client.post("/api/v1/agent/chat", json=say("hi", model_id="x y"))
    assert r.status_code == 422


def test_start_assessment_takes_the_model_options():
    props = agent_tools.BY_NAME["start_assessment"].input_model.model_json_schema()["properties"]
    assert {"model_id", "require_llm", "use_llm"} <= set(props)


# ---------------------------------------------------------------------------
# Saved databases and discovery (DB-11)
# ---------------------------------------------------------------------------

DB_CONN = {"ssh_target": "ec2-user@db-host.example.com", "identity_file": "apcat_live_key",
           "db_user": "apcat_reader", "password_file": "/home/ec2-user/.apcat-reader-password",  # nosec B105 - test value, not a password
           "cluster_endpoint": "10.0.0.5", "database_name": "marketplace"}
# Every connection value, as the model must never see it.
SECRETS = ("ec2-user", "db-host.example.com", "apcat_live_key", "apcat_reader",
           "/home/ec2-user/.apcat-reader-password", "10.0.0.5")


def save_db(client, **extra) -> dict:
    r = client.post("/api/v1/databases", json={"connection": DB_CONN, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def test_database_tools_never_show_the_connection(client):
    default = save_db(client)  # the default label names the SSH host
    named = save_db(client, label="Orders primary",
                    connection={**DB_CONN, "database_name": "orders"})
    run, by = agent_tools.run_server_tool, agent_tools.BY_NAME
    s, page = run(by["list_databases"], {})
    assert s == "success" and page["total"] == 2
    s, one = run(by["get_database"], {"database_id": default["database_id"]})
    assert s == "success" and one["database_name"] == "marketplace"
    assert one["label"] is None and one["label_withheld"] is True
    assert one["readiness"]["status"] == "unchecked" and one["assessments"] == 0
    kept = next(i for i in page["items"] if i["database_id"] == named["database_id"])
    assert kept["label"] == "Orders primary" and kept["label_withheld"] is False
    text = json.dumps([page, one])
    for value in SECRETS:
        assert value not in text, value
    assert "connection" not in one


def test_a_label_holding_any_connection_value_is_withheld():
    from src.contracts.databases import DatabaseConnection
    conn = DatabaseConnection(**DB_CONN)
    assert agent_tools.model_label("Marketplace", conn) == "Marketplace"
    for label in ("marketplace on DB-HOST.example.com", "uses apcat_reader", "at 10.0.0.5"):
        assert agent_tools.model_label(label, conn) is None, label
    local = DatabaseConnection(**{**DB_CONN, "cluster_endpoint": "127.0.0.1"})
    assert agent_tools.model_label("bench on 127.0.0.1", local) == "bench on 127.0.0.1"


def test_get_database_errors(client):
    run, by = agent_tools.run_server_tool, agent_tools.BY_NAME
    s, err = run(by["get_database"], {"database_id": "../etc"})
    assert s == "error" and err["code"] == "invalid_tool_input"
    s, err = run(by["get_database"], {"database_id": "db-0123456789ab"})
    assert s == "error" and err["code"] == "database_not_found"


def test_discover_databases_is_local_only_and_hides_hosts(client, monkeypatch):
    from src.api.routes import discovery as discovery_route
    from src.contracts.discovery import DiscoveredServer, DiscoveryResult, SourceReport
    seen: list[dict] = []

    def fake(**kwargs):
        seen.append(kwargs)
        return DiscoveryResult(
            discovered_at="2026-10-30T00:00:00+00:00",
            servers=[DiscoveredServer(id="ssh_config-1", source="ssh_config", kind="ssh",
                                      label="SSH host prod (db-host.example.com)",
                                      ssh_target="prod", ssh_user="ec2-user",
                                      host="db-host.example.com", port=5432),
                     DiscoveredServer(id="earlier-1", source="earlier_assessment", kind="ssh",
                                      label="marketplace at 10.0.0.5:5432", host="10.0.0.5",
                                      database="marketplace", runs=2, last_verdict="cache")],
            sources=[SourceReport(source="ssh_config", status="ok", found=1,
                                  detail="Read /home/ec2-user/.ssh/config")])

    monkeypatch.setattr(discovery_route, "discover", fake)
    tool = agent_tools.BY_NAME["discover_databases"]
    s, out = agent_tools.run_server_tool(tool, {})
    assert s == "success" and seen[0]["host"] is None
    assert set(seen[0]["sources"]) == {"local_server", "ssh_config", "pg_service",
                                       "earlier_assessment"}
    assert [x["id"] for x in out["servers"]] == ["ssh_config-1", "earlier-1"]
    assert out["servers"][1]["database"] == "marketplace" and out["servers"][1]["runs"] == 2
    assert out["sources"] == [{"source": "ssh_config", "status": "ok", "found": 1}]
    text = json.dumps(out)
    for value in ("prod", "ec2-user", "db-host.example.com", "10.0.0.5", "5432", ".ssh"):
        assert value not in text, value
    # The model cannot name a host: the input takes nothing.
    s, err = agent_tools.run_server_tool(tool, {"host": {"ssh_target": "evil.example.com"}})
    assert s == "error" and err["code"] == "invalid_tool_input" and len(seen) == 1


def test_database_confirm_tools_stop_for_the_user(client, monkeypatch):
    db = save_db(client)["database_id"]
    model = use_model(monkeypatch, FakeModel(tool_reply(
        ("t1", "check_database", {"database_id": db}),
        ("t2", "run_database_assessment", {"database_id": db, "window_minutes": 30}))))
    ev = events(client.post("/api/v1/agent/chat", json=say("assess it", database_id=db)))
    actions = [e for e in ev if e["type"] == "action_required"]
    assert [(a["name"], a["kind"]) for a in actions] == [("check_database", "confirm"),
                                                         ("run_database_assessment", "confirm")]
    assert ev[-1]["reason"] == "action_required" and len(model.calls) == 1
    # Nothing ran: the database is still unchecked and has no job.
    got = client.get(f"/api/v1/databases/{db}").json()
    assert got["readiness"]["status"] == "unchecked" and got["assessments"] == 0
    props = agent_tools.BY_NAME["run_database_assessment"].input_model.model_json_schema()["properties"]
    assert {"database_id", "window_minutes", "start_at", "allow_degraded", "use_llm"} <= set(props)
    assert not {"ssh_target", "db_user", "password_file", "identity_file"} & set(props)


def test_the_database_on_screen_reaches_the_prompt_as_an_id(client, monkeypatch, tmp_path):
    log = tmp_path / "audit.log"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(log))
    model = use_model(monkeypatch, FakeModel(text_reply("ok")))
    events(client.post("/api/v1/agent/chat", json=say("hi", database_id="db-0123456789ab")))
    system = model.calls[0]["system"][0]["text"]
    assert "saved database db-0123456789ab open" in system
    assert '"database_id":"db-0123456789ab"' in log.read_text()
    r = client.post("/api/v1/agent/chat", json=say("hi", database_id="ec2-user@host"))
    assert r.status_code == 422


def test_the_prompt_names_the_database_tools():
    from src.agents.chat.prompts import SYSTEM_PROMPT, system_prompt
    for phrase in ("check_database, run_database_assessment", "list_databases and get_database",
                   "discover_databases looks on this machine only", "You cannot pass a host",
                   "allow_degraded only when the\n  user agrees"):
        assert phrase in SYSTEM_PROMPT, phrase
    assert "Nothing is open." in system_prompt(None, None)
    both = system_prompt("job-1", None, "db-0123456789ab")
    assert "db-0123456789ab" in both and "assessment job-1" in both


def test_no_temperature_for_sonnet_5_5(client, monkeypatch):
    model = use_model(monkeypatch, FakeModel(text_reply("ok"), text_reply("ok")))
    events(client.post("/api/v1/agent/chat", json=say("hi")))
    assert "temperature" not in model.calls[0]["inferenceConfig"]
    events(client.post("/api/v1/agent/chat", json=say("hi", model_id="global.anthropic.claude-sonnet-4-6")))
    assert model.calls[1]["inferenceConfig"]["temperature"] == 0.2
