"""The local executor's binary backend: the host and port reach psql in its environment."""

import json
import os
import stat

import pytest

from src.tools.database.local_executor import LocalPsqlExecutor

# A stand-in psql that records its arguments and the variables psql would read.
PSQL = """#!/usr/bin/python3
import json, os, sys
keep = {k: os.environ.get(k) for k in ("PGHOST", "PGPORT", "PGPASSWORD")}
json.dump({"argv": sys.argv[1:], **keep}, open(OUT, "w"))
"""


@pytest.fixture
def psql(tmp_path):
    path = tmp_path / "psql"
    path.write_text(PSQL.replace("OUT", repr(str(tmp_path / "out.json"))))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path, tmp_path / "out.json"


def test_host_and_port_go_in_the_environment_not_on_the_command_line(psql):
    path, out = psql
    ex = LocalPsqlExecutor(user="apcat_reader", password="pw-in-env", backend="binary",  # nosec B106 - a test value  # nosemgrep: generic-api-key
                           psql_path=str(path))
    ex.run_sql("db.example.com", 5433, "retail", "SELECT 1")
    seen = json.loads(out.read_text())
    assert (seen["PGHOST"], seen["PGPORT"], seen["PGPASSWORD"]) == ("db.example.com", "5433", "pw-in-env")
    assert "db.example.com" not in seen["argv"] and "5433" not in seen["argv"] and "-h" not in seen["argv"]
    assert seen["argv"][:4] == ["-U", "apcat_reader", "-d", "retail"]


@pytest.mark.parametrize("host", ["-oProxyCommand=x", "", "a b", "db;id", "db\n", None])
def test_a_host_that_is_not_a_host_is_refused(psql, host):
    path, out = psql
    ex = LocalPsqlExecutor(user="u", password="p", backend="binary", psql_path=str(path))  # nosec B106 - a test value
    with pytest.raises(ValueError, match="host"):
        ex.run_sql(host, 5432, "retail", "SELECT 1")
    assert not out.exists()


@pytest.mark.parametrize("port", [0, 70000, "5432", True, None])
def test_a_port_that_is_not_a_port_is_refused(psql, port):
    path, out = psql
    ex = LocalPsqlExecutor(user="u", password="p", backend="binary", psql_path=str(path))  # nosec B106 - a test value
    with pytest.raises(ValueError, match="port"):
        ex.run_sql("127.0.0.1", port, "retail", "SELECT 1")
    assert not out.exists()


def test_ipv4_and_ipv6_hosts_are_accepted(psql):
    path, out = psql
    ex = LocalPsqlExecutor(user="u", password="p", backend="binary", psql_path=str(path))  # nosec B106 - a test value
    for host in ("127.0.0.1", "::1", "localhost"):
        ex.run_sql(host, 5432, "retail", "SELECT 1")
        assert json.loads(out.read_text())["PGHOST"] == host
        os.remove(out)
