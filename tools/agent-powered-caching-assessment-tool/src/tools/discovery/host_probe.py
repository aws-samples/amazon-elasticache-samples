"""
Probe one SSH host in stages, and trust its key after the user checks the fingerprint.

    resolve -> reachable -> host_key -> sign_in -> shell -> postgresql -> databases

Each stage passes, fails, warns or is skipped, with fixed text and a fix. The probe stops at
the first failure. The text of an ssh error is read here to tell the failures apart and is
never returned or logged. The cluster scan (process list and socket files) signs in to
nothing. The last stage lists the databases in each instance as the postgres user, through
`sudo -n`, with read-only catalog queries (pg_catalog.py). Without sudo it is a warning: the
probe still passes and the user types the database name.

Trusting a host only ever appends to the known_hosts file the API uses, and only when a fresh
scan of the host matches the fingerprint the user confirmed. A changed key is never replaced.

Every external call (ssh -G, ssh-keyscan, ssh-keygen, the TCP connect, ssh itself and the
public IP lookup) goes through `ProbeDeps`, so tests run with fakes.
Spec: .kiro/specs/ssh-host-probe.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import os
import re
import socket
import subprocess  # nosec B404 - argv lists only, never shell=True
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.contracts.hosts import (
    HostKeyInfo,
    HostProbeResult,
    HostTarget,
    HostTrustResult,
    ProbeStage,
)
from src.tools.database import ssh_values
from src.tools.discovery import pg_catalog, server_scan
from src.tools.discovery.discover import HostProbeTarget, _id, host_problems, servers_from_scan

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SEC = 5
KEYSCAN_TIMEOUT_SEC = 5
SHELL_TIMEOUT_SEC = 25
SCAN_TIMEOUT_SEC = 30
SHELL_MARKER = "apcat-probe-ok"
PUBLIC_IP_URL = "https://checkip.amazonaws.com"
PUBLIC_IP_CACHE_SEC = 300

# Key types a known_hosts line may carry. Anything else from ssh-keyscan is ignored, so a
# hostile answer cannot put text of its choosing into known_hosts.
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
             "ssh-rsa")
_PREFERRED = {t: i for i, t in enumerate(KEY_TYPES)}
_B64 = re.compile(r"[A-Za-z0-9+/]{20,4096}={0,2}")
_HOSTNAME = re.compile(r"[A-Za-z0-9_.:-]{1,253}")
_known_hosts_lock = threading.Lock()


# ---------------------------------------------------------------------------
# External calls
# ---------------------------------------------------------------------------


def run_argv(argv: list[str], timeout: int) -> tuple[int, str, str]:
    """Run a command as an argv list.

    Returns:
        (exit code, stdout, stderr). Exit code 124 for a timeout, 127 for a missing program.
    """
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,  # nosec B603 - fixed argv lists, values shape-checked  # nosemgrep: dangerous-subprocess-use-audit
                           check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except FileNotFoundError:
        return 127, "", "not found"
    return p.returncode, p.stdout, p.stderr


def tcp_connect(host: str, port: int, timeout: float) -> None:
    """Open and close a TCP connection. Raises OSError (TimeoutError for a timeout)."""
    with socket.create_connection((host, port), timeout=timeout):
        pass


_ip_cache: dict[str, tuple[float, str | None]] = {}


def fetch_public_ip() -> str | None:
    """This machine's public IP, as an AWS endpoint sees it, or None. Cached for 5 minutes."""
    now = time.monotonic()
    hit = _ip_cache.get("ip")
    if hit and now - hit[0] < PUBLIC_IP_CACHE_SEC:
        return hit[1]
    ip: str | None = None
    try:
        with urllib.request.urlopen(PUBLIC_IP_URL, timeout=5) as r:  # nosec B310 - fixed https URL
            text = r.read(64).decode("ascii", "replace").strip()
        ip = str(ipaddress.ip_address(text))
    except (OSError, ValueError):
        logger.info("public IP lookup failed")
    _ip_cache["ip"] = (now, ip)
    return ip


def ssh_executor(target: HostTarget, key: Path | None):
    """A commands-only SSH executor (host key checked, BatchMode)."""
    from src.tools.database.ssh_executor import SshPsqlExecutor

    return SshPsqlExecutor(target.ssh_target, "", identity_file=key, ssh_port=target.ssh_port,
                           commands_only=True)


