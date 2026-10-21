"""Narrow identity rules for persistent screener-node worker processes."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

CONTROLLER_HEARTBEAT_READY_SECONDS = 180
"""A worker heartbeat older than this no longer counts as ready capacity."""


def is_enrolled_node_heartbeat_instance(
    *, node_id: str, instance_id: str | None
) -> bool:
    """Return whether a heartbeat instance belongs to one enrolled node.

    A persistent host holds one enrolled credential while its supervised local
    workers report distinct identities. Accept only the base node identity or
    the exact ``-worker-N`` suffix emitted by that host; never let the shared
    credential impersonate another enrolled node or arbitrary instance.
    """
    if instance_id == node_id:
        return True
    if instance_id is None:
        return False
    return (
        re.fullmatch(rf"{re.escape(node_id)}-worker-[1-9][0-9]*", instance_id)
        is not None
    )


def screener_heartbeat_ready(
    *,
    seen_at: datetime | None,
    policy_version: int | None,
    now: datetime,
    required_policy: int,
) -> bool:
    """Return whether one worker heartbeat is fresh, ready screening capacity.

    The controller's node reconciliation and the subnet liveness read share
    this predicate, so "ready" means exactly one thing on both surfaces: a
    signed heartbeat seen within :data:`CONTROLLER_HEARTBEAT_READY_SECONDS`
    from a worker on the currently required screening policy.
    """
    if seen_at is None or policy_version is None:
        return False
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=UTC)
    return (
        now - seen_at <= timedelta(seconds=CONTROLLER_HEARTBEAT_READY_SECONDS)
        and policy_version == required_policy
    )
