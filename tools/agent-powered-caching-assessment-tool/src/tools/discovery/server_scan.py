"""
Find PostgreSQL servers on a machine without connecting to them.

One constant POSIX sh script prints four marked sections. It runs on this machine
(run_local_scan) or on a database host over SSH (the host probe). Nothing from a caller is
put into it, and it executes no binary it finds. Parsing is all here:

  #ps          server processes:  ps -A -o pid=,user=,args=, postgres or postmaster only
               (-A, never -e: BSD ps -e prints every process's environment)
  #sockets     Unix socket files .s.PGSQL.<port> in the usual directories
  #listen      listening TCP sockets: ss -ltnH, else netstat
  #lsclusters  pg_lsclusters --no-header (Debian and Ubuntu)

A server is reported only when a process, a socket file or pg_lsclusters shows it. An open
TCP port alone is not a PostgreSQL server. Spec: .kiro/specs/database-discovery (DISC-3, DISC-4).
"""

from __future__ import annotations

import re
import shlex
import subprocess  # nosec B404 - fixed argv, no shell from Python
from dataclasses import dataclass, field

SCAN_SCRIPT = r"""
echo '#ps'
ps -A -o pid=,user=,args= 2>/dev/null | awk '{n=$3; sub(".*/","",n); if (n=="postgres"||n=="postmaster") print}' | head -n 50
echo '#sockets'
ls -1 /tmp/.s.PGSQL.* /var/run/postgresql/.s.PGSQL.* /run/postgresql/.s.PGSQL.* 2>/dev/null | head -n 50
echo '#listen'
if command -v ss >/dev/null 2>&1; then ss -ltnH 2>/dev/null | head -n 500
else netstat -an 2>/dev/null | grep -i listen | head -n 500; fi
echo '#lsclusters'
if command -v pg_lsclusters >/dev/null 2>&1; then pg_lsclusters --no-header 2>/dev/null | head -n 50; fi
echo '#end'
"""

SECTIONS = ("ps", "sockets", "listen", "lsclusters")
# The local scan gets a fixed PATH and no other environment.
SCAN_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin:/opt/homebrew/bin:/usr/local/bin",
            "LC_ALL": "C"}
MAX_OUTPUT_CHARS = 200_000
MAX_LINE_CHARS = 2_000
SCAN_TIMEOUT_SEC = 20

_PATH = re.compile(r"/[A-Za-z0-9_.@/+-]{0,400}")
_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
_SOCKET = re.compile(r"(/[A-Za-z0-9_./-]{1,200})/\.s\.PGSQL\.(\d{1,5})")
_VERSION_IN_PATH = (
    re.compile(r"/postgresql/(\d{1,2}(?:\.\d{1,2})?)/bin/"),   # Debian, Ubuntu
    re.compile(r"/pgsql-(\d{1,2}(?:\.\d{1,2})?)/bin/"),         # PGDG RPMs
    re.compile(r"postgresql@(\d{1,2}(?:\.\d{1,2})?)/"),         # Homebrew
    re.compile(r"/Postgres\.app/.*/Versions/(\d{1,2}(?:\.\d{1,2})?)/"),
)
_WILDCARD_ADDRESSES = {"0.0.0.0", "*", "[::]", "::"}  # nosec B104 - addresses to recognise in ss output, not a bind


class ScanError(Exception):
    """The scan could not run. The message is fixed text, safe to show."""


@dataclass
class FoundServer:
    """One PostgreSQL server found by the scan, merged by port."""

    port: int | None = None
    data_directory: str | None = None
    version: str | None = None
    owner: str | None = None
    binary: str | None = None
    socket_dirs: list[str] = field(default_factory=list)
    listen_addresses: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def split_sections(output: str) -> dict[str, list[str]]:
    """Split the scan's output into its marked sections.

    Args:
        output: Everything the scan printed. Lines before the first marker (for example an
            SSH banner) are ignored. Overlong output and lines are cut.

    Returns:
        A dict with every name in SECTIONS, each a list of non-empty lines.

    Raises:
        ScanError: the end marker is missing, so the output is incomplete.
    """
    out: dict[str, list[str]] = {name: [] for name in SECTIONS}
    current: str | None = None
    ended = False
    for raw in output[:MAX_OUTPUT_CHARS].splitlines():
        line = raw.strip()[:MAX_LINE_CHARS]
        if line.startswith("#") and line[1:] in (*SECTIONS, "end"):
            if line == "#end":
                ended = True
                break
            current = line[1:]
            continue
        if current and line:
            out[current].append(line)
    if not ended:
        raise ScanError("The server scan did not finish; its output was incomplete.")
    return out


