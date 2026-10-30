# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Optional demo helper: trigger an ElastiCache primary failover.

This exists purely to make the durability story tangible: with items in a cart,
an operator can force the primary of a shard to fail over and watch the cart
(stored server-side in Valkey) survive the promotion of a replica.

Safety
------
This is a disruptive, control-plane action. It is only reachable when
``ENABLE_FAILOVER_DEMO`` is set, it uses the application's own IAM credentials
(which must grant ``elasticache:TestFailover``), and the route that calls it is
POST-only and off by default. ``boto3`` is imported lazily so it is not a hard
dependency unless the demo is actually enabled.
"""
from __future__ import annotations

from dataclasses import dataclass


class FailoverError(RuntimeError):
    """Raised when a failover could not be initiated."""


@dataclass(frozen=True)
class FailoverResult:
    """Outcome of a successful TestFailover request."""

    replication_group_id: str
    node_group_id: str
    status: str


@dataclass(frozen=True)
class FailoverStatus:
    """A point-in-time view of the shard, for live progress on the UI."""

    status: str  # e.g. 'available' | 'modifying'
    in_progress: bool
    members: tuple[dict[str, str], ...]  # [{id, role, az}, ...]


def trigger_test_failover(
    replication_group_id: str,
    node_group_id: str,
    region: str | None = None,
) -> FailoverResult:
    """Call ElastiCache ``TestFailover`` for one shard (node group).

    ElastiCache promotes a replica in the given node group to primary. The call
    is asynchronous: it returns while the replication group transitions to
    ``modifying`` and then back to ``available`` once failover completes.

    Raises :class:`FailoverError` on any client/credential/service error so the
    caller can show a friendly message rather than leaking a stack trace.
    """
    if not replication_group_id:
        raise FailoverError("replication group id is not configured")

    try:
        import boto3  # lazy: only needed when the demo is enabled
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise FailoverError(
            "boto3 is not installed; install it to use the failover demo"
        ) from exc

    client = boto3.client("elasticache", region_name=region)
    try:
        response = client.test_failover(
            ReplicationGroupId=replication_group_id,
            NodeGroupId=node_group_id,
        )
    except (ClientError, BotoCoreError) as exc:
        raise FailoverError(str(exc)) from exc

    status = (
        response.get("ReplicationGroup", {}).get("Status", "modifying")
    )
    return FailoverResult(
        replication_group_id=replication_group_id,
        node_group_id=node_group_id,
        status=status,
    )


def describe_failover_status(
    replication_group_id: str,
    node_group_id: str,
    region: str | None = None,
) -> FailoverStatus:
    """Read the current shard status + member roles (read-only).

    Used by the UI to poll live failover progress: the replication group status
    moves ``available -> modifying -> available`` while a replica is promoted,
    and the member roles flip (a former replica becomes ``primary``).
    """
    if not replication_group_id:
        raise FailoverError("replication group id is not configured")
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover
        raise FailoverError("boto3 is not installed") from exc

    client = boto3.client("elasticache", region_name=region)
    try:
        resp = client.describe_replication_groups(
            ReplicationGroupId=replication_group_id
        )
    except (ClientError, BotoCoreError) as exc:
        raise FailoverError(str(exc)) from exc

    groups = resp.get("ReplicationGroups", [])
    if not groups:
        raise FailoverError("replication group not found")
    rg = groups[0]
    status = rg.get("Status", "unknown")

    members: list[dict[str, str]] = []
    for ng in rg.get("NodeGroups", []):
        if ng.get("NodeGroupId") != node_group_id:
            continue
        for m in ng.get("NodeGroupMembers", []):
            # CurrentRole is 'primary' or 'replica' when populated; don't guess
            # a role when it is absent (label it 'node' instead of assuming).
            role = (m.get("CurrentRole") or "node").lower()
            members.append(
                {
                    "id": m.get("CacheClusterId", ""),
                    "role": role,
                    "az": m.get("PreferredAvailabilityZone", ""),
                }
            )

    return FailoverStatus(
        status=status,
        in_progress=status != "available",
        members=tuple(members),
    )
