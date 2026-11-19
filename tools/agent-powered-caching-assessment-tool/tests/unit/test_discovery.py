"""Database discovery: the scan parser, each source, discover() and the route. No network."""

import json

import pytest
from fastapi.testclient import TestClient

from src.api import common
from src.api.main import app
from src.api.routes import discovery as route
from src.contracts.api_models import JobInput, JobRecord
from src.orchestrator.job_store import LocalJobStore
from src.tools.database.ssh_executor import SshPsqlExecutor
from src.tools.discovery import discover as disc
from src.tools.discovery import pg_service, server_scan, ssh_config

EC2 = """\
** banner the scan must ignore
#ps
3215785 postgres /usr/bin/postgres -D /var/lib/pgsql/data
#sockets
/run/postgresql/.s.PGSQL.5432
/run/postgresql/.s.PGSQL.5432.lock
/tmp/.s.PGSQL.5432
#listen
LISTEN 0      128      0.0.0.0:22   0.0.0.0:*
LISTEN 0      400    127.0.0.1:5432 0.0.0.0:*
#lsclusters
#end
"""

DEBIAN = """\
#ps
900 postgres /usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/16/main -c config_file=/etc/postgresql/16/main/postgresql.conf
901 postgres postgres: 16/main: checkpointer
950 postgres /usr/lib/postgresql/15/bin/postgres -D /var/lib/postgresql/15/old -c port=5433 -c secret_setting=hunter2
#sockets
/var/run/postgresql/.s.PGSQL.5432
/var/run/postgresql/.s.PGSQL.5433
#listen
LISTEN 0 244 0.0.0.0:5432 0.0.0.0:*
LISTEN 0 244 127.0.0.1:5433 0.0.0.0:*
LISTEN 0 244 0.0.0.0:6379 0.0.0.0:*
#lsclusters
15 old  5433 down   postgres /var/lib/postgresql/15/old /var/log/postgresql/postgresql-15-old.log
16 main 5432 online postgres /var/lib/postgresql/16/main /var/log/postgresql/postgresql-16-main.log
#end
"""

MAC = """\
#ps
501 me /opt/homebrew/opt/postgresql@17/bin/postgres -D /opt/homebrew/var/postgresql@17
#sockets
/tmp/.s.PGSQL.5432
#listen
tcp4       0      0  127.0.0.1.5432         *.*                    LISTEN
tcp6       0      0  ::1.5432               *.*                    LISTEN
tcp4       0      0  *.22                   *.*                    LISTEN
#lsclusters
#end
"""


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def scan(text: str) -> list[server_scan.FoundServer]:
    return server_scan.merge(server_scan.split_sections(text))


def test_ec2_one_server_on_loopback_version_unknown():
    (s,) = scan(EC2)
    assert (s.port, s.data_directory, s.owner, s.version) == (5432, "/var/lib/pgsql/data",
                                                              "postgres", None)
    assert s.listen_addresses == ["127.0.0.1"]
    assert s.socket_dirs == ["/run/postgresql", "/tmp"]


def test_debian_two_clusters_children_skipped_other_args_dropped():
    servers = scan(DEBIAN)
    assert [s.port for s in servers] == [5432, 5433]
    main, old = servers
    assert main.version == "16" and old.version == "15"
    assert main.data_directory == "/var/lib/postgresql/16/main"
    assert any("every network interface" in n for n in main.notes)
    assert any("down" in n for n in old.notes)
    assert "hunter2" not in json.dumps([s.__dict__ for s in servers])
    assert all(s.port != 6379 for s in servers)


def test_mac_homebrew_version_from_path_and_bsd_netstat():
    (s,) = scan(MAC)
    assert s.version == "17" and s.port == 5432
    assert s.listen_addresses == ["127.0.0.1", "::1"]


def test_an_open_port_alone_is_not_a_server():
    out = "#ps\n#sockets\n#listen\nLISTEN 0 1 127.0.0.1:5432 0.0.0.0:*\n#lsclusters\n#end\n"
    assert scan(out) == []


def test_a_process_without_a_visible_port_is_reported_with_a_note():
    out = "#ps\n7 pg /usr/bin/postgres -D /data\n#sockets\n#listen\n#lsclusters\n#end\n"
    (s,) = scan(out)
    assert s.port is None and any("port could not be seen" in n for n in s.notes)


def test_incomplete_output_is_refused():
    with pytest.raises(server_scan.ScanError):
        server_scan.split_sections("#ps\n1 postgres /usr/bin/postgres\n")


