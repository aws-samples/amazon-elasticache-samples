"""
Preflight: can a live job work, checked before it is queued.

It follows the path collection takes and stops at the first thing that cannot work:

    ssh  ->  database login  ->  the SSH host is the database host  ->  pg_stat_statements

It starts no job and writes nothing. It makes one SSH connection and runs a few read-only
calls: `true`, `SELECT version()`, the address PostgreSQL accepted the connection on (compared
with the SSH host's own addresses), and the pg_stat_statements probe.

Messages are fixed text. The text of an ssh or psql error is logged and never returned,
because it can name hosts, users and paths. Every value that reaches the ssh command line or a
remote command is checked for shape first (target_problems), and a bad one stops the preflight
before any call.

Spec: .kiro/specs/ssh-collection (SSH-5, HOST-1).
"""

import ipaddress
import logging
import time

from src.agents.collector.postgres_collector import executor_for
from src.agents.collector.preflight_messages import describe_failure
from src.agents.collector.target_checks import target_problems
from src.contracts.collector_input import CollectorInput
from src.contracts.preflight import PreflightCheck, PreflightResult
from src.tools.aws import secrets
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

logger = logging.getLogger(__name__)

SQL_TIMEOUT_SECONDS = 60
SSH_TIMEOUT_SECONDS = 30


