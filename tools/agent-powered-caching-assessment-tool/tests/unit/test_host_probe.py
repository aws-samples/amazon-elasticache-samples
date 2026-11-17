"""The staged SSH host probe, trust-on-confirm, the key list, suggestions and the routes.

Every external call is a fake: no SSH, no DNS, no network.
"""

import json
import os
import socket
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import common
from src.api.main import app
from src.api.routes import hosts as route
from src.contracts.hosts import HostProbeResult, HostTarget
from src.tools.discovery import host_probe as hp
from src.tools.discovery import host_suggestions as hs
from src.tools.discovery import ssh_keys

# A throwaway key made for this test, with the fingerprint ssh-keygen -l -E sha256 printed.
KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIOWdbIOlnvQXGEPQ4HauW2enQZn9fJ2hDm6kWxFfxcXu"  # nosemgrep: generic-api-key - a public test key, not a secret
KEY_FP = "SHA256:6hoHnFekZhkLQkeD48eHbl5wF2kHCyr7FqbEnR7HyJk"
OTHER = "AAAAC3NzaC1lZDI1NTE5AAAAIHVoxcuZ7GYZ1u4vTVLe0Nvc1Y8r1f2bS8YxS0a8AAAA"  # nosemgrep: generic-api-key - a public test key, not a secret
SCAN_OUT = """\
#ps
10 postgres /usr/bin/postgres -D /var/lib/pgsql/data
#sockets
/run/postgresql/.s.PGSQL.5432
#listen
LISTEN 0 400 127.0.0.1:5432 0.0.0.0:*
#lsclusters
#end
"""


def ssh_g(host="10.0.0.5", port=22, user="ec2-user", extra=""):
    return f"user {user}\nhostname {host}\nport {port}\n{extra}"


# What the database listing prints for the 5432 instance in SCAN_OUT, with sudo.
CATALOG_OUT = ("#access sudo\n#cluster 5432 ok 160015 t\n"
               "#db 5432 retail 1288490188 1\n#db 5432 orders 7800000 0\n#end\n")


class FakeExecutor:
    def __init__(self, shell_reply: str | Exception = hp.SHELL_MARKER + "\n", scan: str | Exception = SCAN_OUT,
                 catalog: str | Exception = CATALOG_OUT):
        self.shell, self.scan, self.catalog, self.closed, self.commands = shell_reply, scan, catalog, False, []

    def run_command(self, command, timeout=0):
        self.commands.append(command)
        out = (self.shell if command.startswith("echo")
               else self.catalog if "sudo -n -u postgres" in command else self.scan)
        if isinstance(out, Exception):
            raise out
        return out

    def close(self):
        self.closed = True


class World:
    """Fakes for every external call, set up as a reachable host with a known key."""

    def __init__(self, tmp: Path, *, g=None, keyscan=None, known=None, connect=None, dns=None,
                 executor=None, ip="198.51.100.7"):
        self.kh = tmp / "known_hosts"
        if known is not None:
            self.kh.write_text(known)
        self.g = ssh_g() if g is None else g
        self.keyscan = f"# 10.0.0.5:22 SSH-2.0-OpenSSH\n10.0.0.5 ssh-ed25519 {KEY}\n" if keyscan is None else keyscan
        self.connect_exc, self.dns_exc = connect, dns
        self.exec = executor or FakeExecutor()
        self.ip = ip
        self.calls: list[list[str]] = []

    def run(self, argv, timeout):
        self.calls.append(argv)
        if argv[:2] == ["ssh", "-G"]:
            return 0, self.g, ""
        if argv[0] == "ssh-keyscan":
            return 0, self.keyscan, ""
        if argv[0] == "ssh-keygen" and argv[1] == "-F":
            name = argv[2]
            lines = [ln for ln in (self.kh.read_text().splitlines() if self.kh.exists() else [])
                     if ln.split()[0] == name]
            return (0, "\n".join(lines) + "\n", "") if lines else (1, "", "")
        raise AssertionError(argv)

    def deps(self) -> hp.ProbeDeps:
        def dns(*_a, **_k):
            if self.dns_exc:
                raise self.dns_exc
            return [()]

        def connect(*_a):
            if self.connect_exc:
                raise self.connect_exc

        return hp.ProbeDeps(run=self.run, resolve_dns=dns, connect=connect,
                            executor=lambda _t, _k: self.exec, public_ip=lambda: self.ip,
                            resolve_key=lambda n: Path("/keys") / n, known_hosts=lambda: self.kh)


def stages(r: HostProbeResult) -> dict[str, str]:
    return {s.name: s.status for s in r.stages}


T = HostTarget(ssh_target="ec2-user@10.0.0.5")


# --- pieces ----------------------------------------------------------------


def test_fingerprint_matches_ssh_keygen():
    assert hp.fingerprint(KEY) == KEY_FP


