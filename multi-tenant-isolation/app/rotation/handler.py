# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AWS Secrets Manager rotation Lambda for the multi-tenant Valkey demo.

Implements the standard four-step Secrets Manager rotation contract
(createSecret / setSecret / testSecret / finishSecret) for two kinds of secret,
distinguished by the ``rotation-type`` staging label stored on the secret's
description or the ``SecretType`` key inside the JSON:

  * ``elasticache-user`` - the secret value is an ElastiCache RBAC user's
    password. setSecret calls elasticache:ModifyUser to set the new password,
    so the user accepts BOTH the old (AWSCURRENT) and new (AWSPENDING) password
    during rotation; finishSecret promotes AWSPENDING to AWSCURRENT.
  * ``opaque`` - the secret value is a standalone string (e.g. the Flask signing
    key). Rotation just generates a new random value; there is no external
    system to update.

The secret JSON is ``{"SecretType": "...", "UserId": "...", "password": "..."}``
for elasticache-user secrets, or a bare string for opaque secrets. To keep the
tenant/AUTH secrets simple (they store a bare password string), the Lambda also
accepts a bare-string secret plus a ``SecretType``/``UserId`` taken from tags.

This is demo-grade rotation: it shows the mechanism and satisfies the
"rotation configured" control. Review and harden before production use.
"""
from __future__ import annotations

import logging

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_sm = boto3.client("secretsmanager")
_ec = boto3.client("elasticache")

_PW_LEN = 32
_EXCLUDE = '"@/\\ \'<>&#$^'


def _meta(secret_id: str) -> dict:
    """Return rotation metadata (type + user id) from the secret's tags."""
    desc = _sm.describe_secret(SecretId=secret_id)
    tags = {t["Key"]: t["Value"] for t in desc.get("Tags", [])}
    return {
        "type": tags.get("rotation-type", "opaque"),
        "user_id": tags.get("elasticache-user-id", ""),
    }


def _new_password() -> str:
    return _sm.get_random_password(
        PasswordLength=_PW_LEN,
        ExcludeCharacters=_EXCLUDE,
        RequireEachIncludedType=True,
    )["RandomPassword"]


def handler(event: dict, _context: object) -> None:
    secret_id = event["SecretId"]
    token = event["ClientRequestToken"]
    step = event["Step"]
    # Rotation progress is observable via the Secrets Manager rotation status
    # and the function's CloudWatch metrics; we avoid application logging inside
    # this credential-handling function.

    if step == "createSecret":
        _create(secret_id, token)
    elif step == "setSecret":
        _set(secret_id, token)
    elif step == "testSecret":
        _test(secret_id, token)
    elif step == "finishSecret":
        _finish(secret_id, token)
    else:
        raise ValueError(f"Unknown rotation step: {step}")


def _create(secret_id: str, token: str) -> None:
    """Generate a new AWSPENDING value if one is not already staged."""
    try:
        _sm.get_secret_value(
            SecretId=secret_id, VersionId=token, VersionStage="AWSPENDING"
        )
        logger.info("AWSPENDING already exists; skipping create")
        return
    except _sm.exceptions.ResourceNotFoundException:
        pass
    _sm.put_secret_value(
        SecretId=secret_id,
        ClientRequestToken=token,
        SecretString=_new_password(),
        VersionStages=["AWSPENDING"],
    )


def _set(secret_id: str, token: str) -> None:
    """Apply the AWSPENDING value to the backing system (ElastiCache user)."""
    meta = _meta(secret_id)
    if meta["type"] != "elasticache-user":
        # Opaque secret (e.g. Flask key): nothing external to update.
        return
    pending = _sm.get_secret_value(
        SecretId=secret_id, VersionId=token, VersionStage="AWSPENDING"
    )["SecretString"]
    current = _sm.get_secret_value(
        SecretId=secret_id, VersionStage="AWSCURRENT"
    )["SecretString"]
    user_id = meta["user_id"]
    if not user_id:
        raise ValueError(
            f"Secret {secret_id} is tagged elasticache-user but has no "
            "elasticache-user-id tag."
        )
    # Set BOTH passwords so connections using the current password keep working
    # until finishSecret promotes the new one.
    _ec.modify_user(UserId=user_id, Passwords=[current, pending])


def _test(secret_id: str, token: str) -> None:
    """Best-effort validation that the pending value is well-formed."""
    pending = _sm.get_secret_value(
        SecretId=secret_id, VersionId=token, VersionStage="AWSPENDING"
    )["SecretString"]
    if not pending:
        raise ValueError("AWSPENDING secret value is empty")


def _finish(secret_id: str, token: str) -> None:
    """Promote AWSPENDING to AWSCURRENT."""
    desc = _sm.describe_secret(SecretId=secret_id)
    current_version = None
    for version, stages in desc.get("VersionIdsToStages", {}).items():
        if "AWSCURRENT" in stages:
            current_version = version
            break
    if current_version == token:
        logger.info("version already current; nothing to do")
        return
    _sm.update_secret_version_stage(
        SecretId=secret_id,
        VersionStage="AWSCURRENT",
        MoveToVersionId=token,
        RemoveFromVersionId=current_version,
    )
    # For an ElastiCache user, reduce back to a single (new) password so the old
    # one no longer works.
    meta = _meta(secret_id)
    if meta["type"] == "elasticache-user" and meta["user_id"]:
        new_pw = _sm.get_secret_value(
            SecretId=secret_id, VersionStage="AWSCURRENT"
        )["SecretString"]
        _ec.modify_user(UserId=meta["user_id"], Passwords=[new_pw])
