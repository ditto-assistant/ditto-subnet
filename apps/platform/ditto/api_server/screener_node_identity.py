"""Narrow identity rules for persistent screener-node worker processes."""

from __future__ import annotations

import re


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
