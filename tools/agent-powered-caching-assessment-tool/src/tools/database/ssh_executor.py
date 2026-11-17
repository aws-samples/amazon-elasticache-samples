"""
SSH executor: collect from a PostgreSQL host over SSH.

Runs `psql` and host commands ON the database host over one SSH connection that is reused for
every call. Nothing listens on either machine and no database port is opened. The output of a
call is not limited in size.

    run_sql(host, port, database, sql, timeout)       -> str
    run_sql_json(host, port, database, sql, timeout)  -> list[dict]
    run_command(command, timeout)                     -> str

`host` and `port` are the database as seen FROM the SSH host, usually 127.0.0.1:5432 when the
database runs there. Host metrics describe the SSH host, so it must be the database host itself,
not a jump host (preflight proves it).

HOST KEY
--------
Always checked: StrictHostKeyChecking=yes against a known_hosts file. An unknown or changed key
refuses the connection. The operator adds the host to that file; nothing here ever does.

PASSWORD
--------
Never placed on any command line, local or remote, so it is not visible in `ps` on either
machine. One of:

  password_file  a file ON the database host; the remote shell reads it, so the password never
                 reaches this machine at all.
  password       sent on the SSH session's stdin and read into PGPASSWORD by the remote shell.
                 Used for a Secrets Manager password the server read, and by scripts run by
                 the person who owns the database.

commands_only=True builds an executor for shell commands only (discovery's host probe): no
database user or password, and run_sql refuses.

Spec: .kiro/specs/ssh-collection.
"""

from __future__ import annotations

import atexit
import logging
import os
import shlex
import shutil
import subprocess  # nosec B404 - argv lists only, never shell=True
import tempfile
from pathlib import Path
from typing import Any

from src.tools.database import ssh_values
from src.tools.database.tabular import PSQL_FORMAT_ARGS, parse_tabular_output

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300
CONNECT_TIMEOUT_SEC = 15


