"""Contract tests for shadow core qualification policy."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ditto.api_models.core_qualification import (
    CoreQualificationObservation,
    CoreQualificationPolicy,
    core_qualification_policy_checksum,
)


def _policy(**overrides: object) -> CoreQualificationPolicy:
    values: dict[str, object] = {
        "schema": "ditto-core-qualification-policy-v1",
        "weight_eligible": False,
        "bench_version": 12,
        "enter_composite": 0.8,
        "enter_tool_mean": 0.8,
        "enter_memory_mean": 0.8,
        "exit_composite": 0.7,
        "exit_tool_mean": 0.7,
        "exit_memory_mean": 0.7,
        "enter_observations": 2,
        "exit_observations": 2,
    }
    values.update(overrides)
    return CoreQualificationPolicy.model_validate(values)


def test_policy_checksum_is_stable_and_unknown_fields_are_ignored() -> None:
    original = _policy()
    extended = CoreQualificationPolicy.model_validate(
        {**original.model_dump(mode="json", by_alias=True), "future_hint": "ignored"}
    )
    assert core_qualification_policy_checksum(original) == (
        core_qualification_policy_checksum(extended)
    )


def test_policy_is_permanently_weight_ineligible_and_has_real_hysteresis() -> None:
    with pytest.raises(ValidationError):
        _policy(weight_eligible=True)
    with pytest.raises(ValidationError, match="exit_tool_mean"):
        _policy(exit_tool_mean=0.9)


def _observation(**overrides: object) -> CoreQualificationObservation:
    values: dict[str, object] = {
        "sequence": 1,
        "observation_id": uuid4(),
        "agent_id": uuid4(),
        "artifact_sha256": "ab" * 32,
        "screened_image_sha256": "cd" * 32,
        "bench_version": 12,
        "policy_revision": 1,
        "policy_checksum": "ef" * 32,
        "score_evidence_sha256": "12" * 32,
        "score_count": 3,
        "full_size": True,
        "complete_wave": True,
        "validator_hotkeys": ["validator-a", "validator-b", "validator-c"],
        "run_ids": ["run-a", "run-b", "run-c"],
        "median_composite": 0.9,
        "median_tool_mean": 0.9,
        "median_memory_mean": 0.9,
        "entry_passed": True,
        "retention_passed": True,
        "qualified": True,
        "enter_streak": 2,
        "exit_streak": 0,
        "decision": "entered",
        "source": "score_commit",
        "actor": None,
        "reason": None,
        "observed_at": datetime(2026, 8, 21, tzinfo=UTC),
        "weight_eligible": False,
        "current": True,
        "stale_reason": "current",
    }
    values.update(overrides)
    return CoreQualificationObservation.model_validate(values)


def test_observation_source_and_operator_audit_fields_are_coherent() -> None:
    assert _observation().source == "score_commit"
    assert (
        _observation(
            source="admin_refresh",
            actor="operator@example.com",
            reason="recover missed shadow evidence",
        ).source
        == "admin_refresh"
    )
    with pytest.raises(ValidationError, match="automatic observation"):
        _observation(actor="operator@example.com")
    with pytest.raises(ValidationError, match="bounded actor and reason"):
        _observation(source="admin_refresh")
