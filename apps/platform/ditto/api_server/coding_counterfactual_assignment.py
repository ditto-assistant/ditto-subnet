"""Default-off blinded assignment builder for Coding Memory v2 shadow runs."""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from dataclasses import dataclass

from ditto.api_models.coding_canonical import coding_canonical_json_bytes


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
        or type(authority.seeded_memory_bytes) is not int
        or authority.seeded_memory_bytes <= 0
        or type(authority.model_visible_memory_token_budget) is not int
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
        or not _valid_identifier(authority.repository_epoch, maximum_bytes=256)
    ):
        raise CounterfactualAssignmentError(
            "counterfactual assignment authority invalid"
        )
    preimage = coding_canonical_json_bytes(
        {
            "artifact_sha256": authority.artifact_sha256,
            "group_commitment": authority.opaque_group_commitment,
            "private_condition_commitment": authority.private_condition_commitment,
            "replicate_id": replicate_id,
            "repository_epoch": authority.repository_epoch,
            "schema": "dittobench-coding-counterfactual-assignment-preimage-v2",
        },
        maximum_bytes=4 << 10,
        label="counterfactual assignment preimage",
    )
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


def _valid_identifier(value: str, *, maximum_bytes: int) -> bool:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        bool(value)
        and len(encoded) <= maximum_bytes
        and not any(
            character.isspace()
            or unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co"}
            for character in value
        )
    )
