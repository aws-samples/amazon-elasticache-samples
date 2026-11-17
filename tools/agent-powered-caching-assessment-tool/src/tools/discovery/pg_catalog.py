"""
List the databases in each PostgreSQL instance on a host, as the postgres user through sudo.

WHY
---
A probe finds instances (port, data directory) without signing in. To add a database the user
then had to know and type its name. When the SSH user may run `sudo -n -u postgres`, the
probe can read the catalog instead and list the databases directly, with their size and
whether pg_stat_statements is ready in each.

HOW
---
One constant POSIX sh script runs on the host as the SSH user. The ports come from the
instance scan, are checked to be numbers here and are passed as positional arguments, never
spliced into the script. It:

  - stops with `#access no_sudo` when sudo is missing or asks for a password (`sudo -n` never
    waits for one). The probe still succeeds; the user types the database name instead.
  - per port, reads the server version, whether pg_stat_statements is preloaded, and every
    database that accepts connections, largest first, at most 50. Templates and the default
    `postgres` database are left out: they are not what a user assesses.
  - per database, whether the pg_stat_statements extension is created in it.

It runs read-only catalog queries only: no role, setting or data is touched. A database
name is used only when it has the shape a collection accepts, so a name cannot be read as a
psql option or a connection string. Spec: .kiro/specs/ssh-host-probe (database listing).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

MAX_DATABASES = 50  # also the LIMIT in SCRIPT (test_pg_catalog checks they agree)
TIMEOUT_SEC = 45
_DATABASE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}")

# Positional arguments: the ports. Prints marked lines; #end is last.
SCRIPT = r"""
cd / 2>/dev/null
if ! command -v sudo >/dev/null 2>&1 || ! sudo -n -u postgres true >/dev/null 2>&1; then
  echo '#access no_sudo'; echo '#end'; exit 0
fi
echo '#access sudo'
pq() { p="$1"; d="$2"; shift 2; sudo -n -u postgres psql -X -q -At -p "$p" -d "$d" "$@" 2>/dev/null; }
for port in "$@"; do
  ver=$(pq "$port" template1 -c 'SHOW server_version_num') || { echo "#cluster $port fail"; continue; }
  pre=$(pq "$port" template1 -c "SELECT current_setting('shared_preload_libraries') LIKE '%pg_stat_statements%'")
  echo "#cluster $port ok $ver $pre"
  pq "$port" template1 -F ' ' -c "SELECT datname, pg_database_size(oid) FROM pg_database WHERE NOT datistemplate AND datallowconn AND datname <> 'postgres' ORDER BY 2 DESC LIMIT 50" |
  while read -r db size; do
    case "$db" in ''|-*|*[!A-Za-z0-9_.-]*|postgres) continue ;; esac
    case "$size" in ''|*[!0-9]*) continue ;; esac
    ext=$(pq "$port" "$db" -c "SELECT count(*) FROM pg_extension WHERE extname = 'pg_stat_statements'") || ext='?'
    echo "#db $port $db $size $ext"
  done
done
echo '#end'
"""


@dataclass
class CatalogDatabase:
    """One database in an instance."""

    name: str
    size_bytes: int
    extension: bool | None   # pg_stat_statements created in it; None when not known


@dataclass
class CatalogCluster:
    """One instance, by port."""

    port: int
    ok: bool
    version_num: int | None = None
    preloaded: bool | None = None
    databases: list[CatalogDatabase] = field(default_factory=list)


@dataclass
class CatalogResult:
    """What the listing found."""

    access: str                 # "sudo", "no_sudo" or "failed"
    clusters: dict[int, CatalogCluster] = field(default_factory=dict)

    @property
    def database_count(self) -> int:
        return sum(len(c.databases) for c in self.clusters.values())


def build_command(ports: list[int]) -> str:
    """The command for run_command: the ports as positional arguments, then the script.

    Raises:
        ValueError: a port is not a number from 1 to 65535.
    """
    clean = []
    for p in ports:
        if isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= 65535:
            raise ValueError("a port must be a number from 1 to 65535")
        clean.append(p)
    return f"set -- {' '.join(shlex.quote(str(p)) for p in clean)}; {SCRIPT}"


def parse(out: str) -> CatalogResult:
    """Read the script's marked lines. Lines that are not in the expected shape are ignored.

    Args:
        out: What the script printed.

    Returns:
        The access and, with sudo, each instance and its databases. `failed` when the
        output has no access line or no end marker.
    """
    lines = [ln.strip() for ln in (out or "").splitlines()]
    if "#end" not in lines:
        return CatalogResult(access="failed")
    access = next((ln.split()[1] for ln in lines if ln.startswith("#access ") and len(ln.split()) == 2), "")
    if access not in ("sudo", "no_sudo"):
        return CatalogResult(access="failed")
    result = CatalogResult(access=access)
    for ln in lines:
        parts = ln.split()
        if parts[:1] == ["#cluster"] and len(parts) >= 3 and parts[1].isdigit():
            port = int(parts[1])
            c = CatalogCluster(port=port, ok=parts[2] == "ok")
            if c.ok and len(parts) >= 4 and parts[3].isdigit():
                c.version_num = int(parts[3])
            if c.ok and len(parts) >= 5:
                c.preloaded = {"t": True, "f": False}.get(parts[4])
            result.clusters[port] = c
        elif parts[:1] == ["#db"] and len(parts) == 5 and parts[1].isdigit() and parts[3].isdigit():
            owner = result.clusters.get(int(parts[1]))
            name = parts[2]
            if owner is None or not _DATABASE.fullmatch(name) or len(owner.databases) >= MAX_DATABASES:
                continue
            ext = {"1": True, "0": False}.get(parts[4])
            owner.databases.append(CatalogDatabase(name=name, size_bytes=int(parts[3]), extension=ext))
    return result


def statements_state(preloaded: bool | None, extension: bool | None) -> str | None:
    """pg_stat_statements in one database: ready, not_installed (CREATE EXTENSION needed),
    not_loaded (a restart is needed), or None when not known."""
    if preloaded is False:
        return "not_loaded"
    if preloaded is True and extension is True:
        return "ready"
    if preloaded is True and extension is False:
        return "not_installed"
    return None


def major_version(version_num: int | None) -> str | None:
    """'16' from 160015, '9.6' from 90624."""
    if not version_num:
        return None
    return str(version_num // 10000) if version_num >= 100000 else f"{version_num // 10000}.{version_num // 100 % 100}"
