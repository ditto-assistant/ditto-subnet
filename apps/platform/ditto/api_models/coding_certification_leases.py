"""Typed authority for one dedicated shadow coding-certification lease."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit
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
_SIGNATURE = r"^[0-9a-fA-F]{128}$"
_OCI_DIGEST = r"^sha256:[0-9a-f]{64}$"
_MAX_IMAGE_BYTES = 8 << 30
_MAX_URL_BYTES = 16 << 10


def _opaque(value: str, *, maximum: int = 256) -> str:
    if len(value.encode()) > maximum or any(
        char.isspace() or unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co"}
        for char in value
    ):
        raise ValueError("coding certification lease identifier is invalid")
    return value


OpaqueId = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_opaque)]
ImageRef = Annotated[
    str,
    Field(min_length=1, max_length=512),
    AfterValidator(lambda value: _opaque(value, maximum=512)),
]
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


class CodingCertificationLeaseStatus(StrEnum):
    ISSUED = "issued"
    CLAIMED = "claimed"
    ABORTED = "aborted"
    EXPIRED = "expired"


class CodingCertificationLeaseIssueRequest(CodingCertificationLeaseModel):
    validator_hotkey: Annotated[str, Field(pattern=_SS58)]
    agent_id: UUID
    bench_version: Annotated[int, Field(strict=True, ge=7, le=1_000_000)]
    coding_contract_version: Literal[1] = 1
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{128}$")]

    @field_validator("requested_at")
    @classmethod
    def request_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding certification lease timestamp must be aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def identifiers_are_nonzero(self) -> CodingCertificationLeaseIssueRequest:
        if self.agent_id.int == 0 or self.nonce.int == 0:
            raise ValueError("coding certification lease UUID is nil")
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


class CodingCertificationLeaseAbortRequest(CodingCertificationLeaseClaimRequest):
    pass


class CodingCertificationLeaseResponse(CodingCertificationLeaseModel):
    authority: CodingCertificationLeaseAuthority
    status: CodingCertificationLeaseStatus
    claimed_at: datetime | None = None
    aborted_at: datetime | None = None
    screened_image_id: OpaqueId
    screened_image_ref: ImageRef
    screened_image_upload_id: UUID
    weight_eligible: Literal[False] = False

    @model_validator(mode="after")
    def image_identity_is_nonzero(self) -> CodingCertificationLeaseResponse:
        if self.screened_image_upload_id.int == 0:
            raise ValueError("coding certification lease UUID is nil")
        return self


def _aware_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("coding certification lease timestamp must be aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def coding_certification_lease_issue_signing_message(
    *,
    validator_hotkey: str,
    agent_id: UUID,
    bench_version: int,
    coding_contract_version: int,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    return "\x00".join(
        (
            "dittobench-coding-certification-lease-issue:v1",
            validator_hotkey,
            str(agent_id),
            str(bench_version),
            str(coding_contract_version),
            str(nonce),
            _aware_iso(requested_at),
        )
    ).encode()


def coding_certification_lease_claim_signing_message(
    *,
    validator_hotkey: str,
    lease_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    return "\x00".join(
        (
            "dittobench-coding-certification-lease-claim:v1",
            validator_hotkey,
            str(lease_id),
            str(nonce),
            _aware_iso(requested_at),
        )
    ).encode()


def coding_certification_lease_abort_signing_message(
    *,
    validator_hotkey: str,
    lease_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    return "\x00".join(
        (
            "dittobench-coding-certification-lease-abort:v1",
            validator_hotkey,
            str(lease_id),
            str(nonce),
            _aware_iso(requested_at),
        )
    ).encode()


class CodingCertificationHarnessLaunchRequest(CodingCertificationLeaseModel):
    validator_hotkey: Annotated[str, Field(pattern=_SS58)]
    lease_id: UUID
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE)]

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding certification harness timestamp must be aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def identifiers_are_nonzero(
        self,
    ) -> CodingCertificationHarnessLaunchRequest:
        if self.lease_id.int == 0 or self.nonce.int == 0:
            raise ValueError("coding certification harness UUID is nil")
        return self


class CodingCertificationHarnessLaunchResponse(CodingCertificationLeaseModel):
    schema_name: Literal["dittobench-coding-certification-harness-launch-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    lease_id: UUID
    agent_id: UUID
    lease_deadline: datetime
    bench_version: Annotated[int, Field(strict=True, ge=7, le=1_000_000)]
    agent_artifact_sha256: Sha256
    screened_image_sha256: Sha256
    screened_image_size_bytes: Annotated[
        int, Field(strict=True, gt=0, le=_MAX_IMAGE_BYTES)
    ]
    screened_image_id: Annotated[str, Field(pattern=_OCI_DIGEST)]
    screened_image_ref: ImageRef
    screening_policy_version: Annotated[int, Field(strict=True, ge=9, le=1_000_000)]
    image_url: Annotated[
        str, Field(min_length=1, max_length=_MAX_URL_BYTES, repr=False)
    ]
    expires_at: datetime

    @field_validator("lease_deadline", "expires_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding certification harness timestamp must be aware")
        return value.astimezone(UTC)

    @field_validator("image_url")
    @classmethod
    def image_url_is_private_https_capability(cls, value: str) -> str:
        if len(value.encode()) > _MAX_URL_BYTES or any(
            ord(character) < 32 or ord(character) > 126 for character in value
        ):
            raise ValueError("coding certification harness image URL is outside bounds")
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError(
                "coding certification harness URL port is invalid"
            ) from error
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path
            or not parsed.query
            or parsed.fragment
        ):
            raise ValueError("coding certification harness image URL is invalid")
        return value

    @model_validator(mode="after")
    def authority_is_coherent(self) -> CodingCertificationHarnessLaunchResponse:
        if (
            any(value.int == 0 for value in (self.agent_id, self.lease_id))
            or self.expires_at > self.lease_deadline
            or self.screened_image_ref != f"ditto-screen/{self.agent_id}:latest"
        ):
            raise ValueError(
                "coding certification harness launch authority is incoherent"
            )
        return self


def coding_certification_harness_launch_signing_message(
    *,
    validator_hotkey: str,
    lease_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    return "\x00".join(
        (
            "dittobench-coding-certification-harness-launch:v1",
            validator_hotkey,
            str(lease_id),
            str(nonce),
            _aware_iso(requested_at),
        )
    ).encode()


__all__ = [
    "CodingCertificationHarnessLaunchRequest",
    "CodingCertificationHarnessLaunchResponse",
    "CodingCertificationLeaseAbortRequest",
    "CodingCertificationLeaseAuthority",
    "CodingCertificationLeaseClaimRequest",
    "CodingCertificationLeaseIssueRequest",
    "CodingCertificationLeaseResponse",
    "CodingCertificationLeaseStatus",
    "ImageRef",
    "coding_certification_harness_launch_signing_message",
    "coding_certification_lease_abort_signing_message",
    "coding_certification_lease_claim_signing_message",
    "coding_certification_lease_issue_signing_message",
]
