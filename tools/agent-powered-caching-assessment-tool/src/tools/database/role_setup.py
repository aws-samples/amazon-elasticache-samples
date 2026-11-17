"""
Create the read-only role the tool signs in as, on the database host, over SSH.

WHY
---
Without this a DBA has to create the role, make a password, hash it and store it in a file
before the console can be used. "Create a read-only user" does that once, from the
Save a database form.

HOW
---
One constant POSIX sh script runs on the database host as the SSH user. Only checked values
go into it, as positional arguments, and every value is also shell-quoted. It:

  1. needs python3 (to make the password and its hash) and `sudo -n -u postgres`, which
     signs in to PostgreSQL as the postgres OS user over the local socket. No admin password
     is typed, sent or stored.
  2. When the role does not exist: writes a random password to a new file (mode 600) on the
     host and creates the role with only its SCRAM-SHA-256 verifier in the SQL, so the
     password never reaches the tool, the server log or pg_stat_statements.
  3. When the role exists and its password file is there: applies the read-only setting and
     the grants again, and changes nothing else. When the file is not there, it stops: nobody
     knows that role's password, and changing it could break whatever else uses it.

It never creates or changes a superuser, never touches pg_hba.conf, postgresql.conf or
pg_stat_statements, and never restarts anything. The SQL uses psql variables (:"r", :'v'),
so a value is never spliced into SQL text.
Spec: .kiro/specs/databases (role setup).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from src.tools.database import ssh_values

# Lowercase, so the name needs no quoting anywhere; not a reserved pg_ name.
ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
DEFAULT_ROLE = "apcat_reader"
_DATABASE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}")
TIMEOUT_SEC = 90

# Make a SCRAM-SHA-256 verifier from the password file's contents (same as
# scripts/scale/scram_verifier.py). Standard library only.
_VERIFIER_PY = (
    "import base64,hashlib,hmac,os,sys\n"
    "p=open(sys.argv[1]).read().strip().encode()\n"
    "s=os.urandom(16);n=4096\n"
    "k=hashlib.pbkdf2_hmac('sha256',p,s,n)\n"
    "c=hmac.new(k,b'Client Key',hashlib.sha256).digest()\n"
    "sv=hmac.new(k,b'Server Key',hashlib.sha256).digest()\n"
    "e=lambda b:base64.b64encode(b).decode()\n"
    "print('SCRAM-SHA-256$%d:%s$%s:%s'%(n,e(s),e(hashlib.sha256(c).digest()),e(sv)))\n"
)

# $1 role, $2 database, $3 port, $4 password file (empty: $HOME/.<role>_pw).
# Prints key=value lines; result= is always last.
SCRIPT = r"""
set -u
role="$1"; db="$2"; port="$3"; pwfile="$4"
[ -n "$pwfile" ] || pwfile="$HOME/.${role}_pw"
out() { printf '%s=%s\n' "$1" "$2"; }
out password_file "$pwfile"
command -v python3 >/dev/null 2>&1 || { out result no_python; exit 0; }
cd / 2>/dev/null
sudo -n -u postgres true >/dev/null 2>&1 || { out result no_sudo; exit 0; }
pg() { sudo -n -u postgres psql -X -q -At -v ON_ERROR_STOP=1 -p "$port" -d "$db" -v r="$role" "$@"; }
ver=$(printf '%s\n' "SHOW server_version_num;" | pg 2>/dev/null) || { out result no_database; exit 0; }
[ "${ver:-0}" -ge 140000 ] 2>/dev/null || { out result old_postgres; exit 0; }
info=$(printf '%s\n' "SELECT CASE WHEN rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls THEN 'privileged' ELSE 'plain' END FROM pg_roles WHERE rolname = :'r';" | pg 2>/dev/null) || { out result psql_failed; exit 0; }
grants() {
  printf '%s\n' 'ALTER ROLE :"r" SET default_transaction_read_only = on;' \
    'GRANT pg_monitor TO :"r";' 'GRANT pg_read_all_data TO :"r";' | pg >/dev/null 2>&1
}
if [ "$info" = "privileged" ]; then out result role_privileged; exit 0; fi
if [ "$info" = "plain" ]; then
  [ -s "$pwfile" ] || { out result exists_no_file; exit 0; }
  grants || { out result psql_failed; exit 0; }
  out result reused; exit 0
fi
[ -e "$pwfile" ] && { out result file_exists; exit 0; }
umask 077
python3 -c 'import secrets;print(secrets.token_hex(16),end="")' > "$pwfile" || { out result file_failed; exit 0; }
v=$(python3 -c "$VERIFIER_PY" "$pwfile") || { rm -f "$pwfile"; out result file_failed; exit 0; }
if ! printf '%s\n' 'CREATE ROLE :"r" LOGIN PASSWORD :'"'"'v'"'"' NOSUPERUSER NOCREATEDB NOCREATEROLE;' \
    | pg -v v="$v" >/dev/null 2>&1; then rm -f "$pwfile"; out result psql_failed; exit 0; fi
