"""Read-only verification of a V13 private runner's signed evidence statement.

This authenticates bytes and exact trusted identities only. It neither accepts
evidence into the database nor turns a statistical result into a V13 verdict.
"""

from __future__ import annotations

from datetime import timedelta

from ditto.api_server.endpoints.upload import _verify_signature
from ditto_screening_protocol.v13_private_attestation import (
    SignedV13PrivateEvidence,
    v13_private_evidence_signing_message,
)
from ditto_screening_protocol.v13_private_package import (
    ArtifactCommitment,
    PrivatePackageRegistration,
)
from ditto_screening_protocol.v13_private_stats import (
    PrivateStatisticalUnavailable,
    analyze_v13_private_pairs,
)


def verify_v13_private_evidence(
    *,
    signed: SignedV13PrivateEvidence,
    target_commitment: ArtifactCommitment,
    clean_commitment: ArtifactCommitment,
    registration: PrivatePackageRegistration,
    trusted_runner_hotkey: str,
    expected_pair_inventory_sha256: str,
) -> bool:
    """Verify against separately fetched Platform/registry identities.

    The caller must source all expected values from trusted state, never from
    the submitted statement. A known-benign clean registration and independent
    runner attestation still need to be built before policy use.
    """
    statement = signed.statement
    target = statement.target.summary
    clean = statement.clean_control.summary
    if (
        statement.target_commitment != target_commitment
        or target_commitment.agent_id != registration.agent_id
        or target_commitment.attempt_id != registration.attempt_id
        or target_commitment.artifact_sha256 != registration.artifact_sha256
        or target_commitment.image_sha256 != registration.image_sha256
        or target.manifest_sha256 != registration.manifest_sha256
        or target.profile_sha256 != registration.profile_sha256
        or statement.runner_hotkey != trusted_runner_hotkey
        or statement.pair_inventory_sha256 != expected_pair_inventory_sha256
        or clean.agent_id != clean_commitment.agent_id
        or clean.attempt_id != clean_commitment.attempt_id
        or clean.artifact_sha256 != clean_commitment.artifact_sha256
        or clean.image_sha256 != clean_commitment.image_sha256
        or statement.issued_at <= clean_commitment.committed_at
        or statement.issued_at > registration.registered_at + timedelta(hours=3)
    ):
        return False
    try:
        analyze_v13_private_pairs(
            target=statement.target, clean_control=statement.clean_control
        )
    except PrivateStatisticalUnavailable:
        return False
    return _verify_signature(
        trusted_runner_hotkey,
        v13_private_evidence_signing_message(statement),
        signed.signature,
    )