def test_parse_keyscan_keeps_allowed_types_only():
    out = (f"# banner\nh ssh-ed25519 {KEY}\nh evil-type {KEY}\nh ssh-rsa not!base64\n"
           f"h ssh-ed25519 {KEY}\nh ssh-rsa {OTHER}\n")
    assert hp.parse_keyscan(out) == [("ssh-ed25519", KEY), ("ssh-rsa", OTHER)]


def test_parse_ssh_g():
    r = hp.parse_ssh_g(ssh_g(extra="proxyjump bastion\nhostkeyalias db1\n"))
    assert (r.hostname, r.port, r.user, r.via_jump, r.hostkeyalias) == ("10.0.0.5", 22, "ec2-user", True, "db1")
    assert hp.parse_ssh_g("hostname bad;name\nport 22\n") is None
    assert hp.known_hosts_name(hp.Resolved("db", 2222, None)) == "[db]:2222"


def test_key_state():
    assert hp.key_state([("ssh-ed25519", KEY)], [("ssh-ed25519", KEY)]) == "known"
    assert hp.key_state([("ssh-ed25519", KEY)], [("ssh-ed25519", OTHER)]) == "changed"
    assert hp.key_state([("ssh-ed25519", KEY)], []) == "unknown"
    assert hp.key_state([("ssh-ed25519", KEY)], [("ssh-rsa", OTHER)]) == "unknown"


@pytest.mark.parametrize("text,stage", [
    ("Permission denied (publickey).", "sign_in"),
    ("Host key verification failed.", "host_key"),
    ("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", "host_key"),
    ("ssh: connect to host x port 22: Connection refused", "reachable"),
    ("ssh: connect to host x port 22: Operation timed out", "reachable"),
    ("ssh: Could not resolve hostname x", "resolve"),
    ("Disallowed command", "shell"),
    ("Hi bob! You've successfully authenticated, but GitHub does not provide shell access.", "shell"),
    ("something new and strange about user bob at 10.0.0.5", "sign_in"),
])
def test_classify_never_returns_the_text(text, stage):
    s, message, hint = hp.classify(text)
    assert s == stage
    assert "bob" not in message + hint and "10.0.0.5" not in message + hint


# --- probe ------------------------------------------------------------------


def test_every_stage_passes(tmp_path):
    w = World(tmp_path, known=f"10.0.0.5 ssh-ed25519 {KEY}\n")
    r = hp.probe_host(T, w.deps())
    assert r.ok and set(stages(r).values()) == {"pass"}, stages(r)
    assert r.host_key.state == "known" and [c.port for c in r.clusters] == [5432]
    assert r.public_ip is None and w.exec.closed and r.resolved_user == "ec2-user"


def test_clusters_carry_the_key_the_probe_used(tmp_path):
    w = World(tmp_path, known=f"10.0.0.5 ssh-ed25519 {KEY}\n")
    r = hp.probe_host(HostTarget(ssh_target="ec2-user@10.0.0.5", identity_file="db_key"), w.deps())
    assert [(c.ssh_target, c.identity_file) for c in r.clusters] == [("ec2-user@10.0.0.5", "db_key")]
    r = hp.probe_host(T, w.deps())
    assert r.clusters[0].identity_file is None


def test_hostile_target_runs_nothing(tmp_path):
    w = World(tmp_path)
    r = hp.probe_host(HostTarget(ssh_target="-oProxyCommand=sh"), w.deps())
    assert stages(r)["resolve"] == "fail" and w.calls == [] and not r.ok
    assert "Proxy" not in json.dumps(r.model_dump()["stages"])


def test_dns_failure(tmp_path):
    w = World(tmp_path, dns=socket.gaierror("nope"))
    r = hp.probe_host(T, w.deps())
    assert stages(r)["resolve"] == "fail" and stages(r)["reachable"] == "skipped"


def test_timeout_carries_the_public_ip(tmp_path):
    w = World(tmp_path, connect=TimeoutError())
    r = hp.probe_host(T, w.deps())
    st = {s.name: s for s in r.stages}
    assert st["reachable"].status == "fail" and r.public_ip == "198.51.100.7"
    assert "198.51.100.7" in st["reachable"].hint
    assert not any(c[0] == "ssh-keyscan" for c in w.calls)


def test_refused_has_no_public_ip(tmp_path):
    w = World(tmp_path, connect=ConnectionRefusedError())
    r = hp.probe_host(T, w.deps())
    assert stages(r)["reachable"] == "fail" and r.public_ip is None


def test_unknown_key_gives_the_fingerprint_and_stops(tmp_path):
    w = World(tmp_path)
    r = hp.probe_host(T, w.deps())
    assert stages(r)["host_key"] == "fail" and stages(r)["sign_in"] == "skipped"
    assert r.host_key.state == "unknown" and r.host_key.fingerprint == KEY_FP
    assert w.exec.commands == []


