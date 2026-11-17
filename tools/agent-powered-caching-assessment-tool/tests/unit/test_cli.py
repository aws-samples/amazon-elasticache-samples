"""The `apcat` CLI and the agent skill installer (spec .kiro/specs/agent-skill)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import common
from src.api.main import app
from src.cli import agents, main, servers, settings
from src.cli.client import ApiClient, CliError, api_base, is_loopback_url

FIXTURES = Path(__file__).parent.parent / "fixtures"
CONN = ["--ssh-target", "ec2-user@203.0.113.10", "--db-user", "apcat_ro",
        "--password-file", "/home/ec2-user/.pw", "--database", "marketplace"]


@pytest.fixture
def api(tmp_path, monkeypatch) -> TestClient:
    """The real API in process, its artifacts in tmp_path, wired under the CLI."""
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("APCAT_HOME", str(tmp_path / "home"))
    tc = TestClient(app, client=("127.0.0.1", 50000))

    def transport(method, url, body, _timeout):
        r = tc.request(method, url.replace("http://127.0.0.1:8000", ""), json=body)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text

    monkeypatch.setattr(ApiClient, "from_env",
                        classmethod(lambda cls: cls("http://127.0.0.1:8000", transport)))
    monkeypatch.setattr(servers, "ui_running", lambda *a, **k: False)
    return tc


def run(capsys, *argv: str) -> tuple[int, object]:
    code = main.main(list(argv))
    out = capsys.readouterr().out
    try:
        return code, json.loads(out)
    except ValueError:
        return code, out


# --- loopback only --------------------------------------------------------------------


@pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:8000", True), ("http://localhost:8001", True), ("http://[::1]:8000", True),
    ("http://127.0.0.2:8000", True), ("http://0.0.0.0:8000", False),
    ("http://10.0.0.5:8000", False), ("http://example.com", False), ("file:///etc/passwd", False),
    ("http://127.0.0.1.example.com", False),
])
def test_only_loopback_api_addresses_are_accepted(url, ok):
    assert is_loopback_url(url) is ok


def test_a_remote_api_address_is_refused_before_any_request(monkeypatch, capsys):
    monkeypatch.setenv("APCAT_API", "http://203.0.113.7:8000")
    with pytest.raises(CliError) as e:
        api_base()
    assert e.value.code == "not_loopback"
    code, out = run(capsys, "databases")
    assert code == 1 and out["error"] == "not_loopback"


def test_start_never_widens_the_api_bind(tmp_path, monkeypatch):
    monkeypatch.setenv("APCAT_HOME", str(tmp_path))
    monkeypatch.setenv("API_HOST", "0.0.0.0")  # nosec B104 - the test proves it is dropped
    spawned = []
    monkeypatch.setattr(servers, "_spawn", lambda name, argv, cwd, env: spawned.append((name, argv, env)) or 1)
    monkeypatch.setattr(ApiClient, "health", lambda self, timeout=2.0: None)
    monkeypatch.setattr(servers, "_wait", lambda check, timeout: True)
    monkeypatch.setattr(servers, "ui_running", lambda *a, **k: False)
    servers.start(with_ui=False)
    (name, argv, env), = spawned
    assert name == "api" and argv[1:] == ["-m", "src.api.main"]
    assert "API_HOST" not in env and env["API_PORT"] == "8000"
    assert not any("0.0.0.0" in a for a in argv)  # nosec B104


# --- commands against the real API ----------------------------------------------------


def test_save_takes_a_password_location_never_a_password(api, capsys):
    code, out = run(capsys, "save", *CONN[:-4], "--database", "marketplace")
    assert code == 1 and out["error"] == "invalid_request"  # neither file nor secret
    with pytest.raises(SystemExit):
        main.main(["save", *CONN, "--password", "hunter2"])  # no such option
    capsys.readouterr()


def test_save_list_and_show_a_database(api, capsys):
    code, saved = run(capsys, "save", *CONN, "--label", "Marketplace", "--discovered-from", "ssh_config")
    assert code == 0 and saved["database_id"].startswith("db-")
    assert saved["readiness"]["status"] == "unchecked" and saved["discovered_from"] == "ssh_config"
    code, again = run(capsys, "save", *CONN)
    assert code == 1 and again["error"] == "database_exists"
    code, listed = run(capsys, "databases")
    assert listed["total"] == 1 and listed["items"][0]["label"] == "Marketplace"
    code, one = run(capsys, "database", saved["database_id"])
    assert one["connection"]["password_file"] == "/home/ec2-user/.pw"


def test_run_is_refused_until_the_database_is_checked(api, capsys):
    _, saved = run(capsys, "save", *CONN)
    code, out = run(capsys, "run", saved["database_id"], "--window", "15")
    assert code == 1 and out["error"] == "database_not_checked" and out["http_status"] == 409


def test_an_unsafe_value_is_refused_by_the_api(api, capsys):
    code, out = run(capsys, "save", "--ssh-target=-oProxyCommand=x", *CONN[2:])
    assert code == 1 and out["error"] == "invalid_target"


def test_results_wait_and_links_for_a_finished_assessment(api, capsys):
    body = {"collector_output": json.loads((FIXTURES / "strong_candidate_collection.json").read_text()),
            "use_llm": False, "price": False}
    job = api.post("/api/v1/assessments/from-collection", json=body).json()
    assert job["status"] == "complete"
    j = job["job_id"]

    code, w = run(capsys, "wait", j, "--timeout", "5")
    assert code == 0 and w["done"] is True and w["status"] == "complete"

    code, res = run(capsys, "results", j)
    assert code == 0 and res["summary"]["job_id"] == j and "overview" not in res
    assert res["headline"]["title"] and res["counts"]["queries_recommended"] == res["recommended_queries"]["total"]
    assert res["recommended_tables"]["items"][0]["table_id"] == "public.product_catalog"
    assert len(json.dumps(res)) < 6000 and "--overview" in res["more"]
    code, ov = run(capsys, "results", j, "--overview")
    assert "glossary" not in ov["overview"] and "scoring" not in ov["overview"]
    assert res["links"]["assessment"].endswith(f"/#/assessments/{j}")
    assert res["links"]["report_html"] == f"http://127.0.0.1:8000/api/v1/assessments/{j}/report"
    assert res["links"]["console_running"] is False and "apcat start" in res["links"]["note"]

    code, full = run(capsys, "results", j, "--full")
    assert "glossary" in full["overview"]
    code, q = run(capsys, "results", j, "--queries", "--recommended", "--limit", "2")
    assert code == 0 and q["limit"] == 2 and all(i.get("recommended", True) for i in q["items"])
    code, missing = run(capsys, "results", j, "--table", "nope.nope")
    assert code == 1 and missing["error"] == "item_not_found"


def test_unreachable_api_says_how_to_start_it(monkeypatch, capsys):
    def down(*_a):
        raise ConnectionRefusedError

    monkeypatch.setattr(ApiClient, "from_env", classmethod(lambda cls: cls("http://127.0.0.1:9", down)))
    code, out = run(capsys, "databases")
    assert code == 1 and out["error"] == "api_unreachable" and "apcat start" in out["detail"]


# --- the playbook ---------------------------------------------------------------------


def test_guide_prints_the_skill():
    text = main.guide_text()
    assert text.startswith("---\nname: assess-postgres-caching\ndescription: ")
    assert agents.OWNED_MARK in text


def test_the_playbook_gets_the_console_running_first_and_offers_it():
    text = main.guide_text()
    start = text.split("## 1. Start", 1)[1].split("## 2.", 1)[0]
    assert "`npm ci`" in start and "`apcat start` again" in start
    assert "`ui.url`" in start and "themselves in the console" in start


def test_every_command_the_playbook_names_exists():
    parser = main.build_parser()
    commands = set(next(a for a in parser._actions if a.dest == "command").choices)
    named = set(re.findall(r"`apcat ([a-z][a-z-]*)", main.guide_text()))
    assert named and named <= commands, named - commands


def test_every_flag_the_playbook_names_exists():
    parser = main.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command").choices
    text = main.guide_text().replace("\\\n", " ")  # join shell line continuations
    for cmd, rest in re.findall(r"apcat ([a-z][a-z-]*)((?: [^`\n|]*)?)", text):
        flags = set(re.findall(r"(--[a-z][a-z-]*)", rest))
        known = {o for act in sub[cmd]._actions for o in act.option_strings}
        assert flags <= known, (cmd, flags - known)


# --- agent install --------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def actions(result) -> dict[str, str]:
    return {Path(c["path"]).name: c["action"] for c in result["changes"]}


def test_install_writes_each_agent_and_is_idempotent(home):
    first = agents.install(list(agents.AGENTS))
    assert actions(first) == {"SKILL.md": "written", "assess-postgres.md": "written",
                              "AGENTS.md": "written", "apcat.md": "written"}
    skill = (home / ".claude/skills/assess-postgres-caching/SKILL.md").read_text()
    assert skill.startswith("---\nname: assess-postgres-caching\ndescription: ")
    assert "Run `apcat guide` now" in skill and len(skill) < len(main.guide_text()) / 3
    assert "apcat guide" in (home / ".codex/AGENTS.md").read_text()
    assert (home / ".kiro/steering/apcat.md").read_text().startswith("---\ninclusion: always\n---")
    assert set(actions(agents.install(list(agents.AGENTS))).values()) == {"unchanged"}
    assert {a["state"] for a in agents.status(list(agents.AGENTS))["agents"]} == {"current"}


def test_install_keeps_the_rest_of_a_shared_agents_file(home):
    path = home / ".codex/AGENTS.md"
    path.parent.mkdir(parents=True)
    path.write_text("# My rules\n\nBe terse.\n")
    agents.install(["codex"])
    text = path.read_text()
    assert text.startswith("# My rules\n\nBe terse.\n") and agents.MARK_START in text
    agents.install(["codex"])
    assert path.read_text().count(agents.MARK_START) == 1
    agents.uninstall(["codex"])
    assert path.read_text() == "# My rules\n\nBe terse.\n"


def test_install_leaves_a_file_it_did_not_write_unless_forced(home):
    path = home / ".kiro/steering/apcat.md"
    path.parent.mkdir(parents=True)
    path.write_text("mine")
    out = agents.install(["kiro"])
    assert actions(out) == {"apcat.md": "skipped"} and "--force" in out["note"]
    assert path.read_text() == "mine"
    assert agents.uninstall(["kiro"])["changes"][0]["action"] == "skipped"
    agents.install(["kiro"], force=True)
    assert agents.OWNED_MARK in path.read_text()


def test_install_replaces_an_earlier_apcat_skill(home):
    old = home / ".claude/skills/assess-postgres-caching/SKILL.md"
    old.parent.mkdir(parents=True)
    old.write_text("Run `apcat next --json`\n<!-- installed by apcat agent install; remove with apcat agent uninstall -->\n")
    assert agents.status(["claude"])["agents"][0]["state"] == "outdated"
    assert actions(agents.install(["claude"]))["SKILL.md"] == "updated"
    assert "apcat next" not in old.read_text()


def test_uninstall_removes_exactly_what_install_added(home):
    agents.install(list(agents.AGENTS))
    out = agents.uninstall(list(agents.AGENTS))
    assert set(actions(out).values()) == {"removed"}
    assert not (home / ".claude/skills/assess-postgres-caching").exists()
    assert not (home / ".codex/AGENTS.md").exists()
    assert not (home / ".kiro/steering/apcat.md").exists()


def test_project_install_uses_the_project_folder(home, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    agents.install(["claude", "codex", "kiro"], project=str(proj))
    assert (proj / ".claude/skills/assess-postgres-caching/SKILL.md").is_file()
    assert (proj / "AGENTS.md").is_file() and (proj / ".kiro/steering/apcat.md").is_file()
    assert not (home / ".codex").exists()


def test_unknown_agent_is_refused():
    with pytest.raises(CliError):
        agents.parse_agents("claude,cursor")


# --- handoff to the AWS ElastiCache skill (SKILL-10) ----------------------------------


def _assessed(api, fixture: str = "strong_candidate_collection.json") -> str:
    body = {"collector_output": json.loads((FIXTURES / fixture).read_text()),
            "use_llm": False, "price": False}
    return api.post("/api/v1/assessments/from-collection", json=body).json()["job_id"]


def test_handoff_writes_the_elasticache_requirements_file(api, capsys, tmp_path):
    j = _assessed(api)
    proj = tmp_path / "app"
    proj.mkdir()
    code, out = run(capsys, "handoff", j, "--project", str(proj))
    assert code == 0 and out["written"] and out["created"]
    doc = json.loads((proj / ".elasticache/requirements.json").read_text())
    assert doc["schema_version"] == 1 and doc["last_updated"]
    assert doc["engine"] == "valkey" and doc["patterns"] == ["cache-aside"]
    assert doc["data_source"] == "self-managed-postgresql"
    assert doc["next_steps"] == ["setup", "data-modeling"]
    assert "deployment_model" not in doc  # the tool did not evaluate serverless
    a = doc["assessment"]
    assert a["source"] == "apcat" and a["job_id"] == j and a["invalidation"] == "on_write"
    assert a["queries"] and all("sql" not in q for q in a["queries"])  # no query text by default
    q = a["queries"][0]
    assert {"suggested_cache_key", "key_columns", "invalidated_by"} <= set(q)
    assert a["tables"][0]["table_id"] == "public.product_catalog"
    assert a["sizing"]["note"].startswith("An estimate") and a["sizing"]["priced"] is False
    assert a["links"]["assessment"].endswith(j)
    assert out["queries"] == len(a["queries"]) and "amazon-elasticache" in out["skill"]


def test_handoff_merges_and_never_overwrites_the_skills_state(api, capsys, tmp_path):
    j = _assessed(api)
    path = tmp_path / ".elasticache/requirements.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"schema_version": 1, "engine": "redis", "region": None,
                                "runtime": {"language": "python"},
                                "infrastructure": {"endpoint": "x.cache.amazonaws.com"}}))
    code, out = run(capsys, "handoff", j, "--project", str(tmp_path), "--include-sql")
    assert code == 0 and not out["created"] and out["fields_kept"] == ["engine"]
    doc = json.loads(path.read_text())
    assert doc["engine"] == "redis" and doc["runtime"] == {"language": "python"}
    assert doc["infrastructure"] == {"endpoint": "x.cache.amazonaws.com"}
    assert "sql" in doc["assessment"]["queries"][0]
    run(capsys, "handoff", j, "--project", str(tmp_path))  # again: one section, replaced
    doc2 = json.loads(path.read_text())
    assert "sql" not in doc2["assessment"]["queries"][0] and doc2["infrastructure"] == doc["infrastructure"]


def test_handoff_dry_run_and_bad_files_write_nothing(api, capsys, tmp_path):
    j = _assessed(api)
    code, out = run(capsys, "handoff", j, "--project", str(tmp_path), "--dry-run")
    assert code == 0 and out["written"] is False and out["document"]["assessment"]["job_id"] == j
    assert not (tmp_path / ".elasticache").exists()
    path = tmp_path / ".elasticache/requirements.json"
    path.parent.mkdir()
    path.write_text("{not json")
    code, out = run(capsys, "handoff", j, "--project", str(tmp_path))
    assert code == 1 and out["error"] == "invalid_requirements_file"
    assert path.read_text() == "{not json"


def test_handoff_refuses_when_nothing_is_recommended(api, capsys, tmp_path):
    j = _assessed(api, "no_candidate_collection.json")
    code, out = run(capsys, "handoff", j, "--project", str(tmp_path))
    assert code == 1 and out["error"] == "nothing_recommended"
    assert not (tmp_path / ".elasticache").exists()


def test_the_playbook_hands_off_and_defers_to_the_elasticache_skill():
    text = main.guide_text()
    assert "apcat handoff <job_id>" in text and "amazon-elasticache" in text
    assert "Do not create any from this flow" in text


# --- settings, restart and warnings (agent experience round 2) ------------------------


@pytest.fixture
def apcat_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "apcat-home"
    monkeypatch.setenv("APCAT_HOME", str(home))
    for env in ("SSH_KEY_DIR", "SSH_KNOWN_HOSTS", "ARTIFACT_ROOT"):
        monkeypatch.delenv(env, raising=False)
    return home


def test_config_set_show_and_precedence(apcat_home, monkeypatch, capsys, tmp_path):
    code, out = run(capsys, "config")
    assert code == 0 and out["settings"]["ssh_key_dir"]["source"] == "none"
    (apcat_home / "keys").mkdir(parents=True)
    assert settings.effective()["ssh_key_dir"] == {"value": str(apcat_home / "keys"), "source": "default"}
    code, out = run(capsys, "config", "set", "ssh_known_hosts", str(tmp_path / "kh"))
    assert out["settings"]["ssh_known_hosts"]["source"] == "config"
    assert (apcat_home / "config.json").stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("SSH_KNOWN_HOSTS", "/elsewhere")
    assert settings.effective()["ssh_known_hosts"] == {"value": "/elsewhere", "source": "environment"}
    assert settings.api_env() == {"SSH_KEY_DIR": str(apcat_home / "keys"), "SSH_KNOWN_HOSTS": "/elsewhere"}
    code, out = run(capsys, "config", "unset", "ssh_known_hosts")
    assert "ssh_known_hosts" not in json.loads((apcat_home / "config.json").read_text())
    with pytest.raises(SystemExit):
        main.main(["config", "set", "nope", "x"])


def test_start_gives_the_api_its_ssh_settings_and_records_them(apcat_home, monkeypatch):
    (apcat_home / "keys").mkdir(parents=True)
    spawned = []
    monkeypatch.setattr(servers, "_spawn", lambda name, argv, cwd, env: spawned.append(env) or 1)
    monkeypatch.setattr(ApiClient, "health", lambda self, timeout=2.0: None)
    monkeypatch.setattr(servers, "_wait", lambda check, timeout: True)
    monkeypatch.setattr(servers, "ui_running", lambda *a, **k: False)
    servers.start(with_ui=False)
    assert spawned[0]["SSH_KEY_DIR"] == str(apcat_home / "keys") and "SSH_KNOWN_HOSTS" not in spawned[0]
    recorded = json.loads((apcat_home / "run/api.settings.json").read_text())
    assert recorded == {"SSH_KEY_DIR": str(apcat_home / "keys")}


def test_status_warns_when_the_running_api_has_other_settings(apcat_home, monkeypatch, tmp_path):
    run_dir = servers.run_dir()
    (run_dir / "api.settings.json").write_text(json.dumps({"SSH_KEY_DIR": "/old"}))
    monkeypatch.setattr(servers, "_read_pid", lambda name: 4242 if name == "api" else None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (apcat_home / "keys").mkdir(parents=True)
    out = servers.warnings(ApiClient("http://127.0.0.1:8000", lambda *a: (200, {})), api_running=True)
    assert any("apcat start --restart" in w for w in out)


def test_status_warns_when_the_installed_skill_is_outdated(apcat_home, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    skill = tmp_path / ".claude/skills/assess-postgres-caching/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("old playbook\n<!-- installed by apcat agent install -->\n")
    out = servers.warnings(ApiClient("http://127.0.0.1:8000", lambda *a: (200, {})), api_running=False)
    assert any("apcat agent install" in w for w in out)
    agents.install(["claude"])
    assert not servers.warnings(ApiClient("http://127.0.0.1:8000", lambda *a: (200, {})), api_running=False)


# --- hosts, probe and trust ------------------------------------------------------------


def _fake(routes):
    def transport(method, url, body, _timeout):
        path = url.split("/api/v1", 1)[-1].split("?")[0]
        status, data = routes[(method, path)]
        return status, (data(body) if callable(data) else data)
    return transport


def _with(monkeypatch, routes):
    monkeypatch.setattr(ApiClient, "from_env",
                        classmethod(lambda cls: cls("http://127.0.0.1:8000", _fake(routes))))


def test_hosts_leaves_out_code_hosts_unless_asked(monkeypatch, capsys):
    items = [{"ssh_target": "db1", "label": "db1", "source": "ssh_config", "code_host": False},
             {"ssh_target": "git.example.com", "label": "git", "source": "ssh_config", "code_host": True}]
    _with(monkeypatch, {("GET", "/hosts/suggestions"): (200, lambda _b: {"items": list(items)})})
    _, out = run(capsys, "hosts")
    assert [i["ssh_target"] for i in out["items"]] == ["db1"] and out["code_hosts_hidden"] == 1
    _, out = run(capsys, "hosts", "--all")
    assert len(out["items"]) == 2


def test_probe_points_at_trust_only_for_an_unknown_key(monkeypatch, capsys):
    fp = "SHA256:" + "A" * 43
    def probe(state):
        return {"ok": False, "ssh_target": "h", "ssh_port": 2222, "duration_seconds": 0.1,
                "stages": [{"name": "resolve", "status": "pass", "message": "ok"},
                           {"name": "host_key", "status": "fail", "message": "Not trusted", "hint": "Confirm"}],
                "host_key": {"state": state, "key_type": "ssh-ed25519", "fingerprint": fp}}
    sent = []
    _with(monkeypatch, {("POST", "/hosts/probe"): (200, lambda b: sent.append(b) or probe("unknown"))})
    _, out = run(capsys, "probe", "--host", "apcat@127.0.0.1", "--ssh-port", "2222", "--identity-file", "k")
    assert sent == [{"ssh_target": "apcat@127.0.0.1", "ssh_port": 2222, "identity_file": "k"}]
    assert out["first_failure"]["name"] == "host_key"
    assert f"--fingerprint {fp}" in out["next"] and "confirm" in out["next"]
    _with(monkeypatch, {("POST", "/hosts/probe"): (200, probe("changed"))})
    _, out = run(capsys, "probe", "--host", "h")
    assert out["next"].startswith("Stop") and "trust" not in out["next"].split(":")[0]


def test_probe_with_sudo_offers_the_databases_and_apcat_reader_first(monkeypatch, capsys):
    passed = {"ok": True, "ssh_target": "h", "ssh_port": 22, "duration_seconds": 0.1,
              "stages": [{"name": "databases", "status": "pass", "message": "2 databases listed"}],
              "host_key": {"state": "known"}, "postgres_access": "sudo",
              "databases": [{"id": "d1", "source": "host_probe", "kind": "ssh", "label": "retail",
                             "database": "retail", "port": 5432}]}
    _with(monkeypatch, {("POST", "/hosts/probe"): (200, passed)})
    _, out = run(capsys, "probe", "--host", "ec2-user@db", "--identity-file", "k")
    assert out["first_failure"] is None
    assert out["next"].index("apcat_reader") < out["next"].index("existing")
    assert "apcat create-role --ssh-target ec2-user@db --ssh-port 22 --identity-file k" in out["next"]
    _with(monkeypatch, {("POST", "/hosts/probe"): (200, {**passed, "postgres_access": "no_sudo", "databases": []})})
    _, out = run(capsys, "probe", "--host", "h")
    assert "existing read-only role" in out["next"] and "create-role" not in out["next"]
    _with(monkeypatch, {("POST", "/hosts/probe"): (200, {**passed, "postgres_access": "failed", "databases": []})})
    _, out = run(capsys, "probe", "--host", "h")
    assert out["next"] is None


def test_create_role_says_how_to_save_what_it_made(monkeypatch, capsys):
    sent = []
    made = {"ok": True, "outcome": "created", "message": "Created.", "role_name": "apcat_reader",
            "password_file": "/home/ec2-user/.apcat_reader_pw"}  # nosec B105 - test value, not a password
    _with(monkeypatch, {("POST", "/databases/create-role"): (200, lambda b: sent.append(b) or made)})
    _, out = run(capsys, "create-role", "--ssh-target", "ec2-user@db", "--database", "retail")
    assert sent[0]["role_name"] == "apcat_reader"
    assert ("--db-user apcat_reader --password-file /home/ec2-user/.apcat_reader_pw "
            "--database retail --port 5432") in out["next"]
    refused = {"ok": False, "outcome": "exists_no_file", "message": "No file.", "role_name": "shop_ro"}
    _with(monkeypatch, {("POST", "/databases/create-role"): (200, refused)})
    _, out = run(capsys, "create-role", "--ssh-target", "h", "--database", "retail", "--role-name", "shop_ro")
    assert "--role-name" in out["next"] and "apcat save" not in out["next"]


def test_trust_sends_the_confirmed_fingerprint(monkeypatch, capsys):
    fp = "SHA256:" + "B" * 43
    sent = []
    _with(monkeypatch, {("POST", "/hosts/trust"): (200, lambda b: sent.append(b) or
                                                   {"trusted": True, "outcome": "added", "message": "ok"})})
    code, out = run(capsys, "trust", "--host", "h", "--ssh-port", "2222", "--fingerprint", fp)
    assert code == 0 and sent[0]["fingerprint"] == fp and out["outcome"] == "added"


# --- edit, forget, cancel, rerun, whatif, delete against the real API ------------------


def test_database_exists_gives_the_saved_id_as_a_field(api, capsys):
    _, saved = run(capsys, "save", *CONN)
    code, again = run(capsys, "save", *CONN)
    assert code == 1 and again["error"] == "database_exists"
    assert again["database_id"] == saved["database_id"]


def test_edit_changes_the_connection_and_resets_readiness(api, capsys):
    _, saved = run(capsys, "save", *CONN, "--label", "Shop")
    db = saved["database_id"]
    code, out = run(capsys, "edit", db, "--password-file", "/home/ec2-user/.other", "--ssh-port", "2222")
    assert code == 0 and out["connection"]["password_file"] == "/home/ec2-user/.other"
    assert out["connection"]["ssh_port"] == 2222 and out["connection"]["db_user"] == "apcat_ro"
    assert out["label"] == "Shop" and out["next"] == f"apcat check {db}"
    code, out = run(capsys, "edit", db, "--password-secret-arn",
                    "arn:aws:secretsmanager:us-east-1:123456789012:secret:x")
    assert code == 1 or out["connection"]["password_file"] is None  # refused unless the ARN prefix is allowed


def test_forget_keeps_the_assessments(api, capsys):
    _, saved = run(capsys, "save", *CONN)
    code, out = run(capsys, "forget", saved["database_id"])
    assert code == 0 and "kept" in out["note"]
    code, out = run(capsys, "database", saved["database_id"])
    assert out["error"] == "database_not_found"


def test_rerun_whatif_cancel_and_delete(api, capsys):
    j = _assessed(api)
    code, rr = run(capsys, "rerun", j, "--no-price", "--label", "again")
    assert code == 0 and rr["job_id"] != j
    code, w = run(capsys, "whatif", j, "--set", "peak_factor=2", "--no-price")
    assert code == 0 and w["job_id"] == j
    assert {"name": "peak_factor", "baseline": 1.0, "scenario": 2.0} in w["changes"]
    code, bad = run(capsys, "whatif", j, "--set", "peak_factor")
    assert code == 1 and bad["error"] == "invalid_request"
    code, c = run(capsys, "cancel", j)
    assert code == 1 and c["http_status"] == 409  # already finished
    code, d = run(capsys, "delete", j)
    assert code == 0 and d == {"deleted": j}
    code, gone = run(capsys, "job", j)
    assert gone["error"] == "job_not_found"
