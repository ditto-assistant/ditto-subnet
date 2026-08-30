"""Private validator claim protocol for shadow coding tickets."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
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

_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_PATTERN = r"^[0-9a-fA-F]{128}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _bounded_identifier(value: str, maximum: int) -> str:
    if len(value.encode()) > maximum or any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in value
    ):
        raise ValueError("coding claim identity is invalid")
    return value


InstanceId = Annotated[
    str,
    Field(min_length=1, max_length=128),
    AfterValidator(lambda value: _bounded_identifier(value, 128)),
]
OpaqueId = Annotated[
    str,
    Field(min_length=1, max_length=256),
    AfterValidator(lambda value: _bounded_identifier(value, 256)),
]
Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN)]


class CodingClaimModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class CodingClaimNextRequest(CodingClaimModel):
    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    instance_id: InstanceId
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE_PATTERN)]

    @field_validator("requested_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding claim timestamp must be aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def nonce_is_nonzero(self) -> CodingClaimNextRequest:
        if self.nonce.int == 0:
            raise ValueError("coding claim nonce is nil")
        return self


class CodingClaimActionRequest(CodingClaimNextRequest):
    ticket_id: UUID
    claim_generation: Annotated[int, Field(strict=True, ge=1, le=(1 << 31) - 1)]

    @model_validator(mode="after")
    def ticket_is_nonzero(self) -> CodingClaimActionRequest:
        if self.ticket_id.int == 0:
            raise ValueError("coding claim ticket is nil")
        return self


class CodingClaimResponse(CodingClaimModel):
    schema_name: Literal["dittobench-coding-ticket-claim-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    instance_id: InstanceId
    claim_generation: Annotated[int, Field(strict=True, ge=1, le=(1 << 31) - 1)]
    claim_expires_at: datetime
    claim_started_at: datetime | None
    idempotent: bool
    agent_id: UUID
    run_row_id: UUID
    ticket_id: UUID
    ticket_deadline: datetime
    bench_version: Annotated[int, Field(strict=True, ge=7, le=1_000_000)]
    coding_run_id: OpaqueId
    agent_artifact_sha256: Sha256
    screened_image_sha256: Sha256
    run_manifest_sha256: Sha256
    task_set_manifest_sha256: Sha256

    @field_validator("claim_expires_at", "claim_started_at", "ticket_deadline")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding claim timestamp must be aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def authority_is_coherent(self) -> CodingClaimResponse:
        if (
            any(
                value.int == 0
                for value in (self.agent_id, self.run_row_id, self.ticket_id)
            )
            or self.claim_expires_at > self.ticket_deadline
            or (
                self.claim_started_at is not None
                and (
                    self.claim_started_at >= self.ticket_deadline
                    or self.claim_started_at >= self.claim_expires_at
                )
            )
        ):
            raise ValueError("coding claim authority is incoherent")
        return self


def coding_claim_next_signing_message(
    *,
    validator_hotkey: str,
    instance_id: str,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    return _signing_message(
        domain="dittobench-coding-ticket-claim-next:v1",
        validator_hotkey=validator_hotkey,
        instance_id=instance_id,
        nonce=nonce,
        requested_at=requested_at,
    )


def coding_claim_action_signing_message(
    *,
    action: Literal["start", "heartbeat"],
    validator_hotkey: str,
    instance_id: str,
    ticket_id: UUID,
    claim_generation: int,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    if action not in {"start", "heartbeat"}:
        raise ValueError("coding claim action is invalid")
    return _signing_message(
        domain=f"dittobench-coding-ticket-claim-{action}:v1",
        validator_hotkey=validator_hotkey,
        instance_id=instance_id,
        ticket_id=ticket_id,
        claim_generation=claim_generation,
        nonce=nonce,
        requested_at=requested_at,
    )


def _signing_message(
    *,
    domain: str,
    validator_hotkey: str,
    instance_id: str,
    nonce: UUID,
    requested_at: datetime,
    ticket_id: UUID | None = None,
    claim_generation: int | None = None,
) -> bytes:
    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ValueError("coding claim signing timestamp must be aware")
    if (ticket_id is None) != (claim_generation is None) or (
        ticket_id is not None
        and (
            ticket_id.int == 0
            or claim_generation is None
            or not 1 <= claim_generation <= (1 << 31) - 1
        )
    ):
        raise ValueError("coding claim signing authority is invalid")
    values = [
        domain,
        validator_hotkey,
        instance_id,
    ]
    if ticket_id is not None:
        values.extend((str(ticket_id), str(claim_generation)))
    values.extend(
        (
            str(nonce),
            requested_at.astimezone(UTC).isoformat(timespec="microseconds"),
        )
    )
    return "\x00".join(values).encode()


__all__ = [
    "CodingClaimActionRequest",
    "CodingClaimNextRequest",
    "CodingClaimResponse",
    "coding_claim_action_signing_message",
    "coding_claim_next_signing_message",
]
