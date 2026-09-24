"""Synthetic authenticated aggregate binding; no protected case material."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto_screening_protocol.v13_private_clean_control import (
    V13MatchedCleanControlCommitment,
)
from ditto_screening_protocol.v13_private_execute import (
    PrivateExecutionResult,
    PrivatePairCounts,
)
from ditto_screening_protocol.v13_private_package import V13PrivateRunSummary
from ditto_screening_protocol.v13_private_receipt import (
    V13ReplayPrivateReceipt,
    authentic_replay_private_receipt,
)
from ditto_screening_protocol.v13_replay_observation import V13ReplayBinding


def _result(*, clean: bool) -> PrivateExecutionResult:
    aggregates = tuple(
        PrivatePairCounts(
            transformation_class=class_name,
            seed_commitment=seed,
            pairs=10,
            control_correct=10,
            variant_correct=9,
            control_only_correct=1,
            variant_only_correct=0,
        )
        for seed in ("1" * 64, "2" * 64)
        for class_name in (
            "field_entity_rename",
            "request_paraphrase",
            "record_reorder_decoy",
        )
    )
    return PrivateExecutionResult(
        summary=V13PrivateRunSummary(
            agent_id=UUID(int=5 if clean else 2),
            attempt_id=UUID(int=6 if clean else 3),
            artifact_sha256=("c" if clean else "a") * 64,
            image_sha256=("d" if clean else "b") * 64,
            profile_sha256="e" * 64,
            manifest_sha256=("6" if clean else "5") * 64,
            runner_hotkey="independent-runner",
            status="completed",
            completed_pairs=60,
            evidence_sha256="7" * 64,
        ),
        aggregates=aggregates,
    )


def _receipt() -> V13ReplayPrivateReceipt:
    return V13ReplayPrivateReceipt(
        binding=V13ReplayBinding(
            replay_id=UUID(int=1),
            agent_id=UUID(int=2),
            attempt_id=UUID(int=3),
            artifact_sha256="a" * 64,
            image_sha256="b" * 64,
            image_id="sha256:" + "8" * 64,
        ),
        matched=V13MatchedCleanControlCommitment(
            group_id=UUID(int=4),
            replay_id=UUID(int=1),
            target_agent_id=UUID(int=2),
            target_attempt_id=UUID(int=3),
            target_artifact_sha256="a" * 64,
            target_image_sha256="b" * 64,
            target_manifest_sha256="5" * 64,
            control_agent_id=UUID(int=5),
            control_attempt_id=UUID(int=6),
            control_artifact_sha256="c" * 64,
            control_image_sha256="d" * 64,
            control_manifest_sha256="6" * 64,
            control_approval_id=UUID(int=7),
            control_approval_receipt_sha256="9" * 64,
            target_generation_receipt_sha256="0" * 64,
            control_generation_receipt_sha256="1" * 64,
            profile_sha256="e" * 64,
            pair_inventory_sha256="2" * 64,
            pair_count=60,
        ),
        target=_result(clean=False),
        known_benign=_result(clean=True),
        runner_hotkey="independent-runner",
        observed_at=datetime(2026, 9, 24, 12, 1, tzinfo=UTC),
        signature="0" * 128,
    )


def test_signed_private_receipt_exact_binding_and_lease() -> None:
    unsigned = _receipt()
    signed = unsigned.model_copy(
        update={
            "signature": hashlib.blake2b(
                unsigned.signing_message(), digest_size=64
            ).hexdigest()
        }
    )
    verify = lambda _hotkey, message, signature: (  # noqa: E731
        hashlib.blake2b(message, digest_size=64).hexdigest() == signature
    )
    assert authentic_replay_private_receipt(
        signed,
        expected=signed.binding,
        enrolled_runner_hotkey="independent-runner",
        source_worker_hotkey="original-worker",
        lease_started_at=signed.observed_at - timedelta(minutes=1),
        lease_deadline=signed.observed_at + timedelta(minutes=1),
        verify_signature=verify,
    )
    assert not authentic_replay_private_receipt(
        signed,
        expected=signed.binding,
        enrolled_runner_hotkey="independent-runner",
        source_worker_hotkey="independent-runner",
        lease_started_at=signed.observed_at - timedelta(minutes=1),
        lease_deadline=signed.observed_at + timedelta(minutes=1),
        verify_signature=verify,
    )
    assert not authentic_replay_private_receipt(
        signed,
        expected=signed.binding,
        enrolled_runner_hotkey="independent-runner",
        source_worker_hotkey="original-worker",
        lease_started_at=signed.observed_at,
        lease_deadline=signed.observed_at,
        verify_signature=verify,
    )


def test_private_receipt_rejects_spliced_control_identity() -> None:
    receipt = _receipt()
    payload = receipt.model_dump(mode="json")
    payload["known_benign"]["summary"]["image_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="private receipt identity mismatch"):
        V13ReplayPrivateReceipt.model_validate(payload)