def parse_process(line: str) -> FoundServer | None:
    """Read one `ps -o pid=,user=,args=` line of a PostgreSQL server process.

    Only the program path, -D, -p, -c port= and -c config_file= are kept. Everything else on
    the command line is dropped, so nothing else a process was started with is returned.

    Args:
        line: One line of the #ps section.

    Returns:
        A FoundServer, or None for a line that is not a server process (a child such as
        `postgres: checkpointer`, or a line that does not parse).
    """
    try:
        parts = shlex.split(line)
    except ValueError:
        return None
    if len(parts) < 3:
        return None
    _pid, user, program, *args = parts
    # A child process is titled "postgres: checkpointer", so its program is "postgres:".
    if program.rsplit("/", 1)[-1] not in ("postgres", "postmaster"):
        return None
    s = FoundServer(evidence=["process"])
    if _USER.fullmatch(user):
        s.owner = user
    if _PATH.fullmatch(program):
        s.binary = program
        s.version = version_from_path(program)
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a == "-D" and nxt is not None:
            if _PATH.fullmatch(nxt):
                s.data_directory = nxt
            i += 2
            continue
        if a.startswith("-D") and len(a) > 2 and _PATH.fullmatch(a[2:]):
            s.data_directory = a[2:]
        elif a == "-p" and nxt is not None:
            s.port = _port(nxt)
            i += 2
            continue
        elif a == "-c" and nxt is not None:
            key, _, value = nxt.partition("=")
            if key == "port":
                s.port = _port(value)
            elif key == "data_directory" and _PATH.fullmatch(value):
                s.data_directory = value
            i += 2
            continue
        elif a.startswith("--port="):
            s.port = _port(a.split("=", 1)[1])
        i += 1
    return s


def parse_socket(line: str) -> tuple[str, int] | None:
    """Read one socket file path. Returns (directory, port), or None (also for a .lock file)."""
    if line.endswith(".lock"):
        return None
    m = _SOCKET.fullmatch(line)
    if not m:
        return None
    port = _port(m.group(2))
    return (m.group(1), port) if port else None


def parse_listener(line: str) -> tuple[str, int] | None:
    """Read one listening socket line from ss or netstat. Returns (address, port) or None.

    ss -ltnH:   LISTEN 0 400 127.0.0.1:5432 0.0.0.0:*
    netstat:    tcp4 0 0 127.0.0.1.5432 *.* LISTEN      (BSD, macOS)
                tcp  0 0 127.0.0.1:5432 0.0.0.0:* LISTEN (Linux)
    """
    parts = line.split()
    if not parts:
        return None
    if parts[0].upper() == "LISTEN" and len(parts) >= 4:
        local = parts[3]
    elif parts[0].lower().startswith("tcp") and len(parts) >= 4:
        local = parts[3]
    else:
        return None
    sep = ":" if ":" in local and not re.fullmatch(r"[0-9a-fA-F:]+\.\d+", local) else "."
    addr, _, port_s = local.rpartition(sep)
    port = _port(port_s)
    if not port or not addr or len(addr) > 64:
        return None
    return addr, port


def parse_lscluster(line: str) -> FoundServer | None:
    """Read one `pg_lsclusters --no-header` line: version cluster port status owner datadir log."""
    parts = line.split()
    if len(parts) < 6:
        return None
    version, _cluster, port_s, status, owner, datadir = parts[:6]
    port = _port(port_s)
    if not port or not re.fullmatch(r"\d{1,2}(\.\d{1,2})?", version):
        return None
    s = FoundServer(port=port, version=version, evidence=["pg_lsclusters"])
    if _USER.fullmatch(owner):
        s.owner = owner
    if _PATH.fullmatch(datadir):
        s.data_directory = datadir
    if status.lower() != "online":
        s.notes.append(f"pg_lsclusters reports this cluster as {status.lower()[:20]}.")
    return s


