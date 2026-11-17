"""Listing databases as postgres through sudo: the real script against stand-in sudo and psql,
the parser, and the probe's databases stage. Spec: .kiro/specs/ssh-host-probe (listing)."""
import subprocess
from pathlib import Path

import pytest

from src.contracts.hosts import HostTarget
from src.tools.discovery import host_probe as hp
from src.tools.discovery import pg_catalog as pc

SUDO = """#!/bin/sh
[ "$1" = "-n" ] && shift
[ "$1" = "-u" ] && shift 2
if [ "$1" = "true" ]; then exit "${FAKE_SUDO_RC:-0}"; fi
exec "$@"
"""

# Answers like psql -At: finds -d and -c among its arguments.
PSQL = r"""#!/bin/sh
db=""; sql=""
while [ $# -gt 0 ]; do
  case "$1" in -d) db="$2"; shift ;; -c) sql="$2"; shift ;; esac; shift
done
printf 'DB %s\n' "$db" >> "$FAKE_LOG"
case "$sql" in
  *server_version_num*) echo 160015 ;;
  *shared_preload_libraries*) echo "${FAKE_PRELOAD:-t}" ;;
  *pg_database*) printf 'retail 1288490188\npostgres 7800000\norders 5000000\nbad;name 10\n-x 5\n' ;;
  *pg_extension*) [ "$db" = retail ] && echo 1 || echo 0 ;;
esac
"""


class LocalHost:
    def __init__(self, tmp_path: Path, **env):
        b = tmp_path / "bin"
        b.mkdir()
        for name, body in (("sudo", SUDO), ("psql", PSQL)):
            (b / name).write_text(body)
            (b / name).chmod(0o755)
        self.log = tmp_path / "log"
        self.log.write_text("")
        self.env = {"PATH": f"{b}:/usr/bin:/bin", "FAKE_LOG": str(self.log), **env}

    def run_command(self, command, timeout=60):
        return subprocess.run(["sh", "-c", command], env=self.env, capture_output=True,  # nosec B603 - fixed argv list, no shell  # nosemgrep: dangerous-subprocess-use-audit
                              text=True, timeout=timeout, check=True).stdout


def test_the_script_lists_databases_and_their_pg_stat_statements_state(tmp_path):
    host = LocalHost(tmp_path)
    r = pc.parse(host.run_command(pc.build_command([5432])))
    assert r.access == "sudo"
    c = r.clusters[5432]
    assert (c.ok, c.version_num, c.preloaded) == (True, 160015, True)
    assert [(d.name, d.size_bytes, d.extension) for d in c.databases] == [
        ("retail", 1288490188, True), ("orders", 5000000, False)]
    # A name that could be read as an option or a connection string is never used.
    assert "bad;name" not in host.log.read_text() and "DB -x" not in host.log.read_text()
    # The default postgres database is not listed, and not signed in to for the extension.
    assert "DB postgres" not in host.log.read_text()
    assert "datname <> 'postgres'" in pc.SCRIPT


def test_no_sudo_lists_nothing_and_signs_in_to_nothing(tmp_path):
    host = LocalHost(tmp_path, FAKE_SUDO_RC="1")
    r = pc.parse(host.run_command(pc.build_command([5432])))
    assert r.access == "no_sudo" and r.clusters == {}
    assert host.log.read_text() == ""


@pytest.mark.parametrize("pre,ext,state", [
    (True, True, "ready"), (True, False, "not_installed"), (False, True, "not_loaded"),
    (False, None, "not_loaded"), (None, True, None),
])
def test_pg_stat_statements_state(pre, ext, state):
    assert pc.statements_state(pre, ext) == state


def test_output_without_an_end_marker_is_not_trusted():
    assert pc.parse("#access sudo\n#cluster 5432 ok 160015 t\n").access == "failed"
    assert pc.parse("#weird\n#end\n").access == "failed"


def test_ports_must_be_numbers():
    with pytest.raises(ValueError):
        pc.build_command(["5432; rm -rf /"])  # type: ignore[list-item]
    assert pc.build_command([5432, 5433]).startswith("set -- 5432 5433;")


def test_major_version():
    assert (pc.major_version(160015), pc.major_version(90624), pc.major_version(None)) == ("16", "9.6", None)


# --- The probe's databases stage --------------------------------------------------------

def _probe(tmp_path, catalog):
    from tests.unit.test_host_probe import KEY, FakeExecutor, World
    w = World(tmp_path, known=f"10.0.0.5 ssh-ed25519 {KEY}\n")
    w.exec = FakeExecutor(catalog=catalog)
    return hp.probe_host(HostTarget(ssh_target="ec2-user@10.0.0.5"), w.deps())


def test_with_sudo_the_probe_lists_each_database(tmp_path):
    from tests.unit.test_host_probe import CATALOG_OUT
    r = _probe(tmp_path, CATALOG_OUT)
    stage = next(s for s in r.stages if s.name == "databases")
    assert (stage.status, stage.message) == ("pass", "2 databases listed as the postgres user, through sudo.")
    assert r.postgres_access == "sudo" and r.clusters[0].postgres_access == "sudo"
    assert r.clusters[0].version == "16"
    retail = r.databases[0]
    assert (retail.database, retail.port, retail.host, retail.pg_stat_statements) == (
        "retail", 5432, "127.0.0.1", "ready")
    assert retail.size_mb == pytest.approx(1228.8) and retail.notes[0] == "1.2 GB."
    assert r.databases[1].pg_stat_statements == "not_installed"
    assert len({d.id for d in r.databases} | {r.clusters[0].id}) == 3


def test_without_sudo_the_probe_still_passes_and_says_why(tmp_path):
    r = _probe(tmp_path, "#access no_sudo\n#end\n")
    stage = next(s for s in r.stages if s.name == "databases")
    assert r.ok and stage.status == "warn" and "cannot run commands as postgres" in stage.message
    assert r.postgres_access == "no_sudo" and r.databases == []
    assert r.clusters[0].postgres_access == "no_sudo"


def test_a_listing_that_fails_is_a_warning(tmp_path):
    r = _probe(tmp_path, TimeoutError("ssh command timed out after 45s"))
    assert r.ok and r.postgres_access == "failed"
    assert next(s for s in r.stages if s.name == "databases").status == "warn"


def test_a_host_with_only_the_default_database_lists_none(tmp_path):
    r = _probe(tmp_path, "#access sudo\n#cluster 5432 ok 160015 t\n#end\n")
    stage = next(s for s in r.stages if s.name == "databases")
    assert r.ok and r.databases == [] and r.postgres_access == "sudo"
    assert (stage.status, stage.message) == (
        "pass", "No databases found besides the default postgres database.")


def test_the_script_limit_is_max_databases():
    assert f"LIMIT {pc.MAX_DATABASES}\"" in pc.SCRIPT
