"""Which objects a node should hold next.

Pure and deterministic given its inputs, so Platform and the in-memory
coordinator place identically. Origin always keeps the canonical copy, so
placement optimizes spread, not durability: least-replicated objects first,
and never two replicas behind one operator (a bound hotkey, or the node itself
when unbound).
"""

from __future__ import annotations

from collections.abc import Sequence, Set
from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    """An active object as one node's placement sees it."""

    digest: str
    size_bytes: int
    replicas: int
    """Live assignments on other nodes."""
    operator_holds: bool
    """Another node behind this node's operator already holds it."""
    created_at: float


def plan_assignments(
    *,
    candidates: Sequence[Candidate],
    held: Set[str],
    free_bytes: int,
    replicas_target: int,
    max_new: int,
    max_object_bytes: int,
) -> list[str]:
    """Digests to assign to one node, in assignment order.

    Skips objects the node already holds, objects at the replica target,
    objects its operator already holds, and objects larger than the lane limit
    or the node's remaining quota. Ties break on age, then digest, so the plan
    never depends on input order.
    """
    if max_new <= 0 or free_bytes <= 0 or replicas_target <= 0:
        return []
    eligible = [
        candidate
        for candidate in candidates
        if candidate.digest not in held
        and not candidate.operator_holds
        and candidate.replicas < replicas_target
        and 0 < candidate.size_bytes <= max_object_bytes
    ]
    eligible.sort(key=lambda c: (c.replicas, c.created_at, c.digest))
    chosen: list[str] = []
    remaining = free_bytes
    for candidate in eligible:
        if len(chosen) >= max_new:
            break
        if candidate.size_bytes > remaining:
            continue
        chosen.append(candidate.digest)
        remaining -= candidate.size_bytes
    return chosen