def version_from_path(path: str) -> str | None:
    """The major version in a server binary's path, when the path carries one."""
    for pattern in _VERSION_IN_PATH:
        m = pattern.search(path)
        if m:
            return m.group(1)
    return None


def merge(sections: dict[str, list[str]]) -> list[FoundServer]:
    """Turn the scan's sections into servers, merged by port.

    Args:
        sections: Output of split_sections.

    Returns:
        Servers ordered by port (unknown last). Listen addresses are attached only to ports
        a process, socket or pg_lsclusters already showed.
    """
    by_port: dict[int, FoundServer] = {}
    no_port: list[FoundServer] = []

    def absorb(s: FoundServer) -> None:
        if s.port is None:
            no_port.append(s)
            return
        cur = by_port.setdefault(s.port, FoundServer(port=s.port))
        for attr in ("data_directory", "version", "owner", "binary"):
            if getattr(cur, attr) is None and getattr(s, attr) is not None:
                setattr(cur, attr, getattr(s, attr))
        cur.evidence += [e for e in s.evidence if e not in cur.evidence]
        cur.notes += [n for n in s.notes if n not in cur.notes]

    for line in sections["lsclusters"]:
        if (s := parse_lscluster(line)) is not None:
            absorb(s)
    for line in sections["ps"]:
        if (s := parse_process(line)) is not None:
            absorb(s)
    sockets: dict[int, list[str]] = {}
    for line in sections["sockets"]:
        if (hit := parse_socket(line)) is not None:
            d, port = hit
            sockets.setdefault(port, [])
            if d not in sockets[port]:
                sockets[port].append(d)
    listeners: dict[int, list[str]] = {}
    for line in sections["listen"]:
        if (hit := parse_listener(line)) is not None:
            addr, port = hit
            listeners.setdefault(port, [])
            if addr not in listeners[port]:
                listeners[port].append(addr)

    # A process started without -p or -c port uses 5432, or what postgresql.conf says. Only
    # trust 5432 when a socket or listener confirms it.
    for s in list(no_port):
        if 5432 in sockets or 5432 in listeners or 5432 in by_port:
            s.port = 5432
            no_port.remove(s)
            absorb(s)
        else:
            s.notes.append("Its port could not be seen; it is set in postgresql.conf.")

    for port, dirs in sockets.items():
        cur = by_port.setdefault(port, FoundServer(port=port))
        cur.socket_dirs = dirs
        if "socket" not in cur.evidence:
            cur.evidence.append("socket")
    for port, cur in by_port.items():
        cur.listen_addresses = listeners.get(port, [])
        if any(a in _WILDCARD_ADDRESSES for a in cur.listen_addresses):
            cur.notes.append("It listens on every network interface. A database should "
                             "listen on loopback or a private address only.")
        elif not cur.listen_addresses:
            cur.notes.append("No TCP listener was seen for this port; it may accept local "
                             "socket connections only, or the listing was not visible.")
    return [by_port[p] for p in sorted(by_port)] + no_port


def run_local_scan(timeout: int = SCAN_TIMEOUT_SEC) -> str:
    """Run the scan on this machine.

    Returns:
        The scan's output.

    Raises:
        ScanError: /bin/sh is missing, or the scan timed out. Fixed text.
    """
    try:
        proc = subprocess.run(  # nosec B603 - fixed argv; the script is a constant
            ["/bin/sh", "-c", SCAN_SCRIPT], capture_output=True, text=True,
            timeout=timeout, check=False, env=SCAN_ENV)
    except FileNotFoundError as e:
        raise ScanError("No /bin/sh on this machine, so the server scan cannot run.") from e
    except subprocess.TimeoutExpired as e:
        raise ScanError(f"The server scan took longer than {timeout} s.") from e
    return proc.stdout


def _port(value: str) -> int | None:
    """A TCP port from text, or None."""
    if not value.isdigit() or len(value) > 5:
        return None
    n = int(value)
    return n if 1 <= n <= 65535 else None
