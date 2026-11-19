"""The SSH executor and the shape checks on its values.

`ssh` is replaced by a stub that records its arguments and runs the remote command in a local
shell, and `psql` by a stub that records what it was given. So the real quoting, option order
and password handling run end to end, with no network.
"""

from __future__ import annotations

import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

from src.tools.database import ssh_values
from src.tools.database.ssh_executor import SshPsqlExecutor
from src.tools.database.tabular import FIELD_SEP, RECORD_SEP

# ---------------------------------------------------------------------------
# Shape checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [
    "db1", "ec2-user@10.0.1.50", "ec2-user@ip-10-0-1-50.ec2.internal", "my_alias",
    "user@::1", "a", "u@h",
])
def test_good_targets(value):
    assert ssh_values.target_ok(value)


@pytest.mark.parametrize("value", [
    "", "-oProxyCommand=x", "-p", "a b", "a;b", "a\nb", "a$(x)", "a`x`", "user@", "@host",
    "host\n", "u@h@h2", "a'b", 'a"b', "a|b", "a&b", "a" * 300,
])
def test_bad_targets(value):
    assert not ssh_values.target_ok(value)


@pytest.mark.parametrize("value,ok", [
    ("/home/ec2-user/.pgpass-apcat", True), ("/etc/apcat/pw", True), ("relative/pw", False),
    ("/a/../etc/shadow", False), ("/a b", False), ("/a;b", False), ("/a\n", False),
    ("/", False), ("", False), ("/a/$(x)", False), ("/a'b", False),
])
def test_password_file_shapes(value, ok):
    assert ssh_values.password_file_ok(value) is ok


@pytest.mark.parametrize("value,ok", [
    ("apcat", True), ("retail_admin", True), ("u$er", True), ("1abc", False), ("a b", False),
    ("a;b", False), ("a'b", False), ("", False), ("a\n", False), ("-x", False),
])
def test_db_user_shapes(value, ok):
    assert ssh_values.db_user_ok(value) is ok


@pytest.mark.parametrize("value,ok", [
    ("id_ed25519", True), ("apcat-test.key", True), (".hidden", False), ("-x", False),
    ("a/b", False), ("../x", False), ("", False), ("a\n", False), ("a b", False),
])
def test_key_names(value, ok):
    assert ssh_values.key_name_ok(value) is ok


def test_resolve_key(tmp_path, monkeypatch):
    keys = tmp_path / "keys"
    keys.mkdir()
    good = keys / "k1"
    good.write_text("x")
    good.chmod(0o600)
    loose = keys / "k2"
    loose.write_text("x")
    loose.chmod(0o644)
    outside = tmp_path / "outside"
    outside.write_text("x")
    outside.chmod(0o600)
    (keys / "link").symlink_to(outside)

    monkeypatch.delenv("SSH_KEY_DIR", raising=False)
    with pytest.raises(ValueError, match="SSH_KEY_DIR"):
        ssh_values.resolve_key("k1")
    monkeypatch.setenv("SSH_KEY_DIR", str(keys))
    assert ssh_values.resolve_key("k1") == good.resolve()
    with pytest.raises(ValueError, match="group or others"):
        ssh_values.resolve_key("k2")
    with pytest.raises(FileNotFoundError):
        ssh_values.resolve_key("missing")
    with pytest.raises(ValueError, match="not inside"):
        ssh_values.resolve_key("link")  # a symlink out of the directory
    with pytest.raises(ValueError, match="plain file name"):
        ssh_values.resolve_key("../outside")


def test_known_hosts_file(tmp_path, monkeypatch):
    kh = tmp_path / "kh"
    monkeypatch.setenv("SSH_KNOWN_HOSTS", str(kh))
    with pytest.raises(FileNotFoundError):
        ssh_values.known_hosts_file()
    kh.write_text("")
    assert ssh_values.known_hosts_file() == kh


# ---------------------------------------------------------------------------
# The executor, with stub ssh and psql
# ---------------------------------------------------------------------------


