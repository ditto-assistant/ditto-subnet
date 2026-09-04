"""Default-off blinded assignment builder for Coding Memory v2 shadow runs."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass


class CounterfactualAssignmentError(ValueError):
    """Raised when private v2 assignment authority is inconsistent."""


@dataclass(frozen=True)
class CounterfactualArmAuthority:
    artifact_sha256: str
    opaque_group_commitment: str
    private_condition_commitment: str
    repository_epoch: str
    memory_bundle_sha256: str
    seeded_memory_bytes: int
    model_visible_memory_token_budget: int
    memory_volume_tier: str


def issue_blinded_assignment(
    *, authority: CounterfactualArmAuthority, replicate_id: int, assignment_key: bytes
) -> dict[str, object]:
    """Project one arm without revealing its condition or matched group identity."""

    if (
        len(assignment_key) < 32
        or replicate_id <= 0
        or authority.memory_volume_tier not in {"small", "medium", "large"}
        or authority.seeded_memory_bytes <= 0
        or authority.model_visible_memory_token_budget <= 0
        or any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in (
                authority.artifact_sha256,
                authority.opaque_group_commitment,
                authority.private_condition_commitment,
                authority.memory_bundle_sha256,
            )
        )
        or not authority.repository_epoch
    ):
        raise CounterfactualAssignmentError(
            "counterfactual assignment authority invalid"
        )
    preimage = json.dumps(
        {
            "artifact_sha256": authority.artifact_sha256,
            "group_commitment": authority.opaque_group_commitment,
            "private_condition_commitment": authority.private_condition_commitment,
            "replicate_id": replicate_id,
            "repository_epoch": authority.repository_epoch,
            "schema": "dittobench-coding-counterfactual-assignment-preimage-v2",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assignment_id = hmac.new(assignment_key, preimage, hashlib.sha256).hexdigest()
    return {
        "agent_artifact_sha256": authority.artifact_sha256,
        "coding_contract_version": 2,
        "memory_bundle_sha256": authority.memory_bundle_sha256,
        "memory_volume_tier": authority.memory_volume_tier,
        "model_visible_memory_token_budget": (
            authority.model_visible_memory_token_budget
        ),
        "opaque_assignment_id": assignment_id,
        "repository_epoch": authority.repository_epoch,
        "schema": "dittobench-coding-counterfactual-assignment-v2",
        "seeded_memory_bytes": authority.seeded_memory_bytes,
        "weight_eligible": False,
    }
