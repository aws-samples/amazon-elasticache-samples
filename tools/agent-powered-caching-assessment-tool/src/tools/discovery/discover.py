"""
discover(): run the discovery sources and merge what they found.

Each source runs on its own; one that fails is reported with a fixed sentence and the others
still run (DISC-5). Nothing here reads a password or connects to a database.
Spec: .kiro/specs/database-discovery.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.contracts.discovery import (
    LOCAL_SOURCES,
    DiscoveredServer,
    DiscoveryResult,
    DiscoverySource,
    SourceReport,
)
from src.tools.database import ssh_values
from src.tools.discovery import history, pg_service, server_scan, ssh_config

logger = logging.getLogger(__name__)

HOST_PROBE_TIMEOUT_SEC = 30


@dataclass(frozen=True)
class HostProbeTarget:
    """The database host to probe over SSH. Values are checked before anything runs."""

    ssh_target: str
    ssh_port: int = 22
    identity_file: str | None = None  # a key name inside SSH_KEY_DIR, never a path


def host_problems(target: HostProbeTarget) -> list[str]:
    """What is wrong with the shape of a host probe's values. Names the field, never the value."""
    problems = []
    if not ssh_values.target_ok(target.ssh_target):
        problems.append("host.ssh_target is not in an accepted form")
    if isinstance(target.ssh_port, bool) or not 1 <= target.ssh_port <= 65535:
        problems.append("host.ssh_port must be between 1 and 65535")
    if target.identity_file is not None and not ssh_values.key_name_ok(target.identity_file):
        problems.append("host.identity_file is not in an accepted form")
    return problems


def _id(*parts: Any) -> str:
    """A short stable id for an entry. An identifier, not a security control."""
    text = "|".join("" if p is None else str(p) for p in parts).encode()
    return hashlib.sha1(text, usedforsecurity=False).hexdigest()[:12]  # nosemgrep: insecure-hash-algorithm-sha1


def servers_from_scan(found: list[server_scan.FoundServer], source: DiscoverySource,
                      ssh: HostProbeTarget | None = None) -> list[DiscoveredServer]:
    """Turn scanned servers into contract entries.

    Args:
        found: Output of server_scan.merge.
        source: local_server or host_probe.
        ssh: The probed host, for host_probe.

    Returns:
        One entry per server. host is 127.0.0.1: psql on that machine reaches it there.
    """
    out = []
    where = "this machine" if ssh is None else ssh.ssh_target
    for s in found:
        version = f"PostgreSQL {s.version}" if s.version else "PostgreSQL"
        port = f"port {s.port}" if s.port else "unknown port"
        label = f"{version} on {where}, {port}"
        notes = list(s.notes)
        notes.append("Seen through: " + ", ".join(s.evidence) + ".")
        out.append(DiscoveredServer(
            id=_id(source, ssh.ssh_target if ssh else "", s.port, s.data_directory),
            source=source, kind="ssh" if ssh else "local", label=label,
            ssh_target=ssh.ssh_target if ssh else None,
            ssh_port=ssh.ssh_port if ssh else None,
            identity_file=ssh.identity_file if ssh else None,
            host="127.0.0.1" if s.port else None, port=s.port,
            version=s.version, data_directory=s.data_directory, owner=s.owner,
            listen_addresses=s.listen_addresses, notes=notes))
    return out


def _local_server(scan: Callable[[], str]) -> tuple[list[DiscoveredServer], SourceReport]:
    try:
        found = server_scan.merge(server_scan.split_sections(scan()))
    except server_scan.ScanError as e:
        return [], SourceReport(source="local_server", status="error", detail=str(e))
    servers = servers_from_scan(found, "local_server")
    detail = "" if servers else "No PostgreSQL server is running on this machine."
    return servers, SourceReport(source="local_server", status="ok", found=len(servers),
                                 detail=detail)


def _ssh_config(path: Path | None) -> tuple[list[DiscoveredServer], SourceReport]:
    try:
        parsed = ssh_config.read_ssh_config(path)
    except (OSError, ValueError) as e:
        detail = str(e) if isinstance(e, ValueError) else "The ssh config file could not be read."
        return [], SourceReport(source="ssh_config", status="error", detail=detail)
    if parsed is None:
        return [], SourceReport(source="ssh_config", status="unavailable",
                                detail="There is no ~/.ssh/config file.")
    servers = [DiscoveredServer(
        id=_id("ssh_config", h.alias), source="ssh_config", kind="ssh",
        label=f"SSH host {h.alias}" + (f" ({h.hostname})" if h.hostname else ""),
        ssh_target=h.alias, ssh_port=h.port, ssh_user=h.user,
        notes=["Not probed. Pick it to look for PostgreSQL servers on that host."])
        for h in parsed.hosts]
    return servers, SourceReport(source="ssh_config", status="ok", found=len(servers),
                                 detail=" ".join(parsed.notes))


def _pg_service(path: Path | None) -> tuple[list[DiscoveredServer], SourceReport]:
    try:
        parsed = pg_service.read_pg_service(path)
    except (OSError, ValueError) as e:
        detail = str(e) if isinstance(e, ValueError) else "The service file could not be read."
        return [], SourceReport(source="pg_service", status="error", detail=detail)
    if parsed is None:
        return [], SourceReport(source="pg_service", status="unavailable",
                                detail="There is no pg_service.conf file.")
    servers = []
    for svc in parsed.services:
        notes = ["Has a password in the service file; the tool does not read it."] \
            if svc.has_password else []
        target = f"{svc.host or 'local socket'}:{svc.port or 5432}"
        servers.append(DiscoveredServer(
            id=_id("pg_service", svc.name), source="pg_service", kind="service",
            label=f"Service {svc.name} ({target}" + (f", {svc.dbname})" if svc.dbname else ")"),
            host=svc.host, port=svc.port, database=svc.dbname, ssh_user=None, notes=notes))
    return servers, SourceReport(source="pg_service", status="ok", found=len(servers),
                                 detail=" ".join(parsed.notes))