def _script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def stubs(tmp_path, monkeypatch):
    """A stub ssh and psql on PATH. Returns the paths the stubs write their records to."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rec = tmp_path / "rec"
    rec.mkdir()
    _script(bin_dir / "ssh", f"""
        # one argument per line, then run the last one (the remote command) in a local shell
        for a in "$@"; do printf '%s\\0' "$a"; done > {rec}/ssh_argv
        for last; do :; done
        exec sh -c "$last"
    """)
    _script(bin_dir / "psql", f"""
        for a in "$@"; do printf '%s\\0' "$a"; done > {rec}/psql_argv
        printf '%s' "$PGPASSWORD" > {rec}/psql_password
        if [ -n "$STUB_PSQL_FAIL" ]; then echo "$STUB_PSQL_FAIL" >&2; exit 3; fi
        printf 'ok{FIELD_SEP}note{RECORD_SEP}1{FIELD_SEP}a\\nb\\n'
    """)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    kh = tmp_path / "known_hosts"
    kh.write_text("")
    pw = tmp_path / "pw"  # the stub "host" is this machine, so the password file is local
    pw.write_text("file-password\n")
    return {"rec": rec, "known_hosts": kh, "pw": pw}


def _args(rec: Path, name: str) -> list[str]:
    return (rec / name).read_bytes().decode().split("\0")[:-1]


def make(stubs, **kw):
    kw.setdefault("password_file", str(stubs["pw"]))
    return SshPsqlExecutor("ec2-user@10.0.1.50", "apcat", known_hosts=stubs["known_hosts"], **kw)


def test_ssh_options_are_strict_and_in_front_of_the_target(stubs):
    ex = make(stubs)
    ex.run_command("true")
    argv = _args(stubs["rec"], "ssh_argv")
    assert "BatchMode=yes" in argv and "StrictHostKeyChecking=yes" in argv
    assert f"UserKnownHostsFile={stubs['known_hosts']}" in argv
    assert "ClearAllForwardings=yes" in argv
    assert argv[argv.index("-p") + 1] == "22"
    assert argv[-3] == "--" and argv[-2] == "ec2-user@10.0.1.50"
    ex.close()


def test_a_key_is_passed_with_identities_only(stubs, tmp_path):
    key = tmp_path / "k"
    key.write_text("x")
    ex = make(stubs, identity_file=key, ssh_port=2222)
    ex.run_command("true")
    argv = _args(stubs["rec"], "ssh_argv")
    assert argv[argv.index("-i") + 1] == str(key)
    assert "IdentitiesOnly=yes" in argv
    assert argv[argv.index("-p") + 1] == "2222"
    ex.close()


def test_run_sql_json_parses_a_value_with_a_newline(stubs):
    ex = make(stubs)
    rows = ex.run_sql_json("127.0.0.1", 5432, "marketplace", sql="SELECT 1")
    assert rows == [{"ok": 1, "note": "a\nb"}]
    ex.close()


def test_psql_gets_the_connection_and_format_arguments(stubs):
    ex = make(stubs)
    ex.run_sql("127.0.0.1", 5432, "marketplace", sql="SELECT 1")
    a = _args(stubs["rec"], "psql_argv")
    assert a[a.index("-h") + 1] == "127.0.0.1" and a[a.index("-p") + 1] == "5432"
    assert a[a.index("-U") + 1] == "apcat" and a[a.index("-d") + 1] == "marketplace"
    assert a[a.index("-F") + 1] == FIELD_SEP and a[a.index("-R") + 1] == RECORD_SEP
    assert a[-2:] == ["-c", "SELECT 1"]
    ex.close()


@pytest.mark.parametrize("sql", [
    "SELECT 'a'' || $$b$$; touch /tmp/apcat-pwned",
    "SELECT `id`, $(id), \"q\" FROM t WHERE x = '\\' -- c\nAND y = 1",
    "SELECT 1;\nSELECT 2",
])
def test_sql_reaches_psql_unchanged_and_nothing_else_runs(stubs, sql):
    """Quotes, $(), backticks and newlines stay inside one argument."""
    ex = make(stubs)
    ex.run_sql("127.0.0.1", 5432, "d", sql=sql)
    assert _args(stubs["rec"], "psql_argv")[-1] == sql
    assert not Path("/tmp/apcat-pwned").exists()
    ex.close()


def test_the_password_is_read_from_the_file_on_the_host(stubs, tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("s3cret-from-file\n")
    ex = make(stubs, password_file=str(pw))
    ex.run_sql("127.0.0.1", 5432, "d", sql="SELECT 1")
    assert (stubs["rec"] / "psql_password").read_text() == "s3cret-from-file"
    joined = " ".join(_args(stubs["rec"], "ssh_argv"))
    assert "s3cret" not in joined  # not on the ssh command line
    ex.close()


def test_a_password_sent_on_stdin_is_not_on_any_command_line(stubs):
    ex = SshPsqlExecutor("ec2-user@10.0.1.50", "apcat", password="pw-on-stdin",  # nosec B106 - test value, not a password
                         known_hosts=stubs["known_hosts"])
    ex.run_sql("127.0.0.1", 5432, "d", sql="SELECT 1")
    assert (stubs["rec"] / "psql_password").read_text() == "pw-on-stdin"
    assert "pw-on-stdin" not in " ".join(_args(stubs["rec"], "ssh_argv"))
    assert "pw-on-stdin" not in " ".join(_args(stubs["rec"], "psql_argv"))
    ex.close()


def test_a_failing_command_raises_with_the_error_text(stubs, monkeypatch):
    monkeypatch.setenv("STUB_PSQL_FAIL", "psql: error: connection refused")
    ex = make(stubs)
    with pytest.raises(RuntimeError, match="rc=3.*connection refused"):
        ex.run_sql("127.0.0.1", 5432, "d", sql="SELECT 1")
    ex.close()


def test_a_command_over_the_time_limit_raises_timeout(stubs):
    ex = make(stubs)
    with pytest.raises(TimeoutError):
        ex.run_command("sleep 5", timeout=1)
    ex.close()


def test_a_missing_ssh_client_is_reported(stubs):
    ex = SshPsqlExecutor("h", "apcat", password_file="/p", known_hosts=stubs["known_hosts"],  # nosec B106 - test value, not a password
                         ssh_path="/nonexistent/ssh")
    with pytest.raises(RuntimeError, match="not found"):
        ex.run_command("true")
    ex.close()


@pytest.mark.parametrize("kw,match", [
    ({"ssh_target": "-oProxyCommand=x"}, "ssh_target"),
    ({"ssh_target": "a b"}, "ssh_target"),
    ({"user": "a;b"}, "database user"),
    ({"password_file": "/a/../b"}, "password_file"),  # nosec B105 - test value, not a password
    ({"password_file": "relative"}, "password_file"),  # nosec B105 - test value, not a password
    ({"ssh_port": 0}, "ssh_port"),
    ({"ssh_port": True}, "ssh_port"),
    ({"ssh_port": 70000}, "ssh_port"),
    ({"password_file": None, "password": None}, "password"),  # nosec B105 - test value, not a password
])
def test_bad_values_are_refused_before_anything_runs(stubs, kw, match):
    args = {"ssh_target": "h", "user": "apcat", "password_file": "/p",  # nosec B105 - test value, not a password
            "known_hosts": stubs["known_hosts"], **kw}
    with pytest.raises(ValueError, match=match):
        SshPsqlExecutor(**args)


def test_a_missing_known_hosts_file_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SSH_KNOWN_HOSTS", str(tmp_path / "nope"))
    with pytest.raises(FileNotFoundError):
        SshPsqlExecutor("h", "apcat", password_file="/p")  # nosec B106 - test value, not a password


def test_close_removes_the_socket_directory(stubs):
    ex = make(stubs)
    directory = ex._control_dir
    assert os.path.isdir(directory)
    ex.close()
    assert not os.path.exists(directory)
    ex.close()  # a second close is harmless


def test_probe_reports_reachable_and_unreachable(stubs, monkeypatch):
    ex = make(stubs)
    assert ex.probe()["reachable"] is True  # the stub psql answers with ok = 1
    monkeypatch.setenv("STUB_PSQL_FAIL", "boom")
    r = ex.probe()
    assert r["reachable"] is False and "boom" in r["error"]
    ex.close()
    json.dumps(r)  # serialisable
