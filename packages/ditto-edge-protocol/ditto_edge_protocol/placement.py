"""Which nodes hold which objects: salted rendezvous hashing.

An object's holders are the ``replicas`` live nodes with the highest
``sha256(salt || digest || node_id)``, never two behind one operator (a bound
hotkey, or the node itself when unbound). Every node and every verifier
computes the same answer from the same live set, so placement needs no stored
plan:

* load spreads evenly, about ``objects * replicas / nodes`` each;
* a node that joins takes over its share, and holders that drop out of an
  object's top set stop renewing it, which rebalances without a coordinator
  step;
* the salt is a lane secret, so a node cannot grind device keys to land on
  objects it wants.

Origin always keeps the canonical copy, so placement optimizes spread, not
durability.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PlacementNode:
    node_id: str
    operator: str


@dataclass(frozen=True)
class PlacementObject:
    digest: str
    size_bytes: int
    created_at: float


def rendezvous_score(salt: bytes, digest: str, node_id: str) -> bytes:
    return hashlib.sha256(salt + bytes.fromhex(digest) + node_id.encode()).digest()


def holders(
    *, digest: str, nodes: Sequence[PlacementNode], replicas: int, salt: bytes
) -> list[str]:
    """The object's holders, highest score first, one per operator."""
    ranked = sorted(
        nodes,
        key=lambda node: rendezvous_score(salt, digest, node.node_id),
        reverse=True,
    )
    chosen: list[str] = []
    operators: set[str] = set()
    for node in ranked:
        if len(chosen) >= replicas:
            break
        if node.operator in operators:
            continue
        chosen.append(node.node_id)
        operators.add(node.operator)
    return chosen


def desired_objects(
    *,
    node_id: str,
    objects: Sequence[PlacementObject],
    nodes: Sequence[PlacementNode],
    replicas: int,
    salt: bytes,
    quota_bytes: int,
    max_object_bytes: int,
) -> list[str]:
    """Digests this node should hold, oldest first, within its quota.

    ``nodes`` is the live set and must include this node. Objects that would
    overflow the quota are skipped, so a full node simply holds fewer.
    """
    if replicas <= 0 or quota_bytes <= 0:
        return []
    mine = [
        obj
        for obj in objects
        if 0 < obj.size_bytes <= max_object_bytes
        and node_id
        in holders(digest=obj.digest, nodes=nodes, replicas=replicas, salt=salt)
    ]
    mine.sort(key=lambda obj: (obj.created_at, obj.digest))
    chosen: list[str] = []
    remaining = quota_bytes
    for obj in mine:
        if obj.size_bytes > remaining:
            continue
        chosen.append(obj.digest)
        remaining -= obj.size_bytes
    return chosen