@pytest.mark.parametrize("line", [
    "1 postgres /usr/bin/postgres -D '/tmp/x;rm -rf /'",
    "1 postgres /usr/bin/postgres -D $(touch /tmp/pwn)",
    "1 bad;user /usr/bin/postgres -D /data",
])
def test_hostile_values_are_not_kept(line):
    s = server_scan.parse_process(line)
    assert s is None or (s.data_directory in (None, "/data") and s.owner in (None, "postgres"))


def test_the_scan_script_never_uses_ps_e():
    assert "ps -A" in server_scan.SCAN_SCRIPT and "ps -e" not in server_scan.SCAN_SCRIPT


# ---------------------------------------------------------------------------
# ssh_config and pg_service
# ---------------------------------------------------------------------------


def test_ssh_config_plain_aliases_only_and_no_identity_file():
    text = """
Host *
  ServerAliveInterval 30
Host db1 db2
  HostName 10.0.0.5
  User ec2-user
  Port 2222
  IdentityFile ~/.ssh/secret_key
Host !bad db3 web-*
Include ~/.ssh/other
Match host x
  User nobody
Host db1
  User someoneelse
Host -oProxyCommand=sh
"""
    r = ssh_config.parse_ssh_config(text)
    assert [h.alias for h in r.hosts] == ["db1", "db2", "db3"]
    db1 = r.hosts[0]
    assert (db1.hostname, db1.user, db1.port) == ("10.0.0.5", "ec2-user", 2222)
    assert "secret_key" not in json.dumps([h.__dict__ for h in r.hosts])
    assert any("Include" in n for n in r.notes) and any("wildcard" in n for n in r.notes)


def test_pg_service_never_returns_a_password(tmp_path):
    p = tmp_path / "svc.conf"
    p.write_text("[prod]\nhost=10.0.0.9\nport=5432\ndbname=shop\nuser=app\npassword=hunter2\n"
                 "[bad name]\nhost=x\n[local]\ndbname=dev\n")
    r = pg_service.read_pg_service(p)
    assert [s.name for s in r.services] == ["prod", "local"]
    assert r.services[0].has_password and "hunter2" not in json.dumps([s.__dict__ for s in r.services])
    assert r.notes


def test_missing_files_are_none(tmp_path):
    assert ssh_config.read_ssh_config(tmp_path / "nope") is None
    assert pg_service.read_pg_service(tmp_path / "nope") is None


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


class FakeExecutor:
    def __init__(self, output: str = EC2, fail: Exception | None = None):
        self.output, self.fail, self.commands, self.closed = output, fail, [], False

    def run_command(self, command: str, timeout: int = 0) -> str:
        self.commands.append(command)
        if self.fail:
            raise self.fail
        return self.output

    def close(self) -> None:
        self.closed = True


def run(tmp_path, **kw):
    kw.setdefault("local_scan", lambda: MAC)
    kw.setdefault("ssh_config_path", tmp_path / "no-ssh-config")
    kw.setdefault("pg_service_path", tmp_path / "no-service")
    return disc.discover(**kw)


def test_local_sources_report_each_status(tmp_path):
    r = run(tmp_path)
    st = {s.source: s.status for s in r.sources}
    assert st == {"local_server": "ok", "ssh_config": "unavailable", "pg_service": "unavailable",
                  "earlier_assessment": "unavailable"}
    (s,) = r.servers
    assert s.kind == "local" and s.host == "127.0.0.1" and s.connected is False


def test_a_failing_source_does_not_fail_the_rest(tmp_path):
    def broken():
        raise server_scan.ScanError("The server scan took longer than 20 s.")
    r = run(tmp_path, local_scan=broken)
    assert {s.source: s.status for s in r.sources}["local_server"] == "error"


def test_unexpected_errors_are_contained(tmp_path):
    def boom():
        raise KeyError("x")
    r = run(tmp_path, local_scan=boom)
    rep = {s.source: s for s in r.sources}["local_server"]
    assert rep.status == "error" and "unexpectedly" in rep.detail


def test_host_probe_uses_the_fixed_script_and_closes(tmp_path):
    fake = FakeExecutor()
    r = run(tmp_path, host=disc.HostProbeTarget("ec2-user@10.0.0.1"),
            executor_factory=lambda _t: fake)
    assert fake.commands == [server_scan.SCAN_SCRIPT] and fake.closed
    probe = [s for s in r.servers if s.source == "host_probe"]
    assert len(probe) == 1 and probe[0].ssh_target == "ec2-user@10.0.0.1"
    assert probe[0].database is None and r.probed_host == "ec2-user@10.0.0.1"


def test_host_probe_ssh_failure_is_fixed_text(tmp_path):
    fake = FakeExecutor(fail=RuntimeError("ssh: connect to host 10.0.0.1 port 22: user bob"))
    r = run(tmp_path, host=disc.HostProbeTarget("10.0.0.1"), executor_factory=lambda _t: fake)
    rep = {s.source: s for s in r.sources}["host_probe"]
    assert rep.status == "error" and "bob" not in rep.detail and "10.0.0.1" not in rep.detail


