"""Make a PostgreSQL SCRAM-SHA-256 password verifier, so a role can be created without the
password ever appearing in SQL text.

`CREATE ROLE ... PASSWORD 'secret'` is kept as written by pg_stat_statements and may be logged.
A verifier is a one-way hash of the password, so the statement that carries it does not reveal
the password to anything that logs it.

Usage (reads the password from standard input, prints the verifier):
    printf %s "$PASSWORD" | python3 scram_verifier.py

Standard library only, so it runs on any host with Python 3.9 or later.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys

ITERATIONS = 4096
SALT_BYTES = 16


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    """The SCRAM-SHA-256 verifier PostgreSQL stores for a password.

    Args:
        password: The password.
        salt: Random bytes. A fresh 16-byte salt when omitted. Fixed only for tests.
        iterations: PBKDF2 rounds. PostgreSQL's default is 4096.

    Returns:
        ``SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>``, the form PostgreSQL accepts
        in ``CREATE ROLE ... PASSWORD`` and ``ALTER ROLE ... PASSWORD``.

    Raises:
        ValueError: the password is empty, or the iteration count is not positive.
    """
    if not password:
        raise ValueError("the password must not be empty")
    if iterations < 1:
        raise ValueError("iterations must be positive")
    salt = os.urandom(SALT_BYTES) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def main() -> int:
    """Read a password from standard input and print its verifier.

    Returns:
        0 on success, 2 when no password was given.
    """
    password = sys.stdin.read().rstrip("\n")
    if not password:
        print("error: give the password on standard input", file=sys.stderr)
        return 2
    print(scram_verifier(password))
    return 0


if __name__ == "__main__":
    sys.exit(main())