class _CappedExecutor:
    """Gives every call a time limit, so the executor's 300 second default is not used."""

    def __init__(self, inner, timeout: int):
        self._inner = inner
        self._timeout = timeout

    def run_sql(self, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return self._inner.run_sql(**kwargs)

    def run_sql_json(self, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return self._inner.run_sql_json(**kwargs)

    def run_command(self, command: str, timeout: int | None = None):
        return self._inner.run_command(command, timeout or self._timeout)


def _fail(name: str, exc: Exception) -> PreflightCheck:
    """A failed check from an exception, with fixed text. The error goes to the log."""
    logger.warning("Preflight check %s failed: %s: %s", name, type(exc).__name__, exc)
    message, hint = describe_failure(exc)
    return PreflightCheck(name=name, status="fail", message=message, hint=hint)


def _skipped(name: str, because: str) -> PreflightCheck:
    return PreflightCheck(name=name, status="skipped", message=f"Not checked: {because}.")


def run_preflight(inp: CollectorInput, executor=None) -> PreflightResult:
    """Check whether a live job could work.

    Args:
        inp: The collector input a submit would take.
        executor: A command executor with run_sql, run_sql_json and run_command. An
            SshPsqlExecutor built from the input when omitted. For tests.

    Returns:
        The checks in the order they ran, and whether any failed.

    Raises:
        ValueError: when a value in the input is not in an accepted form. Nothing has
            been called by then.
    """
    problems = target_problems(inp)
    if problems:
        raise ValueError("; ".join(problems))

    started = time.monotonic()
    built: list = []  # executors made here, closed below
    try:
        return _run(inp, executor, built, started)
    finally:
        for made in built:
            made.close()


def _run(inp: CollectorInput, executor, built: list, started: float) -> PreflightResult:
    """The checks, in order, stopping at the first failure."""
    checks: list[PreflightCheck] = []
    # 0. A secret-backed password: the server can read it. Fixed text only.
    password: str | None = None
    arn = inp.live_config.password_secret_arn if inp.live_config else None
    if arn and executor is None:
        try:
            password = secrets.read_password(arn)
        except secrets.SecretError as e:
            message, hint = secrets.MESSAGES[e.kind]
            checks.append(PreflightCheck(name="secret", status="fail", message=message, hint=hint))
            checks += [_skipped(n, "the password secret could not be read")
                       for n in ("ssh", "database", "database_host", "pg_stat_statements")]
            return _result(checks, None, None, started)
        checks.append(PreflightCheck(name="secret", status="pass",
                                     message="Read the password from Secrets Manager."))
    # 1. SSH: the host key is trusted, the key is accepted, and a command runs.
    try:
        if executor is None:
            executor = executor_for(inp, password=password) if password else executor_for(inp)
            built.append(executor)
        executor.run_command("true", SSH_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - reported as a failed check
        checks.append(_fail("ssh", exc))
        checks += [_skipped("database", "SSH did not connect"),
                   _skipped("database_host", "SSH did not connect"),
                   _skipped("pg_stat_statements", "SSH did not connect")]
        return _result(checks, None, None, started)
    checks.append(PreflightCheck(name="ssh", status="pass", message="Connected over SSH."))

    # 2. The database answers through psql on that host, with the password file.
    capped = _CappedExecutor(executor, SQL_TIMEOUT_SECONDS)
    db = PostgreSQLRemoteCollector(
        executor=capped, host=inp.cluster_endpoint, port=inp.port, database=inp.database_name)
    try:
        raw_version = db.get_version()
        if raw_version == "unknown":
            raise RuntimeError("the query returned no rows")
    except Exception as exc:  # noqa: BLE001 - reported as a failed check
        checks.append(_fail("database", exc))
        checks += [_skipped("database_host", "the database could not be reached"),
                   _skipped("pg_stat_statements", "the database could not be reached")]
        return _result(checks, None, None, started)
    version = " ".join(raw_version.split()[:2])
    checks.append(PreflightCheck(name="database", status="pass",
                                 message=f"Connected to {version}."))

    # 3. The SSH host is the database host. Host metrics describe the machine connected to,
    # so a jump host would give numbers for the wrong machine without any error. The backend
    # process that answered us exists under /proc only on the machine PostgreSQL runs on.
    checks.append(_check_database_host(db, capped))
    if checks[-1].status == "fail":
        checks.append(_skipped("pg_stat_statements", "the SSH host is not the database host"))
        return _result(checks, version, None, started)

    # 4. pg_stat_statements. Without it the job still runs, in degraded mode.
    available: bool | None = None
    try:
        probe = db.probe_pg_stat_statements()
        available = bool(probe.get("available"))
    except Exception as exc:  # noqa: BLE001 - a warning: the job does not depend on it
        logger.warning("Preflight pg_stat_statements probe failed: %s: %s",
                       type(exc).__name__, exc)
        checks.append(PreflightCheck(
            name="pg_stat_statements", status="warn",
            message="Could not check pg_stat_statements.",
            hint="See the server log. The job may still run."))
    else:
        if available:
            checks.append(PreflightCheck(name="pg_stat_statements", status="pass",
                                         message="pg_stat_statements is available."))
        else:
            checks.append(PreflightCheck(
                name="pg_stat_statements", status="warn",
                message=f"pg_stat_statements is not usable: {probe.get('reason')}.",
                hint="The job would still run, in degraded mode, without query-level "
                     "scoring."))
    return _result(checks, version, available, started)


LOCAL_ADDRESSES_COMMAND = "hostname -I 2>/dev/null || ip -o addr show 2>/dev/null | awk '{print $4}'"


def _addresses(text: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The IP addresses in command output, ignoring anything that is not one.

    Accepts a bare address or an address with a prefix length (``10.0.0.5/24``).
    """
    found = set()
    for token in text.split():
        try:
            found.add(ipaddress.ip_address(token.split("/")[0].split("%")[0]))
        except ValueError:
            continue
    return found


def _check_database_host(db: PostgreSQLRemoteCollector, executor) -> PreflightCheck:
    """Whether the machine ssh connected to runs the database.

    Asks PostgreSQL which address it accepted the connection on (``inet_server_addr()``) and
    compares it with the addresses of the SSH host. A loopback address means the connection
    never left the SSH host, and so does no address at all (a Unix socket). A database on
    another machine, or one reached through a pooler or a forwarded port on this one, answers
    with an address that is not this host's. Nothing from the database reaches a shell: the
    address is parsed as an IP address and compared here.
    """
    try:
        rows = db._query("SELECT host(inet_server_addr()) AS addr")
        raw = rows[0].get("addr") if rows else None
        server = ipaddress.ip_address(str(raw)) if raw not in (None, "") else None
    except Exception as exc:  # noqa: BLE001 - reported as a failed check
        return _fail("database_host", exc)
    if server is None or server.is_loopback:
        local = True
    else:
        try:
            mine = _addresses(executor.run_command(LOCAL_ADDRESSES_COMMAND))
        except Exception as exc:  # noqa: BLE001 - reported as a failed check
            return _fail("database_host", exc)
        local = server in mine
    if not local:
        logger.warning("Preflight: PostgreSQL answered on %s, which is not an address of the SSH "
                       "host", server)
        return PreflightCheck(
            name="database_host", status="fail",
            message="The SSH host is not the machine PostgreSQL runs on.",
            hint="Host metrics describe the SSH host, so ssh_target must be the database host "
                 "itself, not a jump host. Point ssh_target at the database host and set "
                 "cluster_endpoint to 127.0.0.1.")
    return PreflightCheck(name="database_host", status="pass",
                          message="The SSH host runs the database.")


def _result(checks: list[PreflightCheck], version: str | None, available: bool | None,
            started: float) -> PreflightResult:
    return PreflightResult(
        ok=not any(c.status == "fail" for c in checks), checks=checks,
        database_version=version, pg_stat_statements_available=available,
        duration_seconds=round(time.monotonic() - started, 2))