def test_a_hostile_target_never_builds_an_executor(tmp_path):
    called = []
    r = run(tmp_path, host=disc.HostProbeTarget("-oProxyCommand=sh"),
            executor_factory=lambda t: called.append(t))
    assert called == [] and {s.source: s.status for s in r.sources}["host_probe"] == "error"


def test_earlier_assessments_grouped(tmp_path):
    store = LocalJobStore(tmp_path)
    for i, (db, ts) in enumerate([("shop", "2026-10-01"), ("shop", "2026-10-03"), ("crm", "2026-10-02")]):
        store.create(JobRecord(job_id=f"j{i}", source="live", created_at=ts,
                               input=JobInput(cluster_endpoint="127.0.0.1", port=5432,
                                              database_name=db),
                               workload_verdict="RECOMMENDED" if ts == "2026-10-03" else None))
    r = run(tmp_path, artifact_root=tmp_path)
    past = {s.database: s for s in r.servers if s.source == "earlier_assessment"}
    assert past["shop"].runs == 2 and past["shop"].last_verdict == "RECOMMENDED"
    assert past["crm"].runs == 1


def test_commands_only_executor_needs_no_password_and_refuses_sql(tmp_path, monkeypatch):
    kh = tmp_path / "known_hosts"
    kh.write_text("")
    ex = SshPsqlExecutor("ec2-user@10.0.0.1", "", commands_only=True, known_hosts=kh)
    try:
        with pytest.raises(RuntimeError):
            ex.run_sql("127.0.0.1", 5432, "postgres", "SELECT 1")
    finally:
        ex.close()
    with pytest.raises(ValueError):
        SshPsqlExecutor("ec2-user@10.0.0.1", "", known_hosts=kh)


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    real = disc.discover

    def fake_discover(**kw):
        kw.setdefault("local_scan", lambda: MAC)
        kw.setdefault("ssh_config_path", tmp_path / "none")
        kw.setdefault("pg_service_path", tmp_path / "none")
        kw.setdefault("executor_factory", lambda _t: FakeExecutor())
        return real(**kw)

    monkeypatch.setattr(route, "discover", fake_discover)
    return TestClient(app, client=("127.0.0.1", 50000))


def test_route_default_and_with_a_host(client):
    r = client.post("/api/v1/databases/discover", json={})
    assert r.status_code == 200, r.text
    assert [s["source"] for s in r.json()["sources"]] == [
        "local_server", "ssh_config", "pg_service", "earlier_assessment"]
    r = client.post("/api/v1/databases/discover",
                    json={"sources": ["local_server"], "host": {"ssh_target": "db1"}})
    body = r.json()
    assert r.status_code == 200 and body["probed_host"] == "db1"
    st = {s["source"]: s["status"] for s in body["sources"]}
    assert st["host_probe"] == "ok" and st["ssh_config"] == "skipped"


def test_route_refuses_bad_values(client):
    r = client.post("/api/v1/databases/discover", json={"host": {"ssh_target": "-oProxyCommand=x"}})
    assert r.status_code == 422 and r.json()["code"] == "invalid_target"
    assert "ProxyCommand" not in r.text
    r = client.post("/api/v1/databases/discover", json={"sources": ["docker"]})
    assert r.status_code == 422
    r = client.post("/api/v1/databases/discover", json={"host": {"ssh_target": "db1",
                                                                  "identity_file": "../k"}})
    assert r.status_code == 422 and r.json()["code"] == "invalid_target"


def test_route_is_capped(client):
    held = [route._slots.acquire(blocking=False) for _ in range(common.MAX_CONCURRENT_DISCOVERIES)]
    try:
        r = client.post("/api/v1/databases/discover", json={})
        assert r.status_code == 429 and r.json()["code"] == "discovery_busy"
        assert r.headers["retry-after"] == "10"
    finally:
        for ok in held:
            if ok:
                route._slots.release()


def test_route_is_loopback_only(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.9", 50000))
    assert remote.post("/api/v1/databases/discover", json={}).status_code == 403


def test_route_audit_has_counts_not_targets(client, tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(log))
    client.post("/api/v1/databases/discover", json={"host": {"ssh_target": "ec2-user@db.example"}})
    text = log.read_text()
    assert '"event":"discovery"' in text and "db.example" not in text


def test_no_docker_source():
    # Decision: Docker is neither discovered nor collected.
    from src.contracts.discovery import LOCAL_SOURCES
    assert "docker" not in LOCAL_SOURCES
    assert "docker" not in server_scan.SCAN_SCRIPT
