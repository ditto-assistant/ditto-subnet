"""Default-off blinded assignment builder for Coding Memory v2 shadow runs."""

from __future__ import annotations

import hashlib
import hmac
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
    preimage = "|".join(
        (
            authority.artifact_sha256,
            authority.opaque_group_commitment,
            authority.private_condition_commitment,
            authority.repository_epoch,
            str(replicate_id),
        )
    ).encode("ascii")
    assignment_id = hmac.new(assignment_key, preimage, hashlib.sha256).hexdigest()
    return {
        "coding_contract_version": 2,
        "memory_bundle_sha256": authority.memory_bundle_sha256,
        "memory_volume_tier": authority.memory_volume_tier,
        "opaque_assignment_id": assignment_id,
        "private_condition_commitment": authority.private_condition_commitment,
        "replicate_id": replicate_id,
        "repository_epoch": authority.repository_epoch,
        "seeded_memory_bytes": authority.seeded_memory_bytes,
        "weight_eligible": False,
    }