def test_changed_key_is_a_hard_stop(tmp_path):
    w = World(tmp_path, known=f"10.0.0.5 ssh-ed25519 {OTHER}\n")
    r = hp.probe_host(T, w.deps())
    assert r.host_key.state == "changed" and stages(r)["host_key"] == "fail"
    assert w.exec.commands == []


def test_key_refused(tmp_path):
    w = World(tmp_path, known=f"10.0.0.5 ssh-ed25519 {KEY}\n",
              executor=FakeExecutor(shell_reply=RuntimeError("ssh command failed (rc=255): Permission denied (publickey).")))
    r = hp.probe_host(T, w.deps())
    assert stages(r)["sign_in"] == "fail" and "publickey" not in json.dumps(r.model_dump())


@pytest.mark.parametrize("shell", [RuntimeError("ssh command failed (rc=1): Disallowed command"),
                                   "Welcome\n"])
def test_git_host_stops_at_shell(tmp_path, shell):
    w = World(tmp_path, known=f"10.0.0.5 ssh-ed25519 {KEY}\n", executor=FakeExecutor(shell_reply=shell))
    r = hp.probe_host(T, w.deps())
    assert stages(r)["sign_in"] == "pass" and stages(r)["shell"] == "fail" and not r.ok


def test_no_postgres_is_a_warning(tmp_path):
    empty = "#ps\n#sockets\n#listen\n#lsclusters\n#end\n"
    w = World(tmp_path, known=f"10.0.0.5 ssh-ed25519 {KEY}\n", executor=FakeExecutor(scan=empty))
    r = hp.probe_host(T, w.deps())
    assert r.ok and stages(r)["postgresql"] == "warn" and r.clusters == []


def test_jump_host_skips_direct_checks(tmp_path):
    w = World(tmp_path, g=ssh_g(extra="proxyjump bastion\n"))
    r = hp.probe_host(T, w.deps())
    assert stages(r)["reachable"] == "skipped" and stages(r)["host_key"] == "skipped"
    assert r.ok and r.via_jump_host


def test_unusable_key_name(tmp_path):
    w = World(tmp_path)
    deps = w.deps()
    deps.resolve_key = lambda _n: (_ for _ in ()).throw(FileNotFoundError("the key file does not exist"))
    r = hp.probe_host(HostTarget(ssh_target="db", identity_file="missing"), deps)
    assert stages(r)["resolve"] == "fail" and w.calls == []


# --- trust ------------------------------------------------------------------


def test_trust_adds_once_and_never_replaces(tmp_path):
    w = World(tmp_path)
    t = hp.trust_host(T, "SHA256:" + "A" * 43, w.deps())
    assert t.outcome == "mismatch" and not w.kh.exists()
    t = hp.trust_host(T, KEY_FP, w.deps())
    assert t.outcome == "added" and t.trusted
    assert w.kh.read_text() == f"10.0.0.5 ssh-ed25519 {KEY}\n"
    assert stat.S_IMODE(os.stat(w.kh).st_mode) == 0o600
    assert hp.trust_host(T, KEY_FP, w.deps()).outcome == "already_known"
    w.kh.write_text(f"10.0.0.5 ssh-ed25519 {OTHER}\n")
    t = hp.trust_host(T, KEY_FP, w.deps())
    assert t.outcome == "changed" and not t.trusted
    assert w.kh.read_text() == f"10.0.0.5 ssh-ed25519 {OTHER}\n"


def test_trust_uses_bracketed_name_for_another_port(tmp_path):
    w = World(tmp_path, g=ssh_g(port=2222), known="other ssh-ed25519 " + OTHER)
    t = hp.trust_host(HostTarget(ssh_target="db", ssh_port=2222), KEY_FP, w.deps())
    assert t.outcome == "added"
    assert w.kh.read_text().splitlines() == ["other ssh-ed25519 " + OTHER, f"[10.0.0.5]:2222 ssh-ed25519 {KEY}"]


def test_trust_refuses_a_jump_host(tmp_path):
    w = World(tmp_path, g=ssh_g(extra="proxycommand nc %h %p\n"))
    assert hp.trust_host(T, KEY_FP, w.deps()).outcome == "unavailable"


# --- keys and suggestions ---------------------------------------------------


