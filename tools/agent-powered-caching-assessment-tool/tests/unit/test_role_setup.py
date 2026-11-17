"""Role setup: the real shell script, run here against stand-in sudo and psql programs.

The stand-ins record what psql was given, so the tests can check the SQL and that the password
itself never reached it. python3 is the real one. Spec: .kiro/specs/databases (role setup).
"""
import base64
import hashlib
import hmac
import os
import stat
import subprocess
from pathlib import Path

import pytest

from src.tools.database import role_setup
from src.tools.database.role_setup import build_command, create_role, role_problems

SUDO = """#!/bin/sh
[ "$1" = "-n" ] && shift
[ "$1" = "-u" ] && shift 2
if [ "$1" = "true" ]; then exit "${FAKE_SUDO_RC:-0}"; fi
exec "$@"
"""

PSQL = """#!/bin/sh
input=$(cat)
printf 'ARGS %s\\nSQL %s\\n' "$*" "$input" >> "$FAKE_LOG"
case "$input" in
  *server_version_num*) echo "${FAKE_VER:-160015}" ;;
  *pg_roles*) [ -n "${FAKE_ROLE:-}" ] && echo "$FAKE_ROLE" ;;
  *"CREATE ROLE"*) exit "${FAKE_CREATE_RC:-0}" ;;
esac
exit 0
"""


class LocalHost:
    """Runs commands like SshPsqlExecutor.run_command does, but on this machine."""

    def __init__(self, tmp_path: Path, **env):
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in (("sudo", SUDO), ("psql", PSQL)):
            f = self.bin / name
            f.write_text(body)
            f.chmod(0o755)
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.log = tmp_path / "psql.log"
        self.log.write_text("")
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
                    "FAKE_LOG": str(self.log), **env}

    def run_command(self, command: str, timeout: int = 60) -> str:
        return subprocess.run(["sh", "-c", command], env=self.env, capture_output=True,  # nosec B603 - fixed argv list, no shell  # nosemgrep: dangerous-subprocess-use-audit
                              text=True, timeout=timeout, check=True).stdout

    def sql(self) -> str:
        return self.log.read_text()


def _python3_available() -> bool:
    return subprocess.run(["sh", "-c", "command -v python3"], capture_output=True,  # nosec B603 - fixed argv list, no shell  # nosemgrep: dangerous-subprocess-use-audit
                          env={"PATH": "/usr/bin:/bin"}).returncode == 0


pytestmark = pytest.mark.skipif(not _python3_available(), reason="needs python3 in /usr/bin")


def test_creates_the_role_with_only_a_hash_of_the_password(tmp_path):
    host = LocalHost(tmp_path)
    r = create_role(host, "apcat_reader", "retail", 5432, None)
    assert (r.ok, r.outcome) == (True, "created")
    pw_file = host.home / ".apcat_reader_pw"
    assert r.password_file == str(pw_file)
    assert stat.S_IMODE(os.stat(pw_file).st_mode) == 0o600
    password = pw_file.read_text()
    assert len(password) == 32 and int(password, 16) >= 0
    sql = host.sql()
    assert 'CREATE ROLE :"r" LOGIN PASSWORD :\'v\' NOSUPERUSER NOCREATEDB NOCREATEROLE;' in sql
    assert 'ALTER ROLE :"r" SET default_transaction_read_only = on;' in sql
    assert 'GRANT pg_monitor TO :"r";' in sql and 'GRANT pg_read_all_data TO :"r";' in sql
    assert "-v r=apcat_reader" in sql and "-d retail" in sql and "-p 5432" in sql
    assert password not in sql   # the password itself never reaches psql


def test_the_hash_is_a_valid_verifier_for_the_password(tmp_path):
    host = LocalHost(tmp_path)
    create_role(host, "apcat_reader", "retail", 5432, None)
    password = (host.home / ".apcat_reader_pw").read_text().encode()
    v = next(a for line in host.sql().splitlines() for a in line.split()
             if a.startswith("v=SCRAM-SHA-256$"))[2:]
    head, keys = v.split("$", 1)[1].split("$")
    iterations, salt = head.split(":")
    stored, server = keys.split(":")
    salted = hashlib.pbkdf2_hmac("sha256", password, base64.b64decode(salt), int(iterations))
    client = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    assert base64.b64encode(hashlib.sha256(client).digest()).decode() == stored
    assert base64.b64encode(hmac.new(salted, b"Server Key", hashlib.sha256).digest()).decode() \
        == server


def test_an_existing_role_with_its_file_is_reused_and_not_recreated(tmp_path):
    host = LocalHost(tmp_path, FAKE_ROLE="plain")
    pw_file = host.home / "app.pw"
    pw_file.write_text("known")
    r = create_role(host, "retail_app", "retail", 5432, str(pw_file))
    assert (r.ok, r.outcome, r.password_file) == (True, "reused", str(pw_file))
    assert "CREATE ROLE" not in host.sql() and 'GRANT pg_monitor TO :"r";' in host.sql()
    assert pw_file.read_text() == "known"