class SshPsqlExecutor:
    """Executes SQL and shell commands on a database host over SSH."""

    def __init__(
        self,
        ssh_target: str,
        user: str,
        password: str | None = None,
        password_file: str | None = None,
        identity_file: str | Path | None = None,
        ssh_port: int = 22,
        known_hosts: str | Path | None = None,
        psql_path: str = "psql",
        ssh_path: str = "ssh",
        commands_only: bool = False,
    ):
        """
        Args:
            ssh_target: A host, user@host, or a name from ~/.ssh/config.
            user: PostgreSQL role. Not used, and may be empty, when commands_only.
            password: PostgreSQL password, sent over the session's stdin.
            password_file: Path ON the database host to a file holding the password. Takes
                precedence over `password`.
            identity_file: A private key file (already resolved by the caller).
            ssh_port: The SSH port.
            known_hosts: The known_hosts file. Defaults to SSH_KNOWN_HOSTS, then
                ~/.ssh/known_hosts.
            psql_path: psql on the database host.
            ssh_path: Local ssh client.
            commands_only: Shell commands only, for discovery's host probe. Needs no
                database user or password; run_sql refuses.

        Raises:
            ValueError: no password and no password file, or a value is not in an accepted
                form (see ssh_values).
            FileNotFoundError: the known_hosts file does not exist.
        """
        if not commands_only and not password and not password_file:
            raise ValueError("a password or a password file on the host is required")
        if not ssh_values.target_ok(ssh_target):
            raise ValueError("ssh_target is not in an accepted form")
        if not commands_only and not ssh_values.db_user_ok(user):
            raise ValueError("the database user is not in an accepted form")
        if password_file and not ssh_values.password_file_ok(password_file):
            raise ValueError("password_file is not in an accepted form")
        if isinstance(ssh_port, bool) or not isinstance(ssh_port, int) or not 1 <= ssh_port <= 65535:
            raise ValueError("ssh_port must be between 1 and 65535")
        self.ssh_target = ssh_target
        self.user = user
        self._password = password
        self.password_file = password_file
        self.identity_file = str(identity_file) if identity_file else None
        self.ssh_port = ssh_port
        self.known_hosts = str(Path(known_hosts) if known_hosts else ssh_values.known_hosts_file())
        self.psql_path = psql_path
        self.ssh_path = ssh_path
        self.commands_only = commands_only
        # One SSH connection reused for every statement, through a Unix socket in a private
        # directory. Not a network listener. Removed on close().
        self._control_dir = tempfile.mkdtemp(prefix="pgca-ssh-")
        os.chmod(self._control_dir, 0o700)
        self._control_path = os.path.join(self._control_dir, "cm")
        atexit.register(self.close)

    def close(self) -> None:
        """Close the shared SSH connection and remove its socket directory."""
        if not os.path.isdir(self._control_dir):
            return
        if os.path.exists(self._control_path):
            subprocess.run(  # nosec B603 - argv list, no shell  # nosemgrep: dangerous-subprocess-use-audit
                [self.ssh_path, "-o", f"ControlPath={self._control_path}",
                 "-O", "exit", "--", self.ssh_target],
                capture_output=True, timeout=15, check=False)
        shutil.rmtree(self._control_dir, ignore_errors=True)

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
        """Execute SQL on the database host, returning psql's raw output (see tabular).

        Raises:
            RuntimeError: the executor was built with commands_only.
        """
        if self.commands_only:
            raise RuntimeError("this SSH executor runs shell commands only")
        psql = " ".join(shlex.quote(a) for a in (
            self.psql_path, "-h", host, "-p", str(port), "-U", self.user,
            "-d", database, *PSQL_FORMAT_ARGS, "-c", sql,
        ))
        if self.password_file:
            # Read on the host; the password never crosses this machine.
            remote = (f"PGPASSWORD=\"$(cat {shlex.quote(self.password_file)})\" "
                      f"|| exit 2; export PGPASSWORD; exec {psql}")
            stdin = None
        else:
            # First line of stdin becomes PGPASSWORD in the remote shell.
            remote = f"IFS= read -r PGPASSWORD; export PGPASSWORD; exec {psql}"
            stdin = f"{self._password}\n"
        return self._ssh(remote, timeout, stdin)

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
        """Run a shell command on the database host."""
        return self._ssh(f"sh -c {shlex.quote(command)}", timeout, None)

    def probe(self, host: str = "127.0.0.1", port: int = 5432,
              database: str = "postgres") -> dict[str, Any]:
        """Check SSH and database connectivity before collecting.

        Args:
            host: Database host as seen from the SSH host.
            port: Database port.
            database: Database to connect to.
        """
        try:
            rows = self.run_sql_json(host, port, database, sql="SELECT 1 AS ok", timeout=60)
            return {"reachable": bool(rows) and rows[0].get("ok") == 1,
                    "backend": f"ssh to {self.ssh_target}"}
        except Exception as e:  # noqa: BLE001 - reported to the caller
            return {"reachable": False, "backend": f"ssh to {self.ssh_target}",
                    "error": str(e)}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _argv(self, remote: str) -> list[str]:
        argv = [self.ssh_path,
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={self.known_hosts}",
                "-o", "ClearAllForwardings=yes",
                "-o", f"ConnectTimeout={CONNECT_TIMEOUT_SEC}",
                "-o", "ServerAliveInterval=30",
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self._control_path}",
                "-o", "ControlPersist=120",
                "-p", str(self.ssh_port)]
        if self.identity_file:
            argv += ["-i", self.identity_file, "-o", "IdentitiesOnly=yes"]
        # "--" ends ssh's options; the remote command is one argument, which ssh hands to
        # the remote login shell.
        return argv + ["--", self.ssh_target, remote]

    def _ssh(self, remote: str, timeout: int, stdin: str | None) -> str:
        argv = self._argv(remote)
        try:
            proc = subprocess.run(  # nosec B603 - argv list, no local shell; values checked by ssh_values  # nosemgrep: dangerous-subprocess-use-audit
                argv, input=stdin, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(f"ssh command timed out after {timeout}s") from e
        except FileNotFoundError as e:
            raise RuntimeError(f"executable not found: {argv[0]}") from e
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise RuntimeError(f"ssh command failed (rc={proc.returncode}): {stderr[:400]}")
        return proc.stdout
