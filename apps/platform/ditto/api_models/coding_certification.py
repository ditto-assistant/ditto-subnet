"""Shadow DittoBench Coding capability-certification wire models."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CONTENT_KEY_PATTERN = r"^sha256/[0-9a-f]{64}$"
_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_PATTERN = r"^[0-9a-fA-F]{128}$"


def _bounded_identifier(value: str, maximum: int) -> str:
    if len(value.encode()) > maximum or any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in value
    ):
        raise ValueError("identifier contains whitespace, control, or too many bytes")
    return value


OpaqueId = Annotated[
    str,
    Field(min_length=1, max_length=256),
    AfterValidator(lambda value: _bounded_identifier(value, 256)),
]
ShortName = Annotated[
    str,
    Field(min_length=1, max_length=128),
    AfterValidator(lambda value: _bounded_identifier(value, 128)),
]
Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN)]
ContentAddressedKey = Annotated[str, Field(pattern=_CONTENT_KEY_PATTERN)]


class CodingCertificationModel(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        serialize_by_alias=True,
        validate_by_name=True,
    )


class CodingCertificationStatus(StrEnum):
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    CERTIFIED = "certified"


class CodingCertificationStage(StrEnum):
    HEALTH = "health"
    SEED = "seed"
    RUN = "run"
    FREEZE = "freeze"
    GRADE = "grade"


class CodingCertificationTerminalDomain(StrEnum):
    RESOLVED = "resolved"
    REPAIR_FAILURE = "repair_failure"
    CANDIDATE_INTEGRITY = "candidate_integrity"


class CodingCertificationModelUsageStatus(StrEnum):
    COMPLETE = "complete"
    NOT_INVOKED = "not_invoked"
    PROVIDER_FAILURE = "provider_failure"


class CodingCertificationModelEvidence(CodingCertificationModel):
    model: ShortName
    provider: ShortName
    provider_route_profile: ShortName
    reasoning_effort: Literal["medium"]
    inference_grant_sha256: Sha256
    prompt_sha256: Sha256
    tool_schema_sha256: Sha256
    usage_status: CodingCertificationModelUsageStatus
    fallback_used: Literal[False]
    cost_source: Literal["provider_receipt_v1"]
    currency: Literal["USD"]
    provider_receipt_set_sha256: Sha256 | None
    requests: Annotated[int, Field(ge=0, le=10_000)]
    prompt_tokens: Annotated[int, Field(ge=0)]
    completion_tokens: Annotated[int, Field(ge=0)]
    total_tokens: Annotated[int, Field(ge=0)]
    cost_usd_micros: Annotated[int, Field(ge=0)]
    retry_count: Annotated[int, Field(ge=0, le=100)]

    @model_validator(mode="after")
    def accounting_is_coherent(self) -> CodingCertificationModelEvidence:
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("model token totals disagree")
        counters = (
            self.requests,
            self.prompt_tokens,
            self.completion_tokens,
            self.total_tokens,
            self.cost_usd_micros,
            self.retry_count,
        )
        if self.usage_status is CodingCertificationModelUsageStatus.NOT_INVOKED:
            if any(counters) or self.provider_receipt_set_sha256 is not None:
                raise ValueError("not-invoked model evidence has nonzero accounting")
        elif self.requests == 0 or self.provider_receipt_set_sha256 is None:
            raise ValueError("invoked model evidence lacks a provider receipt root")
        return self


class CodingCapabilityCertificationReceipt(CodingCertificationModel):
    schema_name: Literal["dittobench-coding-capability-certification-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    certification_id: OpaqueId
    agent_artifact_sha256: Sha256
    harness_instance_id: OpaqueId
    canary_manifest_sha256: Sha256
    issued_at_unix: Annotated[int, Field(ge=1)]
    expires_at_unix: Annotated[int, Field(ge=1)]
    status: CodingCertificationStatus
    failure_stage: CodingCertificationStage | None
    failure_code: ShortName | None
    supported_coding_contract_versions: Annotated[list[int], Field(max_length=16)]
    capabilities: Annotated[list[ShortName], Field(max_length=64)]
    memory_bundle_sha256: Sha256
    visible_bundle_sha256: Sha256
    base_tree_sha256: Sha256
    inference_grant_sha256: Sha256
    model_evidence: CodingCertificationModelEvidence | None
    frozen_patch_sha256: Sha256 | None
    frozen_submission_object_key: ContentAddressedKey | None
    changed_path_root: Sha256 | None
    final_tree_sha256: Sha256 | None
    authoring_event_root: Sha256 | None
    authoring_transcript_sha256: Sha256 | None
    authoring_transcript_object_key: ContentAddressedKey | None
    authoring_transcript_bytes: Annotated[int, Field(ge=0)]
    authoring_event_count: Annotated[int, Field(ge=0)]
    protected_paths_intact: bool
    canary_terminal_domain: CodingCertificationTerminalDomain | None
    grader_plan_sha256: Sha256
    grader_execution_receipt_root_sha256: Sha256 | None
    certification_sha256: Sha256

    @model_validator(mode="after")
    def receipt_is_coherent(self) -> CodingCapabilityCertificationReceipt:
        if (
            self.expires_at_unix <= self.issued_at_unix
            or self.expires_at_unix - self.issued_at_unix > 86_400
        ):
            raise ValueError("certification lifetime must be in (0, 24h]")
        if self.supported_coding_contract_versions != sorted(
            self.supported_coding_contract_versions
        ) or len(set(self.supported_coding_contract_versions)) != len(
            self.supported_coding_contract_versions
        ):
            raise ValueError("supported coding versions must be unique and sorted")
        if any(
            version <= 0 or version > 1_000_000
            for version in self.supported_coding_contract_versions
        ):
            raise ValueError("supported coding version is outside bounds")
        if self.capabilities != sorted(self.capabilities) or len(
            set(self.capabilities)
        ) != len(self.capabilities):
            raise ValueError("coding capabilities must be unique and sorted")
        if (self.authoring_transcript_bytes == 0) != (self.authoring_event_count == 0):
            raise ValueError("transcript byte and event counts disagree")
        if self.authoring_transcript_object_key is not None:
            if (
                self.authoring_transcript_sha256 is None
                or self.authoring_transcript_object_key
                != f"sha256/{self.authoring_transcript_sha256}"
            ):
                raise ValueError("transcript object key does not match its digest")
        elif self.authoring_transcript_sha256 is not None:
            raise ValueError("transcript digest requires a durable object key")
        if self.frozen_submission_object_key is not None:
            if (
                self.frozen_patch_sha256 is None
                or self.frozen_submission_object_key
                != f"sha256/{self.frozen_patch_sha256}"
            ):
                raise ValueError("frozen object key does not match its patch digest")
        elif self.frozen_patch_sha256 is not None:
            raise ValueError("frozen patch digest requires a durable object key")
        if self.model_evidence is not None and (
            self.model_evidence.inference_grant_sha256 != self.inference_grant_sha256
        ):
            raise ValueError("model evidence does not match the inference grant")

        execution_fields = (
            self.model_evidence,
            self.frozen_patch_sha256,
            self.frozen_submission_object_key,
            self.changed_path_root,
            self.final_tree_sha256,
            self.authoring_event_root,
            self.authoring_transcript_sha256,
            self.authoring_transcript_object_key,
            self.canary_terminal_domain,
            self.grader_execution_receipt_root_sha256,
        )
        if self.status is CodingCertificationStatus.CERTIFIED:
            if (
                self.failure_stage is not None
                or self.failure_code is not None
                or any(value is None for value in execution_fields)
                or self.model_evidence is None
                or self.model_evidence.usage_status
                is not CodingCertificationModelUsageStatus.COMPLETE
                or self.canary_terminal_domain
                is not CodingCertificationTerminalDomain.RESOLVED
                or self.authoring_transcript_bytes <= 0
                or self.authoring_event_count <= 0
                or not self.protected_paths_intact
                or 1 not in self.supported_coding_contract_versions
                or not {
                    "case_scoped_inference_v1",
                    "coding_runner_tools_v1",
                    "scoped_memory_seed_v1",
                }.issubset(self.capabilities)
            ):
                raise ValueError("certified receipt lacks complete capability evidence")
        elif self.failure_stage is None or self.failure_code is None:
            raise ValueError("non-certified receipt requires failure stage and code")
        if self.status is CodingCertificationStatus.UNSUPPORTED and (
            self.failure_stage is not CodingCertificationStage.HEALTH
            or any(value is not None for value in execution_fields)
            or self.authoring_transcript_bytes != 0
            or self.authoring_event_count != 0
            or self.protected_paths_intact
        ):
            raise ValueError("unsupported receipt carries execution evidence")
        if coding_certification_receipt_digest(self) != self.certification_sha256:
            raise ValueError("certification_sha256 does not match known fields")
        return self


class SubmitCodingCertificationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    bench_version: Annotated[int, Field(ge=1)]
    lease_id: UUID
    screened_image_sha256: Sha256
    receipt: CodingCapabilityCertificationReceipt
    signature: Annotated[str, Field(pattern=_SIGNATURE_PATTERN)]

    @model_validator(mode="after")
    def lease_id_is_nonzero(self) -> SubmitCodingCertificationRequest:
        if self.lease_id.int == 0:
            raise ValueError("coding certification lease UUID is nil")
        return self


class SubmitCodingCertificationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    certification_id: OpaqueId
    status: CodingCertificationStatus
    accepted: Literal[True]
    idempotent: bool
    active: bool


class CodingCertificationRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    certification_row_id: UUID
    validator_hotkey: str
    bench_version: int
    lease_id: UUID | None = None
    ticket_deadline: datetime
    coding_contract_version: int
    certification_id: str
    status: CodingCertificationStatus
    failure_stage: CodingCertificationStage | None
    failure_code: str | None
    certification_sha256: str
    canary_manifest_sha256: str
    screened_image_sha256: str
    transcript_object_key: str | None
    frozen_submission_object_key: str | None
    issued_at: datetime
    expires_at: datetime
    created_at: datetime
    active: bool
    stale_reason: Literal[
        "active",
        "expired",
        "not_certified",
        "settlement_unbound",
        "artifact_changed",
        "screened_image_changed",
    ]


class AgentCodingCertificationStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    agent_name: str
    miner_hotkey: str
    artifact_sha256: str
    screened_image_sha256: str | None
    coding_supported: bool
    coding_certified: bool
    active_certification_count: int
    total: int
    certifications: list[CodingCertificationRecord]


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    body = (
        (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
        .encode()
    )
    if len(body) > 4 << 20:
        raise ValueError("canonical coding certification JSON exceeds 4 MiB")
    return body


def coding_certification_receipt_digest(
    receipt: CodingCapabilityCertificationReceipt,
) -> str:
    projection = receipt.model_dump(mode="json", by_alias=True)
    projection.pop("certification_sha256")
    return hashlib.sha256(_canonical_json_bytes(projection)).hexdigest()


def coding_certification_signing_message(
    *,
    validator_hotkey: str,
    agent_id: UUID,
    bench_version: int,
    lease_id: UUID,
    screened_image_sha256: str,
    certification_sha256: str,
) -> bytes:
    if agent_id.int == 0 or lease_id.int == 0:
        raise ValueError("coding certification UUID is nil")
    return "\x00".join(
        (
            "dittobench-coding-certification:v2",
            validator_hotkey,
            str(agent_id),
            str(bench_version),
            str(lease_id),
            screened_image_sha256,
            certification_sha256,
        )
    ).encode()
