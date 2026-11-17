"""
The database password from AWS Secrets Manager, for a live connection that names a secret.

The server reads the secret with its own AWS credentials, so a caller could otherwise make
it read any secret its role can reach. That is off unless the operator lists the ARN
prefixes it may read in SECRET_ARN_ALLOWED_PREFIXES (comma-separated, for example
``arn:aws:secretsmanager:us-east-1:123456789012:secret:apcat/``). Secrets outside them are
refused before any AWS call.

The password goes to psql on the SSH session's stdin (see SshPsqlExecutor). It is never
written, logged, put on a command line or returned. Errors are fixed text: AWS's own
message can name the server's role and account.

The secret is either the plain password, or JSON with a ``password`` key (the shape RDS and
Secrets Manager's database templates use). IAM: secretsmanager:GetSecretValue on the
secret, and kms:Decrypt when it uses a customer managed key.
"""

import json
import logging
import os
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

SECRET_ARN_PATTERN = re.compile(
    r"^arn:aws[a-z-]*:secretsmanager:[a-z]{2}(-gov)?-[a-z]+-\d:\d{12}:secret:[A-Za-z0-9/_+=.@-]{1,512}$")
SECRET_ARN_MAX_LENGTH = 600


class SecretError(Exception):
    """A secret could not be used. `kind` picks the fixed message the caller shows."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


MESSAGES: dict[str, tuple[str, str]] = {
    "not_enabled": ("This server does not read database passwords from Secrets Manager.",
                    "Use password_file, or ask the operator to set SECRET_ARN_ALLOWED_PREFIXES."),
    "not_allowed": ("This server may not read that secret.",
                    "Use a secret under a prefix in SECRET_ARN_ALLOWED_PREFIXES."),
    "unreadable": ("The secret could not be read.",
                   "Check the ARN, and that the server's role has secretsmanager:GetSecretValue "
                   "on it (and kms:Decrypt for a customer managed key)."),
    "no_password": ("The secret holds no password.",
                    "Store the password as the secret's text, or as JSON with a password key."),
}


def arn_ok(value: str) -> bool:
    """Whether a value is shaped like a Secrets Manager secret ARN."""
    return len(value) <= SECRET_ARN_MAX_LENGTH and bool(SECRET_ARN_PATTERN.fullmatch(value))


def allowed_prefixes() -> list[str]:
    raw = os.environ.get("SECRET_ARN_ALLOWED_PREFIXES", "")
    return [p.strip() for p in raw.split(",") if p.strip().startswith("arn:")]


def enabled() -> bool:
    return bool(allowed_prefixes())


def check_allowed(arn: str) -> None:
    """Raise SecretError unless this server may read the secret. Calls nothing."""
    prefixes = allowed_prefixes()
    if not prefixes:
        raise SecretError("not_enabled")
    if not any(arn.startswith(p) for p in prefixes):
        raise SecretError("not_allowed")


def _client(region: str) -> Any:
    """The Secrets Manager client. Replaced in tests."""
    import boto3

    return boto3.client("secretsmanager", region_name=region)


def read_password(arn: str, client_for: Callable[[str], Any] | None = None) -> str:
    """The password a secret holds.

    Raises:
        SecretError: not_enabled, not_allowed, unreadable or no_password.
    """
    check_allowed(arn)
    region = arn.split(":")[3]
    try:
        value = (client_for or _client)(region).get_secret_value(SecretId=arn)
    except Exception as e:  # noqa: BLE001 - every AWS failure is the same fixed message
        # The exception class only: never the ARN, the response or the value.
        logger.warning("AWS GetSecretValue call failed: %s", type(e).__name__)
        raise SecretError("unreadable") from None
    text = value.get("SecretString")
    if not isinstance(text, str) or not text:
        raise SecretError("no_password")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        password = parsed.get("password")
        if not isinstance(password, str) or not password:
            raise SecretError("no_password")
        return password
    if "\n" in text:
        # stdin hands psql the first line only; a multi-line secret is not a password.
        raise SecretError("no_password")
    return text
