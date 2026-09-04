"""Private shadow coding screened-harness launch authority."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
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

_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_PATTERN = r"^[0-9a-fA-F]{128}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_OCI_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_MAX_IMAGE_BYTES = 8 << 30
_MAX_URL_BYTES = 16 << 10


def _bounded_identifier(value: str, maximum: int) -> str:
    if len(value.encode()) > maximum or any(
        character.isspace()
        or unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co"}
        for character in value
    ):
        raise ValueError("coding harness identity is invalid")
    return value


OpaqueId = Annotated[
    str,
    Field(min_length=1, max_length=256),
    AfterValidator(lambda value: _bounded_identifier(value, 256)),
]
ImageRef = Annotated[
    str,
    Field(min_length=1, max_length=512),
    AfterValidator(lambda value: _bounded_identifier(value, 512)),
]
Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN)]


class CodingHarnessModel(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        serialize_by_alias=True,
        validate_by_name=True,
    )


class CodingHarnessLaunchRequest(CodingHarnessModel):
    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    ticket_id: UUID
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE_PATTERN)]

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding harness request timestamp must be aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def identifiers_are_nonzero(self) -> CodingHarnessLaunchRequest:
        if self.ticket_id.int == 0 or self.nonce.int == 0:
            raise ValueError("coding harness request UUID is nil")
        return self


class CodingHarnessLaunchResponse(CodingHarnessModel):
    schema_name: Literal["dittobench-coding-harness-launch-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    agent_id: UUID
    run_row_id: UUID
    ticket_id: UUID
    ticket_deadline: datetime
    bench_version: Annotated[int, Field(strict=True, ge=7, le=1_000_000)]
    agent_artifact_sha256: Sha256
    screened_image_sha256: Sha256
    screened_image_size_bytes: Annotated[
        int, Field(strict=True, gt=0, le=_MAX_IMAGE_BYTES)
    ]
    screened_image_id: Annotated[str, Field(pattern=_OCI_DIGEST_PATTERN)]
    screened_image_ref: ImageRef
    screening_policy_version: Annotated[int, Field(strict=True, ge=9, le=1_000_000)]
    image_url: Annotated[
        str, Field(min_length=1, max_length=_MAX_URL_BYTES, repr=False)
    ]
    expires_at: datetime

    @field_validator("ticket_deadline", "expires_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding harness timestamp must be aware")
        return value.astimezone(UTC)

    @field_validator("image_url")
    @classmethod
    def image_url_is_private_https_capability(cls, value: str) -> str:
        if len(value.encode()) > _MAX_URL_BYTES or any(
            ord(character) < 32 or ord(character) > 126 for character in value
        ):
            raise ValueError("coding harness image URL is outside bounds")
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("coding harness URL port is invalid") from error
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
            raise ValueError("coding harness image URL is invalid")
        return value

    @model_validator(mode="after")
    def authority_is_coherent(self) -> CodingHarnessLaunchResponse:
        if (
            any(
                value.int == 0
                for value in (self.agent_id, self.run_row_id, self.ticket_id)
            )
            or self.expires_at > self.ticket_deadline
            or self.screened_image_ref != f"ditto-screen/{self.agent_id}:latest"
        ):
            raise ValueError("coding harness launch authority is incoherent")
        return self


def coding_harness_launch_signing_message(
    *,
    validator_hotkey: str,
    ticket_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ValueError("coding harness signing timestamp must be aware")
    timestamp = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return "\x00".join(
        (
            "dittobench-coding-harness-launch:v1",
            validator_hotkey,
            str(ticket_id),
            str(nonce),
            timestamp,
        )
    ).encode()


__all__ = [
    "CodingHarnessLaunchRequest",
    "CodingHarnessLaunchResponse",
    "coding_harness_launch_signing_message",
]
