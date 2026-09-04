"""Fresh-workspace execution coordinator for shadow-only Coding Memory v2."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

_HEX = frozenset("0123456789abcdef")


class CodingCounterfactualExecutionError(ValueError):
    """Counterfactual authority is incomplete or violates isolation."""


@dataclass(frozen=True)
class CounterfactualExecutionArm:
    opaque_assignment_id: str
    private_condition_commitment: str
    replicate_id: int


@dataclass(frozen=True)
class CounterfactualExecutionResult:
    opaque_assignment_id: str
    replicate_id: int
    resolved: bool
    fresh_workspace_id: str


def execute_replicated_conditions(
    arms: Sequence[CounterfactualExecutionArm],
    *,
    fresh_workspace: Callable[[], str],
    execute: Callable[[CounterfactualExecutionArm, str], bool],
) -> tuple[CounterfactualExecutionResult, ...]:
    """Execute each blinded arm exactly once in a unique fresh workspace.

    The caller is responsible for independent validator quorums. This helper
    intentionally has no score, weight, network, or retained-state behavior.
    """

    if not 1 <= len(arms) <= 100:
        raise CodingCounterfactualExecutionError("counterfactual arm count invalid")
    identities = {(arm.opaque_assignment_id, arm.replicate_id) for arm in arms}
    if len(identities) != len(arms) or any(
        not _valid_sha256(arm.opaque_assignment_id)
        or not _valid_sha256(arm.private_condition_commitment)
        or arm.replicate_id <= 0
        for arm in arms
    ):
        raise CodingCounterfactualExecutionError("counterfactual arm authority invalid")
    results: list[CounterfactualExecutionResult] = []
    workspace_ids: set[str] = set()
    for arm in arms:
        workspace_id = fresh_workspace()
        if not workspace_id or workspace_id in workspace_ids:
            raise CodingCounterfactualExecutionError(
                "counterfactual arms require unique fresh workspaces"
            )
        workspace_ids.add(workspace_id)
        resolved = execute(arm, workspace_id)
        if type(resolved) is not bool:
            raise CodingCounterfactualExecutionError(
                "counterfactual execution returned a non-boolean result"
            )
        results.append(
            CounterfactualExecutionResult(
                opaque_assignment_id=arm.opaque_assignment_id,
                replicate_id=arm.replicate_id,
                resolved=resolved,
                fresh_workspace_id=workspace_id,
            )
        )
    return tuple(results)


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in _HEX for character in value)
