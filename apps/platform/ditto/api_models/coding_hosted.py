"""Public control projections for Platform-hosted private Coding evaluation."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Hotkey = Annotated[str, Field(pattern=r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")]
Timestamp = Annotated[int, Field(strict=True, ge=1, le=253402300799)]
Signature = Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]


class _HostedEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    @field_validator(
        "coding_contract_version",
        "shadow_only",
        "weight_eligible",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def flags_are_strict(cls, value: object, info: ValidationInfo) -> object:
        expected = int if info.field_name == "coding_contract_version" else bool
        if type(value) is not expected:
            raise ValueError("hosted Coding flags must use their exact JSON types")
        return value


class HostedCodingRequest(_HostedEnvelope):
    """A validator can act on an assigned handle without supplying task data."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    schema_name: Literal["dittobench-coding-hosted-request-v2"] = Field(alias="schema")
    coding_contract_version: Literal[2]
    shadow_only: Literal[True]
    weight_eligible: Literal[False]
    evaluation_id: UUID
    validator_hotkey: Hotkey
    artifact_sha256: Digest
    assignment_sha256: Digest
    policy_sha256: Digest
    operation: Literal["evaluate", "status", "acknowledge"]
    result_sha256: Digest | None
    nonce: UUID
    issued_at_unix: Timestamp
    expires_at_unix: Timestamp
    signature: Signature

    @model_validator(mode="after")
    def bounded_authority(self) -> HostedCodingRequest:
        if (
            self.evaluation_id.int == 0
            or self.nonce.int == 0
            or not 0 < self.expires_at_unix - self.issued_at_unix <= 120
            or (self.operation == "acknowledge") != (self.result_sha256 is not None)
        ):
            raise ValueError("hosted Coding request authority is invalid")
        return self


class HostedCodingResult(_HostedEnvelope):
    """Validator-visible terminal receipt; private outcomes stay inside Platform."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    schema_name: Literal["dittobench-coding-hosted-result-v2"] = Field(alias="schema")
    coding_contract_version: Literal[2]
    shadow_only: Literal[True]
    weight_eligible: Literal[False]
    evaluation_id: UUID
    attempt_id: UUID
    validator_hotkey: Hotkey
    platform_hotkey: Hotkey
    request_sha256: Digest
    artifact_sha256: Digest
    assignment_sha256: Digest
    policy_sha256: Digest
    execution_profile_sha256: Digest
    grading_profile_sha256: Digest
    evidence_sha256: Digest
    outcome: Literal[
        "completed", "candidate_failure", "infrastructure_failure", "integrity_failure"
    ]
    issued_at_unix: Timestamp
    expires_at_unix: Timestamp
    signature: Signature

    @model_validator(mode="after")
    def bounded_authority(self) -> HostedCodingResult:
        if (
            self.evaluation_id.int == 0
            or self.attempt_id.int == 0
            or not 0 < self.expires_at_unix - self.issued_at_unix <= 3600
        ):
            raise ValueError("hosted Coding result authority is invalid")
        return self


def hosted_signing_bytes(value: HostedCodingRequest | HostedCodingResult) -> bytes:
    # Revalidation prevents model_copy/model_construct from bypassing typed gates.
    checked = type(value).model_validate(value.model_dump(mode="json", by_alias=True))
    fields = checked.model_dump(mode="json", by_alias=True)
    fields.pop("signature")
    return (
        json.dumps(
            fields,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def hosted_message_digest(value: HostedCodingRequest | HostedCodingResult) -> str:
    return hashlib.sha256(hosted_signing_bytes(value)).hexdigest()
