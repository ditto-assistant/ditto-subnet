"""Exact-identity and sr25519 tamper tests for report-only V13 evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import bittensor

from ditto.api_server.v13_private_evidence import verify_v13_private_evidence
from ditto_screening_protocol.v13_private_attestation import (
    SignedV13PrivateEvidence,
    V13PrivateEvidenceStatement,
    v13_private_evidence_signing_message,
)
from ditto_screening_protocol.v13_private_execute import (
    PrivateExecutionResult,
    PrivatePairCounts,
)
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE_SHA256,
    ArtifactCommitment,
    PrivatePackageRegistration,
    V13PrivateRunSummary,
)


def test_signed_private_evidence_binds_target_clean_runner_and_inventory() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    target = ArtifactCommitment(
        agent_id=uuid5(NAMESPACE_URL, "target"),
        attempt_id=uuid5(NAMESPACE_URL, "target-attempt"),
        artifact_sha256="a" * 64,
        image_sha256="b" * 64,
        committed_at=now,
    )
    clean = ArtifactCommitment(
        agent_id=uuid5(NAMESPACE_URL, "clean"),
        attempt_id=uuid5(NAMESPACE_URL, "clean-attempt"),
        artifact_sha256="c" * 64,
        image_sha256="d" * 64,
        committed_at=now,
    )
    registration = PrivatePackageRegistration(
        agent_id=target.agent_id,
        attempt_id=target.attempt_id,
        artifact_sha256=target.artifact_sha256,
        image_sha256=target.image_sha256,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
        manifest_sha256="e" * 64,
        registered_at=now + timedelta(minutes=1),
        registrar_id="trusted-synthetic-registrar",
    )

    def result(commitment: ArtifactCommitment) -> PrivateExecutionResult:
        rows = tuple(
            PrivatePairCounts(
                transformation_class=class_name,
                seed_commitment=seed,
                pairs=10,
                control_correct=10,
                variant_correct=10,
                control_only_correct=0,
                variant_only_correct=0,
            )
            for class_name in (
                "field_entity_rename",
                "request_paraphrase",
                "record_reorder_decoy",
            )
            for seed in ("1" * 64, "2" * 64)
        )
        return PrivateExecutionResult(
            summary=V13PrivateRunSummary(
                agent_id=commitment.agent_id,
                attempt_id=commitment.attempt_id,
                artifact_sha256=commitment.artifact_sha256,
                image_sha256=commitment.image_sha256,
                profile_sha256=V13_PRIVATE_PROFILE_SHA256,
                manifest_sha256=registration.manifest_sha256,
                runner_hotkey=keypair.ss58_address,
                status="completed",
                completed_pairs=60,
                evidence_sha256="f" * 64,
            ),
            aggregates=rows,
        )

    statement = V13PrivateEvidenceStatement(
        target_commitment=target,
        pair_inventory_sha256="1" * 64,
        target=result(target),
        clean_control=result(clean),
        runner_hotkey=keypair.ss58_address,
        issued_at=now + timedelta(minutes=2),
    )
    signature = keypair.sign(v13_private_evidence_signing_message(statement)).hex()
    signed = SignedV13PrivateEvidence(statement=statement, signature=signature)

    def verify(**overrides: object) -> bool:
        values = {
            "signed": signed,
            "target_commitment": target,
            "clean_commitment": clean,
            "registration": registration,
            "trusted_runner_hotkey": keypair.ss58_address,
            "expected_pair_inventory_sha256": "1" * 64,
        }
        values.update(overrides)
        return verify_v13_private_evidence(**values)  # type: ignore[arg-type]

    assert verify()
    assert not verify(expected_pair_inventory_sha256="2" * 64)
    bob_hotkey = bittensor.Keypair.create_from_uri("//Bob").ss58_address
    assert not verify(trusted_runner_hotkey=bob_hotkey)
    assert not verify(
        clean_commitment=clean.model_copy(update={"image_sha256": "3" * 64})
    )
    assert not verify(
        target_commitment=target.model_copy(update={"attempt_id": clean.attempt_id})
    )
    tampered = statement.model_copy(update={"pair_inventory_sha256": "2" * 64})
    assert not verify(
        signed=SignedV13PrivateEvidence(statement=tampered, signature=signature)
    )
    tampered_summary = statement.target.summary.model_copy(
        update={"evidence_sha256": "0" * 64}
    )
    tampered_target = statement.target.model_copy(update={"summary": tampered_summary})
    assert not verify(
        signed=SignedV13PrivateEvidence(
            statement=statement.model_copy(update={"target": tampered_target}),
            signature=signature,
        )
    )
    assert not verify(
        registration=registration.model_copy(update={"manifest_sha256": "0" * 64})
    )
    incomplete = statement.target.model_copy(update={"aggregates": ()})
    invalid_statement = statement.model_copy(update={"target": incomplete})
    invalid_signature = keypair.sign(
        v13_private_evidence_signing_message(invalid_statement)
    ).hex()
    assert not verify(
        signed=SignedV13PrivateEvidence(
            statement=invalid_statement, signature=invalid_signature
        )
    )
