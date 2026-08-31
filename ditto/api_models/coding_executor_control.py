"""Validator-signed authority for one remote coding-executor operation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SHA256 = r"^[0-9a-f]{64}$"
_SIGNATURE = r"^[0-9a-fA-F]{128}$"
MAX_ENVELOPE_LIFETIME = timedelta(minutes=2)


class CodingExecutorOperation(StrEnum):
    SUPERVISOR_PREPARE = "supervisor.prepare"
    SUPERVISOR_AUTHOR = "supervisor.author"
    SUPERVISOR_GRADE = "supervisor.grade"
    SUPERVISOR_ABORT_AUTHORING = "supervisor.abort-authoring"
    SUPERVISOR_ABORT_GRADING = "supervisor.abort-grading"
    SUPERVISOR_RECOVER = "supervisor.recover"
    PUBLICATIONS_PREPARE = "publications.prepare"
    PUBLICATIONS_ACKNOWLEDGE = "publications.acknowledge"
    PUBLICATIONS_PENDING = "publications.pending"
    PUBLICATIONS_OPEN = "publications.open"
    PUBLICATIONS_LOOKUP = "publications.lookup"


class CodingExecutorControlEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_name: Literal["dittobench-coding-executor-control-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    validator_hotkey: Annotated[str, Field(pattern=_SS58)]
    agent_id: UUID
    agent_artifact_sha256: Annotated[str, Field(pattern=_SHA256)]
    coding_run_id: Annotated[str, Field(min_length=1, max_length=256)]
    ticket_id: UUID
    operation: CodingExecutorOperation
    method: Literal["POST"]
    request_body_sha256: Annotated[str, Field(pattern=_SHA256)]
    nonce: UUID
    issued_at: datetime
    expires_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE)]

    @field_validator("issued_at", "expires_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("executor control timestamp must be aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def authority_is_bounded(self) -> CodingExecutorControlEnvelope:
        if (
            self.agent_id.int == 0
            or self.ticket_id.int == 0
            or self.nonce.int == 0
            or self.issued_at.microsecond != 0
            or self.expires_at.microsecond != 0
            or not self.issued_at < self.expires_at
            or self.expires_at - self.issued_at > MAX_ENVELOPE_LIFETIME
            or any(character.isspace() for character in self.coding_run_id)
        ):
            raise ValueError("executor control authority is invalid")
        return self


def coding_executor_control_signing_message(
    envelope: CodingExecutorControlEnvelope,
) -> bytes:
    values = (
        "dittobench-coding-executor-control:v1",
        envelope.validator_hotkey,
        str(envelope.agent_id),
        envelope.agent_artifact_sha256,
        envelope.coding_run_id,
        str(envelope.ticket_id),
        envelope.operation.value,
        envelope.method,
        envelope.request_body_sha256,
        str(envelope.nonce),
        envelope.issued_at.isoformat(timespec="microseconds"),
        envelope.expires_at.isoformat(timespec="microseconds"),
    )
    return "\x00".join(values).encode()


__all__ = [
    "CodingExecutorControlEnvelope",
    "CodingExecutorOperation",
    "coding_executor_control_signing_message",
]
