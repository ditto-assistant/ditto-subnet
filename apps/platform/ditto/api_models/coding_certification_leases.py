"""Typed authority for one dedicated shadow coding-certification lease."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

_SHA256 = r"^[0-9a-f]{64}$"
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"


def _opaque(value: str) -> str:
    if len(value.encode()) > 256 or any(
        char.isspace() or unicodedata.category(char) == "Cc" for char in value
    ):
        raise ValueError("coding certification lease identifier is invalid")
    return value


OpaqueId = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_opaque)]
Sha256 = Annotated[str, Field(pattern=_SHA256)]


class CodingCertificationLeaseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, serialize_by_alias=True)


class CodingCertificationLeaseAuthority(CodingCertificationLeaseModel):
    """Immutable authority minted only from a current core qualification."""

    schema_name: Literal["dittobench-coding-certification-lease-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    lease_id: UUID
    validator_hotkey: Annotated[str, Field(pattern=_SS58)]
    agent_id: UUID
    agent_artifact_sha256: Sha256
    screened_image_sha256: Sha256
    bench_version: Annotated[int, Field(strict=True, ge=7, le=1_000_000)]
    core_qualification_observation_id: UUID
    core_qualification_policy_checksum: Sha256
    canary_manifest_sha256: Sha256
    runner_plan_sha256: Sha256
    grader_plan_sha256: Sha256
    resource_profile_sha256: Sha256
    inference_policy_sha256: Sha256
    issued_at: datetime
    deadline: datetime

    @field_validator("issued_at", "deadline")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding certification lease timestamp must be aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def coherent(self) -> CodingCertificationLeaseAuthority:
        if any(
            value.int == 0
            for value in (
                self.lease_id,
                self.agent_id,
                self.core_qualification_observation_id,
            )
        ):
            raise ValueError("coding certification lease UUID is nil")
        if not self.issued_at < self.deadline <= self.issued_at + timedelta(minutes=30):
            raise ValueError("coding certification lease deadline is invalid")
        return self


class CodingCertificationLeaseClaimRequest(CodingCertificationLeaseModel):
    validator_hotkey: Annotated[str, Field(pattern=_SS58)]
    lease_id: UUID
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{128}$")]

    @field_validator("requested_at")
    @classmethod
    def request_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding certification claim timestamp must be aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def identifiers_are_nonzero(self) -> CodingCertificationLeaseClaimRequest:
        if self.lease_id.int == 0 or self.nonce.int == 0:
            raise ValueError("coding certification claim UUID is nil")
        return self


__all__ = ["CodingCertificationLeaseAuthority", "CodingCertificationLeaseClaimRequest"]
