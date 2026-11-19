"""
Local psql executor: run the collector against a database on this machine.

WHY THIS EXISTS
---------------
The usual way to reach a database host is SshPsqlExecutor. This executor covers the case with
no host to connect to: a container, a local `psql` against a laptop or CI database, or the
collector running on the database host itself (the output is then a file that can be uploaded
and assessed with POST /assessments/from-collection).

It has the same three methods as SshPsqlExecutor, so PostgreSQLRemoteCollector and
HostMetricsCollector work unchanged with either:

    run_sql(host, port, database, sql, timeout)       -> str
    run_sql_json(host, port, database, sql, timeout)  -> list[dict]
    run_command(command, timeout)                     -> str

TWO BACKENDS
------------
  docker : runs psql inside a container via `docker exec`. Needs no psql client
           on the host, which is why it is the default for local work.
  binary : runs a psql binary already on PATH, with PGPASSWORD in the
           environment.

OUTPUT PARITY
-------------
Uses the same psql flags (tabular.PSQL_FORMAT_ARGS) and the same parser as the SSH executor,
so parsed rows are identical between the two. A collection captured locally is directly
comparable to one captured over SSH.

SECURITY
--------
For local and test use. The password is passed via the environment (not argv, so it does not
appear in `ps` output), but there is no audit trail. Do not point it at a production database
from a machine that is not the database host.
"""

import logging
import subprocess  # nosec B404 — local dev tooling; args are passed as a list, never a shell string
from typing import Any

from src.tools.database.tabular import PSQL_FORMAT_ARGS, parse_tabular_output

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300


class LocalPsqlExecutor:
    """Executes SQL and shell commands against a locally reachable PostgreSQL."""

    def __init__(
        self,
        user: str,
        password: str,
        backend: str = "docker",
        container: str | None = None,
        psql_path: str = "psql",
    ):
        """
        Args:
            user: PostgreSQL role to connect as.
            password: Password for that role. Passed via environment, not argv.
            backend: "docker" or "binary".
            container: Container name, required when backend="docker".
            psql_path: psql executable, used when backend="binary".
        """
        if backend not in ("docker", "binary"):
            raise ValueError(f"backend must be 'docker' or 'binary', got {backend!r}")
        if backend == "docker" and not container:
            raise ValueError("container is required when backend='docker'")

        self.user = user
        self._password = password
        self.backend = backend
        self.container = container
        self.psql_path = psql_path

    # ------------------------------------------------------------------
    # Executor surface
    # ------------------------------------------------------------------

    def run_sql(
        self,
        host: str,
        port: int,
        database: str,
        sql: str = "",
        timeout: int = DEFAULT_TIMEOUT,
    ) -> str:
        """Execute SQL, returning psql's raw output (see tabular)."""
        psql_args = ["-U", self.user, "-d", database, *PSQL_FORMAT_ARGS, "-c", sql]

        if self.backend == "docker":
            # -e passes the password as a container env var; it never reaches argv.
            argv = [
                "docker", "exec",
                "-e", f"PGPASSWORD={self._password}",
                str(self.container),
                "psql", *psql_args,
            ]
            env = None
        else:
            argv = [self.psql_path, "-h", host, "-p", str(port), *psql_args]
            env = {"PGPASSWORD": self._password, "PATH": "/usr/bin:/bin:/usr/local/bin"}

        return self._run(argv, timeout, env)

    def run_sql_json(
        self,
        host: str,
        port: int,
        database: str,
        sql: str = "",
        timeout: int = DEFAULT_TIMEOUT,
    ) -> list[dict]:
        """Execute SQL and parse the output into a list of dicts.

        Raises:
            MalformedOutput: a row does not match the header. Never a silently short result.
        """
        return parse_tabular_output(self.run_sql(host, port, database, sql, timeout))

    def run_command(self, command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        """Run a shell command where the database runs.

        For backend="docker" this executes INSIDE the container, so /proc reads
        describe the container's view of the kernel. On Docker Desktop for macOS
        that is the Linux VM, NOT macOS and not a dedicated database host.

        The numbers are real but they describe the wrong machine for assessment
        purposes. The collector labels host metrics accordingly rather than
        presenting them as production-representative.
        """
        if self.backend == "docker":
            argv = ["docker", "exec", str(self.container), "sh", "-c", command]
        else:
            argv = ["sh", "-c", command]
        return self._run(argv, timeout, env=None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run(self, argv: list[str], timeout: int, env: dict[str, str] | None) -> str:
        """Run a command as an argv list. Never uses shell=True."""
        try:
            proc = subprocess.run(  # nosec B603 - argv list, no shell  # nosemgrep: dangerous-subprocess-use-audit
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(f"command timed out after {timeout}s") from e
        except FileNotFoundError as e:
            raise RuntimeError(f"executable not found: {argv[0]}") from e

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise RuntimeError(f"command failed (rc={proc.returncode}): {stderr[:400]}")

        return proc.stdout

    def probe(self) -> dict[str, Any]:
        """Verify connectivity before a collection run begins.

        Cheap up-front check so a misconfigured connection fails with a clear
        message rather than surfacing as a dozen swallowed stage errors.
        """
        try:
            rows = self.run_sql_json("localhost", 5432, "postgres", sql="SELECT 1 AS ok")
            return {"reachable": bool(rows) and rows[0].get("ok") == 1, "backend": self.backend}
        except Exception as e:
            return {"reachable": False, "backend": self.backend, "error": str(e)}
