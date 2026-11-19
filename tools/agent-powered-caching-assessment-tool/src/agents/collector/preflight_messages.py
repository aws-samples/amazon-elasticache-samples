"""
Fixed messages for a failed preflight check.

An exception from ssh or psql is reduced to one of a small set of known problems. The
exception's own text is never returned: ssh and psql put host names, user names and paths in it.
The text is only matched against known phrases, and logged by the caller.
"""

# (substrings of the lower-cased error text, message, hint). First match wins, so the more
# specific ones come first. The ssh client's own errors say "ssh:" or name the host key or the
# key; psql's errors reach us through ssh as "psql: error: ...".
_KNOWN: list[tuple[tuple[str, ...], str, str]] = [
    # --- Before a connection exists: the values and files on this server ---
    (("known_hosts file does not exist",),
     "The server has no known_hosts file.",
     "Create the file named by SSH_KNOWN_HOSTS (or ~/.ssh/known_hosts) and add the database "
     "host to it after checking its fingerprint."),
    (("ssh_key_dir is not set",),
     "A key was named but the server has no key directory.",
     "Set SSH_KEY_DIR on the server, put the key in it, or leave identity_file out."),
    (("key file does not exist",),
     "The named key is not in the server's key directory.",
     "Check identity_file against the files in SSH_KEY_DIR."),
    (("can be read by group or others",),
     "The key file's permissions are too open.",
     "Run chmod 600 on the key file."),
    (("identity_file",),
     "The named key cannot be used.",
     "identity_file must be a plain file name inside SSH_KEY_DIR."),
    (("not in an accepted form",),
     "A value in the request is not in an accepted form.",
     "Check ssh_target, db_user, password_file and identity_file."),
    # --- ssh itself ---
    (("host key verification failed", "remote host identification has changed",
      "no matching host key"),
     "The host key is not trusted.",
     "Check the host's fingerprint, then add it to the known_hosts file the server uses. "
     "A changed key can mean the host was rebuilt, or that something is in the way."),
    (("permission denied (publickey", "permission denied (publickey,", "too many authentication"),
     "The host refused the key or the user.",
     "Check ssh_target's user, that the key is authorized on the host, and identity_file."),
    (("could not resolve hostname", "name or service not known"),
     "The SSH host name did not resolve.",
     "Check ssh_target."),
    (("ssh: connect to host", "connection timed out", "operation timed out", "no route to host",
      "network is unreachable"),
     "The server could not reach the SSH port.",
     "Check ssh_target and ssh_port, the host's security group or firewall for this server's "
     "address, and that sshd is running."),
    (("connection refused",),
     "The connection was refused.",
     "If this is the SSH port, check that sshd is running and ssh_port. If it is the "
     "database, check cluster_endpoint, port, and that PostgreSQL is running."),
    # --- On the database host ---
    (("psql: command not found", "psql: not found"),
     "psql is not installed on the database host.",
     "Install the PostgreSQL client on the host."),
    (("cat: /",),
     "The password file could not be read on the host.",
     "Check password_file: it must exist on the database host and be readable by the SSH user."),
    (("password authentication failed", "authentication failed"),
     "The database rejected the user name or password.",
     "Check db_user and the password in password_file."),
    (("no pg_hba.conf entry",),
     "The database does not allow this connection.",
     "Check that pg_hba.conf allows this user and database from the host psql runs on."),
    (("does not exist",),
     "The database or role was not found.",
     "Check database_name and db_user."),
    (("could not connect", "timeout expired", "could not translate host name"),
     "The database host and port could not be reached from the SSH host.",
     "Check cluster_endpoint and port as seen from the SSH host, and that PostgreSQL is running."),
    (("timedout", "timed out"),
     "The host did not answer in time.",
     "Check the host and the load on it."),
]
_DEFAULT_HINT = "See the server log for the full error."


def describe_failure(exc: Exception) -> tuple[str, str]:
    """A message and a hint for a failed check.

    Args:
        exc: What the check raised.

    Returns:
        (message, hint), both fixed text. Nothing from the exception's own text is
        returned.
    """
    text = str(exc).lower()
    for needles, message, hint in _KNOWN:
        if any(n in text for n in needles):
            return message, hint
    return "The check failed.", _DEFAULT_HINT
