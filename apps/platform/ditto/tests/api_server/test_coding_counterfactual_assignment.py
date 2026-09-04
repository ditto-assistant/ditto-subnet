from __future__ import annotations

from dataclasses import replace

import pytest

from ditto.api_server.coding_counterfactual_assignment import (
    CounterfactualArmAuthority,
    CounterfactualAssignmentError,
    issue_blinded_assignment,
)


def _authority() -> CounterfactualArmAuthority:
    return CounterfactualArmAuthority(
        artifact_sha256="a" * 64,
        opaque_group_commitment="b" * 64,
        private_condition_commitment="c" * 64,
        repository_epoch="repo@abc",
        memory_bundle_sha256="d" * 64,
        seeded_memory_bytes=4096,
        model_visible_memory_token_budget=2048,
        memory_volume_tier="large",
    )


def test_assignment_is_deterministic_blinded_and_shadow_only() -> None:
    result = issue_blinded_assignment(
        authority=_authority(), replicate_id=1, assignment_key=b"k" * 32
    )
    assert result == issue_blinded_assignment(
        authority=_authority(), replicate_id=1, assignment_key=b"k" * 32
    )
    assert result["weight_eligible"] is False
    assert set(result) == {
        "agent_artifact_sha256",
        "coding_contract_version",
        "memory_bundle_sha256",
        "memory_volume_tier",
        "model_visible_memory_token_budget",
        "opaque_assignment_id",
        "repository_epoch",
        "schema",
        "seeded_memory_bytes",
        "weight_eligible",
    }
    assert {
        "condition",
        "private_condition_commitment",
        "group",
        "opaque_group_commitment",
        "replicate_id",
        "quorum_group_id",
    }.isdisjoint(result)


def test_assignment_rejects_unsafe_authority() -> None:
    with pytest.raises(CounterfactualAssignmentError, match="invalid"):
        issue_blinded_assignment(
            authority=_authority(), replicate_id=0, assignment_key=b"short"
        )
    invalid = replace(_authority(), repository_epoch="repo epoch")
    with pytest.raises(CounterfactualAssignmentError, match="invalid"):
        issue_blinded_assignment(
            authority=invalid, replicate_id=1, assignment_key=b"k" * 32
        )