@dataclass
class ProbeDeps:
    """Every external call the probe makes. Replaced in tests."""

    run: Callable[[list[str], int], tuple[int, str, str]] = run_argv
    resolve_dns: Callable[..., Any] = socket.getaddrinfo
    connect: Callable[[str, int, float], None] = tcp_connect
    executor: Callable[[HostTarget, Path | None], Any] = ssh_executor
    public_ip: Callable[[], str | None] = fetch_public_ip
    resolve_key: Callable[[str], Path] = ssh_values.resolve_key
    known_hosts: Callable[[], Path] = ssh_values.known_hosts_path


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------


@dataclass
class Resolved:
    """What `ssh -G` says ssh would connect to."""

    hostname: str
    port: int
    user: str | None
    hostkeyalias: str | None = None
    via_jump: bool = False


def parse_ssh_g(text: str) -> Resolved | None:
    """Read `ssh -G` output. Returns None when it has no usable host name or port."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.strip().partition(" ")
        if key and key not in values:
            values[key.lower()] = value.strip()
    host = values.get("hostname", "")
    port = values.get("port", "")
    if not _HOSTNAME.fullmatch(host) or not port.isdigit():
        return None
    alias = values.get("hostkeyalias")
    jump = values.get("proxyjump", "none").lower() not in ("", "none") or \
        values.get("proxycommand", "none").lower() not in ("", "none")
    return Resolved(hostname=host, port=int(port), user=values.get("user") or None,
                    hostkeyalias=alias if alias and _HOSTNAME.fullmatch(alias) else None,
                    via_jump=jump)


def fingerprint(b64_key: str) -> str:
    """The SHA256 fingerprint of a public key, as `ssh-keygen -l -E sha256` prints it."""
    digest = hashlib.sha256(base64.b64decode(b64_key)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def parse_keyscan(text: str) -> list[tuple[str, str]]:
    """(key type, base64 key) pairs from ssh-keyscan output, allowed types only, best first."""
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 3 and parts[1] in KEY_TYPES and _B64.fullmatch(parts[2]):
            try:
                base64.b64decode(parts[2], validate=True)
            except ValueError:
                continue
            out.append((parts[1], parts[2]))
    return sorted(set(out), key=lambda k: _PREFERRED[k[0]])


def known_hosts_name(r: Resolved) -> str:
    """The name a known_hosts line uses for this host: [name]:port unless the port is 22."""
    name = r.hostkeyalias or r.hostname
    return name if r.port == 22 else f"[{name}]:{r.port}"


def known_keys(deps: ProbeDeps, r: Resolved) -> list[tuple[str, str]] | None:
    """The keys known_hosts holds for this host (plain or hashed entries), or None on error."""
    path = deps.known_hosts()
    if not path.is_file():
        return []
    rc, out, _err = deps.run(["ssh-keygen", "-F", known_hosts_name(r), "-f", str(path)], 10)
    if rc not in (0, 1):  # 1: no entry
        return None
    keys = []
    for line in out.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0].startswith("@"):   # @cert-authority, @revoked: not compared here
            continue
        if len(parts) >= 3 and parts[1] in KEY_TYPES:
            keys.append((parts[1], parts[2]))
    return keys


def key_state(scanned: list[tuple[str, str]], known: list[tuple[str, str]]) -> str:
    """known, changed or unknown, from the scanned keys and the known_hosts keys."""
    if any(k in known for k in scanned):
        return "known"
    known_types = {t for t, _ in known}
    if any(t in known_types for t, _ in scanned):
        return "changed"
    return "unknown"


# Patterns in ssh's error text, checked in order. Only the stage and the fixed text leave.
_FAILURES: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED", re.I), "host_key",
     "The host's key does not match the one trusted for it.",
     "This can mean the host was rebuilt, or that something is intercepting the connection. "
     "Check with the host's owner, then fix known_hosts by hand."),
    (re.compile(r"Host key verification failed|No \S+ host key is known", re.I), "host_key",
     "The host's key is not in the server's known_hosts file.",
     "Trust the host after checking its fingerprint."),
    (re.compile(r"Could not resolve hostname", re.I), "resolve",
     "The host name does not resolve.", "Check the spelling, or use the host's IP address."),
    (re.compile(r"Connection refused", re.I), "reachable",
     "The host refused the connection on the SSH port.",
     "Check the port and that sshd runs on the host."),
    (re.compile(r"timed out|No route to host|Network is unreachable", re.I), "reachable",
     "The SSH port did not answer.",
     "A security group or firewall may not allow this machine."),
    (re.compile(r"Permission denied|Too many authentication failures|no mutual signature",
                re.I), "sign_in",
     "The host refused the SSH key.",
     "Check the user and pick the key that host accepts."),
    (re.compile(r"Disallowed command|does not provide shell access|Welcome to GitLab|"
                r"git-shell|Interactive git shell|only allows sftp|PTY allocation request",
                re.I), "shell",
     "Signed in, but the host runs no shell. It is probably a git or code host.",
     "Probe the database host instead."),
    (re.compile(r"Connection closed|Connection reset|kex_exchange_identification", re.I),
     "sign_in", "The host closed the connection before sign-in finished.",
     "The host may limit connections. Try again, or check its sshd settings."),
]


def classify(text: str) -> tuple[str, str, str]:
    """(stage, message, hint) for an ssh failure. The text itself is never returned."""
    for pattern, stage, message, hint in _FAILURES:
        if pattern.search(text):
            return stage, message, hint
    return ("sign_in", "SSH failed for a reason the probe does not recognise.",
            "Run ssh to the host by hand to see the error. It is not shown here.")


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------

STAGE_ORDER = ("resolve", "reachable", "host_key", "sign_in", "shell", "postgresql", "databases")


@dataclass
class _Run:
    stages: list[ProbeStage] = field(default_factory=list)

    def add(self, name: str, status: str, message: str, hint: str | None = None) -> None:
        self.stages.append(ProbeStage(name=name, status=status, message=message, hint=hint))  # type: ignore[arg-type]

    def skip_rest(self, after: str, why: str) -> None:
        done = {s.name for s in self.stages}
        for name in STAGE_ORDER[STAGE_ORDER.index(after) + 1:]:
            if name not in done:
                self.add(name, "skipped", why)


def probe_host(target: HostTarget, deps: ProbeDeps | None = None) -> HostProbeResult:
    """Probe one SSH host, stage by stage.

    Args:
        target: The SSH target, port and optional key name.
        deps: External calls; the real ones when omitted.

    Returns:
        The stages, the host key state and fingerprint, and the clusters found. `ok` is true
        when SSH works and a shell runs.
    """
    deps = deps or ProbeDeps()
    started = time.monotonic()
    run = _Run()
    result: dict[str, Any] = {"ssh_target": target.ssh_target, "ssh_port": target.ssh_port}
    host_key = HostKeyInfo()

    def done(ok: bool = False, **extra: Any) -> HostProbeResult:
        return HostProbeResult(ok=ok, stages=run.stages, host_key=host_key,
                               duration_seconds=round(time.monotonic() - started, 2),
                               **result, **extra)

    # 1. Resolve: the values, the key, what the SSH config makes of the target, DNS.
    problems = host_problems(HostProbeTarget(target.ssh_target, target.ssh_port,
                                             target.identity_file))
    if problems:
        run.add("resolve", "fail", "; ".join(problems) + ".")
        run.skip_rest("resolve", "The target is not in an accepted form.")
        return done()
    key: Path | None = None
    if target.identity_file:
        try:
            key = deps.resolve_key(target.identity_file)
        except (ValueError, FileNotFoundError) as e:
            run.add("resolve", "fail", f"The SSH key cannot be used: {e}.",
                    "Pick a key from the list. Keys live in SSH_KEY_DIR.")
            run.skip_rest("resolve", "The SSH key cannot be used.")
            return done()
    argv = ["ssh", "-G", "-p", str(target.ssh_port)] + (["-i", str(key)] if key else []) + \
        ["--", target.ssh_target]
    rc, out, _err = deps.run(argv, 10)
    r = parse_ssh_g(out) if rc == 0 else None
    if r is None:
        run.add("resolve", "fail", "The SSH config could not be applied to this target.",
                "Check ~/.ssh/config for this host.")
        run.skip_rest("resolve", "The target could not be resolved.")
        return done()
    result.update(resolved_host=r.hostname, resolved_user=r.user, via_jump_host=r.via_jump)
    if not r.via_jump:
        try:
            deps.resolve_dns(r.hostname, r.port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            run.add("resolve", "fail", "The host name does not resolve.",
                    "Check the spelling, or use the host's IP address.")
            run.skip_rest("resolve", "The host name does not resolve.")
            return done()
    run.add("resolve", "pass", "Resolved through the SSH config."
            + (" It is reached through a jump host." if r.via_jump else ""))

    # 2. Reachable: a TCP connect to the SSH port, unless a jump host sits in between.
    if r.via_jump:
        run.add("reachable", "skipped", "Reached through a jump host, so not tested directly.")
    else:
        try:
            deps.connect(r.hostname, r.port, CONNECT_TIMEOUT_SEC)
        except ConnectionRefusedError:
            run.add("reachable", "fail", "The host refused the connection on the SSH port.",
                    "Check the port and that sshd runs on the host.")
            run.skip_rest("reachable", "The SSH port is not reachable.")
            return done()
        except OSError:
            ip = deps.public_ip()
            hint = ("A security group or firewall may not allow this machine"
                    + (f". This machine's public IP is {ip}." if ip else "."))
            run.add("reachable", "fail",
                    f"The SSH port did not answer within {CONNECT_TIMEOUT_SEC} s.", hint)
            run.skip_rest("reachable", "The SSH port is not reachable.")
            return done(public_ip=ip)
        run.add("reachable", "pass", "The SSH port answered.")

    # 3. Host key: scan it and look it up in the API's known_hosts.
    if r.via_jump:
        run.add("host_key", "skipped", "Behind a jump host the key is checked when signing in.")
    else:
        rc, out, _err = deps.run(["ssh-keyscan", "-T", str(KEYSCAN_TIMEOUT_SEC), "-p", str(r.port),
                                  "-t", "ed25519,ecdsa,rsa", r.hostname], KEYSCAN_TIMEOUT_SEC + 5)
        scanned = parse_keyscan(out)
        known = known_keys(deps, r) if scanned else None
        if not scanned or known is None:
            host_key = HostKeyInfo(state="unavailable")
            run.add("host_key", "warn", "The host's key could not be read ahead of sign-in.",
                    "Signing in still checks it.")
        else:
            state = key_state(scanned, known)
            best_type, best = scanned[0]
            host_key = HostKeyInfo(state=state, key_type=best_type, fingerprint=fingerprint(best))  # type: ignore[arg-type]
            if state == "changed":
                run.add("host_key", "fail", "The host's key does not match the one trusted for it.",
                        "This can mean the host was rebuilt, or that something is intercepting "
                        "the connection. Check with the host's owner, then fix known_hosts by hand.")
                run.skip_rest("host_key", "The host's key changed.")
                return done()
            if state == "unknown":
                run.add("host_key", "fail", "The host's key is not trusted yet.",
                        "Check the fingerprint against the host's console output, then trust it.")
                run.skip_rest("host_key", "The host's key is not trusted yet.")
                return done()
            run.add("host_key", "pass", "The host's key matches known_hosts.")

    # 4 and 5. Sign in and run a shell command, then 6. the cluster scan, on one connection.
    try:
        executor = deps.executor(target, key)
    except FileNotFoundError:
        run.add("sign_in", "fail", "The server's known_hosts file does not exist.",
                "Trust the host, or create the file named by SSH_KNOWN_HOSTS.")
        run.skip_rest("sign_in", "SSH cannot start.")
        return done()
    except ValueError as e:
        run.add("sign_in", "fail", f"SSH cannot start: {e}.")
        run.skip_rest("sign_in", "SSH cannot start.")
        return done()
    try:
        try:
            out = executor.run_command(f"echo {SHELL_MARKER}", timeout=SHELL_TIMEOUT_SEC)
        except TimeoutError:
            run.add("sign_in", "fail", f"Signing in took longer than {SHELL_TIMEOUT_SEC} s.",
                    "The host may be overloaded, or a login banner may be waiting for input.")
            run.skip_rest("sign_in", "Sign-in did not finish.")
            return done()
        except RuntimeError as e:
            stage, message, hint = classify(str(e))
            logger.info("host probe: ssh failed at %s", stage)
            if stage == "shell":
                run.add("sign_in", "pass", "Signed in.")
            elif stage in ("resolve", "reachable", "host_key"):
                # Seen only at sign-in, for example behind a jump host.
                run.stages = [s for s in run.stages if s.name != stage]
                if stage == "host_key":
                    host_key = HostKeyInfo(state="changed" if "does not match" in message
                                           else "unknown")
            run.add(stage, "fail", message, hint)
            run.stages.sort(key=lambda s: STAGE_ORDER.index(s.name))
            run.skip_rest(stage, "An earlier stage failed.")
            return done()
        if SHELL_MARKER not in out:
            run.add("sign_in", "pass", "Signed in.")
            run.add("shell", "fail", "Signed in, but the host runs no shell. It is probably a "
                    "git or code host.", "Probe the database host instead.")
            run.skip_rest("shell", "No shell.")
            return done()
        run.add("sign_in", "pass", f"Signed in as {r.user}." if r.user else "Signed in.")
        run.add("shell", "pass", "A shell command ran.")
        try:
            scan = executor.run_command(server_scan.SCAN_SCRIPT, timeout=SCAN_TIMEOUT_SEC)
            found = server_scan.merge(server_scan.split_sections(scan))
        except (server_scan.ScanError, TimeoutError, RuntimeError):
            run.add("postgresql", "warn", "The check for PostgreSQL did not finish.",
                    "SSH works. Try the probe again.")
            run.add("databases", "skipped", "No instance to look in.")
            return done(ok=True)
        clusters = servers_from_scan(found, "host_probe", ssh=HostProbeTarget(
            target.ssh_target, target.ssh_port, target.identity_file))
        if not clusters:
            run.add("postgresql", "warn", "No PostgreSQL server is running on this host.",
                    "Check that this is the database host.")
            run.add("databases", "skipped", "No instance to look in.")
            return done(ok=True, clusters=clusters)
        n = len(clusters)
        run.add("postgresql", "pass", "1 database instance found." if n == 1
                else f"{n} database instances found.")
        access, clusters, databases = _list_databases(executor, run, clusters, target)
        return done(ok=True, clusters=clusters, databases=databases, postgres_access=access)
    finally:
        close = getattr(executor, "close", None)
        if callable(close):
            close()


def _list_databases(executor: Any, run: _Run, clusters: list, target: HostTarget
                    ) -> tuple[str, list, list]:
    """The databases stage: list each instance's databases as postgres through sudo.

    Returns:
        (postgres_access, the clusters with postgres_access and version filled in, the
        databases as discovery finds).
    """
    ports = sorted({c.port for c in clusters if c.port})
    try:
        out = executor.run_command(pg_catalog.build_command(ports), timeout=pg_catalog.TIMEOUT_SEC)
        cat = pg_catalog.parse(out)
    except (TimeoutError, RuntimeError, ValueError):
        cat = pg_catalog.CatalogResult(access="failed")
    if cat.access == "no_sudo":
        run.add("databases", "warn", "The SSH user cannot run commands as postgres, so the "
                "databases are not listed.",
                "Type the database name when you add it, and give an existing database user. "
                "To list them, allow this user to run sudo -u postgres without a password.")
        return "no_sudo", [c.model_copy(update={"postgres_access": "no_sudo"}) for c in clusters], []
    if cat.access != "sudo":
        run.add("databases", "warn", "The list of databases did not finish.",
                "SSH works. Type the database name, or try the probe again.")
        return "failed", clusters, []

    databases = []
    filled = []
    unread = 0
    for c in clusters:
        info = cat.clusters.get(c.port) if c.port else None
        update: dict[str, Any] = {"postgres_access": "sudo"}
        if info and info.ok and not c.version:
            update["version"] = pg_catalog.major_version(info.version_num)
        filled.append(c.model_copy(update=update))
        if info is None or not info.ok:
            unread += 1
            continue
        for d in info.databases:
            state = pg_catalog.statements_state(info.preloaded, d.extension)
            size_mb = round(d.size_bytes / 1048576, 1)
            notes = [f"{size_mb:,.1f} MB." if size_mb < 1024 else f"{size_mb / 1024:,.1f} GB."]
            notes.append({"ready": "pg_stat_statements is ready.",
                          "not_installed": "pg_stat_statements is loaded but not created in "
                                           "this database: run CREATE EXTENSION pg_stat_statements.",
                          "not_loaded": "pg_stat_statements is not loaded: it needs a "
                                        "shared_preload_libraries change and a restart."}.get(
                state or "", "pg_stat_statements state is not known."))
            databases.append(c.model_copy(update={
                "id": _id("host_probe_db", target.ssh_target, c.port, c.data_directory, d.name),
                "label": f"{d.name} on {target.ssh_target}, port {c.port}",
                "database": d.name, "size_mb": size_mb, "pg_stat_statements": state,
                "postgres_access": "sudo", "notes": notes,
                "version": update.get("version") or c.version}))
    n = len(databases)
    message = ("No databases found besides the default postgres database." if n == 0 else "1 database listed" if n == 1
               else f"{n} databases listed") + (" as the postgres user, through sudo." if n else "")
    if unread:
        run.add("databases", "warn", message + f" {unread} instance(s) could not be read.",
                "Check that PostgreSQL is running on that port.")
    else:
        run.add("databases", "pass", message)
    return "sudo", filled, databases


# ---------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------


def trust_host(target: HostTarget, confirmed: str, deps: ProbeDeps | None = None) -> HostTrustResult:
    """Add a host's key to the API's known_hosts, when a fresh scan matches the fingerprint.

    Args:
        target: The SSH target and port.
        confirmed: The SHA256 fingerprint the user checked.
        deps: External calls; the real ones when omitted.

    Returns:
        added; already_known; mismatch (the scan does not match, nothing added); changed (a
        different key is already trusted, never replaced); unavailable (the key could not be
        read, or the host is behind a jump host).
    """
    deps = deps or ProbeDeps()

    def no(outcome: str, message: str) -> HostTrustResult:
        return HostTrustResult(trusted=False, outcome=outcome, message=message)  # type: ignore[arg-type]

    if host_problems(HostProbeTarget(target.ssh_target, target.ssh_port, None)):
        return no("unavailable", "The target is not in an accepted form.")
    rc, out, _err = deps.run(["ssh", "-G", "-p", str(target.ssh_port), "--", target.ssh_target], 10)
    r = parse_ssh_g(out) if rc == 0 else None
    if r is None:
        return no("unavailable", "The SSH config could not be applied to this target.")
    if r.via_jump:
        return no("unavailable", "This host is reached through a jump host. Add its key to "
                  "known_hosts by hand.")
    rc, out, _err = deps.run(["ssh-keyscan", "-T", str(KEYSCAN_TIMEOUT_SEC), "-p", str(r.port),
                              "-t", "ed25519,ecdsa,rsa", r.hostname], KEYSCAN_TIMEOUT_SEC + 5)
    scanned = parse_keyscan(out)
    match = next(((t, k) for t, k in scanned if fingerprint(k) == confirmed), None)
    if not scanned:
        return no("unavailable", "The host's key could not be read.")
    if match is None:
        return no("mismatch", "The host's key does not match the fingerprint you checked. "
                  "Nothing was added.")
    with _known_hosts_lock:
        known = known_keys(deps, r)
        if known is None:
            return no("unavailable", "The known_hosts file could not be read.")
        state = key_state([match], known)
        if state == "known":
            return HostTrustResult(trusted=True, outcome="already_known",
                                   message="This key is already trusted.", fingerprint=confirmed)
        if state == "changed":
            return no("changed", "A different key is already trusted for this host. It is never "
                      "replaced from here. Check with the host's owner and fix known_hosts by hand.")
        path = deps.known_hosts()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            needs_newline = path.stat().st_size > 0 and not path.read_bytes().endswith(b"\n")
            line = f"{known_hosts_name(r)} {match[0]} {match[1]}\n"
            os.write(fd, (b"\n" if needs_newline else b"") + line.encode("ascii"))
        finally:
            os.close(fd)
    return HostTrustResult(trusted=True, outcome="added", message="The host's key is now "
                           "trusted.", fingerprint=confirmed)
