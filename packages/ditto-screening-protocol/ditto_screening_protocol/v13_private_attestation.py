"""Canonical signed envelope for report-only V13 private paired evidence.

The signature authenticates a runner statement. It does not prove that the
runner is trusted, that the clean image is known benign, or that cases ran in
fresh isolation. Those claims require independent Platform-side registration.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto_screening_protocol.v13_private_execute import PrivateExecutionResult
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE_SHA256,
    ArtifactCommitment,
)

_SHA_PATTERN = r"^[0-9a-f]{64}$"


class V13PrivateEvidenceStatement(BaseModel):
    """One target and known-benign run asserted over the same hidden inventory."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-private-evidence-v1"] = "v13-private-evidence-v1"
    target_commitment: ArtifactCommitment
    pair_inventory_sha256: str = Field(pattern=_SHA_PATTERN)
    target: PrivateExecutionResult
    clean_control: PrivateExecutionResult
    runner_hotkey: str = Field(min_length=1, max_length=120)
    issued_at: datetime

    @model_validator(mode="after")
    def exact_identity(self) -> V13PrivateEvidenceStatement:
        target = self.target.summary
        clean = self.clean_control.summary
        commitment = self.target_commitment
        if (
            self.issued_at.tzinfo is None
            or self.issued_at <= commitment.committed_at
            or target.status != "completed"
            or clean.status != "completed"
            or target.agent_id != commitment.agent_id
            or target.attempt_id != commitment.attempt_id
            or target.artifact_sha256 != commitment.artifact_sha256
            or target.image_sha256 != commitment.image_sha256
            or target.profile_sha256 != V13_PRIVATE_PROFILE_SHA256
            or clean.profile_sha256 != V13_PRIVATE_PROFILE_SHA256
            or clean.manifest_sha256 != target.manifest_sha256
            or clean.image_sha256 == target.image_sha256
            or clean.agent_id == target.agent_id
            or clean.runner_hotkey != self.runner_hotkey
            or target.runner_hotkey != self.runner_hotkey
        ):
            raise ValueError("private evidence identity mismatch")
        return self


class SignedV13PrivateEvidence(BaseModel):
    """An sr25519 signature over exact canonical statement bytes."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    statement: V13PrivateEvidenceStatement
    signature: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{128}$")]


def v13_private_evidence_signing_message(
    statement: V13PrivateEvidenceStatement,
) -> bytes:
    """Domain-separated canonical JSON; the entire paired outcome is signed."""
    return (
        b"ditto-v13-private-evidence:v1:"
        + json.dumps(
            statement.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    )
