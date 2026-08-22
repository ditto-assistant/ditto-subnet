"""Shadow-only DittoBench Coding run and result-ledger contracts."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
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

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_certification import (
    CodingCertificationModelUsageStatus,
    ContentAddressedKey,
)

REPAIR_SCORE_RESOLVED_MICROS = 1_000_000
_MAX_CANONICAL_JSON_BYTES = 4 << 20
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_BLOCK_HASH_PATTERN = r"^0x[0-9a-f]{64}$"
_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_PATTERN = r"^[0-9a-fA-F]{128}$"
_UINT64_MAX = (1 << 64) - 1


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
BlockHash = Annotated[str, Field(pattern=_BLOCK_HASH_PATTERN)]


class CodingEvaluationModel(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        serialize_by_alias=True,
        validate_by_name=True,
    )


class CodingTerminalDomain(StrEnum):
    RESOLVED = "resolved"
    REPAIR_FAILURE = "repair_failure"
    VALIDATOR_INFRASTRUCTURE = "validator_infrastructure"
    TASK_INVALID = "task_invalid"
    CANDIDATE_INTEGRITY = "candidate_integrity"
    CONTROL_PLANE_INTEGRITY = "control_plane_integrity"


class CodingShadowRunAuthority(CodingEvaluationModel):
    """Private-selector projection persisted before validator-specific leases."""

    schema_name: Literal["dittobench-coding-shadow-run-authority-v1"] = Field(
        alias="schema"
    )
    bench_family: Literal["coding"]
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    bench_version: Annotated[int, Field(ge=7)]
    coding_run_id: OpaqueId
    agent_id: UUID
    agent_artifact_sha256: Sha256
    screened_image_sha256: Sha256
    corpus_release_id: OpaqueId
    catalog_merkle_root: Sha256
    selection_derivation_id: ShortName
    selection_chain_genesis_hash: BlockHash
    selection_block_number: Annotated[int, Field(ge=1, le=(1 << 63) - 1)]
    selection_block_hash: BlockHash
    inference_grant_sha256: Sha256
    grader_contract_sha256: Sha256
    task_set_id: OpaqueId
    task_set_manifest_sha256: Sha256
    run_manifest_sha256: Sha256
    task_count: Annotated[int, Field(ge=1, le=100)]


class CodingTaskResult(CodingEvaluationModel):
    case_id: OpaqueId
    variant_id: OpaqueId
    task_evidence_sha256: Sha256
    terminal_domain: CodingTerminalDomain
    repair_score_micros: Annotated[int, Field(ge=0, le=REPAIR_SCORE_RESOLVED_MICROS)]


class CodingRunEvidence(CodingEvaluationModel):
    """Known-field mirror of the shared coding run-evidence v1 contract."""

    schema_name: Literal["dittobench-coding-run-evidence-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    coding_run_id: OpaqueId
    validator_ticket_id: OpaqueId
    run_manifest_sha256: Sha256
    task_set_manifest_sha256: Sha256
    tasks: Annotated[list[CodingTaskResult], Field(min_length=1, max_length=100)]
    resolved_count: Annotated[int, Field(ge=0, le=100)]
    repair_failure_count: Annotated[int, Field(ge=0, le=100)]
    infrastructure_count: Annotated[int, Field(ge=0, le=100)]
    invalid_count: Annotated[int, Field(ge=0, le=100)]
    candidate_integrity_count: Annotated[int, Field(ge=0, le=100)]
    control_plane_integrity_count: Annotated[int, Field(ge=0, le=100)]
    scoreable_task_count: Annotated[int, Field(ge=0, le=100)]
    repair_mean_micros: Annotated[int, Field(ge=0, le=REPAIR_SCORE_RESOLVED_MICROS)]

    @model_validator(mode="after")
    def aggregate_is_coherent(self) -> CodingRunEvidence:
        identities = [(task.case_id, task.variant_id) for task in self.tasks]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError("run tasks must be unique and sorted")
        counts = dict.fromkeys(CodingTerminalDomain, 0)
        for task in self.tasks:
            counts[task.terminal_domain] += 1
            expected = (
                REPAIR_SCORE_RESOLVED_MICROS
                if task.terminal_domain is CodingTerminalDomain.RESOLVED
                else 0
            )
            if task.repair_score_micros != expected:
                raise ValueError("task repair score disagrees with terminal domain")
        expected_counts = (
            counts[CodingTerminalDomain.RESOLVED],
            counts[CodingTerminalDomain.REPAIR_FAILURE],
            counts[CodingTerminalDomain.VALIDATOR_INFRASTRUCTURE],
            counts[CodingTerminalDomain.TASK_INVALID],
            counts[CodingTerminalDomain.CANDIDATE_INTEGRITY],
            counts[CodingTerminalDomain.CONTROL_PLANE_INTEGRITY],
        )
        observed_counts = (
            self.resolved_count,
            self.repair_failure_count,
            self.infrastructure_count,
            self.invalid_count,
            self.candidate_integrity_count,
            self.control_plane_integrity_count,
        )
        expected_scoreable = (
            counts[CodingTerminalDomain.RESOLVED]
            + counts[CodingTerminalDomain.REPAIR_FAILURE]
            + counts[CodingTerminalDomain.CANDIDATE_INTEGRITY]
        )
        expected_mean = (
            counts[CodingTerminalDomain.RESOLVED]
            * REPAIR_SCORE_RESOLVED_MICROS
            // expected_scoreable
            if expected_scoreable
            else 0
        )
        if (
            observed_counts != expected_counts
            or self.scoreable_task_count != expected_scoreable
            or self.repair_mean_micros != expected_mean
        ):
            raise ValueError("run aggregate does not match task vector")
        return self


class CodingAuthoringModelEvidence(CodingEvaluationModel):
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
    requests: Annotated[int, Field(strict=True, ge=0, le=10_000)]
    prompt_tokens: Annotated[int, Field(strict=True, ge=0, le=_UINT64_MAX)]
    completion_tokens: Annotated[int, Field(strict=True, ge=0, le=_UINT64_MAX)]
    total_tokens: Annotated[int, Field(strict=True, ge=0, le=_UINT64_MAX)]
    cost_usd_micros: Annotated[int, Field(strict=True, ge=0, le=_UINT64_MAX)]
    retry_count: Annotated[int, Field(strict=True, ge=0, le=100)]

    @field_validator("fallback_used", mode="before")
    @classmethod
    def fallback_flag_is_strict(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("fallback_used must be a boolean")
        return value

    @model_validator(mode="after")
    def accounting_is_coherent(self) -> CodingAuthoringModelEvidence:
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


class CodingAuthoringEvidence(CodingEvaluationModel):
    """Canonical authoring evidence fixed before any grader release."""

    model: CodingAuthoringModelEvidence
    authoring_event_root: Sha256
    authoring_transcript_sha256: Sha256
    frozen_patch_sha256: Sha256
    changed_path_root: Sha256
    final_tree_sha256: Sha256
    changed_path_count: Annotated[int, Field(strict=True, ge=0, le=10_000)]
    changed_bytes: Annotated[int, Field(strict=True, ge=0, le=1 << 30)]
    protected_paths_intact: bool

    @field_validator("protected_paths_intact", mode="before")
    @classmethod
    def protected_paths_flag_is_strict(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("protected_paths_intact must be a boolean")
        return value


class SubmitCodingAuthoringFreezeRequest(BaseModel):
    """One validator-signed immutable transition out of authoring."""

    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    agent_id: UUID
    bench_version: Annotated[int, Field(strict=True, ge=7)]
    run_row_id: UUID
    ticket_id: UUID
    ticket_deadline: datetime
    coding_run_id: OpaqueId
    agent_artifact_sha256: Sha256
    screened_image_sha256: Sha256
    run_manifest_sha256: Sha256
    task_set_manifest_sha256: Sha256
    authoring_evidence_sha256: Sha256
    evidence: CodingAuthoringEvidence
    authoring_transcript_object_key: ContentAddressedKey
    authoring_transcript_bytes: Annotated[int, Field(strict=True, ge=0, le=512 << 20)]
    authoring_event_count: Annotated[int, Field(strict=True, ge=0, le=1_000)]
    frozen_submission_object_key: ContentAddressedKey
    signature: Annotated[str, Field(pattern=_SIGNATURE_PATTERN)]

    @field_validator("ticket_deadline")
    @classmethod
    def deadline_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding authoring freeze deadline must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def freeze_authority_is_coherent(self) -> SubmitCodingAuthoringFreezeRequest:
        if (
            self.agent_id.int == 0
            or self.run_row_id.int == 0
            or self.ticket_id.int == 0
        ):
            raise ValueError("coding authoring freeze UUIDs must be nonzero")
        if coding_authoring_evidence_digest(self.evidence) != (
            self.authoring_evidence_sha256
        ):
            raise ValueError("authoring_evidence_sha256 does not match known fields")
        if self.authoring_transcript_object_key != (
            f"sha256/{self.evidence.authoring_transcript_sha256}"
        ):
            raise ValueError("authoring transcript key does not match its digest")
        if self.frozen_submission_object_key != (
            f"sha256/{self.evidence.frozen_patch_sha256}"
        ):
            raise ValueError("frozen submission key does not match its patch digest")
        if (self.authoring_transcript_bytes == 0) != (self.authoring_event_count == 0):
            raise ValueError("authoring transcript bytes and event count disagree")
        return self


class SubmitCodingAuthoringFreezeResponse(CodingEvaluationModel):
    freeze_id: UUID
    agent_id: UUID
    run_row_id: UUID
    ticket_id: UUID
    coding_run_id: OpaqueId
    authoring_evidence_sha256: Sha256
    frozen_at: datetime
    accepted: Literal[True]
    idempotent: bool
    weight_eligible: Literal[False] = False

    @field_validator("idempotent", mode="before")
    @classmethod
    def idempotency_flag_is_strict(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("idempotent must be a boolean")
        return value

    @model_validator(mode="after")
    def response_is_coherent(self) -> SubmitCodingAuthoringFreezeResponse:
        if (
            self.freeze_id.int == 0
            or self.agent_id.int == 0
            or self.run_row_id.int == 0
            or self.ticket_id.int == 0
            or self.frozen_at.tzinfo is None
            or self.frozen_at.utcoffset() is None
        ):
            raise ValueError("coding authoring freeze response authority is invalid")
        return self


class CodingAuthoringFreezeRecord(CodingEvaluationModel):
    freeze_id: UUID
    ticket_id: UUID
    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    authoring_evidence_sha256: Sha256
    authoring_event_root: Sha256
    authoring_transcript_sha256: Sha256
    authoring_transcript_bytes: Annotated[int, Field(ge=0, le=512 << 20)]
    authoring_event_count: Annotated[int, Field(ge=0, le=1_000)]
    frozen_patch_sha256: Sha256
    changed_path_root: Sha256
    final_tree_sha256: Sha256
    changed_path_count: Annotated[int, Field(ge=0, le=10_000)]
    changed_bytes: Annotated[int, Field(ge=0, le=1 << 30)]
    protected_paths_intact: bool
    frozen_at: datetime
    weight_eligible: Literal[False] = False

    @model_validator(mode="after")
    def record_is_coherent(self) -> CodingAuthoringFreezeRecord:
        if (
            self.freeze_id.int == 0
            or self.ticket_id.int == 0
            or self.frozen_at.tzinfo is None
            or self.frozen_at.utcoffset() is None
            or (self.authoring_transcript_bytes == 0)
            != (self.authoring_event_count == 0)
        ):
            raise ValueError("coding authoring freeze record is incoherent")
        return self


class SubmitCodingShadowResultRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    bench_version: Annotated[int, Field(ge=7)]
    run_row_id: UUID
    ticket_id: UUID
    ticket_deadline: datetime
    agent_artifact_sha256: Sha256
    screened_image_sha256: Sha256
    run_evidence_sha256: Sha256
    evidence: CodingRunEvidence
    signature: Annotated[str, Field(pattern=_SIGNATURE_PATTERN)]

    @field_validator("ticket_deadline")
    @classmethod
    def deadline_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding shadow ticket deadline must be timezone-aware")
        return value

    @model_validator(mode="after")
    def digest_matches_evidence(self) -> SubmitCodingShadowResultRequest:
        if coding_run_evidence_digest(self.evidence) != self.run_evidence_sha256:
            raise ValueError("run_evidence_sha256 does not match known fields")
        return self


class SubmitCodingShadowResultResponse(CodingEvaluationModel):
    agent_id: UUID
    run_row_id: UUID
    ticket_id: UUID
    coding_run_id: OpaqueId
    accepted: Literal[True]
    idempotent: bool
    weight_eligible: Literal[False] = False


class CodingShadowResultRecord(CodingEvaluationModel):
    result_id: UUID
    ticket_id: UUID
    validator_hotkey: str
    run_evidence_sha256: Sha256
    task_count: Annotated[int, Field(ge=1, le=100)]
    resolved_count: Annotated[int, Field(ge=0, le=100)]
    repair_failure_count: Annotated[int, Field(ge=0, le=100)]
    infrastructure_count: Annotated[int, Field(ge=0, le=100)]
    invalid_count: Annotated[int, Field(ge=0, le=100)]
    candidate_integrity_count: Annotated[int, Field(ge=0, le=100)]
    control_plane_integrity_count: Annotated[int, Field(ge=0, le=100)]
    scoreable_task_count: Annotated[int, Field(ge=0, le=100)]
    repair_mean_micros: Annotated[int, Field(ge=0, le=REPAIR_SCORE_RESOLVED_MICROS)]
    submitted_at: datetime
    weight_eligible: Literal[False] = False


class CodingShadowTicketRecord(CodingEvaluationModel):
    ticket_id: UUID
    validator_hotkey: str
    certification_row_id: UUID
    issued_at: datetime
    deadline: datetime
    authoring_freeze: CodingAuthoringFreezeRecord | None
    result: CodingShadowResultRecord | None

    @model_validator(mode="after")
    def nested_records_match_ticket(self) -> CodingShadowTicketRecord:
        if self.authoring_freeze is not None and (
            self.authoring_freeze.ticket_id != self.ticket_id
            or self.authoring_freeze.validator_hotkey != self.validator_hotkey
        ):
            raise ValueError("coding authoring freeze record disagrees with ticket")
        if self.result is not None and (
            self.result.ticket_id != self.ticket_id
            or self.result.validator_hotkey != self.validator_hotkey
        ):
            raise ValueError("coding result record disagrees with ticket")
        return self


class CodingShadowRunRecord(CodingEvaluationModel):
    run_row_id: UUID
    assignment_row_id: UUID | None
    assignment_sha256: Sha256 | None
    coding_run_id: OpaqueId
    bench_version: Annotated[int, Field(ge=7)]
    coding_contract_version: Literal[1]
    artifact_sha256: Sha256
    screened_image_sha256: Sha256
    corpus_release_id: OpaqueId
    run_manifest_sha256: Sha256
    task_set_manifest_sha256: Sha256
    task_count: Annotated[int, Field(ge=1, le=100)]
    selection_block_number: Annotated[int, Field(ge=1)]
    selection_block_hash: BlockHash
    selection_block_timestamp: datetime | None
    issued: bool
    core_qualification_observation_id: UUID
    ticket_count: Annotated[int, Field(ge=0)]
    result_count: Annotated[int, Field(ge=0)]
    quorum_complete: bool
    median_repair_mean_micros: Annotated[
        int | None, Field(ge=0, le=REPAIR_SCORE_RESOLVED_MICROS)
    ]
    current: bool
    stale_reason: Literal[
        "current",
        "artifact_changed",
        "screened_image_changed",
        "policy_changed",
        "qualification_stale",
        "catalog_retired",
        "issuance_missing",
    ]
    tickets: list[CodingShadowTicketRecord]
    created_at: datetime
    weight_eligible: Literal[False] = False


class CodingSelectionAssignmentRecord(CodingEvaluationModel):
    assignment_row_id: UUID
    assignment_sha256: Sha256
    coding_run_id: OpaqueId
    bench_version: Annotated[int, Field(ge=7)]
    coding_contract_version: Literal[1]
    artifact_sha256: Sha256
    screened_image_sha256: Sha256
    corpus_release_id: OpaqueId
    catalog_commitment_sha256: Sha256
    anchor_block_number: Annotated[int, Field(ge=1)]
    anchor_block_hash: BlockHash
    selection_delay_blocks: Annotated[int, Field(ge=1, le=10_000)]
    selection_block_number: Annotated[int, Field(ge=1)]
    assigned_at: datetime
    task_count: Literal[1]
    core_qualification_observation_id: UUID
    certification_row_id: UUID
    current: bool
    stale_reason: Literal[
        "current",
        "artifact_changed",
        "screened_image_changed",
        "catalog_retired",
        "policy_changed",
        "qualification_stale",
        "certification_stale",
    ]
    created_at: datetime
    weight_eligible: Literal[False] = False


class AgentCodingShadowEvaluationStatus(CodingEvaluationModel):
    agent_id: UUID
    agent_name: str
    miner_hotkey: str
    artifact_sha256: Sha256
    screened_image_sha256: Sha256 | None
    total_assignments: Annotated[int, Field(ge=0)]
    assignments: list[CodingSelectionAssignmentRecord]
    total_runs: Annotated[int, Field(ge=0)]
    runs: list[CodingShadowRunRecord]
    shadow_only: Literal[True] = True


def coding_run_evidence_digest(evidence: CodingRunEvidence) -> str:
    projection = evidence.model_dump(mode="json", by_alias=True)
    return coding_canonical_sha256(
        projection,
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding run evidence",
    )


def coding_authoring_evidence_digest(evidence: CodingAuthoringEvidence) -> str:
    projection = evidence.model_dump(mode="json", by_alias=True)
    return coding_canonical_sha256(
        projection,
        maximum_bytes=_MAX_CANONICAL_JSON_BYTES,
        label="coding authoring evidence",
    )


def coding_authoring_freeze_signing_message(
    *,
    validator_hotkey: str,
    agent_id: UUID,
    bench_version: int,
    run_row_id: UUID,
    ticket_id: UUID,
    ticket_deadline: datetime,
    coding_run_id: str,
    agent_artifact_sha256: str,
    screened_image_sha256: str,
    run_manifest_sha256: str,
    task_set_manifest_sha256: str,
    authoring_evidence_sha256: str,
    authoring_transcript_object_key: str,
    authoring_transcript_bytes: int,
    authoring_event_count: int,
    frozen_submission_object_key: str,
) -> bytes:
    if ticket_deadline.tzinfo is None or ticket_deadline.utcoffset() is None:
        raise ValueError("coding authoring freeze deadline must be timezone-aware")
    deadline = ticket_deadline.astimezone(UTC).isoformat(timespec="microseconds")
    return "\x00".join(
        (
            "dittobench-coding-authoring-freeze:v1",
            validator_hotkey,
            str(agent_id),
            str(bench_version),
            str(run_row_id),
            str(ticket_id),
            deadline,
            coding_run_id,
            agent_artifact_sha256,
            screened_image_sha256,
            run_manifest_sha256,
            task_set_manifest_sha256,
            authoring_evidence_sha256,
            authoring_transcript_object_key,
            str(authoring_transcript_bytes),
            str(authoring_event_count),
            frozen_submission_object_key,
        )
    ).encode()


def coding_shadow_result_signing_message(
    *,
    validator_hotkey: str,
    agent_id: UUID,
    run_row_id: UUID,
    ticket_id: UUID,
    bench_version: int,
    ticket_deadline: datetime,
    agent_artifact_sha256: str,
    screened_image_sha256: str,
    run_evidence_sha256: str,
) -> bytes:
    if ticket_deadline.tzinfo is None:
        raise ValueError("coding shadow ticket deadline must be timezone-aware")
    deadline = ticket_deadline.astimezone(UTC).isoformat(timespec="microseconds")
    return "\x00".join(
        (
            "dittobench-coding-shadow-result:v1",
            validator_hotkey,
            str(agent_id),
            str(run_row_id),
            str(ticket_id),
            str(bench_version),
            deadline,
            agent_artifact_sha256,
            screened_image_sha256,
            run_evidence_sha256,
        )
    ).encode()