def test_an_existing_role_without_its_file_stops_and_changes_nothing(tmp_path):
    host = LocalHost(tmp_path, FAKE_ROLE="plain")
    r = create_role(host, "retail_app", "retail", 5432, None)
    assert (r.ok, r.outcome) == (False, "exists_no_file")
    assert "Give the file that holds its password" in r.message
    assert "ALTER ROLE" not in host.sql() and "GRANT" not in host.sql()


def test_a_privileged_role_is_never_touched(tmp_path):
    host = LocalHost(tmp_path, FAKE_ROLE="privileged")
    (host.home / ".admin_pw").write_text("x")
    r = create_role(host, "admin", "retail", 5432, None)
    assert (r.ok, r.outcome) == (False, "role_privileged")
    assert "ALTER ROLE" not in host.sql() and "GRANT" not in host.sql()


def test_no_sudo_stops_before_postgres_is_asked_anything(tmp_path):
    host = LocalHost(tmp_path, FAKE_SUDO_RC="1")
    r = create_role(host, "apcat_reader", "retail", 5432, None)
    assert (r.ok, r.outcome) == (False, "no_sudo")
    assert host.sql() == ""
    assert not (host.home / ".apcat_reader_pw").exists()


def test_a_file_already_at_the_path_is_left_alone(tmp_path):
    host = LocalHost(tmp_path)
    pw_file = host.home / ".apcat_reader_pw"
    pw_file.write_text("someone else's")
    r = create_role(host, "apcat_reader", "retail", 5432, None)
    assert (r.ok, r.outcome) == (False, "file_exists")
    assert pw_file.read_text() == "someone else's" and "CREATE ROLE" not in host.sql()


def test_postgres_13_is_refused(tmp_path):
    host = LocalHost(tmp_path, FAKE_VER="130012")
    assert create_role(host, "apcat_reader", "retail", 5432, None).outcome == "old_postgres"


def test_a_refused_create_removes_the_new_password_file(tmp_path):
    host = LocalHost(tmp_path, FAKE_CREATE_RC="1")
    r = create_role(host, "apcat_reader", "retail", 5432, None)
    assert (r.ok, r.outcome) == (False, "psql_failed")
    assert not (host.home / ".apcat_reader_pw").exists()


@pytest.mark.parametrize("role,db,port,pw", [
    ("Apcat", "retail", 5432, None), ("a; rm -rf /", "retail", 5432, None),
    ("pg_reader", "retail", 5432, None), ("postgres", "retail", 5432, None),
    ("apcat_reader", "retail; drop", 5432, None), ("apcat_reader", "retail", 0, None),
    ("apcat_reader", "retail", 5432, "relative.pw"), ("apcat_reader", "retail", 5432, "/a b"),
])
def test_values_not_in_an_accepted_form_are_refused(role, db, port, pw):
    assert role_problems(role, db, port, pw)
    with pytest.raises(ValueError):
        create_role(object(), role, db, port, pw)


def test_every_value_is_quoted_in_the_command():
    cmd = build_command("apcat_reader", "re-tail.db", 5432, "/home/u/x.pw")
    assert "set -- apcat_reader re-tail.db 5432 /home/u/x.pw;" in cmd


def test_an_ssh_failure_is_an_outcome_not_an_error():
    class Broken:
        def run_command(self, command, timeout=60):
            raise TimeoutError("ssh command timed out after 90s")
    r = create_role(Broken(), "apcat_reader", "retail", 5432, None)
    assert (r.ok, r.outcome) == (False, "ssh_failed")


def test_unexpected_output_is_not_trusted():
    r = role_setup.parse_output("result=created_ok\n", "apcat_reader")
    assert (r.ok, r.outcome) == (False, "unexpected")


# --- The API route ---------------------------------------------------------------------

def test_the_route_runs_the_setup_and_audits_no_path(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api import audit, common
    from src.api.main import app
    from src.api.routes import databases

    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path / "jobs"))
    host = LocalHost(tmp_path)
    host.close = lambda: None
    monkeypatch.setattr(databases, "ssh_executor_for", lambda target: host)
    seen = []
    monkeypatch.setattr(audit, "record", lambda event, **f: seen.append((event, f)))
    c = TestClient(app, client=("127.0.0.1", 50000))
    r = c.post("/api/v1/databases/create-role",
               json={"ssh_target": "db-host", "database_name": "retail"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["outcome"] == "created" and body["role_name"] == "apcat_reader"
    assert body["password_file"].endswith("/.apcat_reader_pw")
    assert seen == [("role_setup", {"outcome": "created", "role": "apcat_reader"})]

    bad = c.post("/api/v1/databases/create-role",
                 json={"ssh_target": "db-host", "database_name": "retail", "role_name": "x;y"})
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_target"