def _earlier(root: str | Path | None) -> tuple[list[DiscoveredServer], SourceReport]:
    if root is None or not Path(root).is_dir():
        return [], SourceReport(source="earlier_assessment", status="unavailable",
                                detail="There is no artifact folder yet.")
    try:
        targets = history.past_targets(root)
    except OSError:
        return [], SourceReport(source="earlier_assessment", status="error",
                                detail="The job records could not be read.")
    servers = [DiscoveredServer(
        id=_id("earlier_assessment", t.host, t.port, t.database), source="earlier_assessment",
        kind="ssh", label=f"{t.database} at {t.host}:{t.port or 5432}, assessed {t.runs} time(s)",
        host=t.host, port=t.port, database=t.database, runs=t.runs,
        last_assessed_at=t.last_assessed_at or None, last_verdict=t.last_verdict,
        notes=["The SSH host is not kept in job records, so assessments of databases with "
               "the same address, port and name on different hosts are counted together."])
        for t in targets]
    return servers, SourceReport(source="earlier_assessment", status="ok", found=len(servers))


def _host_probe(target: HostProbeTarget,
                executor_factory: Callable[[HostProbeTarget], Any]) -> tuple[list[DiscoveredServer], SourceReport]:
    problems = host_problems(target)
    if problems:
        return [], SourceReport(source="host_probe", status="error", detail="; ".join(problems))
    try:
        executor = executor_factory(target)
    except FileNotFoundError:
        return [], SourceReport(source="host_probe", status="error",
                                detail="The known_hosts file or the named key does not exist.")
    except ValueError as e:
        return [], SourceReport(source="host_probe", status="error", detail=str(e))
    try:
        output = executor.run_command(server_scan.SCAN_SCRIPT, timeout=HOST_PROBE_TIMEOUT_SEC)
        found = server_scan.merge(server_scan.split_sections(output))
    except server_scan.ScanError as e:
        return [], SourceReport(source="host_probe", status="error", detail=str(e))
    except TimeoutError:
        return [], SourceReport(source="host_probe", status="error",
                                detail=f"The host did not answer within {HOST_PROBE_TIMEOUT_SEC} s.")
    except RuntimeError:
        # The ssh error can name users, paths and addresses; it is logged by type only.
        logger.info("host probe: ssh failed")
        return [], SourceReport(source="host_probe", status="error",
                                detail="SSH to the host failed. Check that its key is in "
                                       "known_hosts and that the SSH key is accepted.")
    finally:
        close = getattr(executor, "close", None)
        if callable(close):
            close()
    servers = servers_from_scan(found, "host_probe", ssh=target)
    detail = "" if servers else "No PostgreSQL server is running on that host."
    return servers, SourceReport(source="host_probe", status="ok", found=len(servers),
                                 detail=detail)


def ssh_executor_for(target: HostProbeTarget):
    """A commands-only SSH executor for the host probe (production default)."""
    from src.tools.database.ssh_executor import SshPsqlExecutor

    key = ssh_values.resolve_key(target.identity_file) if target.identity_file else None
    return SshPsqlExecutor(target.ssh_target, "", identity_file=key, ssh_port=target.ssh_port,
                           commands_only=True)


def discover(sources: Iterable[DiscoverySource] = LOCAL_SOURCES,
             host: HostProbeTarget | None = None,
             artifact_root: str | Path | None = None,
             *,
             local_scan: Callable[[], str] = server_scan.run_local_scan,
             ssh_config_path: Path | None = None,
             pg_service_path: Path | None = None,
             executor_factory: Callable[[HostProbeTarget], Any] = ssh_executor_for,
             ) -> DiscoveryResult:
    """Find PostgreSQL servers the user could assess.

    Args:
        sources: The local sources to run. host_probe runs when `host` is given.
        host: A database host to probe over SSH.
        artifact_root: The artifact folder, for earlier assessments.
        local_scan: Runs the server scan on this machine. Replaced in tests.
        ssh_config_path: Defaults to ~/.ssh/config.
        pg_service_path: Defaults to $PGSERVICEFILE, else ~/.pg_service.conf.
        executor_factory: Builds the SSH executor for the host probe. Replaced in tests.

    Returns:
        Every entry found, and one report per source, in a fixed order.
    """
    wanted = list(dict.fromkeys(sources))
    runners: dict[str, Callable[[], tuple[list[DiscoveredServer], SourceReport]]] = {
        "local_server": lambda: _local_server(local_scan),
        "ssh_config": lambda: _ssh_config(ssh_config_path),
        "pg_service": lambda: _pg_service(pg_service_path),
        "earlier_assessment": lambda: _earlier(artifact_root),
    }
    servers: list[DiscoveredServer] = []
    reports: list[SourceReport] = []
    for name in ("host_probe", *LOCAL_SOURCES):
        if name == "host_probe":
            if host is None:
                continue
            got, report = _host_probe(host, executor_factory)
        elif name in wanted:
            try:
                got, report = runners[name]()
            except Exception:  # noqa: BLE001 - one source never fails the whole result
                logger.exception("discovery source %s failed", name)
                got, report = [], SourceReport(source=name, status="error",  # type: ignore[arg-type]
                                               detail="This source failed unexpectedly.")
        else:
            got, report = [], SourceReport(source=name, status="skipped")  # type: ignore[arg-type]
        servers += got
        reports.append(report)
    return DiscoveryResult(discovered_at=datetime.now(UTC).isoformat(),
                           probed_host=host.ssh_target if host else None,
                           servers=servers, sources=reports)