def test_key_list_names_and_fingerprints_only(tmp_path, monkeypatch):
    d = tmp_path / "keys"
    d.mkdir()
    for name, mode in (("good", 0o600), ("loose", 0o644), ("known_hosts", 0o600)):
        (d / name).write_text("PRIVATE KEY MATERIAL")
        os.chmod(d / name, mode)
    (d / "good.pub").write_text("ssh-ed25519 AAAA bob@laptop")
    monkeypatch.setenv("SSH_KEY_DIR", str(d))
    seen = []

    def run(argv, _t):
        seen.append(argv[-1])
        return 0, f"256 {KEY_FP} bob@laptop (ED25519)\n", ""

    keys = ssh_keys.list_keys(run)
    by = {k.name: k for k in keys.items}
    assert set(by) == {"good", "loose"}
    assert by["good"].usable and by["good"].fingerprint == KEY_FP and by["good"].key_type == "ED25519"
    assert not by["loose"].usable and "chmod 600" in by["loose"].problem
    assert str(d / "good.pub") in seen
    text = json.dumps(keys.model_dump())
    assert "PRIVATE" not in text and "bob@laptop" not in text
    monkeypatch.delenv("SSH_KEY_DIR")
    assert ssh_keys.list_keys(run).configured is False


def test_code_host_names():
    for n in ("ssh.gitlab.example.com", "github.com", "git.example.com", "code.example.com", "git@x"):
        assert hs.is_code_host(n), n
    for n in ("db1.internal", "ec2-user@10.0.0.5", "digitals-db"):
        assert not hs.is_code_host(n), n


def test_suggestions_order_dedupe_and_recent(tmp_path):
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host db1\n  HostName 10.0.0.9\nHost ssh.gitlab.example.com\n  User git\n")
    result = HostProbeResult(ok=False, ssh_target="db1", ssh_port=22, stages=[
        {"name": "resolve", "status": "pass", "message": "m"},
        {"name": "reachable", "status": "fail", "message": "m"}], duration_seconds=0.1)
    for _ in range(25):
        hs.record_probe(tmp_path, None, result)
    rows = hs.read_recent(tmp_path)
    assert len(rows) == 1 and rows[0]["last_result"] == "reachable"
    f = tmp_path / "_hosts" / "recent.json"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    out = hs.suggestions(tmp_path, cfg).items
    assert [(s.ssh_target, s.source) for s in out] == [("db1", "recent_probe"),
                                                       ("ssh.gitlab.example.com", "ssh_config")]
    assert out[1].code_host and not out[0].code_host


# --- routes -----------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    w = World(tmp_path, known=f"10.0.0.5 ssh-ed25519 {KEY}\n")
    real_probe, real_trust = hp.probe_host, hp.trust_host
    monkeypatch.setattr(route.host_probe, "probe_host", lambda t: real_probe(t, w.deps()))
    monkeypatch.setattr(route.host_probe, "trust_host", lambda t, fp: real_trust(t, fp, w.deps()))
    return TestClient(app, client=("127.0.0.1", 50000))


def test_probe_route_records_and_audits_statuses_only(client, tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(log))
    r = client.post("/api/v1/hosts/probe", json={"ssh_target": "ec2-user@10.0.0.5"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert hs.read_recent(tmp_path)[0]["ssh_target"] == "ec2-user@10.0.0.5"
    text = log.read_text()
    assert '"event":"host_probe"' in text and "10.0.0.5" not in text and "ec2-user" not in text
    s = client.get("/api/v1/hosts/suggestions").json()["items"]
    assert s[0]["ssh_target"] == "ec2-user@10.0.0.5" and s[0]["last_result"] == "ok"


def test_probe_route_refuses_bad_values_and_is_capped(client):
    r = client.post("/api/v1/hosts/probe", json={"ssh_target": "-oProxyCommand=sh"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_target" and "Proxy" not in r.text
    held = [route._slots.acquire(blocking=False) for _ in range(common.MAX_CONCURRENT_HOST_PROBES)]
    try:
        r = client.post("/api/v1/hosts/probe", json={"ssh_target": "db1"})
        assert r.status_code == 429 and r.json()["code"] == "host_probe_busy"
    finally:
        for ok in held:
            if ok:
                route._slots.release()


def test_trust_route(client):
    r = client.post("/api/v1/hosts/trust", json={"ssh_target": "db1", "fingerprint": KEY_FP})
    assert r.status_code == 200 and r.json()["outcome"] == "already_known"
    r = client.post("/api/v1/hosts/trust", json={"ssh_target": "db1", "fingerprint": "nope"})
    assert r.status_code == 422


def test_request_host_probe_is_a_browser_form():
    from src.agents.chat.prompts import SYSTEM_PROMPT
    from src.api import agent_tools
    t = agent_tools.BY_NAME["request_host_probe"]
    assert t.executor == "client" and t.kind == "form"
    assert "request_host_probe" in SYSTEM_PROMPT and "never tell them to skip the check" in SYSTEM_PROMPT


def test_routes_are_loopback_only(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARTIFACT_ROOT", str(tmp_path))
    remote = TestClient(app, client=("203.0.113.9", 50000))
    assert remote.post("/api/v1/hosts/probe", json={"ssh_target": "db1"}).status_code == 403
    assert remote.get("/api/v1/ssh-keys").status_code == 403
