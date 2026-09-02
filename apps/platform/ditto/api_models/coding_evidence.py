"""Immutable identities for Hippius-mediated shadow Coding evidence."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_evaluation import CodingEvaluationModel, Sha256

_MAX_CANONICAL_BYTES = 32 << 10
_INSTANCE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,128}$")


class CodingSealedEvidenceKind(StrEnum):
    AUTHORING_TRANSCRIPT = "authoring-transcript"
    FROZEN_SUBMISSION = "frozen-submission"
    AUTHORING_PUBLICATION_REQUEST = "authoring-publication-request"
    AUTHORING_PUBLICATION_ACKNOWLEDGEMENT = "authoring-publication-acknowledgement"
    TERMINAL_PUBLICATION_REQUEST = "terminal-publication-request"
    TERMINAL_PUBLICATION_ACKNOWLEDGEMENT = "terminal-publication-acknowledgement"


CODING_SEALED_EVIDENCE_MAX_BYTES = {
    CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT: 512 << 20,
    CodingSealedEvidenceKind.FROZEN_SUBMISSION: 128 << 20,
    CodingSealedEvidenceKind.AUTHORING_PUBLICATION_REQUEST: 4 << 20,
    CodingSealedEvidenceKind.AUTHORING_PUBLICATION_ACKNOWLEDGEMENT: 1 << 20,
    CodingSealedEvidenceKind.TERMINAL_PUBLICATION_REQUEST: 4 << 20,
    CodingSealedEvidenceKind.TERMINAL_PUBLICATION_ACKNOWLEDGEMENT: 1 << 20,
}


class CodingSealedEvidenceIdentity(CodingEvaluationModel):
    """Exact bytes and ticket authority reserved before a Hippius request."""

    schema_name: Literal["dittobench-coding-sealed-evidence-identity-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    reservation_id: UUID
    ticket_id: UUID
    claim_generation: Annotated[int, Field(strict=True, ge=1, le=(1 << 31) - 1)]
    validator_hotkey: Annotated[str, Field(pattern=r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")]
    instance_id: Annotated[str, Field(min_length=1, max_length=128)]
    ticket_deadline: datetime
    evidence_kind: CodingSealedEvidenceKind
    plaintext_sha256: Sha256
    plaintext_size_bytes: Annotated[int, Field(strict=True, ge=1)]
    ciphertext_sha256: Sha256
    ciphertext_size_bytes: Annotated[int, Field(strict=True, ge=17)]
    object_key_sha256: Sha256
    envelope_sha256: Sha256
    wrapping_key_sha256: Sha256
    aad_sha256: Sha256
    identity_sha256: Sha256

    @field_validator("instance_id")
    @classmethod
    def instance_is_bounded(cls, value: str) -> str:
        if _INSTANCE.fullmatch(value) is None or len(value.encode()) > 128:
            raise ValueError("coding evidence instance identity is invalid")
        return value

    @field_validator("ticket_deadline")
    @classmethod
    def deadline_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding evidence ticket deadline must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def identity_is_coherent(self) -> CodingSealedEvidenceIdentity:
        maximum = CODING_SEALED_EVIDENCE_MAX_BYTES[self.evidence_kind]
        if (
            self.reservation_id.int == 0
            or self.ticket_id.int == 0
            or self.plaintext_size_bytes > maximum
            or self.ciphertext_size_bytes != self.plaintext_size_bytes + 16
            or coding_sealed_evidence_identity_digest(self) != self.identity_sha256
        ):
            raise ValueError("coding sealed-evidence identity is inconsistent")
        return self


def coding_sealed_evidence_identity_digest(
    identity: CodingSealedEvidenceIdentity,
) -> str:
    projection = identity.model_dump(mode="json", by_alias=True)
    projection.pop("identity_sha256")
    return coding_canonical_sha256(
        projection,
        maximum_bytes=_MAX_CANONICAL_BYTES,
        label="coding sealed-evidence identity",
    )


def validate_coding_evidence_safe_scalar(value: str, *, maximum_bytes: int) -> bool:
    return (
        bool(value)
        and len(value.encode()) <= maximum_bytes
        and not any(
            character.isspace() or unicodedata.category(character) == "Cc"
            for character in value
        )
    )


__all__ = [
    "CODING_SEALED_EVIDENCE_MAX_BYTES",
    "CodingSealedEvidenceIdentity",
    "CodingSealedEvidenceKind",
    "coding_sealed_evidence_identity_digest",
    "validate_coding_evidence_safe_scalar",
]