grants || { out result psql_failed; exit 0; }
out result created
"""

# Fixed text per outcome, for the form. ok means the role can be used now.
OUTCOMES: dict[str, tuple[bool, str]] = {
    "created": (True, "Created the read-only role. Its password is in a new file on the host."),
    "reused": (True, "The role already existed. The tool made sure it is read-only with the "
                     "grants it needs, and will use its password file."),
    "exists_no_file": (False, "This role already exists, but its password file is not at that "
                              "path. Give the file that holds its password, or pick another "
                              "name."),
    "role_privileged": (False, "This role exists and has admin rights. The tool signs in only as a "
                               "read-only role. Pick another name."),
    "file_exists": (False, "A file is already at that path. Pick another password file path."),
    "no_sudo": (False, "The SSH user cannot run commands as the postgres user without a "
                       "password. Create the role by hand, or use an existing user."),
    "no_python": (False, "python3 is not installed on the database host, so the tool cannot make "
                         "the password there. Create the role by hand."),
    "no_database": (False, "PostgreSQL did not answer for that database and port as the "
                           "postgres user. Check the database name and port."),
    "old_postgres": (False, "PostgreSQL 14 or later is needed to create the role this way. "
                            "Create it by hand."),
    "file_failed": (False, "The password file could not be written on the host."),
    "psql_failed": (False, "PostgreSQL refused the change. Create the role by hand, or ask "
                           "an administrator what blocked it."),
    "ssh_failed": (False, "The tool could not run the setup over SSH. Check the SSH target and "
                          "that the host key is known."),
    "unexpected": (False, "The host answered in a way the tool did not expect. Nothing was "
                          "assumed."),
}


@dataclass
class RoleSetupResult:
    """What the setup did."""

    outcome: str
    ok: bool
    message: str
    role_name: str
    password_file: str | None


def role_problems(role_name: str, database: str, port: int, password_file: str | None) -> list[str]:
    """What is wrong with the shape of the values. Names the field, never the value.

    Args:
        role_name: The role to create or reuse.
        database: The database to connect to as postgres.
        port: PostgreSQL's port.
        password_file: An absolute path on the host, or None for $HOME/.<role>_pw.
    """
    problems = []
    if not ROLE_NAME.fullmatch(role_name or "") or role_name.startswith("pg_"):
        problems.append("role_name must be lowercase letters, digits or _, start with a letter "
                        "or _, and not start with pg_")
    if role_name == "postgres":
        problems.append("role_name must not be postgres")
    if not _DATABASE.fullmatch(database or ""):
        problems.append("database_name is not in an accepted form")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        problems.append("port must be between 1 and 65535")
    if password_file is not None and not ssh_values.password_file_ok(password_file):
        problems.append("password_file must be an absolute path of letters, digits, _, ., / "
                        "or -")
    return problems


def build_command(role_name: str, database: str, port: int, password_file: str | None) -> str:
    """The command for run_command: the values as positional arguments, then the script.

    Args:
        role_name, database, port, password_file: Already checked by role_problems().
    """
    args = " ".join(shlex.quote(str(a)) for a in (role_name, database, port, password_file or ""))
    return (f"VERIFIER_PY={shlex.quote(_VERIFIER_PY)}; set -- {args}; "
            f"export VERIFIER_PY; {SCRIPT}")


def parse_output(raw: str, role_name: str) -> RoleSetupResult:
    """Read the script's key=value lines.

    Args:
        raw: What the script printed.
        role_name: The role asked for.
    """
    values: dict[str, str] = {}
    for line in (raw or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    outcome = values.get("result", "")
    if outcome not in OUTCOMES:
        outcome = "unexpected"
    ok, message = OUTCOMES[outcome]
    pwfile = values.get("password_file") or None
    if pwfile is not None and not ssh_values.password_file_ok(pwfile):
        pwfile = None
    # The path is useful when the role can be used, or when it names what is in the way.
    shown = pwfile if ok or outcome in ("exists_no_file", "file_exists") else None
    return RoleSetupResult(outcome=outcome, ok=ok, message=message, role_name=role_name,
                           password_file=shown)


def create_role(executor, role_name: str, database: str, port: int,
                password_file: str | None) -> RoleSetupResult:
    """Create or reuse the read-only role on the database host.

    Args:
        executor: Anything with run_command(command, timeout) -> str, on the database host.
        role_name, database, port, password_file: As role_problems() takes them.

    Returns:
        The outcome. An SSH failure is the ssh_failed outcome, never a raised error.

    Raises:
        ValueError: a value is not in an accepted form.
    """
    problems = role_problems(role_name, database, port, password_file)
    if problems:
        raise ValueError("; ".join(problems))
    try:
        raw = executor.run_command(build_command(role_name, database, port, password_file),
                                   timeout=TIMEOUT_SEC)
    except Exception:  # noqa: BLE001 - reported as fixed text; the log has the detail
        ok, message = OUTCOMES["ssh_failed"]
        return RoleSetupResult("ssh_failed", ok, message, role_name, None)
    return parse_output(raw, role_name)
